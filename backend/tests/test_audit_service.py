"""审计写入服务单元测试（EP-2）。

本机无 Postgres，故用 ``MagicMock`` 模拟 ``Session`` 做**纯单元测试**：只验证
``write_audit`` 与 Session 的交互契约（``add`` / ``flush`` 且**不** ``commit``）
以及落到 ORM 对象上的字段值。不触碰真实数据库，也不验证 JSONB 实际落库。
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.audit.models import AuditLog
from app.audit.service import write_audit


@pytest.fixture
def mock_session() -> MagicMock:
    """返回一个可断言的假 Session（add / flush / commit 均被记录）。"""
    return MagicMock()


def _call(mock_session: MagicMock) -> AuditLog:
    """以一组典型入参调用 write_audit，返回其返回值。"""
    return write_audit(
        mock_session,
        actor_user_id=42,
        action="dataset.create",
        resource_type="dataset",
        resource_id="ds-1",
        before={"name": "old"},
        after={"name": "new"},
        ip="10.0.0.1",
    )


def test_add_called_once_with_auditlog_instance(mock_session: MagicMock) -> None:
    """1. session.add 恰好被调用一次，且传入的是 AuditLog 实例。"""
    _call(mock_session)
    mock_session.add.assert_called_once()
    added = mock_session.add.call_args.args[0]
    assert isinstance(added, AuditLog)


def test_fields_mapped_to_model(mock_session: MagicMock) -> None:
    """2. action / resource_type / resource_id / actor_user_id / ip 字段正确。"""
    entry = _call(mock_session)
    assert entry.action == "dataset.create"
    assert entry.resource_type == "dataset"
    assert entry.resource_id == "ds-1"
    assert entry.actor_user_id == 42
    assert entry.ip == "10.0.0.1"


def test_before_after_dict_land_on_state_attrs(mock_session: MagicMock) -> None:
    """3. before / after 传入 dict 时落到 before_state / after_state 属性。"""
    entry = _call(mock_session)
    assert entry.before_state == {"name": "old"}
    assert entry.after_state == {"name": "new"}


def test_none_snapshots_stay_none(mock_session: MagicMock) -> None:
    """4. before / after 为 None 时不臆造空 dict，保持 None。"""
    entry = write_audit(
        mock_session,
        actor_user_id=1,
        action="run.create",
        resource_type="run",
        resource_id="run-1",
    )
    assert entry.before_state is None
    assert entry.after_state is None


def test_does_not_commit(mock_session: MagicMock) -> None:
    """5. write_audit 不调用 session.commit（事务边界交给调用方）。"""
    _call(mock_session)
    mock_session.commit.assert_not_called()


def test_flush_called(mock_session: MagicMock) -> None:
    """6. session.flush 被调用（触发约束校验 / 拿自增 id）。"""
    _call(mock_session)
    mock_session.flush.assert_called_once()


def test_system_action_with_none_actor(mock_session: MagicMock) -> None:
    """7. actor_user_id=None（系统动作）正常写入且不报错。"""
    entry = write_audit(
        mock_session,
        actor_user_id=None,
        action="system.sweep",
        resource_type="audit",
        resource_id="sys",
    )
    assert entry.actor_user_id is None
    mock_session.add.assert_called_once()


def test_return_value_is_added_instance(mock_session: MagicMock) -> None:
    """8. 返回值就是被 session.add 的那个 AuditLog 对象。"""
    entry = _call(mock_session)
    assert mock_session.add.call_args.args[0] is entry


def test_resource_id_coerced_to_str(mock_session: MagicMock) -> None:
    """9. resource_id 传入 int / UUID 时内部统一 str() 化为 TEXT。"""
    entry_int = write_audit(
        mock_session,
        actor_user_id=1,
        action="run.create",
        resource_type="run",
        resource_id=123,
    )
    assert entry_int.resource_id == "123"

    uid = uuid.uuid4()
    entry_uuid = write_audit(
        mock_session,
        actor_user_id=1,
        action="run.create",
        resource_type="run",
        resource_id=uid,
    )
    assert entry_uuid.resource_id == str(uid)
