"""参数快照、模型版本与 fingerprint 预览 API（EP-5）。

RBAC 与数据集一致：**写必须 admin，读放行 viewer**。
``/fingerprint`` 是只读预览（按三个 id 算指纹并回显组成部分），故只要求登录。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.auth.deps import AdminUser, CurrentUser, DbSession
from app.connector import JavaEvalClient, JavaEvalError
from app.datasets.service import VersionNotFoundError
from app.params.fingerprint import FrozenKeyError
from app.params.schemas import (
    FingerprintRequest,
    FingerprintResponse,
    ModelVersionCreateRequest,
    ModelVersionResponse,
    ParamSnapshotCreateRequest,
    ParamSnapshotFromJavaRequest,
    ParamSnapshotResponse,
)
from app.params.service import (
    ModelVersionNotFoundError,
    ParamService,
    ParamSnapshotNotFoundError,
)
from app.settings.logging import get_logger

router = APIRouter(prefix="/params", tags=["params"])
logger = get_logger(__name__)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


@router.post("/snapshots", response_model=ParamSnapshotResponse, status_code=status.HTTP_201_CREATED)
def create_param_snapshot(
    payload: ParamSnapshotCreateRequest, db: DbSession, user: AdminUser
) -> ParamSnapshotResponse:
    """手工创建参数快照。

    已存在相同哈希时**返回既有行**（200 语义由响应体表达，状态码仍是 201）——
    这是「取或建」而非「每次新建」，让同一配置组合在多次 run 间复用同一快照。
    """
    try:
        snapshot, _created = ParamService(db).get_or_create_param_snapshot(
            name=payload.name,
            params=payload.params,
            frozen_keys=payload.frozen_keys,
            description=payload.description,
            created_by=user.id,
        )
    except FrozenKeyError as exc:
        raise _unprocessable(str(exc)) from exc
    db.commit()
    return ParamSnapshotResponse.model_validate(snapshot)


@router.post(
    "/snapshots/from-java", response_model=ParamSnapshotResponse, status_code=status.HTTP_201_CREATED
)
def create_param_snapshot_from_java(
    payload: ParamSnapshotFromJavaRequest, db: DbSession, user: AdminUser
) -> ParamSnapshotResponse:
    """从 AgentWrite 拉取当前参数并落快照。

    这是「评测配置以被测系统为准」的落地方式：不手工抄参数，避免抄错或抄漏——
    手抄的参数一旦与线上不符，指纹就在描述一个不存在的配置。
    """
    service = ParamService(db)
    with JavaEvalClient.from_settings() as client:
        try:
            params = service.fetch_params_from_java(client)
        except JavaEvalError as exc:
            # 拉不到参数时不能落一个空快照——那会生成一个看似合法却无意义的指纹。
            logger.error("拉取 AgentWrite 参数失败: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"无法从 AgentWrite 获取参数: {exc.message}",
            ) from exc

    snapshot, _created = service.get_or_create_param_snapshot(
        name=payload.name,
        params=params,
        description=payload.description,
        created_by=user.id,
    )
    db.commit()
    return ParamSnapshotResponse.model_validate(snapshot)


@router.get("/snapshots", response_model=list[ParamSnapshotResponse])
def list_param_snapshots(db: DbSession, _user: CurrentUser) -> list[ParamSnapshotResponse]:
    return [
        ParamSnapshotResponse.model_validate(row) for row in ParamService(db).list_param_snapshots()
    ]


@router.get("/snapshots/{snapshot_id}", response_model=ParamSnapshotResponse)
def get_param_snapshot(snapshot_id: int, db: DbSession, _user: CurrentUser) -> ParamSnapshotResponse:
    try:
        snapshot = ParamService(db).get_param_snapshot(snapshot_id)
    except ParamSnapshotNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    return ParamSnapshotResponse.model_validate(snapshot)


@router.post(
    "/model-versions", response_model=ModelVersionResponse, status_code=status.HTTP_201_CREATED
)
def create_model_version(
    payload: ModelVersionCreateRequest, db: DbSession, user: AdminUser
) -> ModelVersionResponse:
    version, _created = ParamService(db).get_or_create_model_version(
        embedding_model_id=payload.embedding_model_id,
        reranker_model_id=payload.reranker_model_id,
        config=payload.config,
    )
    db.commit()
    return ModelVersionResponse.model_validate(version)


@router.get("/model-versions", response_model=list[ModelVersionResponse])
def list_model_versions(db: DbSession, _user: CurrentUser) -> list[ModelVersionResponse]:
    return [
        ModelVersionResponse.model_validate(row) for row in ParamService(db).list_model_versions()
    ]


@router.get("/model-versions/{version_id}", response_model=ModelVersionResponse)
def get_model_version(version_id: int, db: DbSession, _user: CurrentUser) -> ModelVersionResponse:
    try:
        version = ParamService(db).get_model_version(version_id)
    except ModelVersionNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    return ModelVersionResponse.model_validate(version)


@router.post("/fingerprint", response_model=FingerprintResponse)
def preview_fingerprint(
    payload: FingerprintRequest, db: DbSession, _user: CurrentUser
) -> FingerprintResponse:
    """按 (参数快照, 模型版本, 数据集版本, mode) 算指纹，不落库。

    四项任一不存在即 404——指纹必须指向真实存在的配置组合，否则调用方会拿它去
    建一个外键指向空气的 run。
    """
    try:
        components = ParamService(db).compose_fingerprint(
            param_snapshot_id=payload.param_snapshot_id,
            model_version_id=payload.model_version_id,
            dataset_version_id=payload.dataset_version_id,
            mode=payload.mode,
        )
    except (ParamSnapshotNotFoundError, ModelVersionNotFoundError, VersionNotFoundError) as exc:
        raise _not_found(str(exc)) from exc
    return FingerprintResponse(**components)
