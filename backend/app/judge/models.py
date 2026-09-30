"""LLM-as-judge 任务模型的 ORM 定义（EP-1 模型组 C）。

`eval_judge_jobs` 记录一对回答的成对比较任务：
- `pair_key` 标识被比较的样本对；`position_swap` 支持 A/B 位置互换以消除位置偏差。
- `input_a` / `input_b` 存两条待判输入，`verdict` / `rationale` / `confidence`
  存裁判模型的判定结果。
- `status` 是任务状态机，取值由 CHECK 约束下沉到数据库。

`run_id` 可空（允许脱离跑批的离线判分），指向 `eval_runs`（跨组表），
用字符串外键声明避免 import 其它模型包；`ON DELETE RESTRICT` 保护跑批不被误删。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JudgeJob(Base):
    """成对裁判任务 `eval_judge_jobs`。

    `CHECK (status IN (...))` 把任务状态约束下沉到数据库，防止应用层漏校验
    写入非法状态；命名约定会自动补 `ck_eval_judge_jobs_` 前缀。
    本表仅有 `created_at`（无 `updated_at`），完成时间单独记 `finished_at`。
    """

    __tablename__ = "eval_judge_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    pair_key: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_type: Mapped[str] = mapped_column(Text, nullable=False)
    judge_model: Mapped[str] = mapped_column(Text, nullable=False)
    input_a: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_b: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    position_swap: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
