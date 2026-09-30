"""认证依赖与 RBAC（FastAPI dependency）。

设计要点：

- **角色以数据库为准，不信 token 内的 role 声明**。token 签发后用户角色可能被调整，
  若直接信 token，降权要等到 token 过期才生效——这是一个安全缺口。
  token 只用于确定身份（`sub`），角色每次从库里读。
- `HTTPBearer(auto_error=False)`：缺少 `Authorization` 头时由我们统一返回 401，
  而不是让 FastAPI 直接抛 403——语义更准确（未认证 ≠ 无权限），响应格式也一致。
- 每个请求一个 `Session`，请求结束必然关闭，避免连接泄漏。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.models import User
from app.auth.security import InvalidTokenError, decode_access_token
from app.db.session import get_session_factory

_bearer = HTTPBearer(auto_error=False)


def get_db() -> Iterator[Session]:
    """请求级 Session：进入时创建，退出时关闭。

    **不在此处 commit**——事务边界由各端点显式控制，避免「读接口也提交」
    这种隐式行为。
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: DbSession,
) -> User:
    """解析 Bearer token 并加载用户；任一环节失败返回 401。"""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("缺少 Authorization 头")

    try:
        payload = decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _unauthorized("token 无效或已过期") from exc

    try:
        user_id = int(payload.sub)
    except (TypeError, ValueError) as exc:
        # sub 不是数字说明 token 被伪造或来自其它系统，按无效处理而不是 500
        raise _unauthorized("token 主体非法") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("用户不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*allowed: str):
    """依赖工厂：只允许指定角色通过，否则 403。

    用法：`def create_thing(user: AdminUser)`（见下方 `AdminUser`）。
    """

    def _dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要角色 {'/'.join(allowed)}，当前为 {user.role}",
            )
        return user

    return _dependency


# 两档 RBAC：viewer 只读，admin 可写（建数据集 / 参数快照 / run）
require_admin = require_role("admin")
AdminUser = Annotated[User, Depends(require_admin)]
