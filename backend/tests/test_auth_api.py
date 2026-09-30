"""登录端点与 RBAC 的 **HTTP 层集成测试**（无需数据库）。

手法：用 FastAPI 的 `dependency_overrides` 把 `get_db` 换成假 Session，
从而在无 Postgres 的环境下端到端验证「请求 → 路由 → 认证 → 响应」全链路，
而不只是对函数做单元测试。

未覆盖（需真实数据库）：真实用户查库、审计行落库、事务回滚时审计一并回滚。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.deps import AdminUser, get_db
from app.auth.models import User
from app.auth.security import create_access_token, hash_password
from app.main import create_app

PASSWORD = "correct-horse-battery"


class _FakeResult:
    def __init__(self, user: User | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> User | None:
        return self._user


class FakeSession:
    """够用的假 Session：只支持登录/鉴权路径用到的方法。"""

    def __init__(self, user: User | None = None) -> None:
        self._user = user
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._user)

    def get(self, model: Any, pk: Any) -> User | None:
        if self._user is not None and model is User and self._user.id == pk:
            return self._user
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        pass


def _make_user(user_id: int = 1, *, role: str = "viewer", active: bool = True) -> User:
    return User(
        id=user_id,
        username="alice",
        password_hash=hash_password(PASSWORD),
        role=role,
        is_active=active,
    )


def _client_with(session: FakeSession) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


# --------------------------- 登录 ---------------------------


def test_login_success_returns_token() -> None:
    session = FakeSession(_make_user(role="admin"))
    with _client_with(session) as client:
        resp = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user_id"] == 1
    assert body["role"] == "admin"
    assert body["expires_in"] > 0
    assert body["access_token"]
    # 登录成功要写审计并提交
    assert session.committed is True
    assert any(getattr(a, "action", None) == "auth.login" for a in session.added)


def test_login_wrong_password_is_401() -> None:
    session = FakeSession(_make_user())
    with _client_with(session) as client:
        resp = client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})

    assert resp.status_code == 401
    # 失败也记审计（安全信号），且必须已提交
    assert session.committed is True
    assert any(getattr(a, "action", None) == "auth.login_failed" for a in session.added)


def test_login_unknown_user_is_401_with_same_message() -> None:
    """用户不存在与密码错误必须返回**同样的文案**，否则可用于枚举用户名。"""
    session = FakeSession(None)
    with _client_with(session) as client:
        unknown = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})

    wrong_pw_session = FakeSession(_make_user())
    with _client_with(wrong_pw_session) as client:
        wrong_pw = client.post("/api/v1/auth/login", json={"username": "alice", "password": "x"})

    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["detail"] == wrong_pw.json()["detail"]


def test_login_inactive_user_is_401() -> None:
    session = FakeSession(_make_user(active=False))
    with _client_with(session) as client:
        resp = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})

    assert resp.status_code == 401


# --------------------------- RBAC ---------------------------


def _rbac_app(session: FakeSession) -> TestClient:
    """一个最小应用，用 AdminUser 保护一条路由，用于验证 403 判定。"""
    app = FastAPI()

    @app.get("/admin-only")
    def admin_only(user: AdminUser) -> dict[str, Any]:
        return {"role": user.role}

    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def test_admin_can_access_admin_route() -> None:
    session = FakeSession(_make_user(role="admin"))
    token = create_access_token(subject="1", role="admin")
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == {"role": "admin"}


def test_viewer_is_forbidden_on_admin_route() -> None:
    session = FakeSession(_make_user(role="viewer"))
    token = create_access_token(subject="1", role="viewer")
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403


def test_role_is_read_from_db_not_from_token() -> None:
    """token 里写 admin、但库里是 viewer → 必须按库里的 viewer 拒绝。

    这是关键安全性质：若信 token 里的 role，用户被降权后仍能用旧 token 越权。
    """
    session = FakeSession(_make_user(role="viewer"))
    forged_role_token = create_access_token(subject="1", role="admin")
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {forged_role_token}"})

    assert resp.status_code == 403, "角色必须以数据库为准，不能信 token 声明"


def test_missing_authorization_header_is_401() -> None:
    session = FakeSession(_make_user())
    with _rbac_app(session) as client:
        resp = client.get("/admin-only")

    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_invalid_token_is_401() -> None:
    session = FakeSession(_make_user())
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": "Bearer not-a-jwt"})

    assert resp.status_code == 401


def test_expired_token_is_401() -> None:
    session = FakeSession(_make_user())
    expired = create_access_token(subject="1", role="admin", expires_minutes=-1)
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {expired}"})

    assert resp.status_code == 401


def test_token_for_deactivated_user_is_401() -> None:
    """用户被停用后，其仍有效的 token 必须立刻失效。"""
    session = FakeSession(_make_user(active=False))
    token = create_access_token(subject="1", role="viewer")
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401


def test_non_numeric_subject_is_401_not_500() -> None:
    """伪造/异构系统签发的 token（sub 非数字）应判为无效，而不是抛 500。"""
    session = FakeSession(_make_user())
    token = create_access_token(subject="not-a-number", role="admin")
    with _rbac_app(session) as client:
        resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401
