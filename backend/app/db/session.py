"""数据库引擎与会话工厂。

惰性创建：模块导入时不连库，避免测试或 CLI 命令（如 `alembic --help`）
因为库不可用而失败。
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.registry import import_all_models
from app.settings.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """进程级单例引擎。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,  # 连接被中间件掐断后自动重建，避免 stale connection
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        # 先补齐模型注册再建工厂：所有 ORM 使用路径都经过这里，因此这是保证
        # Base.metadata 完整的唯一必要位置。缺了它，只导入部分模型的进程
        # （如仅 import app.datasets 的 worker）会在解析外键时抛
        # NoReferencedTableError。幂等且惰性，不增加模块导入成本。
        import_all_models()
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def reset_engine() -> None:
    """释放引擎与会话工厂（测试或配置变更后调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
