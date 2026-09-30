"""维度⑤治理评测的纯单测（EP-9）。

不连网络、不连数据库。重点验证：

1. **内容哈希匹配不依赖 id**——id 会随 reset 变化，按内容匹配才可复现；
2. **「不一致」涵盖动作错与合并目标错**——merged_into_id 指向错的那条也算错；
3. **漏判 / 误伤分向**——负样本（空期望）只推高误伤率、不推高漏判率；
4. **consistency 被显式排除**——标记 evaluable=False，而非静默算 0 分；
5. **schema 契约**——合并目标用显式 ``merged_into_content`` 字段表达，MERGE 缺目标、
   非 MERGE 多写目标都会在导入期校验失败。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.datasets.digest import memory_content_hash
from app.datasets.schemas import GovernanceDecisionExpectation
from app.engine.dimensions.governance import (
    ACTION_ARCHIVE,
    ACTION_MERGE,
    ACTION_QUARANTINE,
    TASK_CONSISTENCY,
    TASK_DUPLICATES,
    TASK_EXPIRED,
    TASK_HALLUCINATION,
    GovernanceCaseResult,
    GovernanceEvaluator,
    ReplayedDecision,
)

CONTENT_A = "用户用 Java 17"
CONTENT_B = "用户用 Java 17（重复）"
CONTENT_C = "用户偏好美式咖啡"
CONTENT_D = "用户在做 Agent 项目"
NOISE = "用户今天心情不错"

#: id → 内容映射（集成方从 seed 的 contentToId 反向得到）。
ID_TO_CONTENT = {1: CONTENT_A, 2: CONTENT_B, 3: CONTENT_C, 4: CONTENT_D}


def _payload(task: str) -> dict:
    return {"task": task}


def _decision(action: str, *memory_ids: int, merged_into_id: int | None = None) -> ReplayedDecision:
    return ReplayedDecision(action=action, memory_ids=tuple(memory_ids), merged_into_id=merged_into_id)


def _expectation(
    action: str, *contents: str, merged_into_content: str | None = None
) -> dict:
    expectation: dict = {"action": action, "memory_contents": list(contents)}
    if merged_into_content is not None:
        expectation["merged_into_content"] = merged_into_content
    return expectation


def _gt(*decisions: dict) -> dict:
    return {"decisions": list(decisions)}


# ---------------------------------------------------------------------------
# 完全匹配 / 动作错 / 合并目标错
# ---------------------------------------------------------------------------


class TestMerge:
    def test_exact_match(self) -> None:
        """合并 [A,B] 进 C，实际也合并 [A,B] 进 C → 完全匹配。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, CONTENT_B, merged_into_content=CONTENT_C)),
            decisions=[_decision(ACTION_MERGE, 1, 2, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )

        assert result.evaluable is True
        assert result.wrong_rate == 0.0
        assert result.matched_decisions == 1
        assert result.missed_decisions == 0
        assert result.false_decisions == 0
        assert result.wrong_target is False
        assert result.missed_action_rate == 0.0
        assert result.false_action_rate == 0.0

    def test_wrong_merge_target_is_wrong(self) -> None:
        """合并目标错（进 D 而非 C）也算错——这是本维度明确要求的「不一致」。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, CONTENT_B, merged_into_content=CONTENT_C)),
            decisions=[_decision(ACTION_MERGE, 1, 2, merged_into_id=4)],
            id_to_content=ID_TO_CONTENT,
        )

        assert result.wrong_rate == 1.0
        assert result.wrong_target is True
        assert result.missed_decisions == 1
        assert result.false_decisions == 1

    def test_wrong_action_is_wrong(self) -> None:
        """hallucination 样本上该隔离却归档 → 动作错。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(_expectation(ACTION_QUARANTINE, CONTENT_A)),
            decisions=[_decision(ACTION_ARCHIVE, 1)],
            id_to_content=ID_TO_CONTENT,
        )

        assert result.wrong_rate == 1.0
        assert result.missed_decisions == 1
        assert result.false_decisions == 1

    def test_partial_merge_is_wrong(self) -> None:
        """该合并两条只合并一条 → 源集合不完整，判不一致。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, CONTENT_B, merged_into_content=CONTENT_C)),
            decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 1.0


class TestQuarantineAndArchive:
    def test_quarantine_exact_match(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(_expectation(ACTION_QUARANTINE, CONTENT_A)),
            decisions=[_decision(ACTION_QUARANTINE, 1)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 0.0

    def test_archive_exact_match(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_EXPIRED),
            ground_truth=_gt(_expectation(ACTION_ARCHIVE, CONTENT_A)),
            decisions=[_decision(ACTION_ARCHIVE, 1)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 0.0


# ---------------------------------------------------------------------------
# 漏判 / 误伤
# ---------------------------------------------------------------------------


class TestMissedAndFalse:
    def test_missed_action_on_positive(self) -> None:
        """该动而没动：期望隔离但实际空 → 漏判 1、误伤 0。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(_expectation(ACTION_QUARANTINE, CONTENT_A)),
            decisions=[],
            id_to_content=ID_TO_CONTENT,
        )

        assert result.wrong_rate == 1.0
        assert result.missed_action_rate == 1.0
        assert result.false_action_rate == 0.0

    def test_false_action_on_negative(self) -> None:
        """负样本上乱动：空期望但实际隔离 → 误伤 1、漏判 0。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(),
            decisions=[_decision(ACTION_QUARANTINE, 1)],
            id_to_content=ID_TO_CONTENT,
        )

        assert result.wrong_rate == 1.0
        assert result.missed_action_rate == 0.0
        assert result.false_action_rate == 1.0

    def test_empty_expectation_and_empty_actual(self) -> None:
        """空期望 vs 空实际 → 全 0，「什么都不做」判正确。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(),
            decisions=[],
            id_to_content=ID_TO_CONTENT,
        )

        assert result.wrong_rate == 0.0
        assert result.missed_action_rate == 0.0
        assert result.false_action_rate == 0.0
        assert result.missed_decisions == 0
        assert result.false_decisions == 0

    def test_partial_false_action_rate(self) -> None:
        """期望 1 条、实际 2 条（1 对 1 误伤）→ 误伤率 = 1/2。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_EXPIRED),
            ground_truth=_gt(_expectation(ACTION_ARCHIVE, CONTENT_A)),
            decisions=[_decision(ACTION_ARCHIVE, 1), _decision(ACTION_ARCHIVE, 4)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.false_action_rate == pytest.approx(0.5)
        assert result.missed_action_rate == 0.0


# ---------------------------------------------------------------------------
# 多决策样本
# ---------------------------------------------------------------------------


class TestMultiDecision:
    def test_multiple_decisions_all_match(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(
                _expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C),
                _expectation(ACTION_MERGE, CONTENT_B, merged_into_content=CONTENT_D),
            ),
            decisions=[
                _decision(ACTION_MERGE, 1, merged_into_id=3),
                _decision(ACTION_MERGE, 2, merged_into_id=4),
            ],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 0.0
        assert result.matched_decisions == 2
        assert result.missed_decisions == 0
        assert result.false_decisions == 0

    def test_multiple_decisions_one_wrong(self) -> None:
        """两条期望只命中一条 → 漏判 1、误伤 1，误伤率按实际决策数归一。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(
                _expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C),
                _expectation(ACTION_MERGE, CONTENT_B, merged_into_content=CONTENT_D),
            ),
            decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 1.0
        assert result.matched_decisions == 1
        assert result.missed_decisions == 1
        assert result.false_decisions == 0
        assert result.missed_action_rate == pytest.approx(0.5)
        assert result.false_action_rate == 0.0


# ---------------------------------------------------------------------------
# 内容哈希匹配不依赖 id（可复现性）
# ---------------------------------------------------------------------------


class TestReproducibility:
    def test_matching_does_not_depend_on_ids(self) -> None:
        """reset 后 id 全变，只要内容一致，指标必须完全一致。"""
        ground_truth = _gt(_expectation(ACTION_MERGE, CONTENT_A, CONTENT_B, merged_into_content=CONTENT_C))

        first = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=ground_truth,
            decisions=[_decision(ACTION_MERGE, 1, 2, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )
        # reset 后重 seed，id 全变
        second = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=ground_truth,
            decisions=[_decision(ACTION_MERGE, 901, 902, merged_into_id=903)],
            id_to_content={901: CONTENT_A, 902: CONTENT_B, 903: CONTENT_C},
        )

        assert first.wrong_rate == second.wrong_rate
        assert first.missed_action_rate == second.missed_action_rate
        assert first.false_action_rate == second.false_action_rate

    def test_unresolvable_id_is_never_matched(self) -> None:
        """id 不在映射里 → 退回 id token，与任何内容哈希都不匹配，判错而非假装对。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(_expectation(ACTION_QUARANTINE, CONTENT_A)),
            decisions=[_decision(ACTION_QUARANTINE, 999)],  # 999 无内容
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 1.0
        assert result.missed_decisions == 1
        assert result.false_decisions == 1

    def test_expected_side_ignores_memory_ids(self) -> None:
        """期望同时给 memory_contents 与 memory_ids 时只用内容——id 不可复现。"""
        ground_truth = {
            "decisions": [
                {
                    "action": "MERGE",
                    "memory_contents": [CONTENT_A],
                    "merged_into_content": CONTENT_C,
                    "memory_ids": [1, 3],
                }
            ]
        }
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=ground_truth,
            decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 0.0
        assert result.matched_decisions == 1


# ---------------------------------------------------------------------------
# consistency 与未知任务
# ---------------------------------------------------------------------------


class TestConsistencyExcluded:
    def test_consistency_is_marked_not_evaluable(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_CONSISTENCY),
            ground_truth=_gt(_expectation(ACTION_QUARANTINE, CONTENT_A)),
            decisions=[_decision(ACTION_QUARANTINE, 1)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.evaluable is False
        assert result.wrong_rate == 0.0

    def test_consistency_excluded_from_aggregate(self) -> None:
        evaluator = GovernanceEvaluator()
        dup = evaluator.evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C)),
            decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )
        consistency = evaluator.evaluate_case(
            payload=_payload(TASK_CONSISTENCY),
            ground_truth=_gt(_expectation(ACTION_QUARANTINE, CONTENT_A)),
            decisions=[_decision(ACTION_QUARANTINE, 1)],
            id_to_content=ID_TO_CONTENT,
        )

        aggregated = evaluator.aggregate([dup, consistency])
        assert aggregated["case_count"] == 1.0
        assert aggregated["duplicates_count"] == 1.0
        # consistency 不影响任何指标
        assert aggregated["wrong_merge_rate"] == pytest.approx(0.0)

    def test_unknown_task_is_not_evaluable(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload("some_future_task"),
            ground_truth=_gt(_expectation(ACTION_ARCHIVE, CONTENT_A)),
            decisions=[_decision(ACTION_ARCHIVE, 1)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.evaluable is False


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_per_task_rates_and_counts(self) -> None:
        evaluator = GovernanceEvaluator()
        results = [
            # duplicates: 1 对 1 错
            evaluator.evaluate_case(
                payload=_payload(TASK_DUPLICATES),
                ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C)),
                decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
                id_to_content=ID_TO_CONTENT,
            ),
            evaluator.evaluate_case(
                payload=_payload(TASK_DUPLICATES),
                ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_B, merged_into_content=CONTENT_D)),
                decisions=[_decision(ACTION_MERGE, 2, merged_into_id=4)],
                id_to_content=ID_TO_CONTENT,
            ),
            # hallucination: 误伤（负样本乱动）
            evaluator.evaluate_case(
                payload=_payload(TASK_HALLUCINATION),
                ground_truth=_gt(),
                decisions=[_decision(ACTION_QUARANTINE, 1)],
                id_to_content=ID_TO_CONTENT,
            ),
            # expired: 漏判（该归档没归档）
            evaluator.evaluate_case(
                payload=_payload(TASK_EXPIRED),
                ground_truth=_gt(_expectation(ACTION_ARCHIVE, CONTENT_A)),
                decisions=[],
                id_to_content=ID_TO_CONTENT,
            ),
        ]

        aggregated = evaluator.aggregate(results)

        assert aggregated["case_count"] == 4.0
        assert aggregated["duplicates_count"] == 2.0
        assert aggregated["expired_count"] == 1.0
        assert aggregated["hallucination_count"] == 1.0
        # duplicates 两条都匹配 → 误合并率 0
        assert aggregated["wrong_merge_rate"] == pytest.approx(0.0)
        # hallucination 那一条误伤 → 误隔离率 1.0
        assert aggregated["wrong_quarantine_rate"] == pytest.approx(1.0)
        # expired 那一条漏判 → 误归档率 1.0
        assert aggregated["wrong_archive_rate"] == pytest.approx(1.0)
        # 漏判/误伤在全部 4 条可评测 case 上平均：漏判 1 条（expired），误伤 1 条（hallucination）
        assert aggregated["missed_action_rate"] == pytest.approx(0.25)
        assert aggregated["false_action_rate"] == pytest.approx(0.25)

    def test_aggregate_empty(self) -> None:
        aggregated = GovernanceEvaluator().aggregate([])
        assert aggregated["case_count"] == 0.0
        assert aggregated["wrong_merge_rate"] == 0.0
        assert aggregated["wrong_quarantine_rate"] == 0.0
        assert aggregated["wrong_archive_rate"] == 0.0

    def test_aggregate_all_non_evaluable(self) -> None:
        evaluator = GovernanceEvaluator()
        consistency = evaluator.evaluate_case(
            payload=_payload(TASK_CONSISTENCY),
            ground_truth=_gt(),
            decisions=[],
            id_to_content=ID_TO_CONTENT,
        )
        assert evaluator.aggregate([consistency])["case_count"] == 0.0


# ---------------------------------------------------------------------------
# 明细与适配
# ---------------------------------------------------------------------------


class TestDetailAndAdapter:
    def test_detail_records_mismatch_cause(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C)),
            decisions=[_decision(ACTION_MERGE, 1, merged_into_id=4)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_target is True
        assert result.expected_target_hashes == [memory_content_hash(CONTENT_C)]
        assert result.actual_target_hashes == [memory_content_hash(CONTENT_D)]

    def test_detail_is_json_serializable(self) -> None:
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_DUPLICATES),
            ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C)),
            decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
            id_to_content=ID_TO_CONTENT,
        )
        json.dumps(result.as_detail())  # 不抛即通过

    def test_from_connector_decision(self) -> None:
        class _FakeItem:
            memory_id = 7

        class _FakeDecision:
            action = "MERGE"
            merged_into_id = 8
            items = (_FakeItem(),)

        decision = ReplayedDecision.from_connector_decision(_FakeDecision())
        assert decision.action == "MERGE"
        assert decision.memory_ids == (7,)
        assert decision.merged_into_id == 8

    def test_action_normalization(self) -> None:
        """标注用小写动作也能与 Java 的大写动作对齐。"""
        result = GovernanceEvaluator().evaluate_case(
            payload=_payload(TASK_HALLUCINATION),
            ground_truth=_gt(_expectation("quarantine", CONTENT_A)),
            decisions=[_decision(ACTION_QUARANTINE, 1)],
            id_to_content=ID_TO_CONTENT,
        )
        assert result.wrong_rate == 0.0


# ---------------------------------------------------------------------------
# schema 契约
# ---------------------------------------------------------------------------


class TestSchemaContract:
    def test_merge_requires_merged_into_content(self) -> None:
        """MERGE 缺合并目标必须在导入期失败——否则「合并到哪」无从判断，指标静默失真。"""
        with pytest.raises(ValidationError):
            GovernanceDecisionExpectation.model_validate(
                {"action": "MERGE", "memory_contents": [CONTENT_A, CONTENT_B]}
            )

    def test_non_merge_rejects_merged_into_content(self) -> None:
        """非 MERGE 给合并目标也必须失败——多半是复制粘贴时漏改 action，语义不明。"""
        with pytest.raises(ValidationError):
            GovernanceDecisionExpectation.model_validate(
                {
                    "action": "ARCHIVE",
                    "memory_contents": [CONTENT_A],
                    "merged_into_content": CONTENT_C,
                }
            )


def test_result_type_is_governance_case_result() -> None:
    result = GovernanceEvaluator().evaluate_case(
        payload=_payload(TASK_DUPLICATES),
        ground_truth=_gt(_expectation(ACTION_MERGE, CONTENT_A, merged_into_content=CONTENT_C)),
        decisions=[_decision(ACTION_MERGE, 1, merged_into_id=3)],
        id_to_content=ID_TO_CONTENT,
    )
    assert isinstance(result, GovernanceCaseResult)
