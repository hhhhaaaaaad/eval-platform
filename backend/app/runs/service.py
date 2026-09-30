"""run 创建、幂等、并发约束与独占守卫（EP-6）。

**这一层的核心是「四语义分离」**——四个概念名字相近但职责完全不同，混用会出硬故障：

====================  ==================================  ================================
概念                   来源                                 混淆的后果
====================  ==================================  ================================
run identity          DB 生成的 UUID                       用 fingerprint 当 id → 同一配置重跑
                                                           会覆盖历史结果
config fingerprint    参数+模型+数据集+mode 的摘要           用 idempotency key 当指纹 → 换配置
                                                           重跑被误判为重复提交
idempotency key       调用方提供                             不区分 → 重试产生两个 run
concurrency guard     partial unique index + guard 表       靠应用层 if 判断 → 竞态下双双落库
====================  ==================================  ================================

并发正确性**不依赖应用层检查**：所有唯一性最终由 partial unique index 兜底，
应用层的预检只是为了给出更友好的错误信息。竞态发生时，DB 抛 IntegrityError，
本层负责把约束名翻译成领域错误。

独占守卫（``eval_run_guard``）与上面三者正交：它限制的是「全库同时只能有一个独占 run」，
用 ``SELECT ... FOR UPDATE`` 串行化，与 run 的写入在**同一事务**内完成——
若拆成两个事务，「先占锁、后写 run」之间会出现窗口，写 run 失败时锁已经占住了。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.params.service import ParamService
from app.runs.eval_user import derive_eval_user_id
from app.runs.models import Experiment, Run, RunGuard
from app.runs.schemas import ExperimentCreateRequest, RunCreateRequest
from app.settings.config import get_settings
from app.settings.logging import get_logger

logger = get_logger(__name__)

#: partial unique index 名 → 面向用户的冲突说明。名字必须与迁移里的逐字一致。
_ACTIVE_CONSTRAINTS = {
    "uq_runs_active_cfg": "该配置指纹已有进行中的 run",
    "uq_runs_active_user": "该评测命名空间已有进行中的 run",
}

CONSTRAINT_IDEMPOTENCY = "uq_runs_idempotency"


class RunError(Exception):
    """run 领域错误基类。"""


class RunNotFoundError(RunError):
    pass


class ExperimentNotFoundError(RunError):
    pass


class RunConflictError(RunError):
    """与既有的活跃 run 或独占守卫冲突（409 语义）。

    ``constraint`` 保留触发冲突的约束名，便于排障时区分是「同配置」还是
    「同命名空间」——两者在 per-config 派生下通常是同一件事，但若将来
    派生规则改变，这个字段就是唯一能区分的线索。
    """

    def __init__(self, message: str, *, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


class ExclusiveGuardUnavailableError(RunConflictError):
    """独占守卫正被另一个 run 持有，且其心跳仍然新鲜。"""


class RunStateError(RunError):
    """状态机不允许该操作（如取消一个已完成的 run）。"""


class RunService:
    def __init__(self, db: Session) -> None:
        self._db = db

    # -- 实验 -------------------------------------------------------------

    def create_experiment(
        self, payload: ExperimentCreateRequest, *, created_by: int | None
    ) -> Experiment:
        experiment = Experiment(
            name=payload.name, description=payload.description, created_by=created_by
        )
        self._db.add(experiment)
        self._db.flush()
        return experiment

    def list_experiments(self) -> list[Experiment]:
        return list(self._db.execute(select(Experiment).order_by(Experiment.created_at)).scalars())

    def get_experiment(self, experiment_id: uuid.UUID) -> Experiment:
        experiment = self._db.get(Experiment, experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(f"实验不存在: id={experiment_id}")
        return experiment

    # -- run 创建 ---------------------------------------------------------

    def create_run(
        self, payload: RunCreateRequest, *, created_by: int | None
    ) -> tuple[Run, bool]:
        """创建 run。返回 ``(run, created)``；``created=False`` 表示命中幂等键。

        步骤顺序有讲究：

        1. **先查幂等键**——重复提交是最高频的「非错误」路径，先走掉它，
           后面就不必为了它做任何特殊处理；
        2. 再算指纹与派生命名空间——成本低且无副作用；
        3. 再取独占守卫——拿锁比插行更容易失败，让贵的那步（写 run）最后做；
        4. 最后插 run 并在同一事务内提交由调用方完成。
        """
        if payload.idempotency_key is not None:
            existing = self._find_by_idempotency_key(payload.idempotency_key)
            if existing is not None:
                logger.info(
                    "命中幂等键，返回既有 run",
                    extra={"run_id": str(existing.id)},
                )
                return existing, False

        if payload.experiment_id is not None:
            self.get_experiment(payload.experiment_id)  # 不存在即报错，避免外键失败敷衍成 500

        components = ParamService(self._db).compose_fingerprint(
            param_snapshot_id=payload.param_snapshot_id,
            model_version_id=payload.model_version_id,
            dataset_version_id=payload.dataset_version_id,
            mode=payload.mode,
        )
        fingerprint = components["config_fingerprint"]
        eval_user_id = derive_eval_user_id(fingerprint)

        # 客户端生成 run id 而不是用 server_default：独占守卫需要在插入 run **之前**
        # 就把 owner 写成这个 id，而 server_default 的值要等 flush 后才拿得到。
        run_id = uuid.uuid4()
        if payload.exclusive:
            self._acquire_exclusive_guard(run_id)

        run = Run(
            id=run_id,
            config_fingerprint=fingerprint,
            idempotency_key=payload.idempotency_key,
            experiment_id=payload.experiment_id,
            dataset_version_id=payload.dataset_version_id,
            param_snapshot_id=payload.param_snapshot_id,
            model_version_id=payload.model_version_id,
            eval_user_id=eval_user_id,
            mode=payload.mode,
            status="pending",
            exclusive=payload.exclusive,
            checkpoint={"completed_stages": [], "case_limit": payload.case_limit},
            created_by=created_by,
        )
        self._db.add(run)

        try:
            self._db.flush()
        except IntegrityError as exc:
            return self._handle_insert_conflict(exc, payload)

        logger.info(
            "创建 run",
            extra={"run_id": str(run.id), "status": run.status},
        )
        logger.debug(
            "run 配置: fingerprint=%s eval_user_id=%s mode=%s exclusive=%s",
            fingerprint,
            eval_user_id,
            payload.mode,
            payload.exclusive,
        )
        return run, True

    def _handle_insert_conflict(
        self, exc: IntegrityError, payload: RunCreateRequest
    ) -> tuple[Run, bool]:
        """把数据库约束冲突翻译成领域错误。

        **必须先 rollback**：Postgres 在一个语句失败后会把当前事务标记为 aborted，
        此后的任何查询都会报 ``InFailedSqlTransaction``。不 rollback 就没法重读既有行。
        """
        constraint = _constraint_name(exc)
        self._db.rollback()

        if constraint == CONSTRAINT_IDEMPOTENCY and payload.idempotency_key is not None:
            # 竞态：另一个请求刚用同一个幂等键落了 run。语义上仍是「命中幂等键」，
            # 因此重读并返回它，而不是报错——对调用方而言，重试成功即可。
            existing = self._find_by_idempotency_key(payload.idempotency_key)
            if existing is not None:
                logger.info(
                    "并发提交幂等键，返回先落库的 run",
                    extra={"run_id": str(existing.id)},
                )
                return existing, False

        if constraint in _ACTIVE_CONSTRAINTS:
            raise RunConflictError(_ACTIVE_CONSTRAINTS[constraint], constraint=constraint) from exc

        # 未知约束：原样抛回。这里显式写 `raise exc` 而不是裸 `raise`——
        # 裸 raise 依赖 sys.exc_info() 的隐式状态（在另一个函数帧里也能用，
        # 但可读性差且容易被后续重构破坏）。
        raise exc

    def _find_by_idempotency_key(self, key: str) -> Run | None:
        return self._db.execute(
            select(Run).where(Run.idempotency_key == key)
        ).scalar_one_or_none()

    # -- 独占守卫 ---------------------------------------------------------

    def _acquire_exclusive_guard(self, run_id: uuid.UUID) -> None:
        """在**当前事务内**取独占守卫。

        用 ``SELECT ... FOR UPDATE`` 行锁：锁持有到本事务提交为止，因此
        「检查守卫空闲 → 写入 owner → 插入 run」三步之间没有窗口。
        若改用「先查一次、稍后再写」，两个并发请求会同时看到空闲并双双取锁成功。
        """
        guard = self._db.execute(
            select(RunGuard).where(RunGuard.id == 1).with_for_update()
        ).scalar_one_or_none()

        if guard is None:
            # 守卫行由迁移的 INSERT 初始化；缺失说明迁移没跑完或被人删了。
            # 此时必须失败而不是自建——静默自建会掩盖「迁移状态异常」这个更重要的问题。
            raise RunError(
                "eval_run_guard 未初始化（应有一行 id=1）。请确认迁移已执行到 head。"
            )

        now = datetime.now(UTC)
        if guard.exclusive_owner is not None and not self._guard_is_stale(guard, now):
            raise ExclusiveGuardUnavailableError(
                f"独占守卫被 run {guard.exclusive_owner} 持有且心跳仍新鲜"
                f"（owner_heartbeat={guard.heartbeat_at}）",
                constraint="exclusive_guard",
            )

        if guard.exclusive_owner is not None:
            logger.warning(
                "接管超时的独占守卫",
                extra={"run_id": str(guard.exclusive_owner)},
            )

        guard.exclusive_owner = run_id
        guard.owner_run_id = run_id
        guard.acquired_at = now
        guard.heartbeat_at = now
        self._db.flush()

    @staticmethod
    def _guard_is_stale(guard: RunGuard, now: datetime) -> bool:
        """持锁 run 的心跳是否已过期。

        ``heartbeat_at`` 为空视为**过期**：守卫的语义是「持锁者定期报到」，
        从未报到的锁无法证明持有者活着，按死锁处理比一直堵着更安全。
        """
        if guard.heartbeat_at is None:
            return True
        stale_after = timedelta(seconds=get_settings().exclusive_guard_stale_seconds)
        return (now - guard.heartbeat_at) > stale_after

    def release_exclusive_guard(self, run_id: uuid.UUID) -> bool:
        """按 CAS 释放守卫：只有当前持有者能释放。

        不加 CAS 的话，一个超时被接管的旧 run 结束时会把**新持有者**的锁清掉，
        导致第三个 run 趁虚而入——这正是 fencing 要防的那类脑裂。
        """
        guard = self._db.execute(
            select(RunGuard).where(RunGuard.id == 1).with_for_update()
        ).scalar_one_or_none()
        if guard is None or guard.exclusive_owner != run_id:
            return False

        guard.exclusive_owner = None
        guard.owner_run_id = None
        guard.acquired_at = None
        guard.heartbeat_at = None
        self._db.flush()
        return True

    # -- run 查询与状态 ---------------------------------------------------

    def get_run(self, run_id: uuid.UUID) -> Run:
        run = self._db.get(Run, run_id)
        if run is None:
            raise RunNotFoundError(f"run 不存在: id={run_id}")
        return run

    def list_runs(
        self,
        *,
        status: str | None = None,
        config_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[Run]:
        stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        if config_fingerprint is not None:
            stmt = stmt.where(Run.config_fingerprint == config_fingerprint)
        return list(self._db.execute(stmt).scalars())

    def cancel_run(self, run_id: uuid.UUID) -> Run:
        """请求取消。仅 pending/running 可取消。

        读改写包在 ``SELECT ... FOR UPDATE`` 里：并发下两个取消请求、
        或「取消」与「worker 置为 succeeded」同时到达时，靠行锁串行化，
        避免把一个已完成的 run 覆写成 cancelled（那会让结果凭空消失）。
        """
        run = self._db.execute(select(Run).where(Run.id == run_id).with_for_update()).scalar_one_or_none()
        if run is None:
            raise RunNotFoundError(f"run 不存在: id={run_id}")

        if run.status not in ("pending", "running"):
            raise RunStateError(f"run 处于终态 {run.status}，不能取消")

        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
        self._db.flush()
        logger.info("取消 run", extra={"run_id": str(run_id)})
        return run

    def run_state_counts(self) -> dict[str, int]:
        """按状态计数，供概览接口使用。"""
        rows = self._db.execute(
            select(Run.status, func.count(Run.id)).group_by(Run.status)
        ).all()
        return {status: count for status, count in rows}


def _constraint_name(exc: IntegrityError) -> str | None:
    """从 psycopg 异常里取出违反的约束名。

    取不到时返回 None——调用方据此走「未知约束原样抛出」的分支，
    而不是把它误判成某个已知冲突。
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)
