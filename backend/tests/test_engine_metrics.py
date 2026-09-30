"""指标原语的纯单测（EP-9）。

这些函数的错误方式是**静默的**：公式写错不会抛异常，只会让所有指标系统性偏一点，
而偏了之后无法从结果反推是哪一行代码的问题。所以这里用手算得出的期望值逐条钉死，
包括容易写错的边界（分母是「相关总数」还是 K、IDCG 怎么推、空集怎么办）。
"""

from __future__ import annotations

import math

import pytest

from app.engine.metrics import (
    dcg_at_k,
    f1,
    hit_at_1,
    mean,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    safe_ratio,
)


class TestRecallAtK:
    def test_full_recall(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_denominator_is_total_relevant_not_k(self) -> None:
        """**最容易写错的地方**：分母是相关记忆总数，不是 K。

        相关共 4 条、top-2 命中 2 条 → Recall@2 = 2/4 = 0.5，不是 2/2 = 1.0。
        若把分母写成 K，Recall 会随 K 增大而虚高，恰好掩盖「相关记忆很多但只捞到几条」。
        """
        assert recall_at_k(["a", "b"], {"a", "b", "c", "d"}, 2) == pytest.approx(0.5)

    def test_partial_recall(self) -> None:
        assert recall_at_k(["a", "x", "b"], {"a", "b", "c"}, 3) == pytest.approx(2 / 3)

    def test_k_truncates(self) -> None:
        """K 之外的相关项不算命中。"""
        assert recall_at_k(["x", "y", "a"], {"a"}, 2) == 0.0
        assert recall_at_k(["x", "y", "a"], {"a"}, 3) == 1.0

    def test_empty_relevant_raises(self) -> None:
        """零相关集合的 Recall 数学上无定义，抛错而非静默返回 0——
        静默返回 0 会把数据问题伪装成指标偏低。"""
        with pytest.raises(ValueError):
            recall_at_k(["a"], set(), 5)

    def test_k_zero(self) -> None:
        assert recall_at_k(["a"], {"a"}, 0) == 0.0

    def test_negative_k_raises(self) -> None:
        with pytest.raises(ValueError):
            recall_at_k(["a"], {"a"}, -1)


class TestPrecisionAtK:
    def test_denominator_is_k_not_result_count(self) -> None:
        """分母固定为 K：只返回 2 条却都对，说明系统没取满，这件事应当反映在指标里。"""
        assert precision_at_k(["a", "b"], {"a", "b"}, 5) == pytest.approx(0.4)

    def test_all_relevant(self) -> None:
        assert precision_at_k(["a", "b"], {"a", "b"}, 2) == 1.0

    def test_k_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            precision_at_k(["a"], {"a"}, 0)


class TestHitAt1:
    def test_first_is_relevant(self) -> None:
        assert hit_at_1(["a", "b"], {"a"}) == 1.0

    def test_first_is_not_relevant(self) -> None:
        assert hit_at_1(["x", "a"], {"a"}) == 0.0

    def test_empty_retrieved(self) -> None:
        assert hit_at_1([], {"a"}) == 0.0


class TestReciprocalRank:
    def test_first_rank(self) -> None:
        assert reciprocal_rank(["a", "b"], {"a"}) == 1.0

    def test_second_rank(self) -> None:
        assert reciprocal_rank(["x", "a"], {"a"}) == pytest.approx(0.5)

    def test_third_rank(self) -> None:
        assert reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_no_hit_is_zero(self) -> None:
        """未命中返回 0 是定义，不是「无定义」：MRR 的被除数是 query 数，
        未命中的 query 计入分母贡献 0——这正是惩罚「完全没检索到」的方式。"""
        assert reciprocal_rank(["x", "y"], {"a"}) == 0.0

    def test_empty_retrieved(self) -> None:
        assert reciprocal_rank([], {"a"}) == 0.0

    def test_uses_first_hit_only(self) -> None:
        """只有**首个**命中项参与计算，后续命中不再加分。"""
        assert reciprocal_rank(["x", "a", "b"], {"a", "b"}) == pytest.approx(0.5)


class TestMRR:
    def test_mean_of_reciprocal_ranks(self) -> None:
        results = [
            (["a", "x"], {"a"}),  # 1.0
            (["x", "a"], {"a"}),  # 0.5
            (["x", "y"], {"a"}),  # 0.0
        ]
        assert mrr(results) == pytest.approx((1.0 + 0.5 + 0.0) / 3)

    def test_empty_results(self) -> None:
        assert mrr([]) == 0.0


class TestNDCG:
    def test_perfect_ranking_is_one(self) -> None:
        assert ndcg_at_k(["a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)

    def test_reversed_ranking_is_penalised(self) -> None:
        """**NDCG 存在的意义**：把相关项排到后面要扣分。

        这条用 Recall 是区分不出来的——两种排序的 Recall 都是 1.0。
        """
        perfect = ndcg_at_k(["a", "b"], {"a", "b"}, 2)
        reversed_ = ndcg_at_k(["b", "a"], {"a", "b"}, 2)
        # 二元相关且两条都命中时，两种排序的 DCG 相同（1 + 1/log2(3)），
        # 故此处应相等。真正体现排序差异的是下面那条（相关项数 < 返回数）。
        assert perfect == pytest.approx(reversed_)

        # 相关只有 1 条、返回 3 条时，排在第 1 位与第 3 位的差距就体现出来了
        assert ndcg_at_k(["a", "x", "y"], {"a"}, 3) > ndcg_at_k(["x", "y", "a"], {"a"}, 3)

    def test_hand_computed_value(self) -> None:
        """手算核对：相关 2 条，返回 [x, a, b]（第 2、3 位命中）。

        DCG  = 1/log2(3) + 1/log2(4)
        IDCG = 1/log2(2) + 1/log2(3)
        """
        dcg = 1 / math.log2(3) + 1 / math.log2(4)
        idcg = 1 / math.log2(2) + 1 / math.log2(3)
        assert ndcg_at_k(["x", "a", "b"], {"a", "b"}, 3) == pytest.approx(dcg / idcg)

    def test_no_hit_in_top_k_is_zero(self) -> None:
        assert ndcg_at_k(["x", "y"], {"a"}, 2) == 0.0

    def test_empty_relevant_is_zero(self) -> None:
        assert ndcg_at_k(["a"], set(), 2) == 0.0

    def test_k_zero_is_zero(self) -> None:
        assert ndcg_at_k(["a"], {"a"}, 0) == 0.0

    def test_ideal_hits_capped_by_k(self) -> None:
        """相关 10 条、K=2 时，IDCG 只按前 2 位算——否则 NDCG 永远达不到 1。"""
        assert ndcg_at_k(["a", "b"], {f"m{i}" for i in range(10)} | {"a", "b"}, 2) == pytest.approx(1.0)


class TestDCG:
    def test_first_position_weight_is_one(self) -> None:
        assert dcg_at_k(["a"], {"a"}, 1) == pytest.approx(1.0)

    def test_position_discount(self) -> None:
        assert dcg_at_k(["x", "a"], {"a"}, 2) == pytest.approx(1 / math.log2(3))


class TestHelpers:
    def test_f1_harmonic_mean(self) -> None:
        assert f1(1.0, 1.0) == pytest.approx(1.0)
        assert f1(0.5, 0.5) == pytest.approx(0.5)
        # 极端不均衡：P=1, R=0 → F1=0（调和平均对短板敏感）
        assert f1(1.0, 0.0) == 0.0

    def test_f1_both_zero(self) -> None:
        assert f1(0.0, 0.0) == 0.0

    def test_safe_ratio_zero_denominator(self) -> None:
        """抽取总数为 0 时「错误归属率」业务上等同于「没有错误归属」，记 0 而非抛错。"""
        assert safe_ratio(3, 0) == 0.0

    def test_safe_ratio_normal(self) -> None:
        assert safe_ratio(1, 4) == pytest.approx(0.25)

    def test_mean(self) -> None:
        assert mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)
        assert mean([]) == 0.0
