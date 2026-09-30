"""维度④注入评测的纯单测（EP-9）。

重点验证四件事：

1. 超预算判定是**严格大于**（``token_count > inject_max_tokens``）——恰好等于预算不算超；
2. ``irrelevant_injection_rate`` 的分母是**实际注入的数量**（去重后），不是 K；
3. 相关判定走 **contentHash 匹配**，注入内容去重按 content 哈希、不依赖 id；
4. 无法评测的 case（无 ground truth）被标记 ``answerable=False`` 并在聚合时排除。

全部测试**不需要网络、不需要数据库**：evaluator 收的是已解析好的内容列表与 token 数，
保持纯函数，因而可以穷举每一种边界。
"""

from __future__ import annotations

import pytest

from app.datasets.digest import memory_content_hash
from app.engine.dimensions.injection import InjectionEvaluator

CONTENT_A = "用户用 Java 17"
CONTENT_B = "用户偏好美式咖啡"
CONTENT_C = "用户在做 Agent 项目"
NOISE = "用户今天心情不错"


def _payload(query_id: str = "q1") -> dict:
    return {"query_id": query_id, "query": "用户用什么技术栈？", "task_type": "LEGACY"}


def _gt_by_content(*contents: str) -> dict:
    return {"relevant_memory_contents": list(contents)}


# ---------------------------------------------------------------------------
# 预算：超预算 / 利用率
# ---------------------------------------------------------------------------


class TestTokenBudget:
    def test_over_budget_when_strictly_greater(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=1001,
            injected_contents=[CONTENT_A],
        )
        assert result.over_budget is True
        assert result.token_utilization == pytest.approx(1001 / 1000)

    def test_exactly_equal_budget_is_not_over(self) -> None:
        """**边界**：恰好等于预算说明「正好用完、没超」，不算超预算。"""
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=1000,
            injected_contents=[CONTENT_A],
        )
        assert result.over_budget is False
        assert result.token_utilization == pytest.approx(1.0)

    def test_under_budget_not_over(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=500,
            injected_contents=[CONTENT_A],
        )
        assert result.over_budget is False
        assert result.token_utilization == pytest.approx(0.5)

    def test_inject_max_tokens_recorded_in_result(self) -> None:
        """预算必须记进结果：不同预算下的超预算率/利用率不可直接比较。"""
        result = InjectionEvaluator(inject_max_tokens=2048).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=100,
            injected_contents=[CONTENT_A],
        )
        assert result.inject_max_tokens == 2048
        assert result.as_detail()["inject_max_tokens"] == 2048

    def test_invalid_budget_rejected(self) -> None:
        with pytest.raises(ValueError):
            InjectionEvaluator(inject_max_tokens=0)
        with pytest.raises(ValueError):
            InjectionEvaluator(inject_max_tokens=-1)


# ---------------------------------------------------------------------------
# 无关注入率：匹配与分母口径
# ---------------------------------------------------------------------------


class TestIrrelevantInjection:
    def test_all_relevant_is_zero_rate(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A, CONTENT_B),
            token_count=200,
            injected_contents=[CONTENT_A, CONTENT_B],
        )
        assert result.irrelevant_injection_rate == 0.0
        assert result.injected_total == 2
        assert result.irrelevant_total == 0

    def test_mixed_injection(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A, NOISE],
        )
        assert result.irrelevant_injection_rate == pytest.approx(0.5)

    def test_denominator_is_injected_count_not_k(self) -> None:
        """**关键口径**：分母是「实际注入的数量」，不是 K。

        检索阶段 K=5 只注入了 2 条（1 相关 1 无关），无关注入率 = 1/2 = 0.5，
        而不是 1/5 = 0.2——K 是检索预算而非注入预算，「没注满」由 token_utilization
        单独暴露。
        """
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A, NOISE],
        )
        assert result.injected_total == 2
        assert result.irrelevant_injection_rate == pytest.approx(0.5)

    def test_all_irrelevant_is_one(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[NOISE, NOISE + "2"],
        )
        assert result.irrelevant_injection_rate == 1.0

    def test_matches_by_content_hash(self) -> None:
        """相关判定走 content 哈希：注入内容与相关记忆逐字一致即算相关。

        evaluator 收的是内容列表、没有 id，因此匹配天然与 id 无关——reset 后重新
        seed 拿到不同 id，只要内容一致，指标就一致。
        """
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        assert memory_content_hash(CONTENT_A) in result.injected_hashes
        assert result.irrelevant_hashes == []

    def test_blank_contents_ignored_in_ground_truth(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth={"relevant_memory_contents": [CONTENT_A, "   ", ""]},
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        assert result.irrelevant_injection_rate == 0.0


# ---------------------------------------------------------------------------
# 空注入
# ---------------------------------------------------------------------------


class TestEmptyInjection:
    def test_empty_injection_is_zero_rate_and_answerable(self) -> None:
        """空注入是可观测的系统行为，不是数据问题：answerable=True、无关率记 0。"""
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=0,
            injected_contents=[],
        )
        assert result.answerable is True
        assert result.injected_total == 0
        assert result.irrelevant_injection_rate == 0.0

    def test_empty_injection_has_zero_utilization(self) -> None:
        """「没注入无关内容」成立的同时，「预算一条没用上」要能被 token_utilization 看见。"""
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=0,
            injected_contents=[],
        )
        assert result.token_utilization == 0.0
        assert result.over_budget is False


# ---------------------------------------------------------------------------
# 重复内容去重
# ---------------------------------------------------------------------------


class TestDuplicateContent:
    def test_duplicate_content_deduped(self) -> None:
        """同一记忆被注入两次时去重：分母是 2 不是 3，无关记忆只数一次。"""
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=300,
            injected_contents=[CONTENT_A, CONTENT_A, NOISE],
        )
        assert result.injected_total == 2
        assert result.irrelevant_total == 1
        assert result.irrelevant_injection_rate == pytest.approx(0.5)

    def test_duplicate_relevant_not_double_counted(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A, CONTENT_A],
        )
        assert result.injected_total == 1
        assert result.irrelevant_injection_rate == 0.0


# ---------------------------------------------------------------------------
# 明细：可追溯到 case
# ---------------------------------------------------------------------------


class TestCaseDetail:
    def test_records_injected_relevant_and_irrelevant(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A, CONTENT_B),
            token_count=200,
            injected_contents=[CONTENT_A, NOISE],
        )
        assert result.injected_hashes == [
            memory_content_hash(CONTENT_A),
            memory_content_hash(NOISE),
        ]
        assert result.relevant_hashes == sorted(
            [memory_content_hash(CONTENT_A), memory_content_hash(CONTENT_B)]
        )
        assert result.irrelevant_hashes == [memory_content_hash(NOISE)]

    def test_detail_is_json_serializable(self) -> None:
        import json

        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        json.dumps(result.as_detail())  # 不抛即通过

    def test_query_id_is_carried(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload("q-042"),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        assert result.query_id == "q-042"


# ---------------------------------------------------------------------------
# 不可评测的 case
# ---------------------------------------------------------------------------


class TestUnanswerable:
    def test_no_ground_truth_is_marked_not_scored(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth={},
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        assert result.answerable is False

    def test_empty_relevant_list_is_unanswerable(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth={"relevant_memory_contents": []},
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        assert result.answerable is False

    def test_blank_relevant_list_is_unanswerable(self) -> None:
        result = InjectionEvaluator(inject_max_tokens=1000).evaluate_case(
            payload=_payload(),
            ground_truth={"relevant_memory_contents": ["   ", ""]},
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        assert result.answerable is False

    def test_irrelevant_rate_excludes_unanswerable(self) -> None:
        """相关性指标只覆盖有标注的 case：缺标注的 case 不计入其分母。"""
        evaluator = InjectionEvaluator(inject_max_tokens=1000)
        good = evaluator.evaluate_case(
            payload=_payload("q1"),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=200,
            injected_contents=[CONTENT_A],
        )
        bad = evaluator.evaluate_case(
            payload=_payload("q2"),
            ground_truth={},
            token_count=200,
            injected_contents=[NOISE],
        )

        aggregated = evaluator.aggregate([good, bad])

        assert aggregated["case_count_total"] == 2.0
        assert aggregated["case_count_scored"] == 1.0
        assert aggregated["irrelevant_injection_rate"] == pytest.approx(0.0)

    def test_budget_metrics_cover_unanswerable_cases(self) -> None:
        """**本次改动的核心**：预算指标不依赖 ground truth，缺标注的 case 也计入。

        一个所有 case 都缺标注、且其中一半超预算的 run，超预算率必须是 0.5 而不是 0——
        否则「没数据」和「真没超预算」无法区分。
        """
        evaluator = InjectionEvaluator(inject_max_tokens=1000)
        over = evaluator.evaluate_case(
            payload=_payload("q1"),
            ground_truth={},
            token_count=2000,
            injected_contents=[NOISE],
        )
        under = evaluator.evaluate_case(
            payload=_payload("q2"),
            ground_truth={},
            token_count=500,
            injected_contents=[NOISE],
        )

        aggregated = evaluator.aggregate([over, under])

        assert aggregated["case_count_total"] == 2.0
        assert aggregated["case_count_scored"] == 0.0
        assert aggregated["over_budget_rate"] == pytest.approx(0.5)
        assert aggregated["token_utilization"] == pytest.approx((2.0 + 0.5) / 2)
        assert aggregated["irrelevant_injection_rate"] == 0.0

    def test_aggregate_all_unanswerable_keeps_budget_metrics(self) -> None:
        """全部 case 都缺标注时，预算指标仍照常计算，只有相关性指标为 0。"""
        evaluator = InjectionEvaluator(inject_max_tokens=1000)
        over = evaluator.evaluate_case(
            payload=_payload(), ground_truth={}, token_count=1500, injected_contents=[NOISE]
        )
        aggregated = evaluator.aggregate([over])

        assert aggregated["case_count_total"] == 1.0
        assert aggregated["case_count_scored"] == 0.0
        assert aggregated["over_budget_rate"] == 1.0
        assert aggregated["token_utilization"] == pytest.approx(1.5)
        assert aggregated["irrelevant_injection_rate"] == 0.0

    def test_aggregate_empty(self) -> None:
        aggregated = InjectionEvaluator(inject_max_tokens=1000).aggregate([])
        assert aggregated["case_count_total"] == 0.0
        assert aggregated["case_count_scored"] == 0.0


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_averages_across_cases(self) -> None:
        evaluator = InjectionEvaluator(inject_max_tokens=1000)

        over_budget_case = evaluator.evaluate_case(
            payload=_payload("q1"),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=2000,  # 超预算，利用率 2.0
            injected_contents=[CONTENT_A],  # 无关率 0
        )
        under_budget_noisy = evaluator.evaluate_case(
            payload=_payload("q2"),
            ground_truth=_gt_by_content(CONTENT_A),
            token_count=500,  # 不超预算，利用率 0.5
            injected_contents=[NOISE],  # 无关率 1.0
        )

        aggregated = evaluator.aggregate([over_budget_case, under_budget_noisy])

        assert aggregated["case_count_total"] == 2.0
        assert aggregated["case_count_scored"] == 2.0
        assert aggregated["over_budget_rate"] == pytest.approx(0.5)
        assert aggregated["token_utilization"] == pytest.approx((2.0 + 0.5) / 2)
        assert aggregated["irrelevant_injection_rate"] == pytest.approx((0.0 + 1.0) / 2)

    def test_over_budget_rate_is_fraction_of_cases(self) -> None:
        """超预算率 = 超预算 case 数 / 计分 case 数（0/1 判定的均值）。"""
        evaluator = InjectionEvaluator(inject_max_tokens=1000)
        results = [
            evaluator.evaluate_case(
                payload=_payload(f"q{index}"),
                ground_truth=_gt_by_content(CONTENT_A),
                token_count=token_count,
                injected_contents=[CONTENT_A],
            )
            for index, token_count in enumerate([1001, 1000, 1500, 500], start=1)
        ]
        # 1001 超、1000 恰好等于不算超、1500 超、500 不超 → 2/4 = 0.5
        assert evaluator.aggregate(results)["over_budget_rate"] == pytest.approx(0.5)
