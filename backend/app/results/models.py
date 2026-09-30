"""评测结果模型的 ORM 定义（EP-1 模型组 C）。

两层结果粒度：
- `eval_run_results`：一次跑批在 `(dimension, metric_name)` 维度上的**聚合**指标，
  如总体 accuracy / 各维度均分。
- `eval_case_results`：逐用例的指标明细，`metric_values` 用 JSONB 承载
  「指标名 → 值」的稀疏映射（不同用例类型参与的指标不同）。

两张表都带唯一约束，作为**结果 upsert 的冲突目标**：
- `uq_run_dimension_metric` / `uq_run_case_dimension` 让重复写入同一批结果时
  可用 `ON CONFLICT ... DO UPDATE` 幂等刷新。方案 §5 要求这两个约束必须存在。

`run_id` 指向 `eval_runs`（跨组表），此处用字符串外键声明，避免 import 其它模型包；
`ON DELETE RESTRICT` 保证有结果的跑批不被误删。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RunResult(Base):
    """跑批聚合结果 `eval_run_results`。

    唯一键 `(run_id, dimension, metric_name)` 是聚合结果 upsert 的冲突目标。
    `variance` / `confidence` 可空，部分指标无法给出波动或置信度。
    """

    __tablename__ = "eval_run_results"
    __table_args__ = (
        UniqueConstraint("run_id", "dimension", "metric_name", name="uq_run_dimension_metric"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    variance: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CaseResult(Base):
    """逐用例结果明细 `eval_case_results`。

    唯一键 `(run_id, case_id, dimension)` 是明细结果 upsert 的冲突目标；
    `idx_cr_run` 加速按跑批聚合查询。
    """

    __tablename__ = "eval_case_results"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", "dimension", name="uq_run_case_dimension"),
        Index("idx_cr_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    case_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("eval_cases.id"), nullable=False
    )
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    metric_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
