"""健康探针。

- ``GET /health``：进程存活探针，不触碰任何外部依赖，永远快速返回 200。
- ``GET /ready``：就绪探针，逐项检查 Postgres 与 Redis；任一不可用返回 503。

探针实现必须吞掉**全部**异常并转成 ``{ok: false, error}``：探针本身抛 500 会
掩盖真正的依赖故障，也会让编排系统误判进程状态。
"""

from __future__ import annotations

from typing import Any

import redis
from fastapi import APIRouter, Response, status
from sqlalchemy import create_engine, text

from app.settings.config import get_settings
from app.settings.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health")
def health() -> dict[str, Any]:
    """进程存活探针：只读配置，不打任何下游依赖。"""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }


def _check_postgres(database_url: str) -> dict[str, Any]:
    """用 SQLAlchemy 执行 ``SELECT 1``；connect_timeout=3 避免探针长时间挂起。"""
    try:
        engine = create_engine(
            database_url,
            connect_args={"connect_timeout": 3},
            pool_pre_ping=True,
        )
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - 探针必须吞掉全部异常
        return {"ok": False, "error": str(exc)}


def _check_redis(redis_url: str) -> dict[str, Any]:
    """Redis ``ping`` 检查；连接与读写超时都设为 3 秒。"""
    try:
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3)
        try:
            client.ping()
        finally:
            client.close()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - 探针必须吞掉全部异常
        return {"ok": False, "error": str(exc)}


@router.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    """就绪探针：Postgres 与 Redis 全可用才返回 ready，否则 503。"""
    settings = get_settings()
    checks: dict[str, dict[str, Any]] = {
        "postgres": _check_postgres(settings.database_url),
        "redis": _check_redis(settings.redis_url),
    }
    all_ok = all(item["ok"] for item in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readiness check failed: %s", checks)
    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks,
    }
