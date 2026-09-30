"""Celery 应用定义。

broker 与 result backend 都复用 ``settings.redis_url``（不额外引 Kafka/RabbitMQ）。
关键可靠性配置：

- ``task_acks_late=True``：任务执行成功后才 ack，worker 中途崩溃的任务可被重投；
- ``task_reject_on_worker_lost=True``：worker 被强杀时拒绝任务，交由 broker 重投；
- ``worker_prefetch_multiplier=1``：一次只预取一个任务，长任务场景下多 worker 更公平。
"""

from __future__ import annotations

from celery import Celery

from app.settings.config import get_settings

settings = get_settings()

celery_app = Celery(
    "memory_eval_platform",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # 僵尸 run 回收：必须由**另一个进程**按固定节奏做。
    # worker 崩溃时没人会「顺手」回收自己，只能靠 beat 定期扫。
    #
    # 周期取租约时长的 1/3：太密会让多个 beat 实例互相扫到对方的在途 run
    # （回收本身是幂等的，但会产生无谓的接管与重跑）；太疏则故障恢复变慢。
    # 与 lease 配置同源，改租约时长不必手工同步这里。
    beat_schedule={
        "reap-stale-runs": {
            "task": "app.tasks.eval_tasks.reap_stale_runs",
            "schedule": max(10.0, get_settings().run_lease_seconds / 3),
        },
    },
)


@celery_app.task(name="app.tasks.celery_app.ping")
def ping() -> str:
    """链路自检任务：返回固定字符串，用于验证 worker 能正常消费任务。"""
    return "pong"
