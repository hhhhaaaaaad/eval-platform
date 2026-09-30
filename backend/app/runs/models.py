"""评测实验、run 与全局 guard 的 ORM 定义（EP-1 模型组 B，本组风险集中区）。

三张表构成 run 编排的核心：

- `eval_experiments`：实验分组（逻辑容器），一个实验下可有多次 run。
- `eval_runs`：一次评测执行的状态机 + 并发控制。字段繁多，重点见下。
- `eval_run_guard`：单行全局锁表，协调「独占模式」的全库串行。

== `eval_runs` 的并发约束（方案核心）==

DDL 在 `eval_runs` 上建了 **3 个 partial unique index**，它们是防止重复/并发跑批的
唯一强保证（应用层检查存在竞态，最终以 DB 约束兜底）：

1. `uq_runs_idempotency` (idempotency_key) WHERE idempotency_key IS NOT NULL
   幂等键唯一。调用方带幂等键重试时，最多只有一个 run 能落库；不带键（NULL）的
   run 不受约束（partial —— 否则多个 NULL 在标准 SQL 里本可共存，但显式声明更清晰）。

2. `uq_runs_active_cfg` (config_fingerprint) WHERE status IN ('pending','running')
   同一份配置指纹，**同时只允许一个活跃 run**。已完成（succeeded/failed/cancelled）
   的 run 不占用该指纹，故同一指纹可被历史多次重跑；但只要有 pending/running，
   重复提交就会被 DB 拒绝。

3. `uq_runs_active_user` (eval_user_id) WHERE status IN ('pending','running')
   单个用户**同时只允许一个活跃 run**。这是一条「QPS=1/人」的产品策略，用 DB 约束
   强制，避免绕过应用层限流。

其余普通索引（`idx_runs_status` / `idx_runs_cfg` / `idx_runs_created` /
`idx_runs_heartbeat` / `idx_runs_pending`）服务查询与租约回收：
- `idx_runs_heartbeat` 只覆盖 running，供 watchdog 扫心跳超时的 run。
- `idx_runs_pending` 只覆盖 pending，供调度器按 created_at 取下一个待跑 run。

== 状态机与租约 ==

`status` 取值受 CHECK 约束；`lease_owner` + `heartbeat_at` + `fencing_version`
实现分布式租约：worker 领到 run 后写 owner/心跳，`fencing_version` 单调递增，
旧 worker 的过期写会被拒（fencing token 防脑裂）。`exclusive` 标记该 run 需要
独占（此时须与 `eval_run_guard` 配合）。`checkpoint.completed_stages` 支持断点续跑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Experiment(Base):
    """实验分组表 `eval_experiments`。

    仅作为 run 的逻辑容器（可选归属）。`id` 用 UUID，便于在 API 层直接暴露而不
    泄露自增序号；`created_by` 指向 `eval_users`，可为空。
    """

    __tablename__ = "eval_experiments"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Run(Base, TimestampMixin):
    """评测 run 表 `eval_runs`。

    索引与 CHECK 约束的语义见模块 docstring。`created_at` / `updated_at` 由
    `TimestampMixin` 提供（均 `now()` 默认值），与 DDL 一致。

    跨组外键（`eval_users` / `eval_dataset_versions`）用字符串表名声明，避免
    在本模块 import 其他 worker 尚未完成的模型包。
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('exact','hnsw')", name="mode_valid"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')",
            name="status_valid",
        ),
        # 普通索引：查询 / 调度 / 租约回收
        Index("idx_runs_status", "status"),
        Index("idx_runs_cfg", "config_fingerprint"),
        Index("idx_runs_created", text("created_at DESC")),
        Index(
            "idx_runs_heartbeat",
            "heartbeat_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "idx_runs_pending",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # 3 个 partial unique index —— 并发约束核心，名字必须逐字一致
        Index(
            "uq_runs_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_runs_active_cfg",
            "config_fingerprint",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
        Index(
            "uq_runs_active_user",
            "eval_user_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("eval_experiments.id"), nullable=True
    )
    dataset_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eval_dataset_versions.id"),
        nullable=False,
    )
    param_snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eval_param_snapshots.id"),
        nullable=False,
    )
    model_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eval_model_versions.id"),
        nullable=False,
    )
    eval_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'exact'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fencing_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    exclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    current_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[Any] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("0")
    )
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{\"completed_stages\":[]}'::jsonb"),
    )
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )


class RunGuard(Base):
    """全局 run 独占锁 `eval_run_guard`（单行表）。

    表只有一行（`CHECK (id = 1)`），用来串行化「独占模式」的 run：`exclusive_owner`
    记录当前持锁的 run id（或 owner 标识），`owner_run_id` 指向发起者，`heartbeat_at`
    供超时回收，`acquired_at` 记录加锁时刻。整库同一时刻至多一个独占 run。

    注意：DDL 里的 `INSERT INTO eval_run_guard (id, exclusive_owner) VALUES (1, NULL)`
    是数据初始化，**不在 ORM 层执行**——单行初始化由 Alembic 迁移负责（迁移里在
    `create_table` 后追加该 INSERT）。本模型只定义表结构。
    """

    __tablename__ = "eval_run_guard"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    exclusive_owner: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    owner_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
