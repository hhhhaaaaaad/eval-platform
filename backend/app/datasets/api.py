"""数据集 API（EP-4）。

RBAC 沿用 EP-2 的两档划分：**读无需 admin，写必须 admin**。数据集是评测的输入源，
一旦被非授权人员改动，所有历史指标的归因都会失效，故写入收紧到 admin。

HTTP 状态码映射遵循「调用方能否通过改请求来解决」：

- 404：资源不存在；
- 409：与当前状态冲突（已归档、版本号已存在、digest 重复）——改请求内容才有可能成功；
- 422：请求本身结构不合法（case 校验失败、parent 不属于本数据集）。

注意**没有**更新/删除版本的端点，这是刻意的，见 :mod:`app.datasets.service` 的不变性说明。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.auth.deps import AdminUser, CurrentUser, DbSession
from app.datasets.schemas import (
    CaseInput,
    DatasetCreateRequest,
    DatasetResponse,
    DatasetVersionDetail,
    DatasetVersionImportRequest,
    DatasetVersionResponse,
    DatasetVersionSummary,
)
from app.datasets.service import (
    DatasetArchivedError,
    DatasetNotFoundError,
    DatasetService,
    DuplicateCaseError,
    DuplicateDigestError,
    ParentVersionError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
    audit_dataset_change,
)
from app.settings.logging import get_logger

router = APIRouter(prefix="/datasets", tags=["datasets"])
logger = get_logger(__name__)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


@router.post("", response_model=DatasetResponse, status_code=status.HTTP_201_CREATED)
def create_dataset(
    payload: DatasetCreateRequest, request: Request, db: DbSession, user: AdminUser
) -> DatasetResponse:
    service = DatasetService(db)
    dataset = service.create_dataset(
        name=payload.name, description=payload.description, created_by=user.id
    )
    audit_dataset_change(
        db,
        actor_user_id=user.id,
        action="dataset.create",
        resource_type="dataset",
        resource_id=str(dataset.id),
        after={"name": payload.name},
        ip=_client_ip(request),
    )
    db.commit()
    return DatasetResponse.model_validate(dataset)


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    db: DbSession,
    _user: CurrentUser,
    include_archived: bool = False,
) -> list[DatasetResponse]:
    datasets = DatasetService(db).list_datasets(include_archived=include_archived)
    return [DatasetResponse.model_validate(dataset) for dataset in datasets]


@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(dataset_id: int, db: DbSession, _user: CurrentUser) -> DatasetResponse:
    try:
        dataset = DatasetService(db).get_dataset(dataset_id)
    except DatasetNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    return DatasetResponse.model_validate(dataset)


@router.post("/{dataset_id}/archive", response_model=DatasetResponse)
def archive_dataset(
    dataset_id: int, request: Request, db: DbSession, user: AdminUser
) -> DatasetResponse:
    """归档数据集：封存后不能再导入新版本，但历史版本与结果全部保留。"""
    try:
        dataset = DatasetService(db).archive_dataset(dataset_id)
    except DatasetNotFoundError as exc:
        raise _not_found(str(exc)) from exc

    audit_dataset_change(
        db,
        actor_user_id=user.id,
        action="dataset.archive",
        resource_type="dataset",
        resource_id=str(dataset_id),
        after={"is_archived": True},
        ip=_client_ip(request),
    )
    db.commit()
    return DatasetResponse.model_validate(dataset)


@router.post(
    "/{dataset_id}/versions",
    response_model=DatasetVersionSummary,
    status_code=status.HTTP_201_CREATED,
)
def import_version(
    dataset_id: int,
    payload: DatasetVersionImportRequest,
    request: Request,
    db: DbSession,
    user: AdminUser,
) -> DatasetVersionSummary:
    """导入一个不可变版本。整个导入是一个事务：任一步失败则版本与全部 case 一起回滚。"""
    service = DatasetService(db)
    try:
        version = service.import_version(dataset_id, payload, created_by=user.id)
    except DatasetNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except ParentVersionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except (DatasetArchivedError, VersionAlreadyExistsError, DuplicateDigestError) as exc:
        raise _conflict(str(exc)) from exc
    except DuplicateCaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "content_hash": exc.content_hash, "case_indexes": exc.indexes},
        ) from exc

    audit_dataset_change(
        db,
        actor_user_id=user.id,
        action="dataset.version.import",
        resource_type="dataset_version",
        resource_id=str(version.id),
        after={
            "dataset_id": dataset_id,
            "version": payload.version,
            "case_count": len(payload.cases),
            "content_digest": version.content_digest,
        },
        ip=_client_ip(request),
    )
    db.commit()
    return DatasetVersionSummary(
        **DatasetVersionResponse.model_validate(version).model_dump(),
        case_count=len(payload.cases),
    )


@router.get("/{dataset_id}/versions", response_model=list[DatasetVersionSummary])
def list_versions(dataset_id: int, db: DbSession, _user: CurrentUser) -> list[DatasetVersionSummary]:
    try:
        rows = DatasetService(db).list_versions(dataset_id)
    except DatasetNotFoundError as exc:
        raise _not_found(str(exc)) from exc

    return [
        DatasetVersionSummary(
            **DatasetVersionResponse.model_validate(version).model_dump(),
            case_count=count,
        )
        for version, count in rows
    ]


@router.get("/versions/{version_id}", response_model=DatasetVersionDetail)
def get_version(version_id: int, db: DbSession, _user: CurrentUser) -> DatasetVersionDetail:
    """版本详情：带全部 case，用于审计与导出（可据此完全复现一份评测集）。"""
    try:
        version, cases = DatasetService(db).get_version(version_id)
    except VersionNotFoundError as exc:
        raise _not_found(str(exc)) from exc

    return DatasetVersionDetail(
        **DatasetVersionResponse.model_validate(version).model_dump(),
        config=version.config,
        cases=[
            CaseInput(
                case_type=case.case_type,
                group_key=case.group_key,
                payload=case.payload,
                ground_truth=case.ground_truth,
            )
            for case in cases
        ],
    )
