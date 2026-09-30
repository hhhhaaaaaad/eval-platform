"""维度③：一致性巡检（EP-9）。

**这个维度与另外四个的根本不同**：它没有 case 概念，是对**整个评测命名空间**的
一次离线巡检，产出的是几个比值，而不是「逐 case 打分再聚合」。原因（见
:mod:`app.engine.dimensions` 的说明）：另外四个维度都依赖 ground truth 才算得出
「准确度」，而一致性衡量的是**系统自己对这批记忆的行为**——把多少条判为重复、
多少条判为冲突、多少条判为过期残留、多少条判为幻觉隔离。这是**系统行为度量**，
不是**准确度度量**。因此它没有 ground truth、没有「标注缺失」意义上的 ``answerable``
开关（只有一个 ``total_memories <= 0`` 的「无数据」边界，见下）。

**动作 → 类别（复用治理维度的常量，不在此重新定义）**：``governance/replay`` 的
四类动作对应四类系统判断，本维度只关心「被作用的记忆条数」：

- ``MERGE``（重复检测）→ 被判为**重复**的记忆；
- ``DISPUTE``（冲突检测）→ 被判为**冲突**的记忆；
- ``ARCHIVE``（过期检测）→ 被判为**过期残留**的记忆；
- ``QUARANTINE``（幻觉检测）→ 被判为**幻觉隔离**的记忆。

四类各出一个率（含 ``quarantine_rate``）：replay 全量跑四类分析
（:data:`app.engine.dimensions.governance.GOVERNANCE_TASKS`），只报前三类会让看指标的
人以为系统只有三种动作。隔离率与另外三个同构、成本为零（数据已采），不报才是漏。

**条数口径（本维度最容易做错的地方）**：``ReplayedDecision.memory_ids`` 是「被这条
决策作用到的记忆 id 列表」。同一条记忆**可能出现在同类的多条决策里**——冲突检测把
``{A, B}`` 与 ``{A, C}`` 报成两条 DISPUTE 时记忆 A 出现两次；合并检测也可能把一个 N 元
合并组拆成多条 MERGE。按「决策条数」累加会把同一条记忆数多遍，让率失真甚至越界；按
「去重后的记忆条数」统计才回答「命名空间里有多少条**记忆**被该系统判为 X」。因此
**每个类别内部按 ``memory_id`` 去重**，分子是「该类别下被作用的去重记忆条数」。

**为什么只在类别内去重、不跨类别去重**：四类动作是四次独立扫描，各自回答不同的问题
（「多少是重复」vs「多少是幻觉」）。同一条记忆既被 MERGE 又被 DISPUTE 是合法的——它
既是某条的重复、又与另一条冲突——两个率各自是独立的分式，互不干扰。跨类别去重反而把
「既重复又冲突」的记忆强行归到某一类，丢掉另一个维度的真实信号。

**MERGE 的 ``merged_into_id`` 不参与计数**：合并决策里 ``memory_ids`` 是「被合并掉的
冗余源记忆」，``merged_into_id`` 是「保留下来的幸存者」。重复率衡量「有多少条是冗余
重复」，幸存者是被保留的那条、不是冗余，故只数 ``memory_ids``。

**``total_memories <= 0``（本维度最重要的边界）**：没有任何记忆可巡检。此时四个分子率
记 0（``safe_ratio(n, 0) == 0``：没有记忆自然没有重复/冲突/过期/幻觉），但
**``consistency_rate`` 必须记 0 而非 1.0**——``1 - 0 = 1.0`` 会得出「一致率 100%」，
这是最危险的误读：空命名空间没有「一致」这回事，「一致率」无定义。做法：``total_memories
<= 0`` 时 ``answerable=False``、``consistency_rate=0.0``，且 ``total_memories`` 落进
detail，让读指标的人一眼分辨「没数据」还是「真干净」。负值按 0 同法处理（数据错误，
不该为它 crash 整个 run）。

**比率是否 clamp**：``total_memories`` 小于被标记的去重条数（理论不该发生——命名空间
只有 N 条却判出 N+1 个不同 id，通常是被已删除/已归档的 id 污染，或取数口径不一致）。
此时率会超过 1.0。**率 clamp 到 [0, 1]**，因为率是分式、必须落在该区间；但**原始计数
不 clamp**——``duplicate_total=105, total_memories=100`` 这个矛盾本身留在 detail 里供
排查，不能被 clamp 抹掉。

**未知动作**（既非四类之一的 action，Java 侧将来可能新增动作类型）：**忽略、不计入
任何桶**。理由：四个率各自绑定明确的动作语义，把「将来才有的动作」计入现有某个桶等于
给它贴错误的标签。但**忽略不是静默**——未知动作的决策条数记进 ``unknown_action_total``，
否则 Java 加了新动作后这个维度的数字会悄然变化而无人察觉（同治理维度 ``evaluable=False``
「显式排除而非静默算 0」的取舍）。

**``decisions_by_task`` 为空 / 只含部分 task**：等价于「缺的那些 task 没发现任何问题」。
本模块的契约是「dict 里出现的决策就是观测到的全部决策」——缺失的 task 与「该 task 的
决策列表为空」同义，都对应 0 计数。接线方须保证传入的是「本次观测的忠实结果」，而不是
「采集被跳过」的信号（那种信号应由接线方在上游拦截，不该流到这里）。

**接口形态**：没有 case，所以不做 ``evaluate_case`` + ``aggregate``。一次巡检的入参是
``total_memories`` 与 ``decisions_by_task``，出参是单个 :class:`ConsistencyResult`，
用 ``as_metric_values`` / ``as_detail`` 分别给维度级指标与落库明细，与其余维度的 result
对象同形，接线层（pipeline）可照旧调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.engine.dimensions.governance import (
    ACTION_ARCHIVE,
    ACTION_DISPUTE,
    ACTION_MERGE,
    ACTION_QUARANTINE,
    ReplayedDecision,
)
from app.engine.metrics import safe_ratio

#: 已知动作 → 本维度的类别标签。四类各出一个率（见模块 docstring）。
#: 未知动作不在表中，落入 ``unknown_action_total``。
_ACTION_TO_BUCKET: dict[str, str] = {
    ACTION_MERGE: "duplicate",
    ACTION_DISPUTE: "conflict",
    ACTION_ARCHIVE: "expired_residue",
    ACTION_QUARANTINE: "quarantine",
}


def _normalize_action(action: str | None) -> str:
    """动作归一化：大写 + 去首尾空白，使小写 ``merge`` 与 ``MERGE`` 可比。

    与治理维度的 ``_normalize_action`` 同口径，但后者是私有函数、不跨模块导入，
    故此处保留一份一行实现（不会漂移出语义差异）。
    """
    return (action or "").strip().upper()


def _clamp01(value: float) -> float:
    """把率限制在 [0, 1]：率是分式、必须落在该区间；越界是数据异常，由原始计数暴露。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@dataclass
class ConsistencyResult:
    """一次一致性巡检的结果（整个命名空间，非逐 case）。

    分子率与原始计数同存：只看率无法分辨「重复率 0.1」是 10/100 还是 1/10，
    原始计数让这个分式可被还原、异常（计数 > 总数）可被看见。
    """

    #: 命名空间记忆总数（比率的分母）。``<= 0`` 表示「无记忆可巡检」。
    total_memories: int
    #: 判为重复 / 冲突 / 过期残留 / 幻觉隔离的**去重记忆条数**。
    duplicate_total: int
    conflict_total: int
    expired_residue_total: int
    quarantine_total: int
    #: 动作不在四类之一、被忽略的决策条数（Java 侧将来新增动作类型时不会静默消失）。
    unknown_action_total: int
    #: 本次巡检看到的决策总条数（跨全部 task），用于分辨「0 计数」是「没产出」还是「没动作」。
    decision_total: int
    #: 是否有可巡检对象。False 表示 ``total_memories <= 0``：各率一律 0、
    #: ``consistency_rate`` 刻意不是 1.0（空命名空间没有「一致」这回事）。
    answerable: bool
    #: 四个分子率 + 一致率（``1 - conflict_rate``，见模块 docstring 的边界说明）。
    duplicate_rate: float
    conflict_rate: float
    consistency_rate: float
    expired_residue_rate: float
    quarantine_rate: float

    #: 落进 ``eval_run_results`` 的维度级指标名。
    METRIC_KEYS = (
        "duplicate_rate",
        "conflict_rate",
        "consistency_rate",
        "expired_residue_rate",
        "quarantine_rate",
    )

    def as_metric_values(self) -> dict[str, float]:
        """落进 ``eval_run_results`` 的维度级指标。

        只含五个率：``unknown_action_total`` / 各原始计数 / ``answerable`` 都是
        「解释这些率的上下文」，属于 ``detail`` 而非可聚合数值，与其余维度把
        ``answerable`` 放进 detail 的分工一致。
        """
        return {
            "duplicate_rate": self.duplicate_rate,
            "conflict_rate": self.conflict_rate,
            "consistency_rate": self.consistency_rate,
            "expired_residue_rate": self.expired_residue_rate,
            "quarantine_rate": self.quarantine_rate,
        }

    def as_detail(self) -> dict[str, Any]:
        """落进 ``eval_run_results.detail`` 的明细：原始计数 + 可巡检性。

        至少含 ``total_memories`` 与各原始计数——只给率的话，看指标的人无法判断
        「重复率 0.1」是 10/100 还是 1/10。
        """
        return {
            "total_memories": self.total_memories,
            "answerable": self.answerable,
            "duplicate_total": self.duplicate_total,
            "conflict_total": self.conflict_total,
            "expired_residue_total": self.expired_residue_total,
            "quarantine_total": self.quarantine_total,
            "unknown_action_total": self.unknown_action_total,
            "decision_total": self.decision_total,
        }


class ConsistencyEvaluator:
    """维度③的评测器。

    纯函数、不依赖网络与数据库。入参 ``total_memories`` 与 ``decisions_by_task``
    由集成方提供（治理观测采集阶段已把 replay 决策按 task 分好组，见
    :class:`app.engine.pipeline.GovernanceObservation`），本类只统计去重条数并求比率。
    """

    def evaluate(
        self,
        *,
        total_memories: int,
        decisions_by_task: dict[str, list[ReplayedDecision]],
    ) -> ConsistencyResult:
        """对整个命名空间做一次一致性巡检。"""
        bucket_ids: dict[str, set[int]] = {
            "duplicate": set(),
            "conflict": set(),
            "expired_residue": set(),
            "quarantine": set(),
        }
        unknown_action_total = 0
        decision_total = 0

        for decisions in decisions_by_task.values():
            for decision in decisions:
                decision_total += 1
                bucket = _ACTION_TO_BUCKET.get(_normalize_action(decision.action))
                if bucket is None:
                    unknown_action_total += 1
                    continue
                bucket_ids[bucket].update(
                    memory_id
                    for memory_id in (decision.memory_ids or ())
                    if isinstance(memory_id, int) and not isinstance(memory_id, bool)
                )

        duplicate_total = len(bucket_ids["duplicate"])
        conflict_total = len(bucket_ids["conflict"])
        expired_residue_total = len(bucket_ids["expired_residue"])
        quarantine_total = len(bucket_ids["quarantine"])

        if total_memories <= 0:
            # 无记忆可巡检：四个分子率记 0、consistency_rate 记 0（**不是** 1 - 0 = 1.0，
            # 那会把空命名空间误读成「一致率 100%」）。原始计数仍保留——「空命名空间
            # 却判出动作」这个矛盾值得暴露给排查者。
            return ConsistencyResult(
                total_memories=total_memories,
                duplicate_total=duplicate_total,
                conflict_total=conflict_total,
                expired_residue_total=expired_residue_total,
                quarantine_total=quarantine_total,
                unknown_action_total=unknown_action_total,
                decision_total=decision_total,
                answerable=False,
                duplicate_rate=0.0,
                conflict_rate=0.0,
                consistency_rate=0.0,
                expired_residue_rate=0.0,
                quarantine_rate=0.0,
            )

        duplicate_rate = _clamp01(safe_ratio(duplicate_total, total_memories))
        conflict_rate = _clamp01(safe_ratio(conflict_total, total_memories))
        expired_residue_rate = _clamp01(safe_ratio(expired_residue_total, total_memories))
        quarantine_rate = _clamp01(safe_ratio(quarantine_total, total_memories))
        # conflict_rate 已 clamp 到 [0, 1]，故 1 - conflict_rate 也在 [0, 1]；再 clamp 只为对称。
        consistency_rate = _clamp01(1.0 - conflict_rate)

        return ConsistencyResult(
            total_memories=total_memories,
            duplicate_total=duplicate_total,
            conflict_total=conflict_total,
            expired_residue_total=expired_residue_total,
            quarantine_total=quarantine_total,
            unknown_action_total=unknown_action_total,
            decision_total=decision_total,
            answerable=True,
            duplicate_rate=duplicate_rate,
            conflict_rate=conflict_rate,
            consistency_rate=consistency_rate,
            expired_residue_rate=expired_residue_rate,
            quarantine_rate=quarantine_rate,
        )


__all__ = [
    "ConsistencyEvaluator",
    "ConsistencyResult",
]
