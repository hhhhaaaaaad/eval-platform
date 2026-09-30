"""SQLAlchemy 声明式基类与命名约定。

**所有模型必须继承本模块的 `Base`**，否则不会被 `Base.metadata` 收集，
Alembic 迁移也看不到它。

命名约定（`NAMING_CONVENTION`）让约束名可预测，从而能被后续迁移可靠地
DROP/ALTER。方案 §5 中**显式命名**的索引（如 `uq_runs_active_cfg`）保持其原始名字，
不受本约定影响。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束/索引自动命名模板。%(column_0_N_name)s 支持多列（如 uq_x_a_b）。
#
# fk 刻意**不含** %(referred_table_name)s：PostgreSQL 标识符上限 63 字符，
# 而本项目中 `fk_<表>_<列>_<被引用表>` 会在长表名上超限——
# 例如 fk_eval_dataset_versions_parent_version_id_eval_dataset_versions 有 64 字符，
# fk_eval_feedback_samples_archived_dataset_version_id_eval_dataset_versions 更是 78 字符。
# 去掉被引用表后最长者为 fk_eval_feedback_samples_archived_dataset_version_id（56 字符），
# 留有余量。该缺陷只在 SQLAlchemy 编译 DDL 时才会暴露，metadata 层检查发现不了。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """`created_at` / `updated_at` 通用列。

    时间统一用 `timestamptz`（方案 §5 要求），默认值由数据库 `now()` 生成，
    避免应用服务器与数据库时钟不一致。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
