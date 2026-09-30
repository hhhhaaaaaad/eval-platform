"""参数快照 / 模型版本 / fingerprint 的 API 模型（EP-5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.params.fingerprint import RETRIEVAL_MODES

RetrievalMode = Literal["exact", "hnsw"]


class ParamSnapshotCreateRequest(BaseModel):
    """手工创建参数快照。``frozen_keys`` 缺省即冻结 ``/eval/params`` 的全部字段。"""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    params: dict[str, Any]
    frozen_keys: list[str] | None = None
    description: str | None = None


class ParamSnapshotFromJavaRequest(BaseModel):
    """从 AgentWrite ``/api/v1/eval/params`` 拉取并落快照。"""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)] = "agentwrite-default"
    description: str | None = None


class ParamSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    params: dict[str, Any]
    freeze_config: dict[str, Any]
    params_hash: str
    description: str | None
    created_by: int | None
    created_at: datetime


class ModelVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_model_id: Annotated[str, Field(min_length=1)]
    reranker_model_id: Annotated[str, Field(min_length=1)]
    config: dict[str, Any] = Field(default_factory=dict)


class ModelVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    embedding_model_id: str
    reranker_model_id: str
    config_hash: str
    config: dict[str, Any]


class FingerprintRequest(BaseModel):
    """预览一次配置组合的 fingerprint，不落库。

    用于「这个组合跑过没有」的前置查询：拿到指纹后按它查 run 表即可。
    """

    model_config = ConfigDict(extra="forbid")

    param_snapshot_id: int
    model_version_id: int
    dataset_version_id: int
    mode: RetrievalMode = "exact"


class FingerprintResponse(BaseModel):
    config_fingerprint: str
    params_hash: str
    model_config_hash: str
    dataset_content_digest: str
    mode: str


__all__ = [
    "RETRIEVAL_MODES",
    "FingerprintRequest",
    "FingerprintResponse",
    "ModelVersionCreateRequest",
    "ModelVersionResponse",
    "ParamSnapshotCreateRequest",
    "ParamSnapshotFromJavaRequest",
    "ParamSnapshotResponse",
    "RetrievalMode",
]
