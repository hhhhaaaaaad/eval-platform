"""run 创建与查询的 API 模型（EP-6）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]

#: 幂等键长度上限，与常见实现的约定一致（防止被当成任意大数据字段滥用）。
MAX_IDEMPOTENCY_KEY_LENGTH = 200

#: 单次 run 允许的最大用例数。没有上限的话，一个 10 万条的评测集会让
#: 单次 run 无条件占用命名空间数小时，把整条流水线堵死。
MAX_CASES_PER_RUN = 5000


class ExperimentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None = None


class ExperimentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_by: int | None
    created_at: datetime


class RunCreateRequest(BaseModel):
    """创建一次评测 run。

    四个字段的语义刻意分离，不要混为一谈（方案 §「四语义分离」）：

    - ``dataset_version_id`` / ``param_snapshot_id`` / ``model_version_id`` / ``mode``
      → 决定 **config_fingerprint**（这份配置是什么）；
    - ``idempotency_key`` → **幂等键**（这一次提交身份是什么），重复提交返回同一个 run；
    - ``exclusive`` → **并发守挡**（是否要求全库独占）。

    run 自身的身份（UUID）由数据库生成，不由调用方指定。
    """

    model_config = ConfigDict(extra="forbid")

    dataset_version_id: int
    param_snapshot_id: int
    model_version_id: int
    mode: Literal["exact", "hnsw"] = "exact"
    experiment_id: uuid.UUID | None = None
    idempotency_key: Annotated[str, Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)] | None = None
    #: 独占模式：同一时刻全库至多一个独占 run。用于会互相干扰的批量操作
    #: （如全量治理重放），以牺牲并行度换取隔离。
    exclusive: bool = False
    case_limit: Annotated[int, Field(gt=0, le=MAX_CASES_PER_RUN)] | None = None


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    config_fingerprint: str
    idempotency_key: str | None
    experiment_id: uuid.UUID | None
    dataset_version_id: int
    param_snapshot_id: int
    model_version_id: int
    eval_user_id: int
    mode: str
    #: None = 不限量。调用方需要它来判断「这次指标是跑满算出来的还是抽样算的」——
    #: 两者的指标不可直接比较，趋势查询也要按它分组。
    case_limit: int | None
    status: str
    exclusive: bool
    current_stage: str | None
    progress: float
    retry_count: int
    error_message: str | None
    result_summary: dict[str, Any] | None
    checkpoint: dict[str, Any]
    created_by: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunCreateResponse(RunResponse):
    """创建响应额外带上两个「这次创建发生了什么」的标记。

    ``created=False`` 表示命中了幂等键，返回的是既有 run——调用方据此区分
    「我创建了它」与「它早就存在」，而不用去比对时间戳。
    ``enqueued=False`` 表示任务未投递（broker 不可用或配置关闭），run 停在 pending，
    可由调度器补投。**这两种情况都不是错误**，故不改变 HTTP 状态码语义之外的行为。
    """

    created: bool
    enqueued: bool


# ---------------------------------------------------------------------------
# 结果查询（P1-A2）
# ---------------------------------------------------------------------------


class DimensionMetrics(BaseModel):
    """一个维度的聚合指标。``metrics`` 是「指标名 → 值」的映射，直接喂给图表。"""

    dimension: str
    metrics: dict[str, float]


class RunResultsResponse(BaseModel):
    """按维度分组的聚合结果。

    ``dimensions`` 用数组而不是 ``{维度: {...}}`` 的字典：字典的键顺序在 JSON 里
    不保证，而无序的图例会让每次刷新看到的维度顺序都不同。数组显式定了序，
    前端不必再自己排序。

    维度只有运行时才知道（当前是 retrieval，后续会有 injection / governance 等），
    所以**不能用固定字段**——加一个维度就要改响应结构的话，前端也跟着改。
    """

    run_id: uuid.UUID
    status: str
    dimensions: list[DimensionMetrics]


class CaseResultResponse(BaseModel):
    """逐 case 结果。

    ``metric_values`` 与 ``detail`` 的分工见 ``app.results.service``：
    前者是可聚合的数值，后者是解释这些数值的上下文（命中了哪些、匹配口径、
    是否可评测）。分开是刻意的——把标识符列表混进数值字段会让「按指标过滤」没法写。
    """

    case_id: int
    dimension: str
    metric_values: dict[str, Any]
    detail: dict[str, Any]


class RunCasesResponse(BaseModel):
    """逐 case 明细的**分页**响应。

    分页不是可选项：一个评测集可以有上千条 case，一次性返回既慢又可能把
    浏览器拖死。``total`` 让调用方知道还有多少没取，不必靠「返回条数小于 limit」
    来推断（那在恰好整除时会误判为已取完）。
    """

    run_id: uuid.UUID
    total: int
    limit: int
    offset: int
    cases: list[CaseResultResponse]
