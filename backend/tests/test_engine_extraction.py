"""维度①抽取评测的纯单测（EP-9）。

重点验证六件事：

1. P/R/F1 走 **contentHash 匹配**、去重后集合语义——重复候选不把 Recall 抬过 1.0；
2. ``duplicate_extraction_rate = 1 - 去重数 / 候选总数``；
3. ``attribution_error_rate`` 的分母只在「gt 侧给了 attributed_to」的命中候选上算，
   gt 缺归属 → 排除、候选缺归属 → 算错；
4. ``low_value_write_rate`` 是**严格小于**阈值（恰好等于不算低价值），confidence
   缺失从分母分子一并排除、用计数单独暴露；
5. ``operation=UPDATE`` 候选按内容正常匹配（匹配与 operation 无关）；
6. 无法评测的 case（无 ground truth）被标记 ``answerable=False``，聚合时从依赖 gt 的
   指标分母排除，但保留不依赖 gt 的两个率。

全部测试**不需要网络、不需要数据库**：evaluator 收的是已解析好的候选列表，保持纯函数。
"""

from __future__ import annotations

import json

import pytest

from app.datasets.digest import memory_content_hash
from app.engine.dimensions.extraction import ExtractedCandidate, ExtractionCaseResult, ExtractionEvaluator

CONTENT_A = "用户用 Java 17"
CONTENT_B = "用户偏好美式咖啡"
CONTENT_C = "用户在做 Agent 项目"
NOISE = "用户今天心情不错"

DEFAULT_THRESHOLD = 0.5


def _payload(dialogue_id: str = "d1") -> dict:
    return {
        "dialogue_id": dialogue_id,
        "messages": [{"role": "user", "content": "我用 Java 17 写项目"}],
    }


def _gt(*memories: dict) -> dict:
    return {"ground_truth_memories": list(memories)}


def _candidate(content: str, *, attributed_to: str | None = None,
               operation: str | None = None, confidence: float | None = None) -> ExtractedCandidate:
    return ExtractedCandidate(
        content=content, attributed_to=attributed_to, operation=operation, confidence=confidence
    )


def _eval(*candidates: ExtractedCandidate, gt: dict | None = None, threshold: float = DEFAULT_THRESHOLD):
    return ExtractionEvaluator(confidence_threshold=threshold).evaluate_case(
        payload=_payload(),
        ground_truth=gt if gt is not None else _gt({"content": CONTENT_A}),
        candidates=list(candidates),
    )


# ---------------------------------------------------------------------------
# 提取 P / R / F1
# ---------------------------------------------------------------------------


class TestPrecisionRecall:
    def test_perfect_match(self) -> None:
        result = _eval(
            _candidate(CONTENT_A), _candidate(CONTENT_B),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.precision == pytest.approx(1.0)
        assert result.recall == pytest.approx(1.0)
        assert result.f1 == pytest.approx(1.0)
        assert result.answerable is True

    def test_partial_match(self) -> None:
        """gt 两条、只抽出两条里的一条：precision=1、recall=0.5。"""
        result = _eval(
            _candidate(CONTENT_A),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.precision == pytest.approx(1.0)
        assert result.recall == pytest.approx(0.5)
        assert result.f1 == pytest.approx(2 * 1.0 * 0.5 / 1.5)

    def test_over_extraction_drops_precision(self) -> None:
        """多抽：候选里混入噪声，precision 下降、recall 保持 1。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate(NOISE),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.precision == pytest.approx(0.5)
        assert result.recall == pytest.approx(1.0)
        assert result.f1 == pytest.approx(2 * 0.5 * 1.0 / 1.5)

    def test_under_extraction_drops_recall(self) -> None:
        """漏抽：gt 两条只抽到一条，recall 下降。"""
        result = _eval(
            _candidate(CONTENT_A),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.recall == pytest.approx(0.5)
        assert result.missing_hashes == [memory_content_hash(CONTENT_B)]

    def test_empty_candidates_zero_precision_and_recall(self) -> None:
        """什么都没抽到：P=0、R=0、F1=0，但仍是 answerable（可观测行为）。"""
        result = _eval(gt=_gt({"content": CONTENT_A}))
        assert result.answerable is True
        assert result.precision == 0.0
        assert result.recall == 0.0
        assert result.f1 == 0.0
        assert result.duplicate_extraction_rate == 0.0
        assert result.attribution_error_rate == 0.0
        assert result.low_value_write_rate == 0.0

    def test_matches_by_content_hash_not_id(self) -> None:
        """匹配走 contentHash：候选内容与 gt 内容逐字一致即命中，与 id 无关。"""
        result = _eval(_candidate(CONTENT_A), gt=_gt({"content": CONTENT_A}))
        assert memory_content_hash(CONTENT_A) in result.matched_hashes
        assert result.matched_total == 1

    def test_matches_ignores_surrounding_whitespace(self) -> None:
        """``memory_content_hash`` 先 strip 首尾空白：候选「  A  」应命中 gt「A」。"""
        result = _eval(
            _candidate(f"  {CONTENT_A}  "),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.matched_total == 1
        assert result.precision == pytest.approx(1.0)
        assert result.recall == pytest.approx(1.0)

    def test_duplicate_candidate_does_not_inflate_recall(self) -> None:
        """重复候选不进 P/R：候选 [A, A] 对 gt {A} 的 Recall 必须是 1.0 而非 2.0。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate(CONTENT_A),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.distinct_total == 1
        assert result.precision == pytest.approx(1.0)
        assert result.recall == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 重复抽取率
# ---------------------------------------------------------------------------


class TestDuplicateExtraction:
    def test_single_duplicate_is_half(self) -> None:
        """[A, A]：两条里一条冗余 → 1/2。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate(CONTENT_A),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.candidate_total == 2
        assert result.distinct_total == 1
        assert result.duplicate_total == 1
        assert result.duplicate_extraction_rate == pytest.approx(0.5)

    def test_triple_duplicate_is_two_thirds(self) -> None:
        """[A, A, A]：三条里两条冗余 → 2/3。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate(CONTENT_A), _candidate(CONTENT_A),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.duplicate_extraction_rate == pytest.approx(2 / 3)

    def test_mixed_duplicate_rate(self) -> None:
        """[A, A, B]：3 条候选、2 条 distinct → 冗余 1/3。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate(CONTENT_A), _candidate(CONTENT_B),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.duplicate_extraction_rate == pytest.approx(1 / 3)

    def test_no_duplicate_is_zero(self) -> None:
        result = _eval(
            _candidate(CONTENT_A), _candidate(CONTENT_B),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.duplicate_extraction_rate == 0.0

    def test_duplicate_judged_by_content_hash(self) -> None:
        """重复判定用 contentHash：仅差首尾空白的两条也算重复。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate(f"  {CONTENT_A}  "),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.distinct_total == 1
        assert result.duplicate_extraction_rate == pytest.approx(0.5)

    def test_blank_candidates_ignored(self) -> None:
        """空白内容候选被过滤，不进候选总数。"""
        result = _eval(
            _candidate(CONTENT_A), _candidate("   "), _candidate(""),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.candidate_total == 1
        assert result.duplicate_extraction_rate == 0.0


# ---------------------------------------------------------------------------
# 错误归属率
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_correct_attribution_is_zero(self) -> None:
        result = _eval(
            _candidate(CONTENT_A, attributed_to="user"),
            gt=_gt({"content": CONTENT_A, "attributed_to": "user"}),
        )
        assert result.attribution_scored_total == 1
        assert result.attribution_error_total == 0
        assert result.attribution_error_rate == 0.0

    def test_wrong_attribution_is_one(self) -> None:
        """抽对了内容、搞错了来源：agent 说的话记成 user 的偏好。"""
        result = _eval(
            _candidate(CONTENT_A, attributed_to="agent"),
            gt=_gt({"content": CONTENT_A, "attributed_to": "user"}),
        )
        assert result.attribution_error_total == 1
        assert result.attribution_error_rate == 1.0

    def test_gt_missing_attribution_is_excluded(self) -> None:
        """gt 侧没给 attributed_to → 无可比期望，不计入分母（不是错误）。"""
        result = _eval(
            _candidate(CONTENT_A, attributed_to="user"),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.attribution_scored_total == 0
        assert result.attribution_error_rate == 0.0

    def test_candidate_missing_attribution_counts_as_error(self) -> None:
        """gt 明确给了来源、候选没给 → 漏带来源，算归属错误。"""
        result = _eval(
            _candidate(CONTENT_A, attributed_to=None),
            gt=_gt({"content": CONTENT_A, "attributed_to": "user"}),
        )
        assert result.attribution_scored_total == 1
        assert result.attribution_error_total == 1
        assert result.attribution_error_rate == 1.0

    def test_candidate_blank_attribution_counts_as_missing(self) -> None:
        """候选 attributed_to 为空白字符串，归一化后视为缺失 → 算错。"""
        result = _eval(
            _candidate(CONTENT_A, attributed_to="   "),
            gt=_gt({"content": CONTENT_A, "attributed_to": "user"}),
        )
        assert result.attribution_error_rate == 1.0

    def test_attribution_normalized_case(self) -> None:
        """归属比较前归一化（小写）：候选「User」与 gt「user」应视为一致。"""
        result = _eval(
            _candidate(CONTENT_A, attributed_to="User"),
            gt=_gt({"content": CONTENT_A, "attributed_to": "user"}),
        )
        assert result.attribution_error_rate == 0.0

    def test_only_matched_candidates_scored_for_attribution(self) -> None:
        """归属只在命中 gt 的候选上算：未命中的噪声候选不影响归属率。"""
        result = _eval(
            _candidate(CONTENT_A, attributed_to="user"),
            _candidate(NOISE, attributed_to="agent"),
            gt=_gt({"content": CONTENT_A, "attributed_to": "user"}),
        )
        assert result.attribution_scored_total == 1
        assert result.attribution_error_rate == 0.0


# ---------------------------------------------------------------------------
# 低价值写入率
# ---------------------------------------------------------------------------


class TestLowValueWrite:
    def test_below_threshold_is_low(self) -> None:
        result = _eval(
            _candidate(CONTENT_A, confidence=0.1),
            _candidate(CONTENT_B, confidence=0.9),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.low_value_total == 1
        assert result.confidence_scored_total == 2
        assert result.low_value_write_rate == pytest.approx(0.5)

    def test_exactly_equal_threshold_is_not_low(self) -> None:
        """**边界**：恰好等于阈值说明「正好卡线、没低于」，不算低价值（严格小于）。"""
        result = _eval(
            _candidate(CONTENT_A, confidence=0.5),
            gt=_gt({"content": CONTENT_A}),
            threshold=0.5,
        )
        assert result.low_value_total == 0
        assert result.low_value_write_rate == 0.0

    def test_above_threshold_is_not_low(self) -> None:
        result = _eval(
            _candidate(CONTENT_A, confidence=0.51),
            gt=_gt({"content": CONTENT_A}),
            threshold=0.5,
        )
        assert result.low_value_write_rate == 0.0

    def test_missing_confidence_excluded_and_counted(self) -> None:
        """confidence 缺失不进低价值率的分母分子，用计数单独暴露。"""
        result = _eval(
            _candidate(CONTENT_A, confidence=None),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.confidence_scored_total == 0
        assert result.confidence_missing_total == 1
        assert result.low_value_write_rate == 0.0

    def test_missing_confidence_mixed(self) -> None:
        """有 confidence 的候选才进低价值率：缺的既不抬高也不压低。"""
        result = _eval(
            _candidate(CONTENT_A, confidence=0.1),
            _candidate(CONTENT_B, confidence=None),
            _candidate(CONTENT_C, confidence=0.9),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}, {"content": CONTENT_C}),
        )
        assert result.confidence_scored_total == 2
        assert result.confidence_missing_total == 1
        assert result.low_value_total == 1
        assert result.low_value_write_rate == pytest.approx(0.5)

    def test_threshold_recorded_in_result(self) -> None:
        """阈值必须记进结果：不同阈值下的低价值率不可直接比较。"""
        result = ExtractionEvaluator(confidence_threshold=0.7).evaluate_case(
            payload=_payload(),
            ground_truth=_gt({"content": CONTENT_A}),
            candidates=[_candidate(CONTENT_A)],
        )
        assert result.confidence_threshold == 0.7
        assert result.as_detail()["confidence_threshold"] == 0.7

    def test_invalid_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExtractionEvaluator(confidence_threshold=-1.0)
        with pytest.raises(ValueError):
            ExtractionEvaluator(confidence_threshold=float("nan"))


# ---------------------------------------------------------------------------
# UPDATE 操作
# ---------------------------------------------------------------------------


class TestUpdateOperation:
    def test_update_candidate_matched_by_content(self) -> None:
        """UPDATE 候选按内容正常匹配：抽对了内容就算命中，operation 不影响。"""
        result = _eval(
            _candidate(CONTENT_A, operation="UPDATE"),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.matched_total == 1
        assert result.precision == pytest.approx(1.0)
        assert result.recall == pytest.approx(1.0)

    def test_update_candidate_with_wrong_content_not_matched(self) -> None:
        """UPDATE 但内容对不上 → 不算命中（匹配只认内容）。"""
        result = _eval(
            _candidate(NOISE, operation="UPDATE"),
            gt=_gt({"content": CONTENT_A}),
        )
        assert result.matched_total == 0
        assert result.precision == 0.0

    def test_add_and_update_treated_identically(self) -> None:
        """ADD 与 UPDATE 对 P/R 无差别：都是按内容匹配。"""
        gt = _gt({"content": CONTENT_A}, {"content": CONTENT_B})
        add = _eval(_candidate(CONTENT_A, operation="ADD"), gt=gt)
        update = _eval(_candidate(CONTENT_A, operation="UPDATE"), gt=gt)
        assert add.precision == update.precision
        assert add.recall == update.recall


# ---------------------------------------------------------------------------
# 不可评测的 case
# ---------------------------------------------------------------------------


class TestUnanswerable:
    def test_no_ground_truth_is_unanswerable(self) -> None:
        result = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD).evaluate_case(
            payload=_payload(),
            ground_truth={},
            candidates=[_candidate(CONTENT_A)],
        )
        assert result.answerable is False
        assert result.precision == 0.0
        assert result.recall == 0.0
        assert result.f1 == 0.0
        assert result.attribution_error_rate == 0.0

    def test_empty_ground_truth_memories_is_unanswerable(self) -> None:
        result = _eval(gt={"ground_truth_memories": []})
        assert result.answerable is False

    def test_blank_ground_truth_memories_is_unanswerable(self) -> None:
        """schema 保证非空，但纯函数仍防御：全部空白内容视为无 ground truth。"""
        result = _eval(gt={"ground_truth_memories": [{"content": "   "}, {"content": ""}]})
        assert result.answerable is False

    def test_gt_independent_metrics_still_computed_when_unanswerable(self) -> None:
        """不依赖 gt 的重复率/低价值率在不可评测 case 上仍照常计算。"""
        result = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD).evaluate_case(
            payload=_payload(),
            ground_truth={},
            candidates=[_candidate(CONTENT_A), _candidate(CONTENT_A, confidence=0.1)],
        )
        assert result.answerable is False
        assert result.duplicate_extraction_rate == pytest.approx(0.5)
        assert result.low_value_write_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_two_buckets_have_different_denominators(self) -> None:
        """依赖 gt 的指标只覆盖 scored，不依赖 gt 的覆盖全部 case。"""
        evaluator = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD)
        good = evaluator.evaluate_case(
            payload=_payload("d1"),
            ground_truth=_gt({"content": CONTENT_A}),
            candidates=[_candidate(CONTENT_A), _candidate(NOISE)],
        )
        unanswerable = evaluator.evaluate_case(
            payload=_payload("d2"),
            ground_truth={},
            candidates=[_candidate(CONTENT_A), _candidate(CONTENT_A)],
        )

        aggregated = evaluator.aggregate([good, unanswerable])

        assert aggregated["case_count_total"] == 2.0
        assert aggregated["case_count_scored"] == 1.0
        # 依赖 gt 的：只取 good（precision=0.5）
        assert aggregated["precision"] == pytest.approx(0.5)
        # 不依赖 gt 的：good 重复率 0 + unanswerable 重复率 0.5 → 均值 0.25
        assert aggregated["duplicate_extraction_rate"] == pytest.approx((0.0 + 0.5) / 2)

    def test_gt_dependent_metrics_exclude_unanswerable(self) -> None:
        evaluator = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD)
        perfect = evaluator.evaluate_case(
            payload=_payload("d1"),
            ground_truth=_gt({"content": CONTENT_A}),
            candidates=[_candidate(CONTENT_A)],
        )
        unanswerable = evaluator.evaluate_case(
            payload=_payload("d2"),
            ground_truth={},
            candidates=[_candidate(NOISE)],
        )

        aggregated = evaluator.aggregate([perfect, unanswerable])

        assert aggregated["case_count_scored"] == 1.0
        assert aggregated["precision"] == pytest.approx(1.0)
        assert aggregated["recall"] == pytest.approx(1.0)
        assert aggregated["f1"] == pytest.approx(1.0)

    def test_average_across_cases(self) -> None:
        evaluator = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD)
        results = [
            evaluator.evaluate_case(
                payload=_payload("d1"),
                ground_truth=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
                candidates=[_candidate(CONTENT_A)],
            ),
            evaluator.evaluate_case(
                payload=_payload("d2"),
                ground_truth=_gt({"content": CONTENT_A}),
                candidates=[_candidate(CONTENT_A), _candidate(NOISE)],
            ),
        ]

        aggregated = evaluator.aggregate(results)

        assert aggregated["case_count_total"] == 2.0
        assert aggregated["case_count_scored"] == 2.0
        assert aggregated["recall"] == pytest.approx((0.5 + 1.0) / 2)
        assert aggregated["precision"] == pytest.approx((1.0 + 0.5) / 2)

    def test_aggregate_empty(self) -> None:
        aggregated = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD).aggregate([])
        assert aggregated["case_count_total"] == 0.0
        assert aggregated["case_count_scored"] == 0.0
        assert aggregated["precision"] == 0.0
        assert aggregated["recall"] == 0.0
        assert aggregated["f1"] == 0.0


# ---------------------------------------------------------------------------
# 明细
# ---------------------------------------------------------------------------


class TestDetail:
    def test_detail_is_json_serializable(self) -> None:
        result = _eval(_candidate(CONTENT_A), gt=_gt({"content": CONTENT_A}))
        json.dumps(result.as_detail())  # 不抛即通过

    def test_metric_values_keys(self) -> None:
        result = _eval(_candidate(CONTENT_A), gt=_gt({"content": CONTENT_A}))
        values = result.as_metric_values()
        assert set(values.keys()) == set(ExtractionCaseResult.METRIC_KEYS)


class TestCaseDetail:
    def test_records_hashes_for_traceability(self) -> None:
        result = _eval(
            _candidate(CONTENT_A), _candidate(NOISE),
            gt=_gt({"content": CONTENT_A}, {"content": CONTENT_B}),
        )
        assert result.extracted_hashes == [
            memory_content_hash(CONTENT_A), memory_content_hash(NOISE)
        ]
        assert result.matched_hashes == [memory_content_hash(CONTENT_A)]
        assert result.missing_hashes == [memory_content_hash(CONTENT_B)]
        assert result.spurious_hashes == [memory_content_hash(NOISE)]

    def test_dialogue_id_carried(self) -> None:
        result = ExtractionEvaluator(confidence_threshold=DEFAULT_THRESHOLD).evaluate_case(
            payload=_payload("d-042"),
            ground_truth=_gt({"content": CONTENT_A}),
            candidates=[_candidate(CONTENT_A)],
        )
        assert result.dialogue_id == "d-042"


# ---------------------------------------------------------------------------
# 连接器适配
# ---------------------------------------------------------------------------


class TestFromConnectorCandidate:
    def test_duck_typed_adaptation(self) -> None:
        """``from_connector_candidate`` 鸭子类型适配，避免 engine 层依赖 HTTP DTO。"""

        class FakeConnectorCandidate:
            content = CONTENT_A
            attributed_to = "user"
            operation = None
            confidence = 0.9

        candidate = ExtractedCandidate.from_connector_candidate(FakeConnectorCandidate())
        assert candidate.content == CONTENT_A
        assert candidate.attributed_to == "user"
        assert candidate.confidence == 0.9
