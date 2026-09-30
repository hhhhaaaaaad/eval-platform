"""指标原语（EP-9 的算法核心）。

全部是**纯函数**，输入输出只涉及不透明的标识符（内容哈希或记忆 id）。
这样设计有一个直接好处：指标算法与「如何判定两条记忆是同一个」解耦，
换匹配方式（精确哈希 / 语义匹配）不需要动任何一行指标代码。

公式取自《评测集设计与指标手册》§2，未自行发明。

**关于边界值的取舍**：这些原语对「未定义」的输入一律**抛错**而不是返回 0——
零相关的集合算 Recall 在数学上无定义，静默返回 0 会让一个数据问题伪装成
「指标偏低」，而平均到全量后很难看出是哪几条 case 有问题。要不要跳过这类 case
是业务决策，交给调用方（``dimensions`` 层）显式处理。
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence

#: NDCG / Recall@K 的常用截断位。手册中「K 通常取 5 / 10」。
DEFAULT_K = 5


def _require_non_negative_k(k: int) -> None:
    if k < 0:
        raise ValueError(f"k 不能为负数: {k}")


def recall_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Recall@K = 前 K 个结果里命中相关记忆数 / 相关记忆总数。

    分母是**相关记忆总数**而非 K：这是 Recall 与 Precision 的关键区别。
    相关记忆共 10 条、top-5 命中 3 条时 Recall@5 = 0.3，不是 0.6。
    """
    _require_non_negative_k(k)
    if not relevant:
        raise ValueError("相关记忆集合为空，Recall 无定义（调用方应决定跳过还是记 0）")
    if k == 0:
        return 0.0
    hits = sum(1 for item in retrieved[:k] if item in relevant)
    return hits / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Precision@K = 前 K 个结果里命中相关记忆数 / K。

    分母固定为 K 而非实际返回条数：检索只返回 2 条却都对，说明系统没取满，
    这件事应当反映在指标里（分母按 K 算会得到 0.4），而不是被「返回了几条就除以几」
    掩盖成 1.0。
    """
    _require_non_negative_k(k)
    if k == 0:
        raise ValueError("Precision@0 无定义")
    hits = sum(1 for item in retrieved[:k] if item in relevant)
    return hits / k


def hit_at_1(retrieved: Sequence[str], relevant: Collection[str]) -> float:
    """Hit@1：首个结果就命中相关记忆则为 1，否则 0。

    与 MRR 的区别是只看第一名，不做倒数排名平滑——它对「榜首质量」更敏感。
    """
    if not retrieved:
        return 0.0
    return 1.0 if retrieved[0] in relevant else 0.0


def reciprocal_rank(retrieved: Sequence[str], relevant: Collection[str]) -> float:
    """单次查询的倒数排名 RR = 1 / 首个相关记忆的排名（排名从 1 开始）。

    全部未命中返回 0——这是定义，不是「无定义」：手册里 MRR 的被除数是 query 数，
    未命中的 query 计入分母、贡献 0，正是惩罚「完全没检索到」的方式。
    """
    for index, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def mrr(results: Sequence[tuple[Sequence[str], Collection[str]]]) -> float:
    """MRR = Σ(1 / 首个相关记忆的排名) / query 数。

    入参是 (检索结果, 相关集合) 的序列。空序列返回 0.0——没有 query 时
    MRR 无法定义，但调用方按 run 聚合时遇到空集是正常情况，返回 0 比抛错实用。
    """
    if not results:
        return 0.0
    return sum(reciprocal_rank(retrieved, relevant) for retrieved, relevant in results) / len(results)


def dcg_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """DCG@K = Σ rel_i / log2(i + 1)，二元相关（命中记 1）。

    用 log2 折损：相关项排得越靠后贡献越小，这正是 NDCG 能区分「都对但顺序不同」
    的原因——只看 Recall 的话，把相关记忆全排到最后一名也算满分。
    """
    _require_non_negative_k(k)
    return sum(
        1.0 / math.log2(index + 1)
        for index, item in enumerate(retrieved[:k], start=1)
        if item in relevant
    )


def ndcg_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """NDCG@K = DCG@K / IDCG@K。

    IDCG 是**理想排序**的 DCG：把 min(相关总数, K) 个相关项全部排在最前算出。
    两者相等即 1.0（完美排序）。

    相关集合为空时 DCG 与 IDCG 同为 0，约定返回 0.0（0/0 无定义，但此处
    「没有正确答案」与「一个都没排对」在业务上是同一处境，返回 0 不会误伤）。
    """
    _require_non_negative_k(k)
    if k == 0:
        return 0.0
    ideal_hits = min(len(relevant), k)
    if ideal_hits == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg_at_k(retrieved, relevant, k) / idcg


def f1(precision: float, recall: float) -> float:
    """F1 = 2PR / (P + R)。两者同为 0 时约定为 0（而非 0/0）。"""
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def safe_ratio(numerator: int, denominator: int) -> float:
    """``numerator / denominator``，分母为 0 时返回 0.0。

    用于「率」类指标（错误归属率、重复抽取率等）。这里返回 0 而不是抛错是有意的：
    抽取总数为 0 意味着这条 case 压根没抽到东西，此时「错误归属率」在业务上
    等同于「没有错误归属」，记 0 比让整个 run 失败更合适。
    """
    if denominator == 0:
        return 0.0
    return numerator / denominator


def mean(values: Sequence[float]) -> float:
    """算术平均，空序列返回 0.0（按 run 聚合时无 case 属正常情况）。"""
    if not values:
        return 0.0
    return sum(values) / len(values)
