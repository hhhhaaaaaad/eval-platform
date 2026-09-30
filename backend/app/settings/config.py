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
