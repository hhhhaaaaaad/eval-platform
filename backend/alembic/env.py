"""Alembic 迁移环境。

设计要点：

- 数据库 URL 从 ``app.settings.config.get_settings()`` 读取而非硬编码，保证 API、
  worker、迁移三者看到同一份配置（``alembic.ini`` 里刻意不写 ``sqlalchemy.url``）。
- ``target_metadata`` 绑定 ``Base.metadata``，并先调用 ``import_all_models()``
  把全部模型注册进来——新增模型包时必须在该函数里登记，否则迁移会漏表。
- ``compare_type=True`` 让 autogenerate 能感知列类型变更。
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.db import import_all_models
from app.db.base import Base
from app.settings.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用应用配置覆盖 alembic.ini 中的 URL（ini 里刻意留空）。
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# 关键：先把全部模型导入，Base.metadata 才是完整的。
import_all_models()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：不连库，直接生成 SQL。

    用于在没有数据库的环境下校验迁移能否产出合法 DDL——
    ``alembic upgrade head --sql``。
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
