"""结构化日志与 request id 传播。

设计要点：

- request id 放在 ``ContextVar`` 里，跨 ``await`` 边界仍可读取，无需层层透传；
- JSON 格式供容器/日志采集消费，Plain 格式供本地人读，两者都带 request id；
- ``configure_logging`` 幂等（先清空 root handlers），避免测试或热重载时重复输出。
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# 默认 "-"，即不在请求上下文中时日志也能正常输出。
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# JsonFormatter/PlainFormatter 额外提取的关联字段（仅当 record 上存在时才写），
# 便于把一条日志串到具体的 run/case/stage/endpoint。
_EXTRA_FIELDS = ("run_id", "case_id", "stage", "endpoint", "status", "attempt")


class JsonFormatter(logging.Formatter):
    """把日志渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """本地开发可读格式，仍保留 request_id。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} {record.levelname:<8} [{request_id_var.get()}] {record.name}: {record.getMessage()}"
        extras = [
            f"{field}={getattr(record, field)}"
            for field in _EXTRA_FIELDS
            if getattr(record, field, None) is not None
        ]
        if extras:
            line = f"{line} ({' '.join(extras)})"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """配置 root logger。可安全重复调用。"""
    # Windows 控制台默认按 GBK 编码输出，JSON 里保留的可读中文会被替换成乱码。
    # stdout 与 stderr 都要切到 UTF-8——uvicorn 自身日志走 stderr，本平台的 handler
    # 显式绑定 stdout，漏掉任何一个都会留下乱码。
    # 流被重定向/关闭或不支持 reconfigure 时静默跳过，不影响日志功能。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    # 显式绑定 stdout：StreamHandler() 无参时默认写 stderr，与上面的流选择不一致
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else PlainFormatter())

    root = logging.getLogger()
    # 先移除已有 handler，保证重复调用不会叠加输出。
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn 自带访问日志与本平台的 request id 日志重复，降到 WARNING 避免双份。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """读取 ``X-Request-Id`` 请求头（缺省生成 uuid4），写入 contextvar 并回写响应头。"""

    HEADER = "X-Request-Id"

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # 优先复用上游传入的 trace id，保证跨服务链路可串联。
        request_id = request.headers.get(self.HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            # 无论下游是否抛异常，都要还原 contextvar，避免污染复用它的协程。
            request_id_var.reset(token)
        response.headers[self.HEADER] = request_id
        return response


def get_logger(name: str) -> logging.Logger:
    """返回指定名称的 logger（统一入口，便于后续集中调整）。"""
    return logging.getLogger(name)
