"""数据库层：声明式基类、会话工厂、模型注册。

`Base.metadata` 只包含**已被导入**的模型。模型注册由
:func:`app.db.registry.import_all_models` 负责，它同时被 Alembic 的 ``env.py``
和 :func:`app.db.session.get_session_factory` 调用——迁移路径与运行时路径
共用同一份完整 metadata，不会出现「迁移看得到表、ORM 解析不了外键」的割裂。
"""

from app.db.base import Base, TimestampMixin
from app.db.registry import import_all_models
from app.db.session import get_engine, get_session_factory, reset_engine

__all__ = [
    "Base",
    "TimestampMixin",
    "get_engine",
    "get_session_factory",
    "import_all_models",
    "reset_engine",
]
