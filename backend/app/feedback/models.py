"""反哺样本模型的 ORM 定义（EP-1 模型组 C）。

`eval_feedback_samples` 承接线上/人工产生的候选样本，经审核后归档进评测集，
形成「结果 → 反哺 → 数据集」的闭环：
- `source` / `source_ref` 记录样本来源与外部引用，保证可追溯。
- `status` 是审核状态机，取值由 CHECK 约束下沉到数据库。
- `archived_dataset_version_id` 在被审核通过并归档后回填，指向落地的数据集版本。

`reviewed_by` / `created_by` / 指向用户，用户是审计主体，不随用户删除而级联。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FeedbackSample(Base):
    """反哺候选样本 `eval_feedback_samples`。

    `CHECK (status IN (...))` 把审核状态约束下沉到数据库，防止应用层漏校验
    写入非法状态；命名约定会自动补 `ck_eval_feedback_samples_` 前缀。
    本表仅有 `created_at`（无 `updated_at`），审核时间单独记 `reviewed_at`。
    """

    __tablename__ = "eval_feedback_samples"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new','reviewing','approved','rejected','archived')",
            name="status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ground_truth: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="new"
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_dataset_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_dataset_versions.id"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
