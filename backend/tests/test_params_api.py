"""参数快照 / 模型版本 / fingerprint API 的集成测试（真实 Postgres）。

「取或建」语义是本模块的核心：同一配置组合必须复用同一行，而不是每次新建——
否则历史 run 会指向一堆内容相同但 id 不同的快照，按指纹查历史结果时就散了。

`from-java` 端点用 respx 拦截 httpx，不依赖真实 Java 实例。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import get_db
from app.auth.models import User
from app.auth.security import create_access_token, hash_password
from app.datasets.models import Dataset, DatasetVersion
from app.db.session import get_session_factory
from app.main import create_app

PASSWORD = "correct-horse-battery"
JAVA_PARAMS_URL = "http://localhost:8092/api/v1/eval/params"


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
        pytest.skip(f"Postgres 不可用，跳过参数 API 集成测试: {exc}")
    yield session
    session.close()


def _make_user(db: Session, *, role: str) -> _Actor:
    user = User(
        username=f"pm-{role}-{uuid.uuid4().hex[:10]}",
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    db.commit()
    return _Actor(user.id, create_access_token(subject=str(user.id), role=role))


def _cleanup(db: Session, user_id: int) -> None:
    db.rollback()
    db.execute(text("DELETE FROM eval_audit_log WHERE actor_user_id = :uid"), {"uid": user_id})
    # 版本先于数据集（外键无级联），快照/模型版本直接按创建者删。
    db.execute(
        text(
            "DELETE FROM eval_dataset_versions WHERE dataset_id IN "
            "(SELECT id FROM eval_datasets WHERE created_by = :uid)"
        ),
        {"uid": user_id},
    )
    db.execute(text("DELETE FROM eval_datasets WHERE created_by = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE created_by = :uid"), {"uid": user_id})
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


def _params(*, unique: bool = True) -> dict:
    """一份合法参数。``unique=True`` 时 vector_store 带随机后缀，
    避免不同用例共享同一 params_hash 而互相干扰清理。"""
    return {
        "vector_store": f"memory-{uuid.uuid4().hex[:8]}" if unique else "memory",
        "rrf_k": 60,
        "alpha": 0.5,
        "beta": 0.3,
        "recency_half_life_days": 14.0,
        "profile_boost": 0.2,
        "min_confidence": 0.4,
        "inject_max_tokens": 2000,
    }


def _create_snapshot(client: TestClient, actor: _Actor, params: dict, **extra) -> dict:
    payload = {"name": "snap", "params": params, **extra}
    resp = client.post("/api/v1/params/snapshots", json=payload, headers=actor.headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestAuth:
    def test_viewer_cannot_create_snapshot(self, client: TestClient, viewer: _Actor) -> None:
        resp = client.post(
            "/api/v1/params/snapshots",
            json={"name": "x", "params": _params()},
            headers=viewer.headers,
        )
        assert resp.status_code == 403

    def test_viewer_can_list(self, client: TestClient, viewer: _Actor) -> None:
        assert client.get("/api/v1/params/snapshots", headers=viewer.headers).status_code == 200

    def test_missing_token_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/params/snapshots").status_code == 401


# ---------------------------------------------------------------------------
# 取或建
# ---------------------------------------------------------------------------


class TestGetOrCreate:
    def test_same_params_reuse_same_row(self, client: TestClient, admin: _Actor) -> None:
        params = _params()
        first = _create_snapshot(client, admin, params)
        second = _create_snapshot(client, admin, params, name="另一个名字")

        assert first["id"] == second["id"]
        assert first["params_hash"] == second["params_hash"]

    def test_different_params_create_new_row(self, client: TestClient, admin: _Actor) -> None:
        first = _create_snapshot(client, admin, _params())
        second = _create_snapshot(client, admin, _params())

        assert first["id"] != second["id"]
        assert first["params_hash"] != second["params_hash"]

    def test_freeze_config_records_frozen_keys(self, client: TestClient, admin: _Actor) -> None:
        """审计要能回答「这次指纹冻结了哪些字段」，只存哈希不够。"""
        snapshot = _create_snapshot(client, admin, _params(), frozen_keys=["alpha", "beta"])
        assert snapshot["freeze_config"] == {"frozen_keys": ["alpha", "beta"]}

    def test_unknown_frozen_key_is_422(self, client: TestClient, admin: _Actor) -> None:
        """拼错的键名是调用方的输入错误，应 422 而不是 500。"""
        resp = client.post(
            "/api/v1/params/snapshots",
            json={"name": "x", "params": _params(), "frozen_keys": ["alpah"]},
            headers=admin.headers,
        )
        assert resp.status_code == 422

    def test_snapshot_404(self, client: TestClient, admin: _Actor) -> None:
        assert client.get("/api/v1/params/snapshots/10", headers=admin.headers).status_code == 404


# ---------------------------------------------------------------------------
# from-java
# ---------------------------------------------------------------------------


class TestFromJava:
    @respx.mock
    def test_pulls_params_and_creates_snapshot(self, client: TestClient, admin: _Actor) -> None:
        java_params = {
            "vectorStore": "memory",
            "rrfK": 60,
            "alpha": 0.5,
            "beta": 0.3,
            "recencyHalfLifeDays": 14.0,
            "profileBoost": 0.2,
            "minConfidence": 0.4,
            "injectMaxTokens": 2000,
        }
        respx.get(JAVA_PARAMS_URL).mock(
            return_value=httpx.Response(200, json={"code": "0000", "info": "成功", "data": java_params})
        )

        resp = client.post(
            "/api/v1/params/snapshots/from-java",
            json={"name": "agentwrite-live"},
            headers=admin.headers,
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Java 是 camelCase，平台内部是 snake_case——快照落库时已转好。
        assert body["params"]["vector_store"] == "memory"
        assert body["params"]["inject_max_tokens"] == 2000
        assert body["params_hash"].startswith("sha256:")

    @respx.mock
    def test_java_unavailable_is_502_and_writes_nothing(
        self, client: TestClient, admin: _Actor, db: Session
    ) -> None:
        """拉不到参数时必须失败，不能落一个空快照——那会生成看似合法却无意义的指纹。"""
        respx.get(JAVA_PARAMS_URL).mock(side_effect=httpx.ConnectError("refused"))

        before = db.execute(text("SELECT count(*) FROM eval_param_snapshots")).scalar_one()
        resp = client.post(
            "/api/v1/params/snapshots/from-java",
            json={"name": "x"},
            headers=admin.headers,
        )
        db.rollback()
        after = db.execute(text("SELECT count(*) FROM eval_param_snapshots")).scalar_one()

        assert resp.status_code == 502
        assert after == before


# ---------------------------------------------------------------------------
# 模型版本
# ---------------------------------------------------------------------------


class TestModelVersions:
    def test_get_or_create_reuses_row(self, client: TestClient, admin: _Actor) -> None:
        first = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e", "reranker_model_id": "r", "config": {"dim": 3072}},
            headers=admin.headers,
        )
        second = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e", "reranker_model_id": "r", "config": {"dim": 3072}},
            headers=admin.headers,
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

    def test_different_model_creates_new_row(self, client: TestClient, admin: _Actor) -> None:
        first = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e1", "reranker_model_id": "r", "config": {}},
            headers=admin.headers,
        )
        second = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e2", "reranker_model_id": "r", "config": {}},
            headers=admin.headers,
        )
        assert first.json()["id"] != second.json()["id"]
        assert first.json()["config_hash"] != second.json()["config_hash"]

    def test_model_version_404(self, client: TestClient, admin: _Actor) -> None:
        assert client.get("/api/v1/params/model-versions/10", headers=admin.headers).status_code == 404


# ---------------------------------------------------------------------------
# fingerprint 预览
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_version(db: Session) -> Iterator[tuple[int, int, str]]:
    """建一个最小数据集版本，只为给 fingerprint 预览提供 content_digest。

    **必须 commit**：`/fingerprint` 是只读预览端点，不会替我们提交，
    因此若这里只 flush，测试结束时的 rollback 会把它抹掉，清理阶段就找不到这行。
    返回 ``(dataset_id, version_id)`` 供清理直接使用，不必事后反查。
    """
    digest = f"sha256:{uuid.uuid4().hex}"
    dataset = Dataset(name=f"fp-{uuid.uuid4().hex[:8]}")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        schema_version=1,
        source="manual",
        content_digest=digest,
    )
    db.add(version)
    db.commit()

    dataset_id, version_id = dataset.id, version.id
    yield dataset_id, version_id, digest

    db.rollback()
    db.execute(text("DELETE FROM eval_dataset_versions WHERE dataset_id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
    db.commit()


class TestFingerprintPreview:
    def test_returns_all_components(
        self, client: TestClient, admin: _Actor, seeded_version: tuple[int, int, str]
    ) -> None:
        _, version_id, digest = seeded_version
        snapshot = _create_snapshot(client, admin, _params())
        model = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e", "reranker_model_id": "r", "config": {}},
            headers=admin.headers,
        ).json()

        resp = client.post(
            "/api/v1/params/fingerprint",
            json={
                "param_snapshot_id": snapshot["id"],
                "model_version_id": model["id"],
                "dataset_version_id": version_id,
                "mode": "exact",
            },
            headers=admin.headers,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 组成部分都要回显：跨 run 对比时要能解释「为什么指纹不同」。
        assert body["params_hash"] == snapshot["params_hash"]
        assert body["model_config_hash"] == model["config_hash"]
        assert body["dataset_content_digest"] == digest
        assert body["mode"] == "exact"
        assert body["config_fingerprint"].startswith("sha256:")

    def test_mode_change_changes_fingerprint(
        self, client: TestClient, admin: _Actor, seeded_version: tuple[int, int, str]
    ) -> None:
        """同一组配置换 mode 必须得到不同指纹——exact 与 hnsw 的指标不可直接比。"""
        _, version_id, _ = seeded_version
        snapshot = _create_snapshot(client, admin, _params())
        model = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e", "reranker_model_id": "r", "config": {}},
            headers=admin.headers,
        ).json()

        payload = {
            "param_snapshot_id": snapshot["id"],
            "model_version_id": model["id"],
            "dataset_version_id": version_id,
        }
        exact = client.post(
            "/api/v1/params/fingerprint", json={**payload, "mode": "exact"}, headers=admin.headers
        ).json()["config_fingerprint"]
        hnsw = client.post(
            "/api/v1/params/fingerprint", json={**payload, "mode": "hnsw"}, headers=admin.headers
        ).json()["config_fingerprint"]

        assert exact != hnsw

    def test_unknown_components_are_404(
        self, client: TestClient, admin: _Actor, seeded_version: tuple[int, int, str]
    ) -> None:
        _, version_id, _ = seeded_version
        snapshot = _create_snapshot(client, admin, _params())
        model = client.post(
            "/api/v1/params/model-versions",
            json={"embedding_model_id": "e", "reranker_model_id": "r", "config": {}},
            headers=admin.headers,
        ).json()

        base = {
            "param_snapshot_id": snapshot["id"],
            "model_version_id": model["id"],
            "dataset_version_id": version_id,
        }
        # 每一项不存在都应 404：指纹必须指向真实存在的配置组合，
        # 否则调用方会拿它去建一个外键指向空气的 run。
        for broken in (
            {**base, "param_snapshot_id": 10**12},
            {**base, "model_version_id": 10**12},
            {**base, "dataset_version_id": 10**12},
        ):
            resp = client.post("/api/v1/params/fingerprint", json=broken, headers=admin.headers)
            assert resp.status_code == 404, broken

    def test_invalid_mode_is_422(self, client: TestClient, admin: _Actor) -> None:
        resp = client.post(
            "/api/v1/params/fingerprint",
            json={
                "param_snapshot_id": 1,
                "model_version_id": 1,
                "dataset_version_id": 1,
                "mode": "HNSW",
            },
            headers=admin.headers,
        )
        assert resp.status_code == 422
