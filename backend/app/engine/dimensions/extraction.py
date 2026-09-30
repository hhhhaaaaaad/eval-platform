"""维度①：抽取质量（EP-9）。

对每个 ``conversation_to_memory`` case 的抽取结果（LLM 从对话中抽出的候选记忆列表）
与 ground truth 记忆做对齐，算四类指标：

1. **提取 P / R / F1**（``precision`` / ``recall`` / ``f1``）——抽取内容集合 vs
   期望记忆内容集合，衡量「抽得准不准、抽得全不全」；
2. **重复抽取率**（``duplicate_extraction_rate``）——同一次抽取里内容互为重复的
   占比，衡量「同一句话抽成两条」的冗余倾向；
3. **错误归属率**（``attribution_error_rate``）——命中了 ground truth、但
   ``attributed_to`` 与期望不一致的占比（「抽对了内容、搞错了来源」）；
4. **低价值写入率**（``low_value_write_rate``）——``confidence`` 低于阈值的候选占比。

**匹配方式沿用维度②③④⑤的同一硬约束**：主匹配走 content 的 md5 哈希
（:func:`app.datasets.digest.memory_content_hash`），不依赖记忆 id。id 由 AgentWrite
侧 seed 时产生、``reset`` 后会变，按 id 匹配会让指标跨 run 不可复现；按内容匹配则
只要语料一致结果就一致。抽取维度连「id 兜底」都没有——extract 是纯抽取、压根不返回
记忆 id，候选只能靠 ``content`` 对齐，天然走 contentHash。

P/R/F1 的口径：集合语义（去重后比对）
------------------------------------
``precision = 命中数 / 去重候选数``、``recall = 命中数 / 去重 ground truth 数``。
**必须先按 contentHash 去重**——不去重的话 Recall 会失真：候选 ``[A, A]`` 对
ground truth ``{A}`` 时，把两条 ``A`` 都算命中会得到 Recall = 2/1 = 2.0，直接越界。
去重后 Recall 恰好封顶 1.0。重复条目**不算进 P/R**，因为它由 ``duplicate_extraction_rate``
单独衡量——两套指标各管一件事，不去互相污染。

由于抽取没有 top-K（返回多少条就是多少条，不存在「取前 K」），``metrics`` 里的
``precision_at_k`` / ``recall_at_k`` 是固定分母 K 的版本、不适用；此处 precision 的
分母是「去重后的实际候选数」（与维度④ ``irrelevant_injection_rate`` 的分母口径一致：
「没注满 / 没抽满」是另一个问题，不由 precision 承担），recall 的分母是「ground truth
总数」。``f1`` 直接用 :func:`app.engine.metrics.f1`（0/0 约定为 0），不在此重写。

重复抽取率：``1 - 去重数 / 候选总数``
-----------------------------------
分母是**候选总数**（过滤掉空白内容的候选后，含重复条目），分子是「多出来的」冗余
条目数（同一内容第一次出现算有效、第二次起算冗余）。于是 ``[A, A] → 1/2``、
``[A, A, A] → 2/3``、``[A, A, B] → 1/3``。这正好对应「同一句话抽成两条」的直觉：
一条句子抽出两条，其中一条是冗余，冗余率 1/2。判定重复用 contentHash——``memory_content_hash``
先 strip 首尾空白，两条「仅差首尾空白」的内容视为同一条，与匹配口径一致。

错误归属率：分母只在「gt 侧给了 attributed_to」的命中候选上算
-------------------------------------------------------------
这是本维度最容易做错的分母口径，分四档想清楚：

====================  ============  ==================================================
gt.attributed_to      候选.attributed_to  判定
====================  ============  ==================================================
缺                    任意          **不计入**：gt 没给期望，无可比对象。算进分母等于把
                                   「标注缺字段」这种数据问题伪装成「归属错了」。
给                    缺（None/空白）  **计入、算错**：gt 明确说这是 user 的偏好，候选却
                                   没带来源，属抽取缺陷（漏带了该带的字段）。
给                    给且一致      正确。
给                    给且不一致     错误（抽对了内容、搞错了来源）。
====================  ============  ==================================================

为什么「gt 缺 → 排除」而「候选缺 → 算错」不对称：gt 是期望，候选是产出。期望没指定
来源，那就不存在「错」这回事；期望指定了来源而产出没给，是产出没达标。这个不对称与
recall 对「漏抽」的处理同构——gt 有 10 条、只抽出 5 条时 recall=0.5，漏掉的算进分母
不算进分子；归属指标同理，漏带的来源算进分母不算进分子。

低价值写入率：``confidence < threshold``（严格小于）
----------------------------------------------------
阈值 ``confidence_threshold`` 来自参数快照，作为构造参数传入、**不硬编码**，并记进
case 结果——不同阈值下的低价值率不可直接比较。**严格小于**才算低价值：恰好等于阈值
说明「正好卡线、没低于」最低要求，不该记为低价值（与维度④超预算「严格大于才算超」同一套边界取舍）。

**confidence 缺失怎么办**：缺失既不等于「低置信度」（不能算进分子，凭空抬高低价值率），
也不能当作「高置信度放过去」（不能只进分母、不进分子，那等于默认它可信）。所以缺失的
候选**从低价值率的分母与分子里一并排除**，转而用 ``confidence_missing_total`` 计数单独
暴露——一个「从不给 confidence」的系统，低价值率是 0，但缺失计数会让这件事一眼被看见，
而不是被当成「全部高置信度」。

UPDATE 操作：不参与判定，匹配纯看内容
-----------------------------------
候选里的 ``operation=UPDATE``（更新已有记忆而非新增）**不改变 P/R 匹配**——匹配只看
contentHash。理由：ground truth 是 operation 无关的（它列的是「这段对话后应该存在的
记忆」，不区分「该新建还是该更新」），不存在 create/update 轴可供打分；operation 是
写侧策略（Java 侧决定落库时是 INSERT 还是 UPDATE），不是「抽得准不准」的信号。把
UPDATE 候选按内容正常匹配，抽对了内容就算命中，抽错了内容就不命中——与 ADD 完全同法。

ground truth 为空 → 不可评测（``answerable=False``）
----------------------------------------------------
gt 无记忆时 P/R/F1 与归属率无定义，聚合时**从这几个指标的分母排除**（照抄维度②④）。
但 ``conversation_to_memory`` 的 schema 校验要求 ``ground_truth_memories`` 非空，所以
这条路径理论上不可达——**仍然防御的理由有二**：(1) 校验发生在导入期，历史/旁路写入的
case 可能未经过校验；(2) 更实际的是 recall 的分母是 ``len(gt)``，不防会直接
``ZeroDivisionError`` 崩掉整个 run，而不是优雅地标记「这条不可评测」。防御不是仪式，
是防 crash。

候选为空 → 各指标取值
--------------------
什么都没抽到是**可观测的系统行为，不是数据问题**，故 ``answerable=True``（照抄维度④
「空注入是 answerable」的取舍）。此时 P=0（``safe_ratio(0, 0)=0``）、R=0（命中 0/gt 数）、
F1=0（``f1(0,0)=0``）、重复率=0、归属率=0、低价值率=0——「抽不出东西」本身就是要被
precision=0 记录的失败，而不是被排除在外。

两类指标的分母不同（同维度④）
----------------------------
``duplicate_extraction_rate`` / ``low_value_write_rate`` 只依赖候选本身、**不依赖 gt**，
覆盖**全部** case；``precision`` / ``recall`` / ``f1`` / ``attribution_error_rate`` 依赖
gt，只覆盖 ``answerable=True`` 的 case。``aggregate()`` 用 ``case_count_total`` /
``case_count_scored`` 显式暴露这个差异，避免调用方误以为两组指标共享同一分母。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.datasets.digest import memory_content_hash
from app.engine.metrics import f1, mean, safe_ratio

#: 明细里最多保留多少条内容哈希，避免超长候选列表下 case detail 膨胀。
#: 超出部分只影响「留证」，不影响任何指标数值。
_DETAIL_LIMIT = 50


def _normalize_attr(attributed_to: str | None) -> str | None:
    """归属归一化：去首尾空白 + 小写，使标注与 Java 侧的写法可比。

    空白字符串与 ``None`` 同义（都表示「未给归属」），返回 ``None``。
    """
    if not isinstance(attributed_to, str):
        return None
    value = attributed_to.strip().lower()
    return value if value else None


def _build_ground_truth(
    ground_truth: dict[str, Any],
) -> tuple[list[str], dict[str, str | None]]:
    """把 ground truth 的 ``ground_truth_memories`` 投影成 (内容哈希列表, 哈希→归属映射)。

    只走内容哈希一条路（可复现），不做 id 回退：抽取维度连 id 都拿不到。按 contentHash
    去重（同一内容标注两条视为同一条记忆），归属取**首次出现**的那条——同一内容两条标注
    归属不一致是标注自身的歧义，这里取先见到的、不猜。
    """
    memories = ground_truth.get("ground_truth_memories") or []
    hashes: list[str] = []
    seen: set[str] = set()
    attr_map: dict[str, str | None] = {}
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        content = memory.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        content_hash = memory_content_hash(content)
        if content_hash not in seen:
            seen.add(content_hash)
            hashes.append(content_hash)
            attr_map[content_hash] = _normalize_attr(memory.get("attributed_to"))
    return hashes, attr_map


@dataclass(frozen=True)
class ExtractedCandidate:
    """一条抽取候选。只保留指标需要的字段，使 engine 层不依赖 HTTP DTO。

    ``operation`` 一并保留但不参与任何评分：匹配纯看 ``content``（见模块 docstring
    「UPDATE 操作」一节），保留它只为让投影忠实于上游，便于将来按 operation 分桶统计。
    """

    content: str
    attributed_to: str | None = None
    operation: str | None = None
    confidence: float | None = None

    @classmethod
    def from_connector_candidate(cls, candidate: Any) -> ExtractedCandidate:
        """由 connector 的 ``ExtractCandidate`` 构造（鸭子类型，避免循环导入）。"""
        return cls(
            content=candidate.content,
            attributed_to=candidate.attributed_to,
            operation=candidate.operation,
            confidence=candidate.confidence,
        )


@dataclass
class ExtractionCaseResult:
    """单个 conversation case 的抽取评测结果（可追溯到 case 级）。"""

    dialogue_id: str
    confidence_threshold: float
    #: 提取 P / R / F1（依赖 ground truth，仅在 answerable=True 时有效）。
    precision: float
    recall: float
    f1: float
    #: 不依赖 ground truth 的指标（覆盖全部 case）。
    duplicate_extraction_rate: float
    low_value_write_rate: float
    #: 依赖 ground truth 的指标（仅在 answerable=True 时有效）。
    attribution_error_rate: float
    #: 该 case 是否有可用答案。False 表示 ground truth 缺失，聚合时应从
    #: precision/recall/f1/attribution_error_rate 的分母排除——否则会把
    #: 「数据问题」算成「模型问题」。
    answerable: bool
    #: 计数（traceability）：候选总数（含重复、去空白）、去重候选数、冗余条目数、
    #: gt 记忆数、命中的去重候选数。
    candidate_total: int
    distinct_total: int
    duplicate_total: int
    gt_total: int
    matched_total: int
    #: 归属指标的分母 / 分子（见模块 docstring「错误归属率」）。
    attribution_scored_total: int
    attribution_error_total: int
    #: 低价值指标：低于阈值的候选数、有 confidence 的候选数（分母）、缺失数。
    low_value_total: int
    confidence_scored_total: int
    confidence_missing_total: int
    #: 明细：去重后的候选哈希、gt 哈希、命中 / 漏抽 / 多抽，供排障直接定位。
    extracted_hashes: list[str] = field(default_factory=list)
    gt_hashes: list[str] = field(default_factory=list)
    matched_hashes: list[str] = field(default_factory=list)
    missing_hashes: list[str] = field(default_factory=list)
    spurious_hashes: list[str] = field(default_factory=list)

    #: 落进 ``eval_case_results.metric_values`` 的指标名。
    #: 与 ``as_detail()`` 里其余字段（计数、哈希列表、可评测性）的分工是刻意的：
    #: ``metric_values`` 是**可聚合的数值**，``detail`` 是**解释这些数值的上下文**。
    METRIC_KEYS = (
        "precision",
        "recall",
        "f1",
        "duplicate_extraction_rate",
        "attribution_error_rate",
        "low_value_write_rate",
    )

    def as_metric_values(self) -> dict[str, float]:
        """落进 ``eval_case_results.metric_values`` 的数值指标。"""
        return {
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "duplicate_extraction_rate": self.duplicate_extraction_rate,
            "attribution_error_rate": self.attribution_error_rate,
            "low_value_write_rate": self.low_value_write_rate,
        }

    def as_detail(self) -> dict[str, Any]:
        """落库用的明细（与 ``eval_case_results`` 的粒度对应）。"""
        return {
            "dialogue_id": self.dialogue_id,
            "confidence_threshold": self.confidence_threshold,
            "answerable": self.answerable,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "duplicate_extraction_rate": self.duplicate_extraction_rate,
            "attribution_error_rate": self.attribution_error_rate,
            "low_value_write_rate": self.low_value_write_rate,
            "candidate_total": self.candidate_total,
            "distinct_total": self.distinct_total,
            "duplicate_total": self.duplicate_total,
            "gt_total": self.gt_total,
            "matched_total": self.matched_total,
            "attribution_scored_total": self.attribution_scored_total,
            "attribution_error_total": self.attribution_error_total,
            "low_value_total": self.low_value_total,
            "confidence_scored_total": self.confidence_scored_total,
            "confidence_missing_total": self.confidence_missing_total,
            "extracted": self.extracted_hashes,
            "gt": self.gt_hashes,
            "matched": self.matched_hashes,
            "missing": self.missing_hashes,
            "spurious": self.spurious_hashes,
        }


class ExtractionEvaluator:
    """维度①的评测器。

    ``confidence_threshold`` 必须记进结果：低价值写入率依赖它，不同阈值下该指标不可
    直接比较。它来自参数快照（``ParamsResponse.min_confidence``），故作为构造参数传入
    而非硬编码。
    """

    def __init__(self, *, confidence_threshold: float) -> None:
        threshold = float(confidence_threshold)
        if not math.isfinite(threshold) or threshold < 0.0:
            raise ValueError(f"confidence_threshold 必须为非负有限数值，实际: {confidence_threshold!r}")
        self._confidence_threshold = threshold

    @property
    def confidence_threshold(self) -> float:
        return self._confidence_threshold

    def evaluate_case(
        self,
        *,
        payload: dict[str, Any],
        ground_truth: dict[str, Any],
        candidates: list[ExtractedCandidate],
    ) -> ExtractionCaseResult:
        """评测单个 conversation case。

        ``candidates`` 是 ``/api/v1/eval/extract`` 返回的候选列表，由调用方把 connector
        的 ``ExtractCandidate`` 转成 :class:`ExtractedCandidate` 后传入。
        """
        dialogue_id = str(payload.get("dialogue_id") or "")
        gt_hashes, gt_attr = _build_ground_truth(ground_truth)

        # 遍历候选：过滤空白内容，统计总数、去重哈希（保序）、低价值（逐候选，含重复），
        # 以及去重后每条 distinct 候选的归属（首次出现为准）。
        extracted_hashes: list[str] = []
        seen: set[str] = set()
        distinct_attr: dict[str, str | None] = {}
        candidate_total = 0
        low_value_total = 0
        confidence_scored_total = 0
        confidence_missing_total = 0

        for candidate in candidates:
            content = candidate.content
            if not isinstance(content, str) or not content.strip():
                continue
            candidate_total += 1
            content_hash = memory_content_hash(content)
            if content_hash not in seen:
                seen.add(content_hash)
                extracted_hashes.append(content_hash)
                distinct_attr[content_hash] = _normalize_attr(candidate.attributed_to)

            confidence = candidate.confidence
            if confidence is None:
                confidence_missing_total += 1
            else:
                confidence_scored_total += 1
                if confidence < self._confidence_threshold:
                    low_value_total += 1

        distinct_total = len(extracted_hashes)
        duplicate_total = candidate_total - distinct_total
        duplicate_rate = safe_ratio(duplicate_total, candidate_total)
        low_value_rate = safe_ratio(low_value_total, confidence_scored_total)

        if not gt_hashes:
            # ground truth 缺失：不是「指标为 0」，而是「这条 case 无法评测」。
            # 但仍保留不依赖 gt 的两个率（它们覆盖全部 case，见聚合）。
            return ExtractionCaseResult(
                dialogue_id=dialogue_id,
                confidence_threshold=self._confidence_threshold,
                precision=0.0,
                recall=0.0,
                f1=0.0,
                duplicate_extraction_rate=duplicate_rate,
                low_value_write_rate=low_value_rate,
                attribution_error_rate=0.0,
                answerable=False,
                candidate_total=candidate_total,
                distinct_total=distinct_total,
                duplicate_total=duplicate_total,
                gt_total=0,
                matched_total=0,
                attribution_scored_total=0,
                attribution_error_total=0,
                low_value_total=low_value_total,
                confidence_scored_total=confidence_scored_total,
                confidence_missing_total=confidence_missing_total,
                extracted_hashes=extracted_hashes[:_DETAIL_LIMIT],
            )

        gt_set = set(gt_hashes)
        extracted_set = set(extracted_hashes)
        matched_hashes = [content_hash for content_hash in extracted_hashes if content_hash in gt_set]
        missing_hashes = [content_hash for content_hash in gt_hashes if content_hash not in extracted_set]
        spurious_hashes = [content_hash for content_hash in extracted_hashes if content_hash not in gt_set]

        hits = len(matched_hashes)
        precision = safe_ratio(hits, distinct_total)
        recall = hits / len(gt_hashes)
        f1_score = f1(precision, recall)

        # 归属：只在「命中且 gt 侧给了 attributed_to」的去重候选中算。
        attribution_scored_total = 0
        attribution_error_total = 0
        for content_hash in matched_hashes:
            gt_attr_val = gt_attr.get(content_hash)
            if gt_attr_val is None:
                continue
            attribution_scored_total += 1
            if distinct_attr.get(content_hash) != gt_attr_val:
                attribution_error_total += 1
        attribution_rate = safe_ratio(attribution_error_total, attribution_scored_total)

        return ExtractionCaseResult(
            dialogue_id=dialogue_id,
            confidence_threshold=self._confidence_threshold,
            precision=precision,
            recall=recall,
            f1=f1_score,
            duplicate_extraction_rate=duplicate_rate,
            low_value_write_rate=low_value_rate,
            attribution_error_rate=attribution_rate,
            answerable=True,
            candidate_total=candidate_total,
            distinct_total=distinct_total,
            duplicate_total=duplicate_total,
            gt_total=len(gt_hashes),
            matched_total=hits,
            attribution_scored_total=attribution_scored_total,
            attribution_error_total=attribution_error_total,
            low_value_total=low_value_total,
            confidence_scored_total=confidence_scored_total,
            confidence_missing_total=confidence_missing_total,
            extracted_hashes=extracted_hashes[:_DETAIL_LIMIT],
            gt_hashes=gt_hashes[:_DETAIL_LIMIT],
            matched_hashes=matched_hashes[:_DETAIL_LIMIT],
            missing_hashes=missing_hashes[:_DETAIL_LIMIT],
            spurious_hashes=spurious_hashes[:_DETAIL_LIMIT],
        )

    def aggregate(self, results: list[ExtractionCaseResult]) -> dict[str, float]:
        """把一个 run 内所有 case 的结果聚合成维度级指标。

        **两类指标的分母不同**（见模块 docstring「两类指标的分母不同」一节）：

        - ``precision`` / ``recall`` / ``f1`` / ``attribution_error_rate`` 依赖
          ground truth，只覆盖 ``answerable=True`` 的 case（分母 ``case_count_scored``）。
          把「没有 ground truth」计入分母会把数据问题伪装成模型退步。
        - ``duplicate_extraction_rate`` / ``low_value_write_rate`` 不依赖 ground truth，
          覆盖**全部** case（分母 ``case_count_total``）。把缺标注的 case 也排除等于
          丢弃有效观测——一个所有 case 都缺标注的 run 会因此报告一个无法区分「没数据」
          还是「真没重复/真没低价值」的 0。

        两个 case 数都返回（``case_count_total`` / ``case_count_scored``），差异必须
        从返回值里看得出来，不能只靠注释。
        """
        total = len(results)
        scored = [result for result in results if result.answerable]

        return {
            "case_count_total": float(total),
            "case_count_scored": float(len(scored)),
            "precision": mean([result.precision for result in scored]),
            "recall": mean([result.recall for result in scored]),
            "f1": mean([result.f1 for result in scored]),
            "attribution_error_rate": mean([result.attribution_error_rate for result in scored]),
            "duplicate_extraction_rate": mean([result.duplicate_extraction_rate for result in results]),
            "low_value_write_rate": mean([result.low_value_write_rate for result in results]),
        }


__all__ = [
    "ExtractedCandidate",
    "ExtractionCaseResult",
    "ExtractionEvaluator",
]
