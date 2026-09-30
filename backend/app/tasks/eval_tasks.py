"""评测执行相关的 Celery 任务。

EP-6 只负责**把任务投递出去**；真正的执行逻辑（租约领取、阶段推进、
checkpoint 续跑、心跳、僵尸回收）属于 EP-7。这里刻意让任务体显式失败而不是
静默返回：一个「什么都没做却报成功」的任务会让 run 永远停在 pending，
而调用方以为它在跑——比直接报错难排查得多。
"""

from __future__ import annotations

from app.settings.logging import get_logger
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.eval_tasks.execute_run", bind=True)
def execute_run(self, run_id: str) -> None:
    """执行一次评测 run。

    :param run_id: ``eval_runs.id`` 的字符串形式（UUID 不是 JSON 原生类型，
        统一以字符串跨进程序列化，避免不同 Celery 序列化配置下的歧义）。
    """
    logger.error("execute_run 尚未实现（EP-7 负责），run 将停留在 pending", extra={"run_id": run_id})
    raise NotImplementedError(
        "run 执行器属于 EP-7（Celery worker、lease、checkpoint、reaper）。"
        f"当前 run_id={run_id} 已落库为 pending，实现后可由调度器补投，不会丢失。"
    )
