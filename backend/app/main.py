"""FastAPI 应用入口。

用 ``create_app()`` 工厂构造应用（便于测试里独立实例化），模块级 ``app`` 供
``uvicorn app.main:app`` 直接引用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.auth.api import router as auth_router
from app.settings.config import get_settings
from app.settings.logging import RequestIdMiddleware, configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭钩子：启动时初始化日志并打印一次启动信息。"""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    logger.info(
        "memory-eval-platform 启动: env=%s java_eval_base_url=%s",
        settings.environment,
        settings.java_eval_base_url,
    )
    yield
    logger.info("memory-eval-platform 关闭")


def create_app() -> FastAPI:
    """构造并配置 FastAPI 应用。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    # 中间件在路由之前挂载，保证所有请求都带 request id。
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    return app


app = create_app()
