"""评测集、版本与用例模型的 ORM 定义（EP-1 模型组 A）。

三层结构：`eval_datasets`（逻辑数据集）→ `eval_dataset_versions`（不可变快照）
→ `eval_cases`（单条用例）。版本一旦写入即视为不可变：
- `content_digest` 记录整个版本的指纹，用于导入去重与审计追溯。
- `parent_version_id` 自引用形成版本谱系（fork/演进链）。
- `eval_cases.content_hash` 与版本一起构成唯一键 (`dataset_version_id`,
  `content_hash`)，避免同一版本内重复用例。

外键上的 `ON DELETE CASCADE` 让删除数据集/版本时自动清理下级用例，
但 `created_by` 指向用户不做级联——用户是审计主体，不应因删用户而牵连数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Dataset(Base):
    """逻辑数据集 `eval_datasets`。

    `is_archived` 用软删除替代物理删除，保留历史版本与跑批结果的可追溯性。
    """

    __tablename__ = "eval_datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class DatasetVersion(Base):
    """数据集版本快照 `eval_dataset_versions`。

    唯一键 `(dataset_id, version)` 保证同一数据集下版本号不重复；
    `config` 存该版本导入时的参数（分片、过滤规则等），默认空对象。
    """

    __tablename__ = "eval_dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eval_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    parent_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_dataset_versions.id"), nullable=True
    )
    content_digest: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Case(Base):
    """单条评测用例 `eval_cases`。

    `payload` 是喂给模型的输入，`ground_truth` 是评测参考答案；
    `case_type` / `group_key` 用于分类与分组统计。
    """

    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "content_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eval_dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    case_type: Mapped[str] = mapped_column(Text, nullable=False)
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ground_truth: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
