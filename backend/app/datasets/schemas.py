"""数据集 API 的请求/响应模型，以及**逐 case_type 的 payload / ground_truth 结构校验**。

校验格式直接对齐《评测集设计与指标手册》§1.2 的标注格式，不自行发明字段名——
标注格式是人工标注员与平台之间的契约，改一处就有一批已标注数据作废。

三源样本（EP-4 要求）：

======================  ==========================  ==========================
case_type               payload 形态                   ground_truth 形态
======================  ==========================  ==========================
conversation_to_memory  dialogue_id + messages        ground_truth_memories[]
query_to_memory         query_id + query + task_type  relevant_memory_ids[] 或
                                                      relevant_memory_contents[]
governance              task（四类治理任务之一）        decisions[]
======================  ==========================  ==========================
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.datasets.digest import (
    CASE_TYPE_CONVERSATION_TO_MEMORY,
    CASE_TYPE_GOVERNANCE,
    CASE_TYPE_QUERY_TO_MEMORY,
)

#: 当前支持的 case schema 版本。版本号进 DB，便于将来格式演进时并存多版。
SUPPORTED_SCHEMA_VERSION = 1

CaseType = Literal["conversation_to_memory", "query_to_memory", "governance"]
VersionSource = Literal["manual", "cli", "api", "derived"]
GovernanceTask = Literal["duplicates", "consistency", "expired", "hallucination"]

_MEMORY_TYPES = frozenset({"fact", "preference", "knowledge", "event"})
_ATTRIBUTED_TO = frozenset({"user", "agent", "system"})


class CasePayloadError(ValueError):
    """case 结构不合法。带 ``case_index`` 便于标注员定位到具体哪一条。"""

    def __init__(self, message: str, *, case_index: int | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.case_index = case_index
        self.field = field


# ---------------------------------------------------------------------------
# case 内部结构（payload / ground_truth 的组成部分）
# ---------------------------------------------------------------------------


class DialogueMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str


class GroundTruthMemory(BaseModel):
    """手册 §1.2 的 ground-truth 记忆。``content`` 必需，其余字段用于逐字段比对。"""

    model_config = ConfigDict(extra="ignore")

    content: str
    type: str | None = None
    attributed_to: str | None = None
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None


class GovernanceDecisionExpectation(BaseModel):
    """governance 类 case 的期望决策。

    ``memory_ids`` 用内容哈希而非裸 id 定位——id 要等 seed 之后才存在，且 reset 后会变。
    标注时写内容，运行时用 seed 返回的 ``contentToId`` 解析。

    ``merged_into_content`` 显式表达「合并到哪条」，而不是约定
    ``memory_contents`` 的某个位置（如「最后一个元素是存活者」）。
    位置约定看着省事，实际是隐患：JSON 数组的顺序人写时不可靠，
    写反了不会报错，只会让「误合并率」静默地算在另一批样本上；
    而且约定只存在于文档里，schema 校验拦不住任何东西。
    显式字段让「哪条是目标」成为可校验的事实。
    """

    model_config = ConfigDict(extra="ignore")

    action: str
    memory_ids: list[int] = Field(default_factory=list)
    memory_contents: list[str] = Field(default_factory=list)
    #: 仅 ``action=MERGE`` 时有意义：被合并到的（存活下来的）那条记忆的内容。
    merged_into_content: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_merge_target(self) -> GovernanceDecisionExpectation:
        """MERGE 必须给出合并目标，非 MERGE 不该给。

        两个方向都拦，因为两种写错都会让指标失真却看不出异常：
        MERGE 缺目标 → 无法判断合并对不对，只能退化成「动作对了就算对」；
        非 MERGE 给了目标 → 标注员多半是复制粘贴时漏改 action，实际语义不明。
        """
        is_merge = self.action.strip().upper() == "MERGE"
        if is_merge and not (self.merged_into_content or "").strip():
            raise ValueError("action=MERGE 时必须提供 merged_into_content（合并到哪条记忆）")
        if not is_merge and self.merged_into_content is not None:
            raise ValueError(
                f"action={self.action} 不应提供 merged_into_content（仅 MERGE 有意义）"
            )
        return self


# ---------------------------------------------------------------------------
# case 输入 + 逐类型校验
# ---------------------------------------------------------------------------


class CaseInput(BaseModel):
    """一条评测用例。payload 是输入，ground_truth 是参考答案。"""

    model_config = ConfigDict(extra="forbid")

    case_type: CaseType
    #: 分组标签（如按 4 种记忆类型 × 6 类边界 case 分组），用于分组统计。
    group_key: Annotated[str, Field(min_length=1)]
    payload: dict[str, Any]
    ground_truth: dict[str, Any]

    @model_validator(mode="after")
    def _validate_by_type(self) -> CaseInput:
        """按 case_type 分派到各自的校验器。

        这层校验是「评测集质量问题在导入期暴露」的唯一机会——脏标注一旦入库，
        要等到跑出离谱指标时才会被发现，那时已经很难回溯是哪条标注错了。
        """
        if self.case_type == CASE_TYPE_CONVERSATION_TO_MEMORY:
            _validate_conversation_case(self.payload, self.ground_truth)
        elif self.case_type == CASE_TYPE_QUERY_TO_MEMORY:
            _validate_query_case(self.payload, self.ground_truth)
        elif self.case_type == CASE_TYPE_GOVERNANCE:
            _validate_governance_case(self.payload, self.ground_truth)
        return self


def _require(condition: bool, message: str, *, field: str | None = None) -> None:
    if not condition:
        raise CasePayloadError(message, field=field)


def _validate_conversation_case(payload: dict[str, Any], ground_truth: dict[str, Any]) -> None:
    """对话→记忆对：必须有非空 messages，且 ground truth 记忆非空。"""
    _require(bool(payload.get("dialogue_id")), "payload.dialogue_id 不能为空", field="payload.dialogue_id")

    messages = payload.get("messages")
    _require(isinstance(messages, list) and len(messages) > 0, "payload.messages 必须是非空数组", field="payload.messages")
    for index, message in enumerate(messages):
        _require(
            isinstance(message, dict) and isinstance(message.get("role"), str) and isinstance(message.get("content"), str),
            f"payload.messages[{index}] 必须含字符串 role 与 content",
            field=f"payload.messages[{index}]",
        )

    memories = ground_truth.get("ground_truth_memories")
    _require(
        isinstance(memories, list) and len(memories) > 0,
        "ground_truth.ground_truth_memories 必须是非空数组（没有答案的用例无法计算 P/R）",
        field="ground_truth.ground_truth_memories",
    )
    for index, memory in enumerate(memories):
        try:
            parsed = GroundTruthMemory.model_validate(memory)
        except Exception as exc:
            raise CasePayloadError(
                f"ground_truth_memories[{index}] 结构非法: {exc}",
                field=f"ground_truth.ground_truth_memories[{index}]",
            ) from exc
        _require(bool(parsed.content.strip()), f"ground_truth_memories[{index}].content 不能为空白")
        if parsed.type is not None:
            _require(
                parsed.type in _MEMORY_TYPES,
                f"ground_truth_memories[{index}].type 非法: {parsed.type}（应为 {sorted(_MEMORY_TYPES)}）",
                field=f"ground_truth.ground_truth_memories[{index}].type",
            )
        if parsed.attributed_to is not None:
            _require(
                parsed.attributed_to in _ATTRIBUTED_TO,
                f"ground_truth_memories[{index}].attributed_to 非法: {parsed.attributed_to}",
                field=f"ground_truth.ground_truth_memories[{index}].attributed_to",
            )


def _validate_query_case(payload: dict[str, Any], ground_truth: dict[str, Any]) -> None:
    """query→记忆对。

    ground truth 允许两种标注方式，**至少给一种**：

    - ``relevant_memory_ids``：手册 §1.2 的原生格式；
    - ``relevant_memory_contents``：按内容标注。

    支持后者是因为 id 在 AgentWrite 侧由 seed 产生、``reset`` 后会变，
    同一份评测集跑两次拿到的 id 不同。按内容标注时，运行时用 seed 返回的
    ``contentToId`` 解析成当次 run 的真实 id，指标才可复现。
    """
    _require(bool(payload.get("query_id")), "payload.query_id 不能为空", field="payload.query_id")
    query = payload.get("query")
    _require(isinstance(query, str) and query.strip() != "", "payload.query 不能为空白", field="payload.query")

    ids = ground_truth.get("relevant_memory_ids")
    contents = ground_truth.get("relevant_memory_contents")
    has_ids = isinstance(ids, list) and len(ids) > 0
    has_contents = isinstance(contents, list) and len(contents) > 0

    _require(
        has_ids or has_contents,
        "ground_truth 必须提供 relevant_memory_ids 或 relevant_memory_contents 之一"
        "（否则无正确答案，Recall 无从计算）",
        field="ground_truth",
    )
    if has_ids:
        _require(
            all(isinstance(item, int) and not isinstance(item, bool) for item in ids),
            "ground_truth.relevant_memory_ids 必须是整数数组",
            field="ground_truth.relevant_memory_ids",
        )
    if has_contents:
        _require(
            all(isinstance(item, str) and item.strip() != "" for item in contents),
            "ground_truth.relevant_memory_contents 必须是非空字符串数组",
            field="ground_truth.relevant_memory_contents",
        )

    ranked = ground_truth.get("ranked_memory_ids")
    if ranked is not None:
        _require(
            isinstance(ranked, list) and all(isinstance(item, int) and not isinstance(item, bool) for item in ranked),
            "ground_truth.ranked_memory_ids 必须是整数数组",
            field="ground_truth.ranked_memory_ids",
        )


def _validate_governance_case(payload: dict[str, Any], ground_truth: dict[str, Any]) -> None:
    """治理样本：payload 指定四类治理任务之一，ground truth 给出期望决策。"""
    task = payload.get("task")
    allowed = ("duplicates", "consistency", "expired", "hallucination")
    _require(task in allowed, f"payload.task 必须是 {allowed} 之一，实际: {task}", field="payload.task")

    decisions = ground_truth.get("decisions")
    # 允许 decisions 为空数组：某些治理样本就是「什么都不该做」的反例，
    # 这类负样本恰恰是检验误伤率的关键，不能因为空就判非法。
    _require(isinstance(decisions, list), "ground_truth.decisions 必须是数组", field="ground_truth.decisions")
    for index, decision in enumerate(decisions):
        try:
            parsed = GovernanceDecisionExpectation.model_validate(decision)
        except Exception as exc:
            raise CasePayloadError(
                f"decisions[{index}] 结构非法: {exc}",
                field=f"ground_truth.decisions[{index}]",
            ) from exc
        _require(bool(parsed.action.strip()), f"decisions[{index}].action 不能为空白")


# ---------------------------------------------------------------------------
# 数据集 API DTO
# ---------------------------------------------------------------------------


class DatasetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None = None


class DatasetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    created_by: int | None
    created_at: datetime
    is_archived: bool


class DatasetVersionImportRequest(BaseModel):
    """导入一个新版本。版本一旦写入即不可变——重复导入同一 digest 会被去重拒绝。"""

    model_config = ConfigDict(extra="forbid")

    version: Annotated[str, Field(min_length=1, max_length=64)]
    source: VersionSource = "manual"
    parent_version_id: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    cases: Annotated[list[CaseInput], Field(min_length=1)]


class DatasetVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dataset_id: int
    version: str
    schema_version: int
    source: str
    parent_version_id: int | None
    content_digest: str
    created_by: int | None
    created_at: datetime


class DatasetVersionSummary(DatasetVersionResponse):
    """列表视图：附带回溯用的规模信息，不必拉全部 case。"""

    case_count: int


class DatasetVersionDetail(DatasetVersionResponse):
    """详情视图：带全部 case，用于审计与导出。"""

    config: dict[str, Any]
    cases: list[CaseInput]
