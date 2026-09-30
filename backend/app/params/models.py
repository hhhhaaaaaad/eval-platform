"""参数快照与模型版本模型的 ORM 定义（EP-1 模型组 B）。

本模块包含两张「不可变配置」表：

- `eval_param_snapshots`：一次评测所用的参数快照。`params` 是评测参数原文，
  `freeze_config` 是冻结配置（决定哪些字段参与指纹）。`params_hash` 唯一，
  保证相同参数组合只落一行——这样 run 之间可以复用同一快照，也便于按 hash 去重。
- `eval_model_versions`：embedding / reranker 模型版本组合。`config_hash` 唯一，
  保证相同的 (embedding, reranker, config) 三元组只落一行，让「模型版本」成为
  可复用的稳定引用，而不是每次跑批都新建。

两张表刻意**只读**：写入后不更新，变更即产生新行。这样任何历史 run 通过外键
回指的快照/版本永远保持不变，评测结果可复现。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ParamSnapshot(Base):
    """参数快照表 `eval_param_snapshots`。

    - `params_hash` 唯一约束：同一组参数只存一份，run 通过 `param_snapshot_id`
      引用它。哈希在应用层计算（见 EP-5），DB 只负责强约束唯一，防止并发写入
      出重复快照。
    - `created_by` 指向 `eval_users`，可为空（系统自动生成的快照无创建者）。
    """

    __tablename__ = "eval_param_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    freeze_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    params_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelVersion(Base):
    """模型版本表 `eval_model_versions`。

    - `config_hash` 唯一约束：相同的 (embedding, reranker, config) 组合只存一份，
      让模型版本成为可复用的稳定引用。
    - 本表**不带时间戳**：模型版本是纯配置的不可变标识，何时插入不参与业务语义，
      run 侧自带 `created_at` 足以追溯。
    """

    __tablename__ = "eval_model_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    embedding_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    reranker_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
