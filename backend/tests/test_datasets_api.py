"""数据集 API 的 **HTTP 层**集成测试（真实 Postgres）。

覆盖三件只有走完整 HTTP 栈才能验证的事：

1. **RBAC**：读放行 viewer、写必须 admin（写接口被非授权者调用必须 403，而不是「没这条路由」）；
2. **状态码映射**：404 / 409 / 422 各自对应到「不存在」「与状态冲突」「结构不合法」；
3. **没有更新/删除版本的路由**——不可变性在 API 表面的体现（调用应得 405）。

需要 Postgres；不可用时整模块跳过。API 端点会 commit，故用例结束需显式清理
（先删数据集再删用户，因为 `eval_datasets.created_by` 无级联）。
"""

from __future__ import annotations

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
        pytest.skip(f"Postgres 不可用，跳过数据集 API 集成测试: {exc}")
    yield session
    session.close()


def _make_user(db: Session, *, role: str) -> _Actor:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"ds-{role}-{suffix}",
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    db.commit()
    token = create_access_token(subject=str(user.id), role=role)
    return _Actor(user.id, token)


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


def _cleanup(db: Session, user_id: int) -> None:
    """按外键依赖倒序清理。

    `eval_audit_log.actor_user_id` 也指向本用户且**无级联**（审计主体不应因删用户而消失），
    所以必须先删审计行再删用户——这一步本身就是「mutation 确实写了审计」的旁证。
    """
    db.rollback()
    db.execute(text("DELETE FROM eval_audit_log WHERE actor_user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM eval_datasets WHERE created_by = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM eval_users WHERE id = :uid"), {"uid": user_id})
    db.commit()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


def _query_case(query_id: str, memory_ids: list[int] | None = None) -> dict:
    return {
        "case_type": "query_to_memory",
        "group_key": "g1",
        "payload": {"query_id": query_id, "query": "咖啡", "task_type": "LEGACY"},
        "ground_truth": {"relevant_memory_ids": memory_ids if memory_ids is not None else [101]},
    }


def _create_dataset(client: TestClient, actor: _Actor, name: str = "评测集") -> int:
    resp = client.post("/api/v1/datasets", json={"name": name}, headers=actor.headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _import(client: TestClient, actor: _Actor, dataset_id: int, version: str, cases: list[dict]):
    return client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        json={"version": version, "source": "manual", "cases": cases},
        headers=actor.headers,
    )


# ---------------------------------------------------------------------------
# 认证与授权
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_token_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/datasets").status_code == 401

    def test_viewer_can_list(self, client: TestClient, viewer: _Actor) -> None:
        """读接口对 viewer 放行——只读角色不该被挡在评测数据之外。"""
        resp = client.get("/api/v1/datasets", headers=viewer.headers)
        assert resp.status_code == 200

    def test_viewer_cannot_create(self, client: TestClient, viewer: _Actor) -> None:
        resp = client.post("/api/v1/datasets", json={"name": "x"}, headers=viewer.headers)
        assert resp.status_code == 403

    def test_viewer_cannot_import_version(self, client: TestClient, admin: _Actor, viewer: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        resp = _import(client, viewer, dataset_id, "v1", [_query_case("q1")])
        assert resp.status_code == 403

    def test_viewer_cannot_archive(self, client: TestClient, admin: _Actor, viewer: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        resp = client.post(f"/api/v1/datasets/{dataset_id}/archive", headers=viewer.headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_create_list_and_get(self, client: TestClient, admin: _Actor) -> None:
        dataset_id = _create_dataset(client, admin, name="记忆系统黄金集")

        listing = client.get("/api/v1/datasets", headers=admin.headers)
        assert listing.status_code == 200
        assert any(item["id"] == dataset_id for item in listing.json())

        detail = client.get(f"/api/v1/datasets/{dataset_id}", headers=admin.headers)
        assert detail.status_code == 200
        assert detail.json()["name"] == "记忆系统黄金集"
        assert detail.json()["is_archived"] is False

    def test_import_version_returns_count_and_digest(self, client: TestClient, admin: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        resp = _import(client, admin, dataset_id, "v1", [_query_case("q1"), _query_case("q2")])

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["case_count"] == 2
        assert body["content_digest"].startswith("sha256:")
        assert body["schema_version"] == 1

    def test_list_versions_and_fetch_detail(self, client: TestClient, admin: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        _import(client, admin, dataset_id, "v1", [_query_case("q1")])
        _import(client, admin, dataset_id, "v2", [_query_case("q1"), _query_case("q2")])

        versions = client.get(f"/api/v1/datasets/{dataset_id}/versions", headers=admin.headers)
        assert versions.status_code == 200
        assert [(v["version"], v["case_count"]) for v in versions.json()] == [("v1", 1), ("v2", 2)]

        version_id = versions.json()[0]["id"]
        detail = client.get(f"/api/v1/datasets/versions/{version_id}", headers=admin.headers)
        assert detail.status_code == 200
        assert len(detail.json()["cases"]) == 1
        # 详情足以完全复现一份评测集：payload 与 ground_truth 都要在。
        assert detail.json()["cases"][0]["ground_truth"]["relevant_memory_ids"] == [101]


# ---------------------------------------------------------------------------
# 状态码映射
# ---------------------------------------------------------------------------


class TestStatusCodes:
    def test_unknown_dataset_is_404(self, client: TestClient, admin: _Actor) -> None:
        assert client.get("/api/v1/datasets/10", headers=admin.headers).status_code == 404
        assert client.get("/api/v1/datasets/10/versions", headers=admin.headers).status_code == 404
        assert _import(client, admin, 10, "v1", [_query_case("q1")]).status_code == 404

    def test_unknown_version_is_404(self, client: TestClient, admin: _Actor) -> None:
        assert client.get("/api/v1/datasets/versions/10", headers=admin.headers).status_code == 404

    def test_duplicate_version_number_is_409(self, client: TestClient, admin: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        _import(client, admin, dataset_id, "v1", [_query_case("q1")])

        resp = _import(client, admin, dataset_id, "v1", [_query_case("q2")])
        assert resp.status_code == 409

    def test_duplicate_digest_is_409(self, client: TestClient, admin: _Actor) -> None:
        """同内容换个版本号再导一次 → 409，不是 201。"""
        dataset_id = _create_dataset(client, admin)
        _import(client, admin, dataset_id, "v1", [_query_case("q1")])

        resp = _import(client, admin, dataset_id, "v2", [_query_case("q1")])
        assert resp.status_code == 409

    def test_archived_dataset_import_is_409(self, client: TestClient, admin: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        _import(client, admin, dataset_id, "v1", [_query_case("q1")])
        assert client.post(f"/api/v1/datasets/{dataset_id}/archive", headers=admin.headers).status_code == 200

        resp = _import(client, admin, dataset_id, "v2", [_query_case("q2")])
        assert resp.status_code == 409

    def test_duplicate_cases_in_batch_is_422_with_location(self, client: TestClient, admin: _Actor) -> None:
        """422 且要能定位到具体是哪几条重复——标注员需要知道改哪一行。"""
        dataset_id = _create_dataset(client, admin)
        resp = _import(client, admin, dataset_id, "v1", [_query_case("q1"), _query_case("q1")])

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["case_indexes"] == [0, 1]
        assert detail["content_hash"].startswith("sha256:")

    def test_invalid_case_payload_is_422(self, client: TestClient, admin: _Actor) -> None:
        """缺 dialogue_id 的对话类用例必须在导入期被拦下，而不是等到跑批才发现。"""
        dataset_id = _create_dataset(client, admin)
        bad_case = {
            "case_type": "conversation_to_memory",
            "group_key": "g",
            "payload": {"messages": [{"role": "user", "content": "hi"}]},
            "ground_truth": {"ground_truth_memories": [{"content": "x"}]},
        }
        assert _import(client, admin, dataset_id, "v1", [bad_case]).status_code == 422

    def test_query_case_without_answer_is_422(self, client: TestClient, admin: _Actor) -> None:
        """没有正确答案的用例算不出 Recall，必须拒绝。"""
        dataset_id = _create_dataset(client, admin)
        bad_case = {
            "case_type": "query_to_memory",
            "group_key": "g",
            "payload": {"query_id": "q1", "query": "咖啡"},
            "ground_truth": {},
        }
        assert _import(client, admin, dataset_id, "v1", [bad_case]).status_code == 422

    def test_parent_version_from_other_dataset_is_422(self, client: TestClient, admin: _Actor) -> None:
        a = _create_dataset(client, admin, name="A")
        b = _create_dataset(client, admin, name="B")
        parent = _import(client, admin, a, "v1", [_query_case("q1")]).json()

        resp = client.post(
            f"/api/v1/datasets/{b}/versions",
            json={
                "version": "v1",
                "source": "manual",
                "parent_version_id": parent["id"],
                "cases": [_query_case("q2")],
            },
            headers=admin.headers,
        )
        assert resp.status_code == 422

    def test_empty_cases_is_422(self, client: TestClient, admin: _Actor) -> None:
        dataset_id = _create_dataset(client, admin)
        assert _import(client, admin, dataset_id, "v1", []).status_code == 422


# ---------------------------------------------------------------------------
# 不可变性在 API 表面的体现
# ---------------------------------------------------------------------------


class TestImmutabilitySurface:
    @pytest.mark.parametrize("method", ["put", "patch", "delete"])
    def test_no_mutation_routes_for_versions(self, client: TestClient, admin: _Actor, method: str) -> None:
        """版本不可变：不提供任何改/删路由。405 而非 404 说明路径存在但没有该方法——
        两种情况都表明「改不动」，这里只要求不是 2xx。"""
        dataset_id = _create_dataset(client, admin)
        version_id = _import(client, admin, dataset_id, "v1", [_query_case("q1")]).json()["id"]

        resp = getattr(client, method)(
            f"/api/v1/datasets/versions/{version_id}", headers=admin.headers
        )
        assert resp.status_code in (404, 405), f"{method.upper()} 不应成功: {resp.status_code}"

    def test_archive_hides_from_default_listing_but_keeps_versions(
        self, client: TestClient, admin: _Actor
    ) -> None:
        dataset_id = _create_dataset(client, admin)
        _import(client, admin, dataset_id, "v1", [_query_case("q1")])
        client.post(f"/api/v1/datasets/{dataset_id}/archive", headers=admin.headers)

        default = client.get("/api/v1/datasets", headers=admin.headers).json()
        assert dataset_id not in [item["id"] for item in default]

        with_archived = client.get(
            "/api/v1/datasets?include_archived=true", headers=admin.headers
        ).json()
        assert dataset_id in [item["id"] for item in with_archived]

        # 归档是封存不是删除：版本必须还在
        versions = client.get(f"/api/v1/datasets/{dataset_id}/versions", headers=admin.headers)
        assert versions.status_code == 200
        assert len(versions.json()) == 1
