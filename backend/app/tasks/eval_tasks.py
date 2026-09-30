"""评测执行相关的 Celery 任务。

任务体保持**极薄**：开一个独立 session、构造 pipeline、跑、提交、记日志。
业务逻辑全在 :mod:`app.engine.pipeline`，这样编排的分支与顺序约束可以用
普通测试穷举验证，不必起 worker——worker 里才发现的 bug 排查成本高一个量级。

**为何自己管事务而不复用 API 层的 session 依赖**：worker 不在请求上下文里，
session 生命周期必须由任务体自己保证（异常路径也要关掉，否则连接泄漏，
跑几百个 run 后连接池就干了）。
"""

from __future__ import annotations

import uuid

from app.connector import JavaEvalClient
from app.db.session import get_session_factory
from app.engine.pipeline import RunPipeline
from app.runs.lease import LeaseManager
from app.settings.logging import get_logger
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)


def _owner_name() -> str:
    """租约持有者标识：hostname+pid 足够区分并发 worker。

    不用 UUID：排障时「谁在跑这个 run」要能从标识直接读出来，随机串只能靠反查日志。

    **必须是纯 ASCII**：这个值会进 ``X-Eval-Run-Id`` 请求头，而 HTTP 头值按规范
    是 ASCII（httpx 会 ``value.encode("ascii")``，非 ASCII 直接抛 UnicodeEncodeError）。
    中文 Windows 机器名（如「某某的电脑」）会让 worker 一启动任务就崩，
    且崩在**错误处理路径**上——失败记录都写不下去。实跑时踩过这个坑。
    非 ASCII 机器名退化为哈希后缀：失去可读性，但换来跨环境可用。
    """
    import hashlib
    import os
    import socket

    host = socket.gethostname()
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:8]
        logger.info("机器名含非 ASCII 字符，owner 退化为哈希后缀：%s -> host-%s", host, digest)
        host = f"host-{digest}"
    return f"{host}:{os.getpid()}"


@celery_app.task(name="app.tasks.eval_tasks.execute_run", bind=True)
def execute_run(self, run_id: str) -> str:
    """执行一次评测 run。

    :param run_id: ``eval_runs.id`` 的字符串形式（UUID 不是 JSON 原生类型，
        统一以字符串跨进程序列化）。
    :return: 结局状态，便于在 Flower 里直接看到而不必查库。
    """
    parsed = uuid.UUID(run_id)
    session = get_session_factory()()
    try:
        pipeline = RunPipeline(session, JavaEvalClient.from_settings, owner=_owner_name())
        outcome = pipeline.execute(parsed)
        session.commit()

        if outcome.status == "succeeded":
            logger.info(
                "run 执行完成: %s", outcome.metrics, extra={"run_id": run_id, "status": "succeeded"}
            )
        elif outcome.status in ("failed", "aborted"):
            # 任务返回成功（不抛异常）是刻意的：run 的失败已经写进库了，
            # 抛出去只会让 Celery 重投，而重投的是同一个已判定失败的 run——
            # 没有意义，还会掩盖真正的失败原因。
            logger.error(
                "run 执行结束（非成功）: status=%s error=%s",
                outcome.status,
                outcome.error,
                extra={"run_id": run_id, "status": outcome.status},
            )
        else:
            logger.info(
                "run 未被本 worker 接管，跳过", extra={"run_id": run_id, "status": outcome.status}
            )
        return outcome.status
    except Exception:
        # 未预期异常：必须 rollback，否则这个 session 的失败事务会一直挂着。
        session.rollback()
        logger.exception("run 执行发生未预期异常", extra={"run_id": run_id})
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.eval_tasks.reap_stale_runs")
def reap_stale_runs() -> dict[str, int]:
    """定时回收心跳过期的 run（由 beat 周期触发）。

    reaper 做成独立任务而非塞进执行流程：worker 崩溃时没人会「顺手」回收自己，
    回收必须由**另一个进程**按固定节奏做。
    """
    session = get_session_factory()()
    try:
        result = LeaseManager(session).reap_stale_runs()
        session.commit()
        if result.total:
            logger.warning("本次回收 %d 个僵尸 run", result.total, extra={"status": "reaped"})
        return {"requeued": len(result.requeued), "failed": len(result.failed)}
    except Exception:
        session.rollback()
        logger.exception("回收僵尸 run 失败")
        raise
    finally:
        session.close()
