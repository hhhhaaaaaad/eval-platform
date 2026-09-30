"""add eval_runs.case_limit

**为什么 case_limit 需要一个正式的列**：它原先只被塞在 ``checkpoint`` 这个 JSONB 里。
早期那还算说得过去（它只影响取数范围），但现在它参与 ``config_fingerprint``——
也就是说它决定了 ``eval_user_id``（评测命名空间）、决定了 ``uq_runs_active_cfg``
的分组。而 ``checkpoint`` 在语义上是「跑到哪了」的可变进度标记，把配置放进去意味着
将来任何一次清理或重置 checkpoint 的重构，都会让 case_limit 静默变成 NULL，
**把一个限量 10 条的 run 悄悄变成跑全量**，且不报任何错。

列可空：NULL 表示不限量。既有行全部回填为 NULL，与它们当时的行为一致
（本列引入前 case_limit 从未被真正执行过，见 pipeline 的历史实现）。

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


#: CHECK 约束的**短名**。
#:
#: 传入的名字会被 ``Base.metadata`` 的 naming_convention
#: （``ck_%(table_name)s_%(constraint_name)s``）展开成 ``ck_eval_runs_case_limit_positive``。
#: **create 与 drop 都必须传这个短名**——``op.drop_constraint`` 同样会走命名约定再展开一次，
#: 传展开后的长名会得到 ``ck_eval_runs_ck_eval_runs_case_limit_positive``，
#: PostgreSQL 报「constraint does not exist」，而迁移代码看起来完全正常。
#: 这个坑只在真正执行 downgrade 时才暴露，平时没人跑 downgrade，
#: 往往等到需要回滚的那天才发现——所以本文件被实际跑过一次 down/up 往返才提交。
_CONSTRAINT_SHORT_NAME = "case_limit_positive"


def upgrade() -> None:
    op.add_column(
        "eval_runs",
        sa.Column(
            "case_limit",
            sa.Integer(),
            nullable=True,
            comment="本次 run 最多评多少条 case；NULL=不限量",
        ),
    )
    # CHECK 而不是仅靠 API 层的 gt=0 校验：
    # 0 或负数在语义上不是「不限量」而是调用方算错了。放行会让 run 一条 case 都跑不到，
    # 却因为指纹不同而占住一个看似正常的并发槽位，排查时极难想到是这里。
    # NULL 在 SQL 的 CHECK 里求值为 UNKNOWN，因此不会被这条约束拦下。
    op.create_check_constraint(
        _CONSTRAINT_SHORT_NAME, "eval_runs", "case_limit IS NULL OR case_limit > 0"
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_SHORT_NAME, "eval_runs", type_="check")
    op.drop_column("eval_runs", "case_limit")
