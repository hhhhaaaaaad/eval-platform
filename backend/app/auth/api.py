"""认证 API：登录。

登录不做「先查用户、再校验密码」的短路——即使用户不存在也走一次 bcrypt 校验，
避免通过响应耗时区分「用户不存在」与「密码错误」（用户名枚举）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.audit.service import write_audit
from app.auth.deps import DbSession
from app.auth.models import User
from app.auth.schemas import LoginRequest, LoginResponse
from app.auth.security import create_access_token, hash_password, verify_password
from app.settings.config import get_settings
from app.settings.logging import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)

# 用户不存在时用来「陪跑」校验的假 hash，保证两条路径耗时接近。
# 惰性生成：bcrypt 一次约 100ms，不应让模块 import 承担这个成本。
_dummy_hash_cache: str | None = None


def _dummy_hash() -> str:
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = hash_password("dummy-password-for-timing-equalization")
    return _dummy_hash_cache


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, db: DbSession) -> LoginResponse:
    settings = get_settings()

    user = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()

    # 两条路径都执行 bcrypt，避免用户名枚举
    hashed = user.password_hash if user is not None else _dummy_hash()
    password_ok = verify_password(payload.password, hashed)

    if user is None or not password_ok or not user.is_active:
        # 失败也记审计：登录失败是重要的安全信号。
        # 必须显式 commit——下面抛异常会让事务回滚，审计就白写了。
        write_audit(
            db,
            actor_user_id=user.id if user is not None else None,
            action="auth.login_failed",
            resource_type="user",
            resource_id=str(user.id) if user is not None else payload.username,
            after={"reason": "invalid_credentials" if user is not None else "unknown_user"},
            ip=_client_ip(request),
        )
        db.commit()
        logger.warning("登录失败 username=%s ip=%s", payload.username, _client_ip(request))
        # 统一文案，不区分「用户不存在」与「密码错误」
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(subject=str(user.id), role=user.role)

    write_audit(
        db,
        actor_user_id=user.id,
        action="auth.login",
        resource_type="user",
        resource_id=str(user.id),
        ip=_client_ip(request),
    )
    db.commit()

    logger.info("登录成功 user_id=%s role=%s", user.id, user.role)
    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )
