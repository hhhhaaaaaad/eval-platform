"""维度②：检索质量（EP-9）。

对每个 ``query_to_memory`` case 跑检索，把返回结果与 ground truth 对齐，
算 Recall@K / Precision@K / Hit@1 / MRR / NDCG@K。

**匹配方式有一条硬约束**（EP-9 验收条款「contentHash 匹配不依赖 createdId」）：
主匹配走 **content 的 md5 哈希**，不依赖记忆 id。原因是 id 由 AgentWrite 侧
seed 时产生、``reset`` 后会变——同一份评测集跑两次拿到的 id 不同，
按 id 匹配等于让指标不可复现。按内容匹配时，只要语料内容一致，结果就一致。

实现手法是**投影到统一的标识符空间**：把 ground truth 与检索结果都映射成
一组带前缀的标识符（``h:<contentHash>`` / ``i:<id>``），命中判定就退化为
集合成员判断，指标本身直接复用 :mod:`app.engine.metrics` 的纯函数——
不在这里重写一遍公式，避免两处实现悄悄分叉。

**两条匹配路径的优先级**（重要）：ground truth 同时给出
``relevant_memory_contents`` 与 ``relevant_memory_ids`` 时，**只用内容哈希**，
忽略 id。理由是这样算出的「相关记忆总数」才正确——两种形式描述的是同一批记忆，
若把它们并起来当分母，Recall 会被系统性低估一半左右。
（数据集 schema 允许两种标注并存，正是为了兼容手册原生格式与可复现格式。）

每个 case 的结果对象保留**完整明细**（命中了哪些、漏了哪些、多召回了哪些），
这是验收条款「每个指标可以追溯到 case detail」的落地方式。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.datasets.digest import memory_content_hash
from app.engine.metrics import (
    DEFAULT_K,
    hit_at_1,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

#: 明细里最多保留多少条标识符，避免长 top-K 下 case detail 膨胀。
#: 超出部分只影响「留证」，不影响任何指标数值。
_DETAIL_LIMIT = 50


@dataclass(frozen=True)
class RetrievedItem:
    """一条检索结果。只保留指标需要的字段，使 engine 层不依赖 HTTP DTO。"""

    id: int | None
    content: str
    score: float = 0.0

    @property
    def content_hash(self) -> str:
        return memory_content_hash(self.content)

    @classmethod
    def from_search_item(cls, item: Any) -> RetrievedItem:
        """由 connector 的 ``SearchItem`` 构造（鸭子类型，避免循环导入）。"""
        return cls(id=item.id, content=item.content, score=item.score)


@dataclass
class RetrievalCaseResult:
    """单个 query case 的检索评测结果（可追溯到 case 级）。"""

    query_id: str
    k: int
    recall_at_k: float
    precision_at_k: float
    hit_at_1: float
    reciprocal_rank: float
    ndcg_at_k: float
    #: 该 case 是否有可用答案。False 表示 ground truth 缺失，聚合时应排除——
    #: 否则会把「数据问题」算成「模型问题」。
    answerable: bool
    #: 实际采用的匹配口径，落库后用于解释指标（换口径后指标不可直接比较）
    match_mode: str
    relevant_total: int
    retrieved_hashes: list[str] = field(default_factory=list)
    relevant_hashes: list[str] = field(default_factory=list)
    #: 命中 / 漏召回 / 多召回，供排障直接定位
    matched_hashes: list[str] = field(default_factory=list)
    missing_hashes: list[str] = field(default_factory=list)
    spurious_hashes: list[str] = field(default_factory=list)

    #: 落进 ``eval_case_results.metric_values`` 的指标名。
    #: 与 ``as_detail()`` 里其余字段（标识符列表、口径、可评测性）的分工是刻意的：
    #: ``metric_values`` 是**可聚合的数值**，``detail`` 是**解释这些数值的上下文**。
    #: 把 matched/missing 这类列表塞进 metric_values 会让「按指标值过滤」变得没法写。
    METRIC_KEYS = (
        "recall_at_k",
        "precision_at_k",
        "hit_at_1",
        "reciprocal_rank",
        "ndcg_at_k",
    )

    def as_metric_values(self) -> dict[str, float]:
        """落进 ``eval_case_results.metric_values`` 的数值指标。"""
        return {
            "recall_at_k": self.recall_at_k,
            "precision_at_k": self.precision_at_k,
            "hit_at_1": self.hit_at_1,
            "reciprocal_rank": self.reciprocal_rank,
            "ndcg_at_k": self.ndcg_at_k,
        }

    def as_detail(self) -> dict[str, Any]:
        """落库用的明细（与 ``eval_case_results`` 的粒度对应）。"""
        return {
            "query_id": self.query_id,
            "k": self.k,
            "answerable": self.answerable,
            "match_mode": self.match_mode,
            "relevant_total": self.relevant_total,
            "recall_at_k": self.recall_at_k,
            "precision_at_k": self.precision_at_k,
            "hit_at_1": self.hit_at_1,
            "reciprocal_rank": self.reciprocal_rank,
            "ndcg_at_k": self.ndcg_at_k,
            "matched": self.matched_hashes,
            "missing": self.missing_hashes,
            "spurious": self.spurious_hashes,
        }


#: 匹配口径标签，落库以便解释指标
MATCH_BY_CONTENT = "content_hash"
MATCH_BY_ID = "memory_id"
MATCH_NONE = "none"


def _build_relevant(ground_truth: dict[str, Any]) -> tuple[set[str], str]:
    """把 ground truth 投影成相关标识符集合，并返回所用口径。

    优先内容哈希（可复现），仅在没有内容时才退回 id。
    """
    contents = [
        content
        for content in (ground_truth.get("relevant_memory_contents") or [])
        if isinstance(content, str) and content.strip()
    ]
    if contents:
        return {f"h:{memory_content_hash(content)}" for content in contents}, MATCH_BY_CONTENT

    ids = [
        memory_id
        for memory_id in (ground_truth.get("relevant_memory_ids") or [])
        if isinstance(memory_id, int) and not isinstance(memory_id, bool)
    ]
    if ids:
        return {f"i:{memory_id}" for memory_id in ids}, MATCH_BY_ID

    return set(), MATCH_NONE


def _project(item: RetrievedItem, index: int) -> tuple[str, str]:
    """把一条检索结果投影成 (内容标识符, id 标识符)。

    返回两个候选标识符而非单个，是为了让「按内容」与「按 id」两种口径都能直接
    做集合成员判断，而不必为每种口径各写一套命中逻辑。
    """
    id_token = f"i:{item.id}" if item.id is not None else f"i:none-{index}"
    return f"h:{item.content_hash}", id_token


class RetrievalEvaluator:
    """维度②的评测器。

    ``k`` 必须记进结果：手册的判定基线明确绑定 K=5（Recall@5 ≥ 改造前基线），
    不同 K 的 Recall 不可直接比较。
    """

    def __init__(self, *, k: int = DEFAULT_K) -> None:
        if k <= 0:
            raise ValueError(f"k 必须为正数，实际: {k}")
        self._k = k

    @property
    def k(self) -> int:
        return self._k

    def evaluate_case(
        self,
        *,
        payload: dict[str, Any],
        ground_truth: dict[str, Any],
        retrieved: list[RetrievedItem],
    ) -> RetrievalCaseResult:
        """评测单个 query case。"""
        query_id = str(payload.get("query_id") or "")
        relevant, match_mode = _build_relevant(ground_truth)

        retrieved_hashes = [item.content_hash for item in retrieved]

        if not relevant:
            # ground truth 缺失：不是「指标为 0」，而是「这条 case 无法评测」。
            return RetrievalCaseResult(
                query_id=query_id,
                k=self._k,
                recall_at_k=0.0,
                precision_at_k=0.0,
                hit_at_1=0.0,
                reciprocal_rank=0.0,
                ndcg_at_k=0.0,
                answerable=False,
                match_mode=MATCH_NONE,
                relevant_total=0,
                retrieved_hashes=retrieved_hashes[:_DETAIL_LIMIT],
            )

        # 投影：命中项的标识符取自 relevant，未命中项给一个必然不撞的哨兵。
        projected: list[str] = []
        hit_flags: list[bool] = []
        for index, item in enumerate(retrieved):
            content_token, id_token = _project(item, index)
            token = content_token if content_token in relevant else id_token
            is_hit = token in relevant
            projected.append(token if is_hit else f"miss#{index}")
            hit_flags.append(is_hit)

        # 全部指标直接复用 metrics 的纯函数——不在此处重写公式。
        recall = recall_at_k(projected, relevant, self._k)
        precision = precision_at_k(projected, relevant, self._k)
        top1 = hit_at_1(projected, relevant)
        rr = reciprocal_rank(projected, relevant)
        ndcg = ndcg_at_k(projected, relevant, self._k)

        matched = [retrieved_hashes[index] for index, hit in enumerate(hit_flags) if hit]
        missing = sorted(
            token.split(":", 1)[1] for token in relevant if token not in set(projected)
        )
        spurious = [
            retrieved_hashes[index]
            for index in range(min(len(retrieved), self._k))
            if projected[index] not in relevant
        ]

        return RetrievalCaseResult(
            query_id=query_id,
            k=self._k,
            recall_at_k=recall,
            precision_at_k=precision,
            hit_at_1=top1,
            reciprocal_rank=rr,
            ndcg_at_k=ndcg,
            answerable=True,
            match_mode=match_mode,
            relevant_total=len(relevant),
            retrieved_hashes=retrieved_hashes[:_DETAIL_LIMIT],
            relevant_hashes=sorted(token.split(":", 1)[1] for token in relevant),
            matched_hashes=matched[:_DETAIL_LIMIT],
            missing_hashes=missing[:_DETAIL_LIMIT],
            spurious_hashes=spurious[:_DETAIL_LIMIT],
        )

    def aggregate(self, results: list[RetrievalCaseResult]) -> dict[str, float]:
        """把一个 run 内所有 case 的结果聚合成维度级指标。

        **不可评测的 case 被排除在分母之外**（``answerable=False``）：
        把「没有 ground truth」计入分母会把数据问题伪装成模型退步。
        但排除数量必须能看出来——调用方应同时记录总 case 数。
        """
        usable = [result for result in results if result.answerable]
        if not usable:
            return {
                "case_count": 0.0,
                "recall_at_k": 0.0,
                "precision_at_k": 0.0,
                "hit_at_1": 0.0,
                "mrr": 0.0,
                "ndcg_at_k": 0.0,
            }

        return {
            "case_count": float(len(usable)),
            "recall_at_k": sum(r.recall_at_k for r in usable) / len(usable),
            "precision_at_k": sum(r.precision_at_k for r in usable) / len(usable),
            "hit_at_1": sum(r.hit_at_1 for r in usable) / len(usable),
            # MRR 对 case 的 reciprocal_rank 取平均。刻意不用
            # mrr([(retrieved_hashes, matched_hashes)]) 那种写法：明细列表被
            # _DETAIL_LIMIT 截断，K 超过上限时会算错，而 rr 是逐 case 现算的精确值。
            "mrr": sum(r.reciprocal_rank for r in usable) / len(usable),
            "ndcg_at_k": sum(r.ndcg_at_k for r in usable) / len(usable),
        }

    @staticmethod
    def ndcg_denominator(k: int, relevant_total: int) -> float:
        """暴露 IDCG，便于测试直接验证归一化项（不必反推）。"""
        ideal_hits = min(relevant_total, k)
        return sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
