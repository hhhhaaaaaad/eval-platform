"""应用配置。

用 ``pydantic-settings`` 的 ``BaseSettings``：字段可从环境变量或 ``.env`` 覆盖，
环境变量名大小写不敏感。``get_settings()`` 用 ``lru_cache`` 做进程级单例，保证
同一进程内只解析一次 ``.env``、只构造一个配置对象（迁移、API、worker 共享同一份
语义）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。默认值面向本地开发，生产环境通过环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "memory-eval-platform"
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8093

    # Postgres 用 15432、Redis 用 16380，刻意与 AgentWrite 的
    # MySQL(13306)/Redis(16379) 错开，保证评测平台与业务系统本地互不污染。
    database_url: str = "postgresql+psycopg://eval:eval@localhost:15432/eval_platform"
    redis_url: str = "redis://localhost:16380/0"

    # AgentWrite（Java）侧暴露的评测端点。
    java_eval_base_url: str = "http://localhost:8092"
    java_eval_token: str = ""
    java_eval_timeout_seconds: float = 30.0
    java_eval_max_retries: int = 3

    # AgentWrite 侧评测命名空间的取值区间，必须与其 MemoryProperties.Eval 一致：
    # baseUserId=9_000_000_000、userIdRange=1_000_000。平台派生的 eval_user_id
    # 必须落在这个区间内，否则 Java 端会以 E0403 拒绝（命名空间越界）。
    # 这两个值**不在 /eval/params 里**（那是检索/注入参数，不是安全边界），
    # 故只能配置；改 Java 侧配置时这里必须同步。
    eval_user_id_base: int = 9_000_000_000
    eval_user_id_range: int = 1_000_000

    # 创建 run 后是否投递 Celery 任务。本地无 broker 时可关掉；
    # 关闭不会丢 run——pending 状态的 run 可由调度器补投（idx_runs_pending 即为此存在）。
    enqueue_runs: bool = True

    # 租约时长：worker 超过这个时长未更新心跳即视为已死，run 可被接管/回收。
    # **独占守卫的陈旧阈值复用同一个值**（见 app.runs.service._guard_is_stale）——
    # 两处用不同阈值会得出互相矛盾的判断：「reaper 认为持有者还活着、
    # 守卫认为它的锁可以抢」，进而两个 run 同时以为自己独占。
    run_lease_seconds: int = 300

    # 单次 run 的最大重试次数。租约超时被回收时，未耗尽的重试会把 run 放回
    # pending 重新排队；耗尽则置 failed，避免坏 run 无限占用调度。
    run_max_retries: int = 3

    # 默认值仅用于本地开发，生产必须用环境变量覆盖。
    # 长度须 >= 32 字节：短于 32 字节时 PyJWT 会发 InsecureKeyLengthWarning
    # （RFC 7518 §3.2），且密钥过短会显著削弱 HS256 的抗暴力破解能力。
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-production-0123456789"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级唯一的 ``Settings`` 实例（首次调用时构造并缓存）。"""
    return Settings()
