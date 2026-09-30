"""run 租约：领取、心跳、结束与僵尸回收（EP-7 的状态机核心）。

**为什么用租约而不是「worker 拉起来就一直跑」**：worker 会崩、会被 OOM 杀掉、
会卡在某个 HTTP 调用里。没有租约的话，一个死掉的 run 会永远停在 running，
既占着 ``uq_runs_active_cfg`` 的槽位（同配置再也跑不起来），也占着独占守卫。
租约把「进程还活着」这件事变成**可证伪**的：心跳停止超过阈值即判定死亡。

**所有状态迁移都是单条 CAS UPDATE**，不做「先查后写」：

    UPDATE ... WHERE id = :id AND lease_owner = :owner AND status = 'running'

这样做的直接好处是**心跳同时充当取消检查**——run 被取消（status 变
cancelled）或被接管（lease_owner 变了）后，心跳的 ``rowcount`` 立刻变 0，
worker 据此自行退出。不需要额外查一次状态，也不可能出现「查到还在跑、
但写完就已被取消」的窗口。

``fencing_version`` 在每次领取时自增：旧 worker 即使没察觉自己已被接管，
它的写入也会因为带的是旧版本号而被拒——这是防脑裂的第二道闩（第一道是
``lease_owner`` 的 CAS）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.runs.models import Run, RunGuard
from app.settings.config import get_settings
from app.settings.logging import get_logger

logger = get_logger(__name__)

#: 可被领取的状态：pending（待跑）或 running 但心跳已过期（前任已死，接手）
_CLAIM_SQL = """
UPDATE eval_runs
SET status = 'running',
    lease_owner = :owner,
    heartbeat_at = now(),
    started_at = COALESCE(started_at, now()),
    fencing_version = fencing_version + 1,
    updated_at = now()
WHERE id = :run_id
  AND (
        status = 'pending'
     OR (status = 'running' AND (heartbeat_at IS NULL
                                 OR heartbeat_at < now() - make_interval(secs => :lease_seconds)))
      )
RETURNING fencing_version
"""

#: 心跳。WHERE 里的 status/owner 条件是「我是否仍是合法持有者」的判据。
_HEARTBEAT_SQL = """
UPDATE eval_runs
SET heartbeat_at = now(), updated_at = now()
WHERE id = :run_id
  AND lease_owner = :owner
  AND status = 'running'
"""

#: 结束：CAS on owner，防止已被接管的旧 worker 写入最终结果。
_FINISH_SQL = """
UPDATE eval_runs
SET status = :status,
    finished_at = now(),
    lease_owner = NULL,
    heartbeat_at = NULL,
    error_message = :error_message,
    result_summary = CAST(:result_summary AS jsonb),
    progress = CASE WHEN :status = 'succeeded' THEN 100 ELSE progress END,
    updated_at = now()
WHERE id = :run_id
  AND lease_owner = :owner
  AND status = 'running'
"""

#: 僵尸回收（第一段）：重试未耗尽的放回 pending 重新排队。
_REAP_REQUEUE_SQL = """
UPDATE eval_runs
SET status = 'pending',
    retry_count = retry_count + 1,
    lease_owner = NULL,
    heartbeat_at = NULL,
    error_message = :reason,
    updated_at = now()
WHERE status = 'running'
  AND (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(secs => :lease_seconds))
  AND retry_count < :max_retries
RETURNING id
"""

#: 僵尸回收（第二段）：重试耗尽的置 failed。
_REAP_FAIL_SQL = """
UPDATE eval_runs
SET status = 'failed',
    finished_at = now(),
    lease_owner = NULL,
    heartbeat_at = NULL,
    error_message = :reason,
    updated_at = now()
WHERE status = 'running'
  AND (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(secs => :lease_seconds))
  AND retry_count >= :max_retries
RETURNING id
"""


@dataclass(frozen=True)
class ClaimResult:
    """领取结果。``claimed=False`` 时 ``fencing_version`` 无意义。"""

    claimed: bool
    fencing_version: int | None = None


@dataclass(frozen=True)
class ReapResult:
    requeued: tuple[uuid.UUID, ...]
    failed: tuple[uuid.UUID, ...]

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.failed)


class LeaseManager:
    """run 租约的读写入口。

    **不 commit**——事务边界由调用方控制（与其余 service 一致）。
    reaper 通常由 Celery beat 周期触发，由它的任务体负责提交。
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- 领取 -------------------------------------------------------------

    def claim(self, run_id: uuid.UUID, owner: str, *, lease_seconds: int | None = None) -> ClaimResult:
        """尝试领取 run 的租约。

        可领取的条件是二选一：run 处于 pending，或处于 running 但前任的心跳已过期。
        后者让「worker 崩溃后换一个 worker 接着跑」成为可能，而无需人工介入。
        """
        seconds = lease_seconds if lease_seconds is not None else get_settings().run_lease_seconds
        row = self._db.execute(
            text(_CLAIM_SQL),
            {"run_id": str(run_id), "owner": owner, "lease_seconds": seconds},
        ).first()

        if row is None:
            # 拿不到没有任何异常可言：这就是「这个 run 现在不归我」的正常结果。
            return ClaimResult(claimed=False)

        fencing_version = int(row[0])
        logger.info(
            "领取 run 租约: owner=%s fencing_version=%s", owner, fencing_version,
            extra={"run_id": str(run_id)},
        )
        return ClaimResult(claimed=True, fencing_version=fencing_version)

    # -- 心跳 -------------------------------------------------------------

    def heartbeat(self, run_id: uuid.UUID, owner: str) -> bool:
        """续租。返回 False 表示**已失去租约**，worker 应当停止工作。

        失去租约的三种情形，返回值都是 False，worker 不需要（也无法）区分：

        - run 被取消（status 变成 cancelled）；
        - 心跳超时被 reaper 回收（lease_owner 被清空）；
        - 被另一个 worker 接管（lease_owner 换了人）。

        把它们统一成「不再持有」是刻意的：worker 的唯一正确反应都是立即停止，
        区分原因只会诱导出「某些情况下可以继续」的错误分支。
        """
        result = self._db.execute(text(_HEARTBEAT_SQL), {"run_id": str(run_id), "owner": owner})
        alive = result.rowcount == 1
        if not alive:
            logger.warning("心跳失败，已失去租约", extra={"run_id": str(run_id)})
        return alive

    # -- 结束 -------------------------------------------------------------

    def finish(
        self,
        run_id: uuid.UUID,
        owner: str,
        *,
        status: str,
        error_message: str | None = None,
        result_summary: dict | None = None,
    ) -> bool:
        """结束 run（succeeded / failed）。返回是否写入成功。

        同样是 CAS：已被回收或接管的 run，其旧 worker 写不进结果——
        否则一个卡了十分钟的 worker 醒来后会把已被别人跑完的结果覆盖掉。
        """
        if status not in ("succeeded", "failed"):
            raise ValueError(f"finish 只接受终态 succeeded/failed，实际: {status!r}")

        result = self._db.execute(
            text(_FINISH_SQL),
            {
                "run_id": str(run_id),
                "owner": owner,
                "status": status,
                "error_message": error_message,
                "result_summary": json.dumps(result_summary or {}, ensure_ascii=False),
            },
        )
        written = result.rowcount == 1
        if not written:
            logger.warning(
                "run 结束写入被拒（租约已易主或状态已变）",
                extra={"run_id": str(run_id), "status": status},
            )
        return written

    # -- 僵尸回收 ---------------------------------------------------------

    def reap_stale_runs(self, *, lease_seconds: int | None = None) -> ReapResult:
        """回收心跳过期的 running run。

        两段式：重试次数未耗尽的放回 pending（**重试而不是直接失败**——worker
        崩溃多半是偶发的 OOM 或网络抖动，重跑一次常常就好了），耗尽的置 failed
        （避免一个必然失败的 run 无限占用调度与命名空间）。

        两条 UPDATE 的 WHERE 条件完全相同，且第一段执行后这些行已不再是 running，
        因此第二段只会命中「重试已耗尽」的那批，不会重复处理。
        """
        settings = get_settings()
        seconds = lease_seconds if lease_seconds is not None else settings.run_lease_seconds
        reason = f"租约超时（{seconds}s 无心跳），已由 reaper 回收"

        requeued = tuple(
            uuid.UUID(str(row[0]))
            for row in self._db.execute(
                text(_REAP_REQUEUE_SQL),
                {"lease_seconds": seconds, "max_retries": settings.run_max_retries, "reason": reason},
            ).all()
        )
        failed = tuple(
            uuid.UUID(str(row[0]))
            for row in self._db.execute(
                text(_REAP_FAIL_SQL),
                {"lease_seconds": seconds, "max_retries": settings.run_max_retries, "reason": reason},
            ).all()
        )

        reaped = requeued + failed
        if reaped:
            self._release_guards_for(reaped)
            # 回收是「系统自愈」事件：不告警的话，worker 频繁崩溃会被静默消化，
            # 指标悄悄变差而没人知道根因是基础设施不稳。
            logger.warning(
                "回收僵尸 run: requeued=%d failed=%d", len(requeued), len(failed)
            )

        return ReapResult(requeued=requeued, failed=failed)

    def _release_guards_for(self, run_ids: tuple[uuid.UUID, ...]) -> None:
        """如果被回收的 run 持有独占守卫，一并释放。

        漏掉这一步的后果很具体：一个独占 run 崩溃被回收后，守卫仍指着它，
        所有后续独占 run 都会被拒——直到守卫自己超时。回收时就顺手放掉，
        故障恢复时间从「等一个阈值」缩短到「下一次 reaper 周期」。
        """
        for run_id in run_ids:
            guard = self._db.get(RunGuard, 1)
            if guard is not None and guard.exclusive_owner == run_id:
                guard.exclusive_owner = None
                guard.owner_run_id = None
                guard.acquired_at = None
                guard.heartbeat_at = None
        self._db.flush()

    # -- 只读辅助 ---------------------------------------------------------

    def get_run(self, run_id: uuid.UUID) -> Run | None:
        return self._db.get(Run, run_id)

    def is_owned_by(self, run_id: uuid.UUID, owner: str) -> bool:
        """查询用：某 run 当前是否由 ``owner`` 持有。"""
        run = self._db.get(Run, run_id)
        return run is not None and run.lease_owner == owner and run.status == "running"
