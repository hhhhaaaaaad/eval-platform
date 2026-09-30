"""initial schema (EP-1)

按《记忆系统独立评测平台方案》§5 建立全部 14 张表。

要点：
- 状态列用 TEXT + CHECK（不建 PG ENUM，便于后续加状态而无需 ALTER TYPE）
- JSON 用 jsonb，时间用 timestamptz，主键 BIGSERIAL / UUID(gen_random_uuid())
- 三个 partial unique index（uq_runs_active_cfg / uq_runs_active_user /
  uq_runs_idempotency）是并发约束的核心：只对「进行中的 run」加唯一性，
  终态 run 不占用槽位
- eval_run_guard 是单行互斥表，迁移里完成单行初始化

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- 5.1 用户与权限 ----------
    op.create_table(
        "eval_users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="viewer", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_users"),
        sa.UniqueConstraint("username", name="uq_eval_users_username"),
        sa.CheckConstraint("role IN ('admin','viewer')", name="role_valid"),
    )

    # ---------- 5.2 评测集版本化 ----------
    op.create_table(
        "eval_datasets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_datasets"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["eval_users.id"], name="fk_eval_datasets_created_by"
        ),
    )

    op.create_table(
        "eval_dataset_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("parent_version_id", sa.BigInteger(), nullable=True),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_dataset_versions"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["eval_datasets.id"],
            name="fk_eval_dataset_versions_dataset_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["eval_dataset_versions.id"],
            name="fk_eval_dataset_versions_parent_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["eval_users.id"],
            name="fk_eval_dataset_versions_created_by",
        ),
        sa.UniqueConstraint("dataset_id", "version", name="uq_eval_dataset_versions_dataset_id_version"),
    )

    op.create_table(
        "eval_cases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_version_id", sa.BigInteger(), nullable=False),
        sa.Column("case_type", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ground_truth", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_cases"),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["eval_dataset_versions.id"],
            name="fk_eval_cases_dataset_version_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "dataset_version_id", "content_hash", name="uq_eval_cases_dataset_version_id_content_hash"
        ),
    )

    # ---------- 5.3 参数快照 + 模型版本 ----------
    op.create_table(
        "eval_param_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("freeze_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("params_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_param_snapshots"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["eval_users.id"], name="fk_eval_param_snapshots_created_by"
        ),
        sa.UniqueConstraint("params_hash", name="uq_eval_param_snapshots_params_hash"),
    )

    op.create_table(
        "eval_model_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("embedding_model_id", sa.Text(), nullable=False),
        sa.Column("reranker_model_id", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_model_versions"),
        sa.UniqueConstraint("config_hash", name="uq_eval_model_versions_config_hash"),
    )

    # ---------- 5.4 experiment / run / guard ----------
    op.create_table(
        "eval_experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_experiments"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["eval_users.id"], name="fk_eval_experiments_created_by"
        ),
    )

    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_version_id", sa.BigInteger(), nullable=False),
        sa.Column("param_snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("model_version_id", sa.BigInteger(), nullable=False),
        sa.Column("eval_user_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), server_default="exact", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("exclusive", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("current_stage", sa.Text(), nullable=True),
        sa.Column("progress", sa.Numeric(5, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{\"completed_stages\":[]}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_runs"),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["eval_experiments.id"], name="fk_eval_runs_experiment_id"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["eval_dataset_versions.id"],
            name="fk_eval_runs_dataset_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["param_snapshot_id"],
            ["eval_param_snapshots.id"],
            name="fk_eval_runs_param_snapshot_id",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["eval_model_versions.id"],
            name="fk_eval_runs_model_version_id",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["eval_users.id"], name="fk_eval_runs_created_by"),
        sa.CheckConstraint("mode IN ('exact','hnsw')", name="mode_valid"),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')",
            name="status_valid",
        ),
    )

    # 普通索引
    op.create_index("idx_runs_status", "eval_runs", ["status"])
    op.create_index("idx_runs_cfg", "eval_runs", ["config_fingerprint"])
    op.create_index("idx_runs_created", "eval_runs", [sa.text("created_at DESC")])
    op.create_index(
        "idx_runs_heartbeat",
        "eval_runs",
        ["heartbeat_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "idx_runs_pending",
        "eval_runs",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # 三个 partial unique index —— 并发约束的核心
    op.create_index(
        "uq_runs_idempotency",
        "eval_runs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_runs_active_cfg",
        "eval_runs",
        ["config_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    op.create_index(
        "uq_runs_active_user",
        "eval_runs",
        ["eval_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )

    op.create_table(
        "eval_run_guard",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exclusive_owner", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_eval_run_guard"),
        sa.CheckConstraint("id = 1", name="single_row"),
    )
    # 单行初始化：exclusive run 的全局互斥占位（D-6）
    op.execute("INSERT INTO eval_run_guard (id, exclusive_owner) VALUES (1, NULL)")

    # ---------- 5.5 结果 ----------
    op.create_table(
        "eval_run_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("metric_value", sa.Numeric(), nullable=False),
        sa.Column("variance", sa.Numeric(), nullable=True),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_run_results"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["eval_runs.id"],
            name="fk_eval_run_results_run_id",
            ondelete="RESTRICT",
        ),
        # 短名有意为之：这是结果 upsert 的 ON CONFLICT 冲突目标。
        # 必须与 app/results/models.py 逐字一致，否则 alembic check 会报漂移。
        sa.UniqueConstraint("run_id", "dimension", "metric_name", name="uq_run_dimension_metric"),
    )

    op.create_table(
        "eval_case_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("metric_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_case_results"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["eval_runs.id"],
            name="fk_eval_case_results_run_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["eval_cases.id"], name="fk_eval_case_results_case_id"
        ),
        # 同 uq_run_dimension_metric：与 app/results/models.py 保持一致
        sa.UniqueConstraint("run_id", "case_id", "dimension", name="uq_run_case_dimension"),
    )
    op.create_index("idx_cr_run", "eval_case_results", ["run_id"])

    # ---------- 5.6 反哺闭环 ----------
    op.create_table(
        "eval_feedback_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("case_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ground_truth", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("archived_dataset_version_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_eval_feedback_samples"),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["eval_users.id"], name="fk_eval_feedback_samples_reviewed_by"
        ),
        sa.ForeignKeyConstraint(
            ["archived_dataset_version_id"],
            ["eval_dataset_versions.id"],
            name="fk_eval_feedback_samples_archived_dataset_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["eval_users.id"], name="fk_eval_feedback_samples_created_by"
        ),
        sa.CheckConstraint(
            "status IN ('new','reviewing','approved','rejected','archived')",
            name="status_valid",
        ),
    )

    # ---------- 5.7 LLM-judge ----------
    op.create_table(
        "eval_judge_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pair_key", sa.Text(), nullable=False),
        sa.Column("prompt_type", sa.Text(), nullable=False),
        sa.Column("judge_model", sa.Text(), nullable=False),
        sa.Column("input_a", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_b", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("position_swap", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_eval_judge_jobs"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["eval_runs.id"],
            name="fk_eval_judge_jobs_run_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="status_valid",
        ),
    )

    # ---------- 5.8 审计（append-only）----------
    op.create_table(
        "eval_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_audit_log"),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["eval_users.id"], name="fk_eval_audit_log_actor_user_id"
        ),
    )


def downgrade() -> None:
    # 逆依赖顺序删除；guard 的初始化数据随表一起消失，无需单独 DELETE
    op.drop_table("eval_audit_log")
    op.drop_table("eval_judge_jobs")
    op.drop_table("eval_feedback_samples")
    op.drop_index("idx_cr_run", table_name="eval_case_results")
    op.drop_table("eval_case_results")
    op.drop_table("eval_run_results")

    op.drop_table("eval_run_guard")

    op.drop_index("uq_runs_active_user", table_name="eval_runs")
    op.drop_index("uq_runs_active_cfg", table_name="eval_runs")
    op.drop_index("uq_runs_idempotency", table_name="eval_runs")
    op.drop_index("idx_runs_pending", table_name="eval_runs")
    op.drop_index("idx_runs_heartbeat", table_name="eval_runs")
    op.drop_index("idx_runs_created", table_name="eval_runs")
    op.drop_index("idx_runs_cfg", table_name="eval_runs")
    op.drop_index("idx_runs_status", table_name="eval_runs")
    op.drop_table("eval_runs")

    op.drop_table("eval_experiments")
    op.drop_table("eval_model_versions")
    op.drop_table("eval_param_snapshots")
    op.drop_table("eval_cases")
    op.drop_table("eval_dataset_versions")
    op.drop_table("eval_datasets")
    op.drop_table("eval_users")
