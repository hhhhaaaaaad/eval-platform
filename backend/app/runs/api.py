"""实验与 run 的 API（EP-6）。

投递时机是这里唯一需要留神的地方：**先 commit 再投递**。反过来的话，
worker 可能在事务提交前就拿到任务去查 run，查不到——一个只在负载高时偶发的
「run 不存在」错误。代价是投递失败时 run 已落库，但那正是 pending 状态存在的意义。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.audit.service import write_audit
from app.auth.deps import AdminUser, CurrentUser, DbSession
from app.datasets.service import VersionNotFoundError
from app.params.service import ModelVersionNotFoundError, ParamSnapshotNotFoundError
from app.runs import dispatch
from app.runs.schemas import (
    ExperimentCreateRequest,
    ExperimentResponse,
    RunCreateRequest,
    RunCreateResponse,
    RunResponse,
)
from app.runs.service import (
    ExclusiveGuardUnavailableError,
    ExperimentNotFoundError,
    RunConflictError,
    RunNotFoundError,
    RunService,
    RunStateError,
)
from app.settings.logging import get_logger

router = APIRouter(tags=["runs"])
logger = get_logger(__name__)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


# ---------------------------------------------------------------------------
# 实验
# ---------------------------------------------------------------------------


@router.post(
    "/experiments", response_model=ExperimentResponse, status_code=status.HTTP_201_CREATED
)
def create_experiment(
    payload: ExperimentCreateRequest, request: Request, db: DbSession, user: AdminUser
) -> ExperimentResponse:
    experiment = RunService(db).create_experiment(payload, created_by=user.id)
    write_audit(
        db,
        actor_user_id=user.id,
        action="experiment.create",
        resource_type="experiment",
        resource_id=str(experiment.id),
        after={"name": payload.name},
        ip=_client_ip(request),
    )
    db.commit()
    return ExperimentResponse.model_validate(experiment)


@router.get("/experiments", response_model=list[ExperimentResponse])
def list_experiments(db: DbSession, _user: CurrentUser) -> list[ExperimentResponse]:
    return [ExperimentResponse.model_validate(row) for row in RunService(db).list_experiments()]


@router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
def get_experiment(
    experiment_id: uuid.UUID, db: DbSession, _user: CurrentUser
) -> ExperimentResponse:
    try:
        experiment = RunService(db).get_experiment(experiment_id)
    except ExperimentNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    return ExperimentResponse.model_validate(experiment)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@router.post("/runs", response_model=RunCreateResponse, status_code=status.HTTP_201_CREATED)
def create_run(
    payload: RunCreateRequest, request: Request, db: DbSession, user: AdminUser
) -> RunCreateResponse:
    """创建 run 并投递执行任务。

    响应里的 ``created`` 区分「本次创建」与「命中幂等键返回既有 run」——
    两者都是 201，因为从调用方视角「你要的 run 存在且可用」这件事都成立了。
    """
    service = RunService(db)
    try:
        run, created = service.create_run(payload, created_by=user.id)
    except ExperimentNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except (ParamSnapshotNotFoundError, ModelVersionNotFoundError, VersionNotFoundError) as exc:
        # 配置组合指向不存在的行：请求内容有问题（422）而不是服务端错误。
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ExclusiveGuardUnavailableError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RunConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "constraint": exc.constraint},
        ) from exc

    if created:
        write_audit(
            db,
            actor_user_id=user.id,
            action="run.create",
            resource_type="run",
            resource_id=str(run.id),
            after={
                "config_fingerprint": run.config_fingerprint,
                "eval_user_id": run.eval_user_id,
                "mode": run.mode,
                "exclusive": run.exclusive,
            },
            ip=_client_ip(request),
        )
    db.commit()

    # 必须在 commit 之后投递：worker 若在事务可见前查到 run，会报「不存在」。
    enqueued = dispatch.dispatch_run(run.id) if created else False

    # 不能直接 RunCreateResponse.model_validate(run)：created/enqueued 不是 Run 的属性，
    # 从 ORM 对象校验会因缺字段报错。先取基类字段，再补上这两个创建期标记。
    return RunCreateResponse(
        **RunResponse.model_validate(run).model_dump(),
        created=created,
        enqueued=enqueued,
    )


@router.get("/runs", response_model=list[RunResponse])
def list_runs(
    db: DbSession,
    _user: CurrentUser,
    run_status: str | None = Query(default=None, alias="status"),
    config_fingerprint: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[RunResponse]:
    runs = RunService(db).list_runs(
        status=run_status, config_fingerprint=config_fingerprint, limit=limit
    )
    return [RunResponse.model_validate(row) for row in runs]


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: uuid.UUID, db: DbSession, _user: CurrentUser) -> RunResponse:
    try:
        run = RunService(db).get_run(run_id)
    except RunNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    return RunResponse.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: uuid.UUID, request: Request, db: DbSession, user: AdminUser
) -> RunResponse:
    """请求取消 run。仅 pending/running 可取消，终态返回 409。

    取消只置状态，不直接杀 worker——worker 在自己的心跳循环里发现状态变为
    cancelled 后自行退出。这样「取消」在分布式下才可靠：无法保证能杀掉一个
    可能正卡在 HTTP 调用里的进程，但可以保证它下次检查时不再继续。
    """
    service = RunService(db)
    try:
        run = service.cancel_run(run_id)
    except RunNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except RunStateError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    write_audit(
        db,
        actor_user_id=user.id,
        action="run.cancel",
        resource_type="run",
        resource_id=str(run_id),
        after={"status": "cancelled"},
        ip=_client_ip(request),
    )
    db.commit()
    return RunResponse.model_validate(run)


@router.get("/runs/summary/counts", response_model=dict[str, int])
def run_state_counts(db: DbSession, _user: CurrentUser) -> dict[str, int]:
    """各状态的 run 数量，供概览页使用。

    路径刻意放在 ``/runs/{run_id}`` 之前语义上不冲突——FastAPI 按声明顺序匹配，
    但 ``summary/counts`` 有两段，不会与单段 UUID 路径混淆。
    """
    return RunService(db).run_state_counts()
