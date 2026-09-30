"""审计日志模型的 ORM 定义（EP-1 模型组 C）。

`eval_audit_log` 记录敏感写操作的审计轨迹，字段 `before` / `after` 保存变更
前后的资源快照。

**append-only**：本表只 INSERT，不 UPDATE / DELETE，由应用层与服务端权限
（回收 UPDATE/DELETE 权限）共同保证，确保审计记录不可篡改。

**列名改名说明**：`before` 与 `after` 是 SQLAlchemy `Base` 的保留属性名，直接
作属性会与 ORM 内部机制冲突。故属性名改用 `before_state` / `after_state`，
同时通过 `mapped_column("before", ...)` 显式指定**数据库列名仍为 `before` / `after`**，
保持与 DDL 完全一致。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """审计日志 `eval_audit_log`（append-only）。

    `actor_user_id` 指向操作用户（可空，允许系统/匿名操作）；
    `resource_type` + `resource_id` 定位被操作资源，`resource_id` 用 TEXT
    以兼容不同主键类型（BIGINT / UUID）。`ip` 记录来源地址。
    """

    __tablename__ = "eval_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("eval_users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 属性名 before_state / after_state，数据库列名仍为 before / after
    before_state: Mapped[dict[str, Any] | None] = mapped_column(
        "before", JSONB, nullable=True
    )
    after_state: Mapped[dict[str, Any] | None] = mapped_column(
        "after", JSONB, nullable=True
    )
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
