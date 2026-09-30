"""审计写入服务（EP-2）。

`eval_audit_log` 是 **append-only** 表：本模块只提供 INSERT 语义的写入接口，
**不提供任何修改（UPDATE）或删除（DELETE）审计记录的方法**。审计轨迹一旦落库
即不可变，配合服务端回收 UPDATE / DELETE 权限，保证记录不可篡改。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.audit.models import AuditLog


def write_audit(
    session: Session,
    *,
    actor_user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | int | uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """追加一条审计记录并 flush。

    **不 commit**：只做 ``session.add(...)`` + ``session.flush()``，事务边界交由
    调用方决定。审计必须与业务写操作处于**同一事务**内——若此处自行 commit，一旦
    业务后续回滚，审计却已落库，会产生与业务状态不符的「孤儿审计」；反之在同一
    事务内，业务回滚会连带撤销审计，两者保持一致。flush 用于触发数据库约束校验
    并拿到自增 ``id``，但不结束事务。

    ``resource_id`` 接受 ``str | int | uuid.UUID``，内部统一 ``str()`` 化为 TEXT
    存储；调用方无需自行转换。

    ``before`` / ``after`` 为 ``None`` 时，数据库列 ``before`` / ``after`` 保持
    NULL（不臆造空 ``{}``），以区分「无快照」与「空快照」。

    :param session: 调用方的事务会话，本函数不提交也不关闭它。
    :param actor_user_id: 操作用户 id；``None`` 表示系统 / 匿名动作。
    :param action: 动作标识，如 ``"login"`` / ``"dataset.create"`` / ``"run.create"``。
    :param resource_type: 资源类型，如 ``"dataset"`` / ``"run"`` / ``"param_snapshot"``。
    :param resource_id: 资源标识，内部 ``str()`` 化后写入 TEXT 列。
    :param before: 变更前快照；``None`` 表示无。
    :param after: 变更后快照；``None`` 表示无。
    :param ip: 来源地址；``None`` 表示未知。
    :returns: 被 ``session.add`` 的 ``AuditLog`` 实例（flush 后已带 ``id``）。
    """
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        # None 即 NULL：显式传入而非构造 {}，保留「无快照」语义
        before_state=before,
        after_state=after,
        ip=ip,
    )
    session.add(entry)
    session.flush()
    return entry
