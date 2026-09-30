"""health 端点与 request id 中间件测试。

只覆盖 ``/health``（进程存活探针）与 request id 透传；**不测** ``/ready``——它
依赖 Postgres/Redis，骨架阶段本地未必具备这些依赖。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok() -> None:
    """a. /api/v1/health 返回 200 且 status == "ok"。"""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "service" in body
    assert "environment" in body


def test_health_has_request_id_header() -> None:
    """b. 响应带 X-Request-Id 头（未传时由服务端生成）。"""
    response = client.get("/api/v1/health")
    assert "X-Request-Id" in response.headers
    assert response.headers["X-Request-Id"]


def test_request_id_header_is_echoed() -> None:
    """c. 传入 X-Request-Id 时响应头回显同值。"""
    response = client.get("/api/v1/health", headers={"X-Request-Id": "my-trace-1"})
    assert response.headers["X-Request-Id"] == "my-trace-1"
