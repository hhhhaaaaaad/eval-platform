"""run 任务投递。

单独成模块是为了让「投递」可被替换：测试不需要 broker，生产需要重试与告警。
API 层按 ``dispatch.dispatch_run(...)`` 调用（模块属性访问而非直接 import 函数名），
这样测试可以用 monkeypatch 替换掉整个投递动作。

**投递失败不算创建失败**：run 已经落库为 ``pending``，这是可恢复状态——
调度器可以按 ``idx_runs_pending`` 补投。因此投递异常只记日志、返回 False，
不向上抛。若把它当创建失败，调用方会重试创建，而重试会撞上
``uq_runs_active_cfg`` 得到 409，反而更难理解。
"""

from __future__ import annotations

import uuid

from app.settings.config import get_settings
from app.settings.logging import get_logger
from app.tasks.eval_tasks import execute_run

logger = get_logger(__name__)


def dispatch_run(run_id: uuid.UUID) -> bool:
    """把 run 投递给 Celery worker；返回是否投递成功。"""
    if not get_settings().enqueue_runs:
        logger.info("enqueue_runs 已关闭，run 留在 pending 等待调度器补投", extra={"run_id": str(run_id)})
        return False

    try:
        execute_run.delay(str(run_id))
    except Exception as exc:
        logger.error(
            "投递 run 任务失败，run 留在 pending 等待调度器补投",
            extra={"run_id": str(run_id), "status": None},
            exc_info=exc,
        )
        return False

    logger.info("已投递 run 任务", extra={"run_id": str(run_id)})
    return True
