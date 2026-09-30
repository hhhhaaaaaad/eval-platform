"""维度④：注入质量（EP-9）。

对每个 ``query_to_memory`` case 的注入上下文做两层检查：注入是否**超预算**，
以及注入的内容是否**相关**（命中 ground truth 的相关记忆）。

**与维度② 共享一条匹配硬约束**：相关判定走 content 的 md5 哈希
（:func:`app.datasets.digest.memory_content_hash`），不依赖记忆 id。id 由 AgentWrite
侧 seed 产生、``reset`` 后会变，按 id 匹配会让指标跨 run 不可复现；按内容匹配则只要
语料一致结果就一致。

**为什么 evaluator 收「已解析好的内容列表」而不是原始 ``budgeted_ids``**：
``RetrieveContextResponse.budgeted_ids`` 只有 id、没有内容，而 id 会随 reset 变化，
无法据此做 contentHash 匹配。pipeline 在 seed 阶段已经拿到
``SeedResponse.content_to_id``（内容 → id），可以反查出 ``budgeted_ids`` 对应的内容。
让调用方先把 id 解析成内容列表再传入，evaluator 就保持**纯函数**：不依赖网络、
不依赖数据库，可以用普通单测穷举每一种边界（空注入 / 重复内容 / 恰好等于预算……）。

指标定义
--------
1. ``over_budget_rate`` —— ``token_count > inject_max_tokens`` 的 case 占比。
   **严格大于**才算超预算：恰好等于预算说明「正好用完、没超」，不该被记为超预算。
   ``inject_max_tokens`` 来自参数快照（``ParamsResponse.inject_max_tokens``），作为
   构造参数传入、**不硬编码**，并记进 case 结果——不同预算下的超预算率不可直接比较。
2. ``token_utilization`` —— ``token_count / inject_max_tokens`` 的均值。
   超预算率是 0/1 判定，看不出「预算用得多满」；一个「从不超预算」的系统可能只是
   「几乎没注入」。利用率补上这个维度，让「0 超预算 + 0 利用率」一眼被识破。
3. ``irrelevant_injection_rate`` —— 被注入记忆里，**不在相关集合内**的占比。
   分母是「实际注入的数量」（去重后），**不是 K**：只注入 2 条而 K=5 时，分母是 2。
   这与维度② 的 Precision@K（分母固定 K）刻意相反——K 是检索预算而非注入预算；
   注入阶段「没注满」是另一个问题，交给 ``token_utilization`` 单独暴露。

边界口径（均经过取舍，理由如下）
--------------------------------
- **ground truth 无相关记忆**：``answerable=False``，聚合时**只从 ``irrelevant_injection_rate``
  排除**。照抄维度② 的做法——把「标注缺失」这种数据问题算进相关性指标，会让它伪装成
  「注入质量退步」。但 ``over_budget_rate`` / ``token_utilization`` 不依赖 ground truth，
  对缺标注的 case 也排除等于丢弃有效观测：一个所有 case 都缺标注的 run 若一并排除，
  会报告「超预算率 = 0」，而这个 0 是「没数据」还是「真没超预算」从响应里看不出。
- **注入为空**（``budgeted_ids`` 为空 → 内容列表为空）：``irrelevant_injection_rate``
  记 **0**。逻辑上「没注入任何内容」确实「没注入无关内容」，记 0 而非「不可评测」；
  但这件事**必须能被看见**——此时 ``token_utilization`` 为 0，把「预算一条没用上」
  单独暴露出来。因此空注入仍是 ``answerable=True``：它是一个可观测的系统行为，
  不是数据问题。
- **重复内容**（同一记忆被注入两次）：**去重**。``content_to_id`` 是 dict，同一内容
  对应同一 id，重复注入只说明 Java 侧返回列表里有重复元素；若不去重，分母被注水、
  同一条无关记忆被数两遍，无关注入率失去意义。去重按 content 哈希、保留首次出现顺序。

两类指标的分母不同（重要，务必读）
----------------------------------
``over_budget_rate`` / ``token_utilization`` 的输入只有 ``token_count`` 与
``inject_max_tokens``，**不依赖 ground truth**；``irrelevant_injection_rate`` 依赖
ground truth 的相关集合。因此聚合时分母不同：预算指标覆盖**全部** case，相关性指标
只覆盖 ``answerable=True`` 的 case。理由有二：

- 依赖标注的指标：把「缺标注」算进分母，等于把「数据问题」伪装成「模型退步」。
- 不依赖标注的指标：把缺标注的 case 也排除，等于**丢弃有效观测**——一个所有 case 都
  缺标注的 run，若一并排除，会报告「超预算率 = 0」，而这个 0 是「没数据」还是
  「真没超预算」从响应里根本看不出。

``aggregate()`` 的返回字典用 ``case_count_total``（全部 case 数）与 ``case_count_scored``
（参与相关性指标的 case 数）把差异显式暴露，避免调用方误以为两个指标共享同一分母。

**关于 ``match_mode``**：维度② 有 ``MATCH_BY_CONTENT`` / ``MATCH_BY_ID`` 两种口径、
需要 ``match_mode`` 标注。注入维度只走 content 哈希一条路（evaluator 拿不到 id、
也不该依赖 id），不存在口径切换，故不设 ``match_mode``——常量标签不携带信息时，
加上只是仪式、不是实质。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.datasets.digest import memory_content_hash
from app.engine.metrics import mean, safe_ratio

#: 明细里最多保留多少条标识符，避免超长注入列表下 case detail 膨胀。
#: 超出部分只影响「留证」，不影响任何指标数值。
_DETAIL_LIMIT = 50


def _build_relevant_hashes(ground_truth: dict[str, Any]) -> set[str]:
    """把 ground truth 的相关记忆投影成 content 哈希集合。

    只走内容哈希一条路（可复现），不做 id 回退：注入 evaluator 收的是内容列表，
    拿不到 id，按 id 匹配既不可能也不可靠。
    """
    contents = [
        content
        for content in (ground_truth.get("relevant_memory_contents") or [])
        if isinstance(content, str) and content.strip()
    ]
    return {memory_content_hash(content) for content in contents}


def _dedup_injected_hashes(injected_contents: Sequence[str]) -> list[str]:
    """把注入内容去重后投影成 content 哈希列表，保留首次出现顺序。

    按 content 哈希去重而非按原始字符串去重：``memory_content_hash`` 会先 strip 首尾
    空白，两条「仅差首尾空白」的内容应视为同一条记忆，去重口径与匹配口径必须一致。
    """
    hashes: list[str] = []
    seen: set[str] = set()
    for content in injected_contents:
        if not isinstance(content, str) or not content.strip():
            continue
        content_hash = memory_content_hash(content)
        if content_hash not in seen:
            seen.add(content_hash)
            hashes.append(content_hash)
    return hashes


@dataclass
class InjectionCaseResult:
    """单个 query case 的注入评测结果（可追溯到 case 级）。"""

    query_id: str
    inject_max_tokens: int
    token_count: int
    #: ``token_count > inject_max_tokens``。严格大于——恰好等于预算不算超。
    over_budget: bool
    token_utilization: float
    #: 去重后实际注入的记忆数（``irrelevant_injection_rate`` 的分母）。
    injected_total: int
    irrelevant_total: int
    irrelevant_injection_rate: float
    #: 该 case 是否有可用答案。False 表示 ground truth 缺失，聚合时应排除——
    #: 否则会把「数据问题」算成「模型问题」。
    answerable: bool
    injected_hashes: list[str] = field(default_factory=list)
    relevant_hashes: list[str] = field(default_factory=list)
    irrelevant_hashes: list[str] = field(default_factory=list)

    #: 落进 ``eval_case_results.metric_values`` 的指标名。
    #: 与 ``as_detail()`` 里其余字段（标识符列表、可评测性）的分工见
    #: ``app.results.service``：可聚合的数值与解释它们的上下文分开存。
    METRIC_KEYS = (
        "token_utilization",
        "irrelevant_injection_rate",
    )

    def as_metric_values(self) -> dict[str, float]:
        """落进 ``eval_case_results.metric_values`` 的数值指标。

        ``over_budget`` 是布尔而非数值，故不进这里——它放进 ``detail``。
        想按它筛 case 的人用 ``detail.over_budget``：JSONB 的布尔字段照样能过滤，
        而把它转成 0/1 塞进数值列只会让「这个维度有哪些数值指标」变得含混。
        """
        return {
            "token_utilization": self.token_utilization,
            "irrelevant_injection_rate": self.irrelevant_injection_rate,
        }

    def as_detail(self) -> dict[str, Any]:
        """落库用的明细（与 ``eval_case_results`` 的粒度对应）。"""
        return {
            "query_id": self.query_id,
            "inject_max_tokens": self.inject_max_tokens,
            "token_count": self.token_count,
            "over_budget": self.over_budget,
            "token_utilization": self.token_utilization,
            "answerable": self.answerable,
            "injected_total": self.injected_total,
            "irrelevant_total": self.irrelevant_total,
            "irrelevant_injection_rate": self.irrelevant_injection_rate,
            "injected": self.injected_hashes,
            "relevant": self.relevant_hashes,
            "irrelevant": self.irrelevant_hashes,
        }


class InjectionEvaluator:
    """维度④的评测器。

    ``inject_max_tokens`` 必须记进结果：超预算率与利用率都依赖该预算，不同预算下
    这两个指标不可直接比较。它来自参数快照，故作为构造参数传入而非硬编码。
    """

    def __init__(self, *, inject_max_tokens: int) -> None:
        if inject_max_tokens <= 0:
            raise ValueError(f"inject_max_tokens 必须为正数，实际: {inject_max_tokens}")
        self._inject_max_tokens = inject_max_tokens

    @property
    def inject_max_tokens(self) -> int:
        return self._inject_max_tokens

    def evaluate_case(
        self,
        *,
        payload: dict[str, Any],
        ground_truth: dict[str, Any],
        token_count: int,
        injected_contents: Sequence[str],
    ) -> InjectionCaseResult:
        """评测单个 query case。

        ``injected_contents`` 是 ``budgeted_ids`` 经 ``content_to_id`` 反查出的内容
        列表（由调用方解析好传入），``token_count`` 是 ``RetrieveContextResponse``
        的 ``token_count``。
        """
        query_id = str(payload.get("query_id") or "")
        relevant = _build_relevant_hashes(ground_truth)

        over_budget = token_count > self._inject_max_tokens
        token_utilization = token_count / self._inject_max_tokens

        if not relevant:
            # ground truth 缺失：不是「指标为 0」，而是「这条 case 无法评测」。
            return InjectionCaseResult(
                query_id=query_id,
                inject_max_tokens=self._inject_max_tokens,
                token_count=token_count,
                over_budget=over_budget,
                token_utilization=token_utilization,
                injected_total=0,
                irrelevant_total=0,
                irrelevant_injection_rate=0.0,
                answerable=False,
            )

        injected_hashes = _dedup_injected_hashes(injected_contents)
        irrelevant_hashes = [content_hash for content_hash in injected_hashes if content_hash not in relevant]

        injected_total = len(injected_hashes)
        irrelevant_total = len(irrelevant_hashes)
        # 注入为空时分母为 0，safe_ratio 约定记 0（「没有注入无关内容」成立）。
        irrelevant_rate = safe_ratio(irrelevant_total, injected_total)

        return InjectionCaseResult(
            query_id=query_id,
            inject_max_tokens=self._inject_max_tokens,
            token_count=token_count,
            over_budget=over_budget,
            token_utilization=token_utilization,
            injected_total=injected_total,
            irrelevant_total=irrelevant_total,
            irrelevant_injection_rate=irrelevant_rate,
            answerable=True,
            injected_hashes=injected_hashes[:_DETAIL_LIMIT],
            relevant_hashes=sorted(relevant)[:_DETAIL_LIMIT],
            irrelevant_hashes=irrelevant_hashes[:_DETAIL_LIMIT],
        )

    def aggregate(self, results: list[InjectionCaseResult]) -> dict[str, float]:
        """把一个 run 内所有 case 的结果聚合成维度级指标。

        **两类指标的分母不同**（见模块 docstring「两类指标的分母不同」一节）：

        - ``over_budget_rate`` / ``token_utilization`` 不依赖 ground truth，覆盖
          **全部** case（分母 ``case_count_total``）。把缺标注的 case 也排除等于丢弃
          有效观测——一个所有 case 都缺标注的 run 会因此报告一个无法区分「没数据」
          还是「真没超预算」的 0。
        - ``irrelevant_injection_rate`` 依赖 ground truth，只覆盖 ``answerable=True``
          的 case（分母 ``case_count_scored``）。把「没有 ground truth」计入相关性
          分母会把数据问题伪装成模型退步。

        两个 case 数都返回（``case_count_total`` / ``case_count_scored``），差异必须
        从返回值里看得出来，不能只靠注释。
        """
        total = len(results)
        scored = [result for result in results if result.answerable]

        return {
            "case_count_total": float(total),
            "case_count_scored": float(len(scored)),
            "over_budget_rate": safe_ratio(
                sum(1 for result in results if result.over_budget), total
            ),
            "token_utilization": mean([result.token_utilization for result in results]),
            "irrelevant_injection_rate": mean(
                [result.irrelevant_injection_rate for result in scored]
            ),
        }
