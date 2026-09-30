"""维度⑤：治理质量（EP-9）。

只评**决策质量**，不评**执行效果**。原因是一个刻意的范围决定，不是遗漏：

Java 侧 ``/api/v1/eval/governance/replay`` 是**只读的**——它只跑 compute 层、
绝不落库，平台拿不到「执行后」的状态。``/api/v1/eval/**`` 下也没有 apply/undo
端点，要做到「撤销成功率」必须新增 Java 侧端点，超出本次范围。因此本维度
只能对齐「期望决策」与「实际决策」，衡量决策本身对不对。

**决策如何对齐（不依赖 id）**：期望决策用 ``memory_contents``（内容）标注，
实际决策用 ``items[].memoryId`` / ``mergedIntoId`` 定位。id 由 AgentWrite 侧 seed
产生、``reset`` 后会变，按 id 比对会让指标不可复现。所以与维度②同法：**投影到
内容哈希空间**（:func:`app.datasets.digest.memory_content_hash`）。evaluator 接收
「已解析好的 id→内容映射」（``dict[int, str]``，由集成方从 seed 的 ``contentToId``
反向得到），把实际决策的每个 id 解析成内容再哈希，与期望的内容哈希直接比对。
映射里解析不到的 id 退回 ``i:<id>`` 标识符（与维度②的 ``i:`` 前缀同源）——它
与任何内容哈希都不会撞，因此「解析不到」只会被判为「不匹配」，绝不会被误判成
「匹配」。这是保守且正确的：拿不到内容就无法确认决策正确，宁可记错也不假装对。

**多决策样本怎么算（逐决策 P/R，而非整体 0/1）**：治理的两类错法代价不对称——
「该动的没动」（漏判）是风险外溢（重复记忆没合并、幻觉没隔离），「不该动的动了」
（误伤）是数据损坏（把真实记忆归档/隔离了）。整体 0/1 把这两种完全相反的失败
抹成同一个数。所以本维度在**决策粒度**上做签名比对：每条期望决策投影成一个
签名 ``(action, 源记忆集合, 合并目标)``，与实际决策的签名做多重集匹配，得到
「匹配 / 漏判 / 误伤」三档计数，再汇成两个率：

- ``missed_action_rate``（漏判率）= 漏判决策数 / 期望决策数；
- ``false_action_rate``（误伤率）= 误伤决策数 / 实际决策数。

**「不一致率」与漏判/误伤的关系**：三个 ``wrong_*_rate`` 是「这一条 case 是否
有任一决策不匹配」的 0/1，聚合后即「有错误的 case 占比」；``missed_action_rate`` /
``false_action_rate`` 是决策粒度的占比，二者分工不同——前者回答「多少 case 是
干净的」，后者回答「错在哪个方向」。负样本（空期望）正是靠 ``false_action_rate``
被单独暴露出来的：空期望 + 空实际 → 全 0；空期望 + 乱动 → 漏判 0、误伤 1。

**合并目标如何表达（``merged_into_id`` 的比对）**：期望侧用
``GovernanceDecisionExpectation`` 的显式字段 ``merged_into_content``（内容）表达
「合并到哪条」，而不是约定 ``memory_contents`` 的某个位置（如「末位是幸存者」）。
位置约定看着省事，实则是隐患：JSON 数组顺序由人写时不可靠，写反了不会报任何错，
只会让 ``wrong_merge_rate`` 静默地算在另一批样本上——指标看起来正常，衡量的却是
别的东西；而且约定只存在于文档里，schema 校验拦不住任何东西。显式字段让「哪条
是目标」成为 schema 可校验的事实：MERGE 缺目标、非 MERGE 多写目标都会在导入期
被 :mod:`app.datasets.schemas` 拦下。因此 ``memory_contents`` 只承载**被作用的
源记忆**，目标单独走 ``merged_into_content``；解析后二者分别投影成签名里的
``source_tokens`` 与 ``target_token``。

**期望侧为何忽略 ``memory_ids``**：``GovernanceDecisionExpectation`` 同时有
``memory_ids`` 与 ``memory_contents``。本维度**只用 ``memory_contents``**——
id 不可复现，用 id 标注会让同一份评测集跑两次得出不同分数。``memory_ids`` 是
给「已经解析到当次 run 真实 id」的场景预留的，治理评测不使用它。

**期望为空（负样本）的取值**（本维度最重要的边界情况）：

- 空期望 + 空实际：``wrong_rate=0``、``missed_action_rate=0``、``false_action_rate=0``
  ——「什么都不做」判正确；
- 空期望 + 非空实际：``wrong_rate=1``、``missed_action_rate=0``（无期望可漏）、
  ``false_action_rate=1``（实际全是误伤）——这正是「乱做」的探测器。

分母为 0 时漏判/误伤率记 0 而非抛错，复用 :func:`app.engine.metrics.safe_ratio`：
「没有期望可漏」在业务上等同于「没有漏判」。

**``consistency`` 任务怎么办**：本维度只评 duplicates / hallucination / expired
三类，``task=consistency`` 的样本**明确标记为不参与**（``evaluable=False``），
聚合时排除在分母之外，而不是静默算成 0 分——静默算 0 会把「一致性是另一条
评测路径（离线巡检扫库，见维度③）」伪装成本维度的模型退步。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.datasets.digest import memory_content_hash
from app.engine.metrics import mean, safe_ratio

#: 四类治理任务。``consistency`` 不参与本维度评测（见模块 docstring）。
TASK_DUPLICATES = "duplicates"
TASK_CONSISTENCY = "consistency"
TASK_EXPIRED = "expired"
TASK_HALLUCINATION = "hallucination"

#: 参与本维度评测的任务。
EVALUABLE_TASKS = frozenset({TASK_DUPLICATES, TASK_EXPIRED, TASK_HALLUCINATION})

#: ``governance/replay`` 支持的全部 task 开关，顺序固定。
#:
#: 比 :data:`EVALUABLE_TASKS` **多一个 ``consistency``**：它可以被 replay 计算，
#: 只是本维度不评它（一致性靠离线巡检扫库统计，路径不同）。
#: 两者不能混用——用 EVALUABLE_TASKS 去构造 replay 的开关就会漏掉 consistency，
#: 那些 case 会拿到空决策列表，进而被误判成「该动没动」。
GOVERNANCE_TASKS = (TASK_DUPLICATES, TASK_CONSISTENCY, TASK_EXPIRED, TASK_HALLUCINATION)

#: 规范化后的动作。Java 侧 ``GovernanceDecision.action`` ∈ MERGE / ARCHIVE /
#: DISPUTE / QUARANTINE（见 connector/schemas.py）。
ACTION_MERGE = "MERGE"
ACTION_ARCHIVE = "ARCHIVE"
ACTION_DISPUTE = "DISPUTE"
ACTION_QUARANTINE = "QUARANTINE"

#: 内容哈希 token 前缀（主口径，可复现）。
_CONTENT_PREFIX = "h:"
#: id 兜底 token 前缀（仅当 id 解析不到内容时使用，永不与内容哈希撞）。
_ID_FALLBACK_PREFIX = "i:"


def _normalize_action(action: str | None) -> str:
    """动作归一化：大写 + 去首尾空白，使标注的 ``merge`` 与 Java 的 ``MERGE`` 可比。"""
    return (action or "").strip().upper()


def _content_token(content: str) -> str:
    return f"{_CONTENT_PREFIX}{memory_content_hash(content)}"


def _id_token(memory_id: int) -> str:
    return f"{_ID_FALLBACK_PREFIX}{memory_id}"


def _raw(token: str) -> str:
    """detail 用：去掉 ``h:`` 前缀（``i:`` 兜底保留，因为它不是 md5，一眼可辨）。"""
    if token.startswith(_CONTENT_PREFIX):
        return token[len(_CONTENT_PREFIX):]
    return token


@dataclass(frozen=True)
class ReplayedDecision:
    """一条 replay 返回的实际决策，只保留指标需要的字段。

    与 :class:`app.connector.schemas.GovernanceDecision` 解耦：engine 层不依赖
    HTTP DTO，由 ``from_connector_decision`` 做鸭子类型适配（同维度②的
    ``RetrievedItem.from_search_item``）。
    """

    action: str
    #: 被作用的记忆 id（对应 Java ``items[].memoryId``）
    memory_ids: tuple[int, ...] = ()
    #: 合并目标 id（仅 MERGE 有意义，对应 Java ``mergedIntoId``）
    merged_into_id: int | None = None

    @classmethod
    def from_connector_decision(cls, decision: Any) -> ReplayedDecision:
        """由 connector 的 ``GovernanceDecision`` 构造（鸭子类型，避免循环导入）。"""
        memory_ids = tuple(
            item.memory_id for item in (decision.items or []) if item.memory_id is not None
        )
        return cls(
            action=decision.action,
            memory_ids=memory_ids,
            merged_into_id=decision.merged_into_id,
        )


@dataclass(frozen=True)
class _DecisionSig:
    """一条决策投影到内容哈希空间后的签名。

    签名相等即「同一条决策」。``source_tokens`` 是被作用的记忆（内容哈希 token），
    ``target_token`` 是 MERGE 的幸存者（None 表示无目标）。三字段任一不同都是
    「不一致」——包括「动作不同」与「合并目标不同」。
    """

    action: str
    source_tokens: frozenset[str]
    target_token: str | None


def _expected_signatures(decisions: list[dict[str, Any]]) -> list[_DecisionSig]:
    """把 ground_truth.decisions（期望）投影成签名列表。

    只用 ``memory_contents`` 与 ``merged_into_content``（可复现）；``memory_ids``
    被忽略（id 会随 reset 变化）。合并目标来自显式的 ``merged_into_content``
    字段，而非 ``memory_contents`` 的某个位置（见模块 docstring）。
    """
    signatures: list[_DecisionSig] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        action = _normalize_action(decision.get("action"))
        contents = [
            content
            for content in (decision.get("memory_contents") or [])
            if isinstance(content, str) and content.strip()
        ]
        target_content = decision.get("merged_into_content")
        target = (
            target_content.strip()
            if isinstance(target_content, str) and target_content.strip()
            else None
        )
        signatures.append(
            _DecisionSig(
                action=action,
                source_tokens=frozenset(_content_token(c) for c in contents),
                target_token=_content_token(target) if target is not None else None,
            )
        )
    return signatures


def _resolve_token(memory_id: int, id_to_content: dict[int, str]) -> str:
    """把实际决策里的一个记忆 id 解析成内容哈希 token（拿不到内容则退回 id token）。"""
    content = id_to_content.get(memory_id)
    if content is not None and content.strip():
        return _content_token(content)
    return _id_token(memory_id)


def _actual_signatures(
    decisions: list[ReplayedDecision], id_to_content: dict[int, str]
) -> list[_DecisionSig]:
    """把 replay 返回的实际决策投影成签名列表。"""
    signatures: list[_DecisionSig] = []
    for decision in decisions:
        action = _normalize_action(decision.action)
        source_tokens = frozenset(
            _resolve_token(memory_id, id_to_content) for memory_id in decision.memory_ids
        )
        target_token = (
            _resolve_token(decision.merged_into_id, id_to_content)
            if decision.merged_into_id is not None
            else None
        )
        signatures.append(
            _DecisionSig(action=action, source_tokens=source_tokens, target_token=target_token)
        )
    return signatures


def _match_signatures(
    expected: list[_DecisionSig], actual: list[_DecisionSig]
) -> tuple[int, int, int]:
    """按签名做多重集匹配，返回 ``(matched, missed, false)``。

    ``matched`` 是两边的签名交集（含多重性）；``missed`` 是期望有、实际没有的；
    ``false`` 是实际有、期望没有的。
    """
    expected_counter = Counter(expected)
    actual_counter = Counter(actual)
    matched = 0
    for signature, count in expected_counter.items():
        matched += min(count, actual_counter.get(signature, 0))
    return matched, len(expected) - matched, len(actual) - matched


@dataclass
class GovernanceCaseResult:
    """单个 governance case 的评测结果（可追溯到 case 级）。"""

    task: str
    #: 是否参与本维度评测。False 表示 consistency 或未知任务，聚合时应排除——
    #: 否则会把「另一条评测路径的样本」算成「治理质量退步」。
    evaluable: bool
    expected_decisions: int
    actual_decisions: int
    matched_decisions: int
    missed_decisions: int
    false_decisions: int
    #: 合并目标是否与期望不一致（仅 duplicates 有意义，作为「错在哪」的旁证）。
    wrong_target: bool
    #: 本条 case 是否有任一决策不匹配（0.0 / 1.0）。聚合后即「有错误的 case 占比」。
    wrong_rate: float
    missed_action_rate: float
    false_action_rate: float
    #: 明细：源记忆与合并目标的内容哈希（``i:`` 前缀表示 id 兜底、未解析到内容）。
    expected_action_hashes: list[str] = field(default_factory=list)
    actual_action_hashes: list[str] = field(default_factory=list)
    expected_target_hashes: list[str] = field(default_factory=list)
    actual_target_hashes: list[str] = field(default_factory=list)

    #: 落进 ``eval_case_results.metric_values`` 的指标名。
    METRIC_KEYS = (
        "wrong_rate",
        "missed_action_rate",
        "false_action_rate",
    )

    def as_metric_values(self) -> dict[str, float]:
        """落进 ``eval_case_results.metric_values`` 的数值指标。

        ``wrong_target`` 是布尔、``evaluable`` 是筛选依据，都不进数值列——
        理由同注入维度的 ``over_budget``：转成 0/1 会让「这个维度有哪些数值指标」
        变得含混，而 JSONB 里的布尔字段照样能按它过滤。
        """
        return {
            "wrong_rate": self.wrong_rate,
            "missed_action_rate": self.missed_action_rate,
            "false_action_rate": self.false_action_rate,
        }

    def as_detail(self) -> dict[str, Any]:
        """落库用的明细（与 ``eval_case_results`` 的粒度对应）。"""
        return {
            "task": self.task,
            "evaluable": self.evaluable,
            "expected_decisions": self.expected_decisions,
            "actual_decisions": self.actual_decisions,
            "matched_decisions": self.matched_decisions,
            "missed_decisions": self.missed_decisions,
            "false_decisions": self.false_decisions,
            "wrong_target": self.wrong_target,
            "wrong_rate": self.wrong_rate,
            "missed_action_rate": self.missed_action_rate,
            "false_action_rate": self.false_action_rate,
            "expected_action_hashes": self.expected_action_hashes,
            "actual_action_hashes": self.actual_action_hashes,
            "expected_target_hashes": self.expected_target_hashes,
            "actual_target_hashes": self.actual_target_hashes,
        }


def _not_evaluable(task: str, decisions: list[ReplayedDecision]) -> GovernanceCaseResult:
    """consistency / 未知任务的 case：标记为不参与，指标一律 0（聚合时被排除）。"""
    return GovernanceCaseResult(
        task=task,
        evaluable=False,
        expected_decisions=0,
        actual_decisions=len(decisions),
        matched_decisions=0,
        missed_decisions=0,
        false_decisions=0,
        wrong_target=False,
        wrong_rate=0.0,
        missed_action_rate=0.0,
        false_action_rate=0.0,
    )


class GovernanceEvaluator:
    """维度⑤的评测器。

    纯函数、不依赖网络与数据库。id→内容映射由集成方解析好传入（seed 的
    ``contentToId`` 反向得到 ``{memory_id: content}``），本类只做对齐与聚合。
    """

    def evaluate_case(
        self,
        *,
        payload: dict[str, Any],
        ground_truth: dict[str, Any],
        decisions: list[ReplayedDecision],
        id_to_content: dict[int, str],
    ) -> GovernanceCaseResult:
        """评测单个 governance case。"""
        task = str(payload.get("task") or "")

        if task not in EVALUABLE_TASKS:
            return _not_evaluable(task, decisions)

        expected = _expected_signatures(ground_truth.get("decisions") or [])
        actual = _actual_signatures(decisions, id_to_content)

        matched, missed, false = _match_signatures(expected, actual)

        expected_targets = sorted(sig.target_token for sig in expected if sig.target_token)
        actual_targets = sorted(sig.target_token for sig in actual if sig.target_token)
        wrong_target = expected_targets != actual_targets

        wrong_rate = 1.0 if (missed + false) > 0 else 0.0
        # 分母为 0（负样本 / 空实际）时记 0：没有期望可漏、没有实际可误伤。
        missed_action_rate = safe_ratio(missed, len(expected))
        false_action_rate = safe_ratio(false, len(actual))

        return GovernanceCaseResult(
            task=task,
            evaluable=True,
            expected_decisions=len(expected),
            actual_decisions=len(actual),
            matched_decisions=matched,
            missed_decisions=missed,
            false_decisions=false,
            wrong_target=wrong_target,
            wrong_rate=wrong_rate,
            missed_action_rate=missed_action_rate,
            false_action_rate=false_action_rate,
            expected_action_hashes=sorted(
                _raw(token) for sig in expected for token in sorted(sig.source_tokens)
            ),
            actual_action_hashes=sorted(
                _raw(token) for sig in actual for token in sorted(sig.source_tokens)
            ),
            expected_target_hashes=[_raw(token) for token in expected_targets],
            actual_target_hashes=[_raw(token) for token in actual_targets],
        )

    def aggregate(self, results: list[GovernanceCaseResult]) -> dict[str, float]:
        """把一个 run 内所有 case 的结果聚合成维度级指标。

        **不参与评测的 case（``evaluable=False``）被排除在分母之外**。三个
        ``wrong_*_rate`` 按任务分桶取平均（「有错误的 case 占比」），漏判/误伤率
        在全部可评测 case 上取平均。每个任务桶的数量一并输出，使「0 错误」与
        「该任务没有样本」可以被区分开。
        """
        evaluable = [result for result in results if result.evaluable]
        by_task: dict[str, list[GovernanceCaseResult]] = {
            TASK_DUPLICATES: [],
            TASK_EXPIRED: [],
            TASK_HALLUCINATION: [],
        }
        for result in evaluable:
            if result.task in by_task:
                by_task[result.task].append(result)

        return {
            "case_count": float(len(evaluable)),
            "duplicates_count": float(len(by_task[TASK_DUPLICATES])),
            "expired_count": float(len(by_task[TASK_EXPIRED])),
            "hallucination_count": float(len(by_task[TASK_HALLUCINATION])),
            "wrong_merge_rate": mean([r.wrong_rate for r in by_task[TASK_DUPLICATES]]),
            "wrong_archive_rate": mean([r.wrong_rate for r in by_task[TASK_EXPIRED]]),
            "wrong_quarantine_rate": mean([r.wrong_rate for r in by_task[TASK_HALLUCINATION]]),
            "missed_action_rate": mean([r.missed_action_rate for r in evaluable]),
            "false_action_rate": mean([r.false_action_rate for r in evaluable]),
        }


__all__ = [
    "ACTION_ARCHIVE",
    "ACTION_DISPUTE",
    "ACTION_MERGE",
    "ACTION_QUARANTINE",
    "EVALUABLE_TASKS",
    "GOVERNANCE_TASKS",
    "TASK_CONSISTENCY",
    "TASK_DUPLICATES",
    "TASK_EXPIRED",
    "TASK_HALLUCINATION",
    "GovernanceCaseResult",
    "GovernanceEvaluator",
    "ReplayedDecision",
]
