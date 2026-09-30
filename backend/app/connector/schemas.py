"""Java 评测端点的请求/响应 DTO。

字段与 ``cn.sutone.ai.api.dto.memory.*`` **逐一对齐**；Java 侧 Jackson 默认输出
camelCase，故这里统一用 ``to_camel`` 别名生成器，Python 侧保持 snake_case 可读性。

两条解析策略（刻意不同，理由如下）：

- **响应**宽容未知字段（``extra="ignore"``）：Java 增加一个字段不应该让平台崩。
  契约漂移由 ``tests/test_connector_contract.py`` 的固定 fixture 在 CI 捕获，
  而不是靠运行时的严苛模式——后者只会把「上游加了个字段」变成生产故障。
- **必填字段严格**：缺字段或类型不符一律抛 :class:`JavaEvalContractError`，
  绝不做 ``None`` 兜底或类型猜测。矩阵要求「返回结构不兼容 → 立即失败，禁止猜测字段」，
  否则脏数据会一路流进指标计算，算出来的分数没有意义。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")

#: 所有 DTO 共用的配置：camelCase 别名 + 允许按字段名构造（便于测试写关键字参数）。
_BASE_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _Dto(BaseModel):
    """DTO 基类，统一 camelCase 别名策略。"""

    model_config = _BASE_CONFIG


class JavaResponse(_Dto, Generic[T]):
    """Java 统一响应信封 ``{code, info, data}``。

    ``code == "0000"`` 表示成功；**其余一律是业务失败，且 HTTP 状态码通常仍是 200**。
    见 :mod:`app.connector.errors` 的说明。
    """

    code: str
    info: str | None = None
    data: T | None = None


# ---------------------------------------------------------------------------
# seed / reset —— 破坏性写，需 X-Eval-Run-Id（必填）+ X-Eval-Fencing（可选）
# ---------------------------------------------------------------------------


class SeedItem(_Dto):
    type: str
    content: str


class SeedRequest(_Dto):
    eval_user_id: int
    items: list[SeedItem]


class SeedResponse(_Dto):
    """``content_to_id`` 是「内容 → 记忆 id」映射，平台靠它把 ground truth 绑定到真实 id。"""

    inserted: int
    existed: int
    content_to_id: dict[str, int] = Field(default_factory=dict)


class ResetRequest(_Dto):
    eval_user_id: int


class ResetResponse(_Dto):
    mysql_deleted: int
    vector_cleared: bool


# ---------------------------------------------------------------------------
# search / retrieve-context / extract —— 只读观测
# ---------------------------------------------------------------------------


class SearchRequest(_Dto):
    eval_user_id: int
    query: str
    top_k: int = 5
    #: None 表示用 Java 侧默认阈值（0.1）；显式传 0 是合法值，故不能用 ``or`` 兜底。
    threshold: float | None = None
    #: 冻结副作用：评测检索不应顺带更新 access_count / 时间衰减，否则重复跑同一 case
    #: 会因状态漂移产生不同结果，指标失去可复现性。默认 True。
    freeze_side_effects: bool = True
    #: None = 用 Java 默认（HNSW）；True = 暴力精确检索（指标基线用）。
    exact: bool | None = None
    hnsw_ef: int | None = None


class SearchItem(_Dto):
    id: int
    #: 必须返回 content：平台的 Recall 用 ``md5(content)`` 与 ground truth 直接匹配，
    #: 只给 id 无法跨 run 对账（id 会随 reset 变化）。
    content: str
    score: float
    importance: float | None = None
    type: str | None = None
    confidence: float | None = None


class SearchResponse(_Dto):
    items: list[SearchItem] = Field(default_factory=list)


class RetrieveContextRequest(_Dto):
    eval_user_id: int
    query_context: str
    top_k: int = 5


class RetrieveContextResponse(_Dto):
    formatted: str
    token_count: int
    budgeted_ids: list[int] = Field(default_factory=list)


class ExtractRequest(_Dto):
    eval_user_id: int
    #: 形如 ``[{"role": "user", "content": "..."}]``，与 Java ``List<Map<String,String>>`` 对应。
    messages: list[dict[str, str]]


class ExtractCandidate(_Dto):
    """对应 Java ``MemoryCandidate`` record 的 10 个字段。"""

    content: str
    type: str | None = None
    attributed_to: str | None = None
    operation: str | None = None
    target_memory_id: int | None = None
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    evidence: str | None = None
    confidence: float | None = None


# ---------------------------------------------------------------------------
# fencing —— 权威态
# ---------------------------------------------------------------------------


class FencingState(_Dto):
    """无行时 Java 返回 ``(0, null)``，不代表错误。"""

    fencing_version: int
    active_run_id: str | None = None


class AcquireRequest(_Dto):
    eval_user_id: int
    expected_version: int
    run_id: str


class AcquireResult(_Dto):
    """``acquired=False`` 时 ``version`` 是**当前权威版本**，用于 alignment protocol。"""

    acquired: bool
    version: int


class ReleaseRequest(_Dto):
    eval_user_id: int
    run_id: str


# ---------------------------------------------------------------------------
# params / metrics / circuit-breaker —— 观测与配置快照
# ---------------------------------------------------------------------------


class ParamsResponse(_Dto):
    """评测参数快照，平台据此计算 ``config_fingerprint``。"""

    vector_store: str
    rrf_k: int
    alpha: float
    beta: float
    recency_half_life_days: float
    profile_boost: float
    min_confidence: float
    inject_max_tokens: int


class MetricsResponse(_Dto):
    extraction_reject_rate: float
    vector_sync_pending_count: int


# ---------------------------------------------------------------------------
# governance —— 只读样本导出 + 无副作用重放
# ---------------------------------------------------------------------------


class GovernanceSamplesRequest(_Dto):
    eval_user_id: int


class GovernanceSampleItem(_Dto):
    memory_id: int
    user_id: int | None = None
    type: str | None = None
    content: str | None = None
    status: str | None = None
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    confidence: float | None = None


class GovernanceBucket(_Dto):
    total: int
    items: list[GovernanceSampleItem] = Field(default_factory=list)


class GovernanceSamplesResponse(_Dto):
    duplicates: GovernanceBucket
    consistency: GovernanceBucket
    expired: GovernanceBucket
    hallucination: GovernanceBucket


class GovernanceReplayRequest(_Dto):
    eval_user_id: int
    duplicates: bool = True
    consistency: bool = True
    expired: bool = True
    hallucination: bool = True


class GovernanceDecisionItem(_Dto):
    memory_id: int
    before_status: str | None = None
    after_status: str | None = None


class GovernanceDecision(_Dto):
    """``action`` ∈ MERGE / ARCHIVE / DISPUTE / QUARANTINE。"""

    action: str
    merged_into_id: int | None = None
    reason: str | None = None
    items: list[GovernanceDecisionItem] = Field(default_factory=list)


__all__ = [
    "AcquireRequest",
    "AcquireResult",
    "ExtractCandidate",
    "ExtractRequest",
    "FencingState",
    "GovernanceBucket",
    "GovernanceDecision",
    "GovernanceDecisionItem",
    "GovernanceReplayRequest",
    "GovernanceSampleItem",
    "GovernanceSamplesRequest",
    "GovernanceSamplesResponse",
    "JavaResponse",
    "MetricsResponse",
    "ParamsResponse",
    "ReleaseRequest",
    "ResetRequest",
    "ResetResponse",
    "RetrieveContextRequest",
    "RetrieveContextResponse",
    "SearchItem",
    "SearchRequest",
    "SearchResponse",
    "SeedItem",
    "SeedRequest",
    "SeedResponse",
]
