"""维度②检索评测的纯单测（EP-9）。

重点验证三件事：

1. **contentHash 匹配不依赖 createdId**（EP-9 验收条款）——id 由 AgentWrite 侧
   seed 产生、``reset`` 后会变，按内容匹配才能让指标跨 run 可复现；
2. ground truth 同时给内容与 id 时**不被重复计数**（分母写错会让 Recall 系统性腰斩）；
3. 无法评测的 case（无 ground truth）被标记而非记 0——否则数据问题会伪装成模型退步。
"""

from __future__ import annotations

import pytest

from app.datasets.digest import memory_content_hash
from app.engine.dimensions.retrieval import (
    MATCH_BY_CONTENT,
    MATCH_BY_ID,
    MATCH_NONE,
    RetrievalEvaluator,
    RetrievedItem,
)

CONTENT_A = "用户用 Java 17"
CONTENT_B = "用户偏好美式咖啡"
CONTENT_C = "用户在做 Agent 项目"
NOISE = "用户今天心情不错"


def _items(*contents: str, ids: list[int | None] | None = None) -> list[RetrievedItem]:
    if ids is None:
        ids = list(range(100, 100 + len(contents)))
    return [
        RetrievedItem(id=ids[index], content=content, score=1.0 - index * 0.1)
        for index, content in enumerate(contents)
    ]


def _payload(query_id: str = "q1") -> dict:
    return {"query_id": query_id, "query": "用户用什么技术栈？", "task_type": "LEGACY"}


def _gt_by_content(*contents: str) -> dict:
    return {"relevant_memory_contents": list(contents)}


# ---------------------------------------------------------------------------
# 匹配方式
# ---------------------------------------------------------------------------


class TestMatching:
    def test_matches_by_content_hash(self) -> None:
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A, CONTENT_B),
            retrieved=_items(CONTENT_A, CONTENT_B, NOISE),
        )

        assert result.match_mode == MATCH_BY_CONTENT
        assert result.recall_at_k == pytest.approx(1.0)
        assert result.precision_at_k == pytest.approx(2 / 5)

    def test_content_matching_does_not_depend_on_ids(self) -> None:
        """**EP-9 验收条款**：匹配不依赖 createdId。

        构造两次「同一批内容、不同 id」的检索结果，模拟 reset 后重新 seed 的场景。
        两次必须得到**完全相同**的指标——否则同一份评测集跑两次会得出不同分数。
        """
        ground_truth = _gt_by_content(CONTENT_A, CONTENT_B)
        contents = (CONTENT_A, CONTENT_B, NOISE)

        first = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=ground_truth,
            retrieved=_items(*contents, ids=[101, 102, 103]),
        )
        # reset 后重 seed，id 全变了
        second = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=ground_truth,
            retrieved=_items(*contents, ids=[9001, 9002, 9003]),
        )

        assert first.recall_at_k == second.recall_at_k
        assert first.ndcg_at_k == second.ndcg_at_k
        assert first.reciprocal_rank == second.reciprocal_rank

    def test_matching_works_with_null_ids(self) -> None:
        """即使 Java 侧没返回 id（或返回 null），按内容依然能算出指标。"""
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            retrieved=_items(CONTENT_A, ids=[None]),
        )

        assert result.recall_at_k == pytest.approx(1.0)

    def test_falls_back_to_ids_when_no_contents(self) -> None:
        """ground truth 只给 id（手册原生格式）时仍能算，但放弃可复现性。

        相关 id 是 101/102，检索回来的两个只命中 101（另一个是 999），
        故 Recall = 1/2。
        """
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth={"relevant_memory_ids": [101, 102]},
            retrieved=_items(CONTENT_A, NOISE, ids=[101, 999]),
        )

        assert result.match_mode == MATCH_BY_ID
        assert result.relevant_total == 2
        assert result.recall_at_k == pytest.approx(0.5)

    def test_contents_take_priority_and_are_not_double_counted(self) -> None:
        """**两条路径同时给出时只按内容算**。

        两种形式描述的是同一批记忆。若把 `h:*` 与 `i:*` 并起来当分母，
        相关总数会翻倍、Recall 被系统性腰斩——这类错误不会报错，只会让指标偏低。
        """
        ground_truth = {
            "relevant_memory_contents": [CONTENT_A, CONTENT_B],
            "relevant_memory_ids": [101, 102],
        }
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=ground_truth,
            retrieved=_items(CONTENT_A, CONTENT_B, ids=[101, 102]),
        )

        assert result.match_mode == MATCH_BY_CONTENT
        # 相关总数 = 2（不是 4）
        assert result.relevant_total == 2
        assert result.recall_at_k == pytest.approx(1.0)

    def test_blank_contents_are_ignored(self) -> None:
        ground_truth = {"relevant_memory_contents": [CONTENT_A, "   ", ""]}
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(), ground_truth=ground_truth, retrieved=_items(CONTENT_A)
        )
        assert result.relevant_total == 1


# ---------------------------------------------------------------------------
# 指标数值
# ---------------------------------------------------------------------------


class TestMetricValues:
    def test_partial_recall(self) -> None:
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A, CONTENT_B, CONTENT_C),
            retrieved=_items(CONTENT_A, NOISE),
        )
        assert result.recall_at_k == pytest.approx(1 / 3)
        assert result.relevant_total == 3

    def test_miss_is_zero_recall(self) -> None:
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            retrieved=_items(NOISE, NOISE + "2"),
        )
        assert result.recall_at_k == 0.0
        assert result.hit_at_1 == 0.0
        assert result.reciprocal_rank == 0.0
        assert result.ndcg_at_k == 0.0

    def test_hit_at_1_only_looks_at_first(self) -> None:
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A),
            retrieved=_items(NOISE, CONTENT_A),
        )
        assert result.hit_at_1 == 0.0
        assert result.reciprocal_rank == pytest.approx(0.5)

    def test_k_truncates_recall(self) -> None:
        retrieved = _items(NOISE, NOISE + "2", CONTENT_A)
        ground_truth = _gt_by_content(CONTENT_A)

        assert RetrievalEvaluator(k=2).evaluate_case(
            payload=_payload(), ground_truth=ground_truth, retrieved=retrieved
        ).recall_at_k == 0.0
        assert RetrievalEvaluator(k=3).evaluate_case(
            payload=_payload(), ground_truth=ground_truth, retrieved=retrieved
        ).recall_at_k == 1.0

    def test_k_recorded_in_result(self) -> None:
        """K 必须记进结果：不同 K 的 Recall 不可直接比较。"""
        result = RetrievalEvaluator(k=10).evaluate_case(
            payload=_payload(), ground_truth=_gt_by_content(CONTENT_A), retrieved=_items(CONTENT_A)
        )
        assert result.k == 10
        assert result.as_detail()["k"] == 10

    def test_invalid_k_rejected(self) -> None:
        with pytest.raises(ValueError):
            RetrievalEvaluator(k=0)


# ---------------------------------------------------------------------------
# 明细：可追溯到 case
# ---------------------------------------------------------------------------


class TestCaseDetail:
    def test_records_matched_missing_and_spurious(self) -> None:
        """验收条款「每个指标可以追溯到 case detail」：要能看出漏了什么、多了什么。"""
        result = RetrievalEvaluator(k=3).evaluate_case(
            payload=_payload(),
            ground_truth=_gt_by_content(CONTENT_A, CONTENT_B),
            retrieved=_items(CONTENT_A, NOISE),
        )

        assert result.matched_hashes == [memory_content_hash(CONTENT_A)]
        assert result.missing_hashes == [memory_content_hash(CONTENT_B)]
        assert memory_content_hash(NOISE) in result.spurious_hashes

    def test_detail_is_json_serializable(self) -> None:
        """明细要落库，不能含 set / 自定义对象。"""
        import json

        result = RetrievalEvaluator(k=3).evaluate_case(
            payload=_payload(), ground_truth=_gt_by_content(CONTENT_A), retrieved=_items(CONTENT_A)
        )
        json.dumps(result.as_detail())  # 不抛即通过

    def test_query_id_is_carried(self) -> None:
        result = RetrievalEvaluator(k=3).evaluate_case(
            payload=_payload("q-042"),
            ground_truth=_gt_by_content(CONTENT_A),
            retrieved=_items(CONTENT_A),
        )
        assert result.query_id == "q-042"


# ---------------------------------------------------------------------------
# 不可评测的 case
# ---------------------------------------------------------------------------


class TestUnanswerable:
    def test_no_ground_truth_is_marked_not_scored(self) -> None:
        """无 ground truth 时标记 answerable=False，而不是当作「全错」。

        当作全错会污染维度指标：一个标注漏填的 case 会让 Recall 掉，
        看起来像检索退步，实际是数据问题。
        """
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(), ground_truth={}, retrieved=_items(CONTENT_A)
        )

        assert result.answerable is False
        assert result.match_mode == MATCH_NONE
        assert result.relevant_total == 0

    def test_empty_relevant_list_is_unanswerable(self) -> None:
        result = RetrievalEvaluator(k=5).evaluate_case(
            payload=_payload(),
            ground_truth={"relevant_memory_contents": []},
            retrieved=_items(CONTENT_A),
        )
        assert result.answerable is False

    def test_aggregate_excludes_unanswerable(self) -> None:
        """聚合时不可评测的 case 必须在分母之外。"""
        evaluator = RetrievalEvaluator(k=5)
        good = evaluator.evaluate_case(
            payload=_payload("q1"), ground_truth=_gt_by_content(CONTENT_A), retrieved=_items(CONTENT_A)
        )
        bad = evaluator.evaluate_case(payload=_payload("q2"), ground_truth={}, retrieved=_items(NOISE))

        aggregated = evaluator.aggregate([good, bad])

        assert aggregated["case_count"] == 1.0
        assert aggregated["recall_at_k"] == pytest.approx(1.0)

    def test_aggregate_all_unanswerable(self) -> None:
        evaluator = RetrievalEvaluator(k=5)
        bad = evaluator.evaluate_case(payload=_payload(), ground_truth={}, retrieved=_items(NOISE))
        assert evaluator.aggregate([bad])["case_count"] == 0.0

    def test_aggregate_empty(self) -> None:
        assert RetrievalEvaluator(k=5).aggregate([])["case_count"] == 0.0


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_averages_across_cases(self) -> None:
        evaluator = RetrievalEvaluator(k=2)
        perfect = evaluator.evaluate_case(
            payload=_payload("q1"),
            ground_truth=_gt_by_content(CONTENT_A),
            retrieved=_items(CONTENT_A),
        )
        missed = evaluator.evaluate_case(
            payload=_payload("q2"), ground_truth=_gt_by_content(CONTENT_A), retrieved=_items(NOISE)
        )

        aggregated = evaluator.aggregate([perfect, missed])

        assert aggregated["recall_at_k"] == pytest.approx(0.5)
        assert aggregated["hit_at_1"] == pytest.approx(0.5)
        assert aggregated["mrr"] == pytest.approx(0.5)  # (1.0 + 0.0) / 2

    def test_mrr_matches_manual_computation(self) -> None:
        """MRR 用逐 case 的 reciprocal_rank 平均，与手算一致。"""
        evaluator = RetrievalEvaluator(k=5)
        results = [
            evaluator.evaluate_case(
                payload=_payload(f"q{index}"),
                ground_truth=_gt_by_content(CONTENT_A),
                retrieved=_items(*retrieved),
            )
            for index, retrieved in enumerate(
                [(CONTENT_A,), (NOISE, CONTENT_A), (NOISE, NOISE + "2")]
            )
        ]
        expected = (1.0 + 0.5 + 0.0) / 3
        assert evaluator.aggregate(results)["mrr"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 与 connector DTO 的衔接
# ---------------------------------------------------------------------------


class TestConnectorInterop:
    def test_from_search_item(self) -> None:
        """engine 层不依赖 HTTP DTO，但需要一条明确的适配路径。"""

        class _FakeSearchItem:
            id = 7
            content = CONTENT_A
            score = 0.87

        item = RetrievedItem.from_search_item(_FakeSearchItem())
        assert item.id == 7
        assert item.content == CONTENT_A
        assert item.content_hash == memory_content_hash(CONTENT_A)
