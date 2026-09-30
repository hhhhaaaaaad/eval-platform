"""用户与角色模型的 ORM 定义（EP-1 模型组 A）。

`eval_users` 是认证与 RBAC 的根表：
- 登录时按 `username` 查用户、校验 `password_hash`；`username` 唯一约束保证账号不重名。
- `role` 决定接口级权限（admin 可写配置/导入数据，viewer 只读）。
- 保留 `is_active` 而非删除用户，是为了禁用账号时不破坏历史数据的引用完整性
  （`eval_datasets.created_by` / `eval_dataset_versions.created_by` 均指向本表）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """用户表 `eval_users`。

    `CHECK (role IN ('admin','viewer'))` 把角色取值约束下沉到数据库，
    防止应用层漏校验写入非法角色；命名约定会自动补 `ck_eval_users_` 前缀。
    """

    __tablename__ = "eval_users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','viewer')", name="role_valid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="viewer"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
