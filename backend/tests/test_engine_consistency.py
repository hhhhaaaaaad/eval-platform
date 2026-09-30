"""维度③一致性巡检的纯单测（EP-9）。

不连网络、不连数据库。重点验证：

1. **条数口径**——同类别内同一 ``memory_id`` 去重、跨类别不去重、MERGE 的
   ``merged_into_id``（幸存者）不计入重复率；
2. **``total_memories == 0`` 不误读成「一致率 100%」**——answerable=False、consistency_rate=0；
3. **比率 clamp**——计数超过总数时率封顶 1.0、原始计数不 clamp；
4. **未知动作忽略但计数**——不计入任何桶、暴露进 ``unknown_action_total``；
5. **空 / 部分 task**——等价于「那些 task 没发现问题」，有记忆无冲突时一致率才是 1.0。
"""

from __future__ import annotations

import json

import pytest

from app.engine.dimensions.consistency import ConsistencyEvaluator, ConsistencyResult
from app.engine.dimensions.governance import (
    ACTION_ARCHIVE,
    ACTION_DISPUTE,
    ACTION_MERGE,
    ACTION_QUARANTINE,
    ReplayedDecision,
)


def _decision(action: str, *memory_ids: int, merged_into_id: int | None = None) -> ReplayedDecision:
    return ReplayedDecision(action=action, memory_ids=tuple(memory_ids), merged_into_id=merged_into_id)


class TestRates:
    def test_all_four_rates_computed(self) -> None:
        """四类动作各出一个率，一致率 = 1 - 冲突率。"""
        decisions = {
            "duplicates": [_decision(ACTION_MERGE, 1, 2)],
            "consistency": [_decision(ACTION_DISPUTE, 3)],
            "expired": [_decision(ACTION_ARCHIVE, 4, 5)],
            "hallucination": [_decision(ACTION_QUARANTINE, 6)],
        }
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.duplicate_total == 2
        assert result.conflict_total == 1
        assert result.expired_residue_total == 2
        assert result.quarantine_total == 1
        assert result.duplicate_rate == pytest.approx(0.2)
        assert result.conflict_rate == pytest.approx(0.1)
        assert result.consistency_rate == pytest.approx(0.9)
        assert result.expired_residue_rate == pytest.approx(0.2)
        assert result.quarantine_rate == pytest.approx(0.1)
        assert result.answerable is True


class TestDedup:
    def test_same_memory_in_two_decisions_counted_once(self) -> None:
        """A 与 B 冲突、A 与 C 冲突 → A 出现两次，同一类别内去重后只数 {1,2,3}。"""
        decisions = {
            "consistency": [_decision(ACTION_DISPUTE, 1, 2), _decision(ACTION_DISPUTE, 1, 3)],
        }
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.conflict_total == 3
        assert result.conflict_rate == pytest.approx(0.3)

    def test_same_memory_across_tasks_counted_in_both(self) -> None:
        """记忆 1 既被 MERGE 又被 DISPUTE：跨类别不去重，两个率各自独立计数。"""
        decisions = {
            "duplicates": [_decision(ACTION_MERGE, 1, 2)],
            "consistency": [_decision(ACTION_DISPUTE, 1)],
        }
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.duplicate_total == 2
        assert result.conflict_total == 1

    def test_merge_survivor_not_counted(self) -> None:
        """MERGE [1,2] 进 3：1、2 是冗余重复，3 是幸存者，不计入重复率。"""
        decisions = {"duplicates": [_decision(ACTION_MERGE, 1, 2, merged_into_id=3)]}
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.duplicate_total == 2
        assert result.duplicate_rate == pytest.approx(0.2)

    def test_empty_memory_ids_contribute_nothing(self) -> None:
        decisions = {"duplicates": [_decision(ACTION_MERGE)]}
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.duplicate_total == 0
        assert result.decision_total == 1


class TestEmptyNamespace:
    def test_zero_total_is_not_100_percent_consistent(self) -> None:
        """空命名空间没有「一致」这回事，consistency_rate 必须 0 而非 1.0。"""
        result = ConsistencyEvaluator().evaluate(total_memories=0, decisions_by_task={})

        assert result.answerable is False
        assert result.consistency_rate == 0.0
        assert result.duplicate_rate == 0.0
        assert result.conflict_rate == 0.0
        assert result.expired_residue_rate == 0.0
        assert result.quarantine_rate == 0.0

    def test_zero_total_with_decisions_keeps_raw_counts(self) -> None:
        """空命名空间却判出动作（矛盾）：率记 0，但原始计数保留供排查。"""
        decisions = {"duplicates": [_decision(ACTION_MERGE, 1)]}
        result = ConsistencyEvaluator().evaluate(total_memories=0, decisions_by_task=decisions)

        assert result.answerable is False
        assert result.duplicate_rate == 0.0
        assert result.duplicate_total == 1

    def test_negative_total_treated_as_unanswerable(self) -> None:
        result = ConsistencyEvaluator().evaluate(total_memories=-1, decisions_by_task={})
        assert result.answerable is False
        assert result.consistency_rate == 0.0


class TestClamp:
    def test_rate_clamped_when_count_exceeds_total(self) -> None:
        """计数超过总数（4 条 > 3 条）：率封顶 1.0，原始计数不 clamp。"""
        decisions = {"duplicates": [_decision(ACTION_MERGE, 1, 2, 3, 4)]}
        result = ConsistencyEvaluator().evaluate(total_memories=3, decisions_by_task=decisions)

        assert result.duplicate_total == 4
        assert result.duplicate_rate == 1.0


class TestUnknownAction:
    def test_unknown_action_ignored_and_counted(self) -> None:
        """未知动作（未来 Java 侧新增）不计入任何桶，但暴露进 unknown_action_total。"""
        decisions = {
            "duplicates": [_decision(ACTION_MERGE, 1), _decision("PROMOTE", 2)],
        }
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.duplicate_total == 1
        assert result.unknown_action_total == 1
        assert result.decision_total == 2


class TestEmptyDecisions:
    def test_empty_decisions_means_no_problems(self) -> None:
        """有记忆、无任何决策 → 无冲突，一致率 1.0（与空命名空间区分开）。"""
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task={})

        assert result.answerable is True
        assert result.duplicate_rate == 0.0
        assert result.conflict_rate == 0.0
        assert result.consistency_rate == 1.0

    def test_partial_tasks_treated_as_missing_zero(self) -> None:
        """只给 duplicates、缺其余 task → 其余 task 等价于「没发现问题」，计数为 0。"""
        decisions = {"duplicates": [_decision(ACTION_MERGE, 1)]}
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)

        assert result.duplicate_total == 1
        assert result.conflict_total == 0
        assert result.expired_residue_total == 0
        assert result.quarantine_total == 0
        assert result.consistency_rate == 1.0


class TestNormalization:
    def test_lowercase_action_normalized(self) -> None:
        decisions = {"duplicates": [_decision("merge", 1)]}
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)
        assert result.duplicate_total == 1


class TestInterface:
    def test_metric_keys_exact(self) -> None:
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task={})
        assert set(result.as_metric_values().keys()) == {
            "duplicate_rate",
            "conflict_rate",
            "consistency_rate",
            "expired_residue_rate",
            "quarantine_rate",
        }

    def test_detail_has_total_and_raw_counts(self) -> None:
        decisions = {"duplicates": [_decision(ACTION_MERGE, 1)]}
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)
        detail = result.as_detail()

        assert detail["total_memories"] == 10
        assert detail["duplicate_total"] == 1
        assert detail["answerable"] is True

    def test_detail_reveals_ratio_denominator(self) -> None:
        """重复率 0.01 可由 total_memories=100 与 duplicate_total=1 还原，而非 1/10 的误读。"""
        result = ConsistencyEvaluator().evaluate(
            total_memories=100,
            decisions_by_task={"duplicates": [_decision(ACTION_MERGE, 1)]},
        )
        assert result.duplicate_rate == pytest.approx(0.01)
        assert result.as_detail()["duplicate_total"] == 1
        assert result.as_detail()["total_memories"] == 100

    def test_detail_is_json_serializable(self) -> None:
        decisions = {"duplicates": [_decision(ACTION_MERGE, 1)]}
        result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task=decisions)
        json.dumps(result.as_detail())  # 不抛即通过


def test_result_type_is_consistency_result() -> None:
    result = ConsistencyEvaluator().evaluate(total_memories=10, decisions_by_task={})
    assert isinstance(result, ConsistencyResult)
