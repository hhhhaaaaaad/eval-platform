"""实验与 run API 的集成测试（真实 Postgres）。

除常规的 RBAC 与状态码映射外，这里有一条别处测不到的性质：
**投递必须发生在 commit 之后**。做法是让被替换的 ``dispatch_run`` 用一条
**独立连接**去查这个 run——若 API 先投递后提交，独立连接看不到该行，
这条用例就会失败。用同一个 session 是测不出来的（同事务内必然可见）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import get_db
from app.auth.models import User
from app.auth.security import create_access_token, hash_password
from app.db.session import get_session_factory
from app.main import create_app
from app.runs import dispatch

PASSWORD = "correct-horse-battery"


class _Actor:
    def __init__(self, user_id: int, token: str) -> None:
        self.user_id = user_id
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture
def db() -> Iterator[Session]:
    try:
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 探活失败即跳过
        pytest.skip(f"Postgres 不可用，跳过 run API 集成测试: {exc}")
    yield session
    session.close()


def _make_user(db: Session, *, role: str) -> _Actor:
    user = User(
        username=f"run-{role}-{uuid.uuid4().hex[:10]}",
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    db.commit()
    return _Actor(user.id, create_access_token(subject=str(user.id), role=role))


def _cleanup(db: Session, user_id: int) -> None:
    db.rollback()
    # run 引用用户/实验/配置，必须先删；参数快照与模型版本也按创建者清理。
    db.execute(text("DELETE FROM eval_runs WHERE created_by = :uid"), {"uid": user_id})
    # 守卫是**单行表**（CHECK id = 1）：只能清 owner，绝不能删行。
    # 删掉那一行会让后续所有独占用例报「guard 未初始化」——这个坑踩过一次，
    # 而且是**全量跑才暴露**（单文件跑时没有下一条独占用例来承接后果），
    # 并且删的是已提交数据，改测试代码也补不回来，只能手工恢复。
    # 因此这里既清 owner，又幂等补回那一行：测试夹具负责维护全局不变量。
    # （应用侧仍拒绝自动重建——那属于迁移异常，必须显式失败而非静默掩盖。）
    db.execute(
        text(
            "INSERT INTO eval_run_guard (id, exclusive_owner) VALUES (1, NULL) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db.execute(
        text(
            "UPDATE eval_run_guard SET exclusive_owner = NULL, owner_run_id = NULL, "
            "acquired_at = NULL, heartbeat_at = NULL WHERE id = 1"
        )
    )
    db.execute(text("DELETE FROM eval_experiments WHERE created_by = :uid"), {"uid": user_id})
    db.execute(
        text(
            "DELETE FROM eval_dataset_versions WHERE dataset_id IN "
            "(SELECT id FROM eval_datasets WHERE created_by = :uid)"
        ),
        {"uid": user_id},
    )
    db.execute(text("DELETE FROM eval_datasets WHERE created_by = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE created_by = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM eval_audit_log WHERE actor_user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM eval_users WHERE id = :uid"), {"uid": user_id})
    db.commit()


@pytest.fixture
def admin(db: Session) -> Iterator[_Actor]:
    actor = _make_user(db, role="admin")
    yield actor
    _cleanup(db, actor.user_id)


@pytest.fixture
def viewer(db: Session) -> Iterator[_Actor]:
    actor = _make_user(db, role="viewer")
    yield actor
    _cleanup(db, actor.user_id)


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_config(db: Session, admin: _Actor) -> dict[str, int]:
    """经 HTTP 建出配置三件套，确保它们属于本用例、且 created_by 可被清理。"""
    suffix = uuid.uuid4().hex[:8]

    dataset_id = db.execute(
        text("INSERT INTO eval_datasets(name, created_by) VALUES (:n, :u) RETURNING id"),
        {"n": f"api-ds-{suffix}", "u": admin.user_id},
    ).scalar_one()
    version_id = db.execute(
        text(
            "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, content_digest, created_by) "
            "VALUES (:d, 'v1', 1, 'manual', :digest, :u) RETURNING id"
        ),
        {"d": dataset_id, "digest": f"sha256:{uuid.uuid4().hex}", "u": admin.user_id},
    ).scalar_one()
    snapshot_id = db.execute(
        text(
            "INSERT INTO eval_param_snapshots(name, params, freeze_config, params_hash, created_by) "
            # 用 CAST 而非 `:p::jsonb`：text() 的解析器会把 `:p::jsonb` 读成参数名 `:p:`，
            # 报 "syntax error at or near :"。
            "VALUES (:n, CAST(:p AS jsonb), '{}'::jsonb, :h, :u) RETURNING id"
        ),
        {
            "n": f"api-snap-{suffix}",
            "p": json.dumps({"vector_store": f"memory-{suffix}"}),
            "h": f"sha256:{uuid.uuid4().hex}",
            "u": admin.user_id,
        },
    ).scalar_one()
    model_id = db.execute(
        text(
            "INSERT INTO eval_model_versions(embedding_model_id, reranker_model_id, config_hash, config) "
            "VALUES (:e, :r, :h, '{}'::jsonb) RETURNING id"
        ),
        {"e": f"emb-{suffix}", "r": f"rr-{suffix}", "h": f"sha256:{uuid.uuid4().hex}"},
    ).scalar_one()
    db.commit()

    return {
        "dataset_version_id": version_id,
        "param_snapshot_id": snapshot_id,
        "model_version_id": model_id,
    }


def _payload(config: dict[str, int], **overrides) -> dict:
    body = {
        "dataset_version_id": config["dataset_version_id"],
        "param_snapshot_id": config["param_snapshot_id"],
        "model_version_id": config["model_version_id"],
        "mode": "exact",
    }
    body.update(overrides)
    return body


class _Dispatched:
    """记录投递动作：被投递的 run id，以及它在**独立连接**上是否可见。"""

    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.visible: list[bool] = []

    def __len__(self) -> int:
        return len(self.run_ids)


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> _Dispatched:
    """替换投递动作，记录被投递的 run id，并**用独立连接验可见性**。

    独立连接只能读到已提交的数据——这正是「先 commit 后投递」的判据。
    用 API 自己的 session 是测不出来的（同事务内必然可见）。
    """
    record = _Dispatched()

    def _fake(run_id: uuid.UUID) -> bool:
        record.run_ids.append(str(run_id))
        other = get_session_factory()()
        try:
            found = other.execute(
                text("SELECT 1 FROM eval_runs WHERE id = :id"), {"id": str(run_id)}
            ).scalar_one_or_none()
            record.visible.append(found is not None)
        finally:
            other.close()
        return True

    monkeypatch.setattr(dispatch, "dispatch_run", _fake)
    return record


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_token_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/runs").status_code == 401

    def test_viewer_cannot_create_run(self, client: TestClient, viewer: _Actor) -> None:
        resp = client.post("/api/v1/runs", json={}, headers=viewer.headers)
        assert resp.status_code == 403

    def test_viewer_can_list_runs(self, client: TestClient, viewer: _Actor) -> None:
        assert client.get("/api/v1/runs", headers=viewer.headers).status_code == 200

    def test_viewer_cannot_cancel(self, client: TestClient, viewer: _Actor) -> None:
        resp = client.post(f"/api/v1/runs/{uuid.uuid4()}/cancel", headers=viewer.headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


class TestCreate:
    def test_creates_run_and_enqueues(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        resp = client.post("/api/v1/runs", json=_payload(seeded_config), headers=admin.headers)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["enqueued"] is True
        assert body["status"] == "pending"
        assert body["config_fingerprint"].startswith("sha256:")
        assert dispatched.run_ids == [body["id"]]

    def test_dispatch_happens_after_commit(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        """**关键性质**：投递时 run 必须已对独立连接可见。

        先投递后提交的话，worker 可能在事务可见前就查 run，得到一个只在负载高时
        偶发的「run 不存在」错误——极难排查。
        """
        client.post("/api/v1/runs", json=_payload(seeded_config), headers=admin.headers)

        assert dispatched.visible == [True]  # type: ignore[attr-defined]

    def test_idempotent_replay_returns_same_run(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        body = _payload(seeded_config, idempotency_key="click-once")
        first = client.post("/api/v1/runs", json=body, headers=admin.headers)
        second = client.post("/api/v1/runs", json=body, headers=admin.headers)

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        # 命中幂等键时不重复投递——重复投递会让同一个 run 被两个 worker 跑。
        assert len(dispatched) == 1

    def test_active_config_conflict_is_409(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        client.post("/api/v1/runs", json=_payload(seeded_config), headers=admin.headers)

        resp = client.post("/api/v1/runs", json=_payload(seeded_config), headers=admin.headers)
        assert resp.status_code == 409
        assert resp.json()["detail"]["constraint"] in ("uq_runs_active_cfg", "uq_runs_active_user")

    def test_exclusive_conflict_is_409(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched, db: Session
    ) -> None:
        client.post(
            "/api/v1/runs", json=_payload(seeded_config, exclusive=True), headers=admin.headers
        )

        resp = client.post(
            "/api/v1/runs", json=_payload(seeded_config, exclusive=True), headers=admin.headers
        )
        assert resp.status_code == 409

    def test_unknown_config_component_is_422(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        resp = client.post(
            "/api/v1/runs",
            json=_payload(seeded_config, param_snapshot_id=10**12),
            headers=admin.headers,
        )
        assert resp.status_code == 422

    def test_unknown_experiment_is_404(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        resp = client.post(
            "/api/v1/runs",
            json=_payload(seeded_config, experiment_id=str(uuid.uuid4())),
            headers=admin.headers,
        )
        assert resp.status_code == 404

    def test_invalid_mode_is_422(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        resp = client.post(
            "/api/v1/runs", json=_payload(seeded_config, mode="HNSW"), headers=admin.headers
        )
        assert resp.status_code == 422

    def test_enqueue_flag_reflects_dispatch_result(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """投递失败不算创建失败：run 仍落库为 pending，调度器可补投。"""
        monkeypatch.setattr(dispatch, "dispatch_run", lambda _run_id: False)

        resp = client.post("/api/v1/runs", json=_payload(seeded_config), headers=admin.headers)

        assert resp.status_code == 201
        assert resp.json()["enqueued"] is False
        assert resp.json()["status"] == "pending"

    def test_experiment_can_be_created_and_attached(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        experiment = client.post(
            "/api/v1/experiments", json={"name": "A/B 融合权重"}, headers=admin.headers
        )
        assert experiment.status_code == 201

        resp = client.post(
            "/api/v1/runs",
            json=_payload(seeded_config, experiment_id=experiment.json()["id"]),
            headers=admin.headers,
        )
        assert resp.status_code == 201
        assert resp.json()["experiment_id"] == experiment.json()["id"]


# ---------------------------------------------------------------------------
# 查询与取消
# ---------------------------------------------------------------------------


class TestQueryAndCancel:
    def test_get_run(self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched) -> None:
        run_id = client.post(
            "/api/v1/runs", json=_payload(seeded_config), headers=admin.headers
        ).json()["id"]

        resp = client.get(f"/api/v1/runs/{run_id}", headers=admin.headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == run_id

    def test_unknown_run_is_404(self, client: TestClient, admin: _Actor) -> None:
        assert client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=admin.headers).status_code == 404

    def test_malformed_run_id_is_422(self, client: TestClient, admin: _Actor) -> None:
        """非 UUID 的路径参数应是 422（参数不合法），而不是 500。"""
        assert client.get("/api/v1/runs/not-a-uuid", headers=admin.headers).status_code == 422

    def test_cancel_then_conflict_on_second_cancel(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        run_id = client.post(
            "/api/v1/runs", json=_payload(seeded_config), headers=admin.headers
        ).json()["id"]

        first = client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin.headers)
        assert first.status_code == 200
        assert first.json()["status"] == "cancelled"

        second = client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin.headers)
        assert second.status_code == 409

    def test_cancelled_run_frees_slot_for_same_config(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        first = client.post(
            "/api/v1/runs", json=_payload(seeded_config), headers=admin.headers
        ).json()
        client.post(f"/api/v1/runs/{first['id']}/cancel", headers=admin.headers)

        resp = client.post("/api/v1/runs", json=_payload(seeded_config), headers=admin.headers)
        assert resp.status_code == 201
        assert resp.json()["created"] is True

    def test_list_filters_by_status(
        self, client: TestClient, admin: _Actor, seeded_config: dict[str, int], dispatched: _Dispatched
    ) -> None:
        run_id = client.post(
            "/api/v1/runs", json=_payload(seeded_config), headers=admin.headers
        ).json()["id"]

        pending = client.get("/api/v1/runs?status=pending", headers=admin.headers).json()
        assert run_id in [item["id"] for item in pending]

    def test_state_counts(self, client: TestClient, admin: _Actor) -> None:
        resp = client.get("/api/v1/runs/summary/counts", headers=admin.headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_experiment_listing(
        self, client: TestClient, admin: _Actor
    ) -> None:
        created = client.post(
            "/api/v1/experiments", json={"name": "E-list"}, headers=admin.headers
        ).json()

        listing = client.get("/api/v1/experiments", headers=admin.headers)
        assert listing.status_code == 200
        assert created["id"] in [item["id"] for item in listing.json()]

        detail = client.get(f"/api/v1/experiments/{created['id']}", headers=admin.headers)
        assert detail.status_code == 200

    def test_unknown_experiment_get_is_404(self, client: TestClient, admin: _Actor) -> None:
        resp = client.get(f"/api/v1/experiments/{uuid.uuid4()}", headers=admin.headers)
        assert resp.status_code == 404
