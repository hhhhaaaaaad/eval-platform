"""Java connector 契约测试（EP-3 出口条件）。

用 ``respx`` 拦截 httpx，**不发真实请求**——这些测试验证的是「平台理解的 Java 契约」
与「Java 实际返回的契约」是否一致，因此 fixture 里的 JSON 必须**逐字照抄**
``MemoryEvalController`` 的真实返回结构，而不是凭想象编。

三条验收条款在此逐条落地：

1. **Java 端点返回变化时测试能捕获** → :func:`test_missing_required_field_is_contract_error`
   等一系列结构断言；字段改名/缺失即红。
2. **fencing 403 不被误判为 transient retry** → :func:`test_fencing_rejection_is_not_retried`
   （且 Java 侧这种拒绝的真实形态是 **HTTP 200 + code=E0403**，不是 HTTP 403）。
3. **connector 日志含 run_id、eval_user_id、endpoint、status** → :func:`test_failure_log_carries_correlation_fields`。

另有一类测试专门钉住「HTTP 200 不代表成功」这个反直觉约定，见
:class:`TestBusinessCodeOverHttp200`。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.connector import JavaEvalClient
from app.connector.errors import (
    JavaEvalAuthError,
    JavaEvalCircuitOpenError,
    JavaEvalConflictError,
    JavaEvalContractError,
    JavaEvalError,
    JavaEvalFencingError,
    JavaEvalTransientError,
    JavaEvalValidationError,
)
from app.connector.resilience import CircuitBreaker, RateLimiter, RetryPolicy
from app.connector.schemas import SeedItem

BASE = "http://java.test"
SEED_URL = f"{BASE}/api/v1/eval/seed"
RESET_URL = f"{BASE}/api/v1/eval/reset"
SEARCH_URL = f"{BASE}/api/v1/eval/search"
FENCING_GET_URL = f"{BASE}/api/v1/eval/fencing/9000000001"
FENCING_ACQUIRE_URL = f"{BASE}/api/v1/eval/fencing/acquire"

USER = 9_000_000_001
RUN = "run-abc-123"


def _noop_sleep(_seconds: float) -> None:
    """测试里不真等：退避时长由 RetryPolicy 单测断言，契约测试只关心「重试了几次」。"""


def _envelope(code: str, data: object = None, info: str = "成功") -> dict[str, object]:
    """构造 Java ``Response<T>`` 信封 ``{code, info, data}``。"""
    return {"code": code, "info": info, "data": data}


@pytest.fixture
def client() -> Iterator[JavaEvalClient]:
    """受测客户端：限流放宽、退避不等待，使测试专注于协议语义。"""
    c = JavaEvalClient(
        base_url=BASE,
        token="eval-token",
        timeout=5.0,
        rate_limiter=RateLimiter(rate_per_second=10_000.0, burst=1_000),
        circuit_breaker=CircuitBreaker(failure_threshold=99),
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, sleep=_noop_sleep),
    )
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 请求侧契约：URL、请求头、camelCase 字段名
# ---------------------------------------------------------------------------


class TestRequestShape:
    """平台发出去的请求必须与 Java DTO 对得上——字段名错一个，Jackson 会静默取 null。"""

    @respx.mock
    def test_seed_sends_run_id_header_and_camel_case_body(self, client: JavaEvalClient) -> None:
        route = respx.post(SEED_URL).mock(
            return_value=httpx.Response(
                200,
                json=_envelope(
                    "0000",
                    {"inserted": 2, "existed": 1, "contentToId": {"喜欢喝美式": 12345}},
                ),
            )
        )

        result = client.seed(
            USER,
            items=[SeedItem(type="preference", content="喜欢喝美式")],
            run_id=RUN,
            fencing_version=7,
        )

        request = route.calls[0].request
        assert request.headers["X-Eval-Run-Id"] == RUN
        assert request.headers["X-Eval-Fencing"] == "7"
        assert request.headers["Authorization"] == "Bearer eval-token"

        body = json.loads(request.content)
        # camelCase 而非 snake_case——Java 侧 Jackson 按字段名反序列化。
        assert body == {
            "evalUserId": USER,
            "items": [{"type": "preference", "content": "喜欢喝美式"}],
        }
        assert result.inserted == 2
        assert result.existed == 1
        assert result.content_to_id == {"喜欢喝美式": 12345}

    @respx.mock
    def test_reset_omits_fencing_header_when_not_supplied(self, client: JavaEvalClient) -> None:
        route = respx.post(RESET_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"mysqlDeleted": 5, "vectorCleared": True}))
        )

        result = client.reset(USER, run_id=RUN)

        request = route.calls[0].request
        assert request.headers["X-Eval-Run-Id"] == RUN
        # 未传 fencing 时不能发该头：Java 用 required=false + null 表示「不校验版本」，
        # 发空串会让 Long 解析成 null 之外的路径，语义不同。
        assert "X-Eval-Fencing" not in request.headers
        assert result.mysql_deleted == 5
        assert result.vector_cleared is True

    @respx.mock
    def test_search_omits_none_optionals_but_keeps_explicit_values(self, client: JavaEvalClient) -> None:
        route = respx.post(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"items": []}))
        )

        client.search(USER, "咖啡", top_k=5, threshold=0.0, freeze_side_effects=True)

        body = json.loads(route.calls[0].request.content)
        # threshold=0.0 是合法值，必须原样发出；若用 `or` 兜底会被误当成「未传」。
        assert body["threshold"] == 0.0
        assert body["topK"] == 5
        assert body["freezeSideEffects"] is True
        # exact/hnswEf 为 None → 不发送，交给 Java 侧默认值。
        assert "exact" not in body
        assert "hnswEf" not in body

    @respx.mock
    def test_search_sends_exact_and_hnsw_ef_when_set(self, client: JavaEvalClient) -> None:
        route = respx.post(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"items": []}))
        )

        client.search(USER, "咖啡", exact=True, hnsw_ef=256)

        body = json.loads(route.calls[0].request.content)
        assert body["exact"] is True
        assert body["hnswEf"] == 256

    @respx.mock
    def test_no_authorization_header_when_token_empty(self) -> None:
        """token 未配置时不应发 ``Bearer ``（空 token）——那会被当成无效凭证而非「无凭证」。"""
        c = JavaEvalClient(base_url=BASE, token="", rate_limiter=RateLimiter(10_000.0, burst=100))
        try:
            route = respx.get(f"{BASE}/api/v1/eval/params").mock(
                return_value=httpx.Response(
                    200,
                    json=_envelope(
                        "0000",
                        {
                            "vectorStore": "memory",
                            "rrfK": 60,
                            "alpha": 0.5,
                            "beta": 0.3,
                            "recencyHalfLifeDays": 14.0,
                            "profileBoost": 0.2,
                            "minConfidence": 0.4,
                            "injectMaxTokens": 2000,
                        },
                    ),
                )
            )
            params = c.params()
            assert "Authorization" not in route.calls[0].request.headers
            assert params.vector_store == "memory"
            assert params.inject_max_tokens == 2000
        finally:
            c.close()


# ---------------------------------------------------------------------------
# HTTP 200 + 业务错误码：本 connector 最反直觉也最要紧的一类
# ---------------------------------------------------------------------------


class TestBusinessCodeOverHttp200:
    """Java 对业务错误返回 HTTP 200，错误码在响应体 ``code``。

    若把「HTTP 200」当成功，fencing 拒绝会被静默吞掉，破坏性写照常执行——
    这是数据损坏级的 bug，故单列一组测试钉死。
    """

    @respx.mock
    def test_fencing_rejection_is_not_retried(self, client: JavaEvalClient) -> None:
        """**EP-3 核心验收条款**：fencing 403 不得被当成瞬态错误重试。"""
        route = respx.post(RESET_URL).mock(
            return_value=httpx.Response(
                200,
                json=_envelope("E0403", None, "评测请求被拒绝（权限/命名空间/fencing）"),
            )
        )

        with pytest.raises(JavaEvalFencingError) as excinfo:
            client.reset(USER, run_id=RUN)

        error = excinfo.value
        assert error.retryable is False
        assert error.code == "E0403"
        # HTTP 状态码是 200，不是 403——错误分类靠 code，不靠状态码。
        assert error.http_status == 200
        # 只发了 1 次。重试 fencing 拒绝毫无意义：版本不会因为重试而变对。
        assert len(route.calls) == 1

    @respx.mock
    def test_conflict_is_not_retried_by_connector(self, client: JavaEvalClient) -> None:
        """E0409 记为「仅按场景」重试：connector 不自行重试，如实抛给上层。"""
        route = respx.post(SEED_URL).mock(
            return_value=httpx.Response(200, json=_envelope("E0409", None, "评测并发冲突"))
        )

        with pytest.raises(JavaEvalConflictError) as excinfo:
            client.seed(USER, items=[SeedItem(type="fact", content="x")], run_id=RUN)

        assert excinfo.value.retryable is False
        assert len(route.calls) == 1

    @respx.mock
    def test_invalid_params_is_not_retried(self, client: JavaEvalClient) -> None:
        route = respx.post(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_envelope("E0422", None, "评测参数非法"))
        )

        with pytest.raises(JavaEvalValidationError) as excinfo:
            client.search(USER, "x")

        assert excinfo.value.retryable is False
        assert len(route.calls) == 1

    @respx.mock
    def test_java_unknown_error_is_not_retried(self, client: JavaEvalClient) -> None:
        """``UN_ERROR`` 是 Java 内部失败；按不可重试处理，避免盲目重放破坏性写。"""
        route = respx.post(RESET_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0001", None, "未知失败"))
        )

        with pytest.raises(JavaEvalError) as excinfo:
            client.reset(USER, run_id=RUN)

        assert excinfo.value.retryable is False
        assert len(route.calls) == 1


# ---------------------------------------------------------------------------
# HTTP 层错误码 → 分类（Spring Security / 容器产生，不走 Response 信封）
# ---------------------------------------------------------------------------


class TestHttpStatusClassification:
    @respx.mock
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_are_not_retried(self, client: JavaEvalClient, status: int) -> None:
        route = respx.get(f"{BASE}/api/v1/eval/metrics").mock(
            return_value=httpx.Response(status, text="Forbidden")
        )

        with pytest.raises(JavaEvalAuthError) as excinfo:
            client.metrics()

        assert excinfo.value.retryable is False
        assert len(route.calls) == 1

    @respx.mock
    def test_422_is_validation_error(self, client: JavaEvalClient) -> None:
        respx.post(SEARCH_URL).mock(return_value=httpx.Response(422, text="Unprocessable"))
        with pytest.raises(JavaEvalValidationError):
            client.search(USER, "x")

    @respx.mock
    def test_500_is_retried_then_raises_transient(self, client: JavaEvalClient) -> None:
        route = respx.get(f"{BASE}/api/v1/eval/metrics").mock(
            return_value=httpx.Response(500, text="boom")
        )

        with pytest.raises(JavaEvalTransientError) as excinfo:
            client.metrics()

        assert excinfo.value.retryable is True
        # max_attempts=3 → 共 3 次尝试。
        assert len(route.calls) == 3

    @respx.mock
    def test_429_is_retried(self, client: JavaEvalClient) -> None:
        route = respx.get(f"{BASE}/api/v1/eval/metrics").mock(
            return_value=httpx.Response(429, text="slow down")
        )

        with pytest.raises(JavaEvalTransientError):
            client.metrics()

        assert len(route.calls) == 3

    @respx.mock
    def test_timeout_is_retried(self, client: JavaEvalClient) -> None:
        route = respx.get(f"{BASE}/api/v1/eval/metrics").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )

        with pytest.raises(JavaEvalTransientError):
            client.metrics()

        assert len(route.calls) == 3

    @respx.mock
    def test_transient_then_success_returns_value(self, client: JavaEvalClient) -> None:
        """重试的意义在于恢复：第 2 次成功就该正常返回，而不是把第 1 次失败抛出去。"""
        route = respx.get(f"{BASE}/api/v1/eval/circuit-breaker").mock(
            side_effect=[
                httpx.Response(503, text="unavailable"),
                httpx.Response(200, json=_envelope("0000", True)),
            ]
        )

        assert client.circuit_breaker_degraded() is True
        assert len(route.calls) == 2


# ---------------------------------------------------------------------------
# 契约漂移：结构不符必须立即失败，禁止猜测字段
# ---------------------------------------------------------------------------


class TestContractDrift:
    @respx.mock
    def test_missing_required_field_is_contract_error(self, client: JavaEvalClient) -> None:
        """Java 若删掉/改名 ``existed``，必须立刻炸——而不是默认成 0 让指标悄悄算错。"""
        respx.post(SEED_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"inserted": 1}))
        )

        with pytest.raises(JavaEvalContractError) as excinfo:
            client.seed(USER, items=[SeedItem(type="fact", content="x")], run_id=RUN)

        assert excinfo.value.retryable is False

    @respx.mock
    def test_wrong_type_is_contract_error(self, client: JavaEvalClient) -> None:
        respx.post(SEED_URL).mock(
            return_value=httpx.Response(
                200, json=_envelope("0000", {"inserted": "一", "existed": 0, "contentToId": {}})
            )
        )
        with pytest.raises(JavaEvalContractError):
            client.seed(USER, items=[SeedItem(type="fact", content="x")], run_id=RUN)

    @respx.mock
    def test_null_data_is_contract_error(self, client: JavaEvalClient) -> None:
        """成功码但 data 为 null：不是成功，是契约破裂。"""
        respx.post(SEED_URL).mock(return_value=httpx.Response(200, json=_envelope("0000", None)))

        with pytest.raises(JavaEvalContractError):
            client.seed(USER, items=[SeedItem(type="fact", content="x")], run_id=RUN)

    @respx.mock
    def test_non_json_body_is_contract_error(self, client: JavaEvalClient) -> None:
        """网关返回 HTML 错误页时必须报契约错误，而不是 JSONDecodeError 逃逸。"""
        respx.get(f"{BASE}/api/v1/eval/params").mock(
            return_value=httpx.Response(200, text="<html>502 Bad Gateway</html>")
        )

        with pytest.raises(JavaEvalContractError):
            client.params()

    @respx.mock
    def test_list_response_must_be_array(self, client: JavaEvalClient) -> None:
        respx.post(f"{BASE}/api/v1/eval/extract").mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"content": "不是数组"}))
        )

        with pytest.raises(JavaEvalContractError):
            client.extract(USER, [{"role": "user", "content": "hi"}])

    @respx.mock
    def test_boolean_response_rejects_truthy_non_bool(self, client: JavaEvalClient) -> None:
        """不接受 "true" / 1 之类的宽松等价——那是猜测字段。"""
        respx.get(f"{BASE}/api/v1/eval/circuit-breaker").mock(
            return_value=httpx.Response(200, json=_envelope("0000", "true"))
        )

        with pytest.raises(JavaEvalContractError):
            client.circuit_breaker_degraded()

    @respx.mock
    def test_contract_error_does_not_open_circuit(self, client: JavaEvalClient) -> None:
        """契约错误不是基础设施问题，不该把熔断器打开、连带阻断健康调用。"""
        respx.get(f"{BASE}/api/v1/eval/params").mock(
            return_value=httpx.Response(200, text="<html>nope</html>")
        )
        with pytest.raises(JavaEvalContractError):
            client.params()
        assert client._circuit.state.value == "closed"


# ---------------------------------------------------------------------------
# 各端点返回体解析
# ---------------------------------------------------------------------------


class TestEndpointParsing:
    @respx.mock
    def test_search_returns_content_for_hash_matching(self, client: JavaEvalClient) -> None:
        """``content`` 必须被解析出来：平台的 Recall 用 md5(content) 与 ground truth 匹配。"""
        respx.post(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=_envelope(
                    "0000",
                    {
                        "items": [
                            {
                                "id": 7,
                                "content": "用户喜欢美式咖啡",
                                "score": 0.87,
                                "importance": 0.6,
                                "type": "preference",
                                "confidence": 0.9,
                            }
                        ]
                    },
                ),
            )
        )

        result = client.search(USER, "咖啡")
        assert len(result.items) == 1
        item = result.items[0]
        assert item.id == 7
        assert item.content == "用户喜欢美式咖啡"
        assert item.score == pytest.approx(0.87)

    @respx.mock
    def test_extract_parses_candidate_list(self, client: JavaEvalClient) -> None:
        respx.post(f"{BASE}/api/v1/eval/extract").mock(
            return_value=httpx.Response(
                200,
                json=_envelope(
                    "0000",
                    [
                        {
                            "content": "用户是 CS 学生",
                            "type": "fact",
                            "attributedTo": "user",
                            "operation": "ADD",
                            "targetMemoryId": None,
                            "subject": "用户",
                            "predicate": "身份",
                            "value": "CS 学生",
                            "evidence": "我是学计算机的",
                            "confidence": 0.95,
                        }
                    ],
                ),
            )
        )

        candidates = client.extract(USER, [{"role": "user", "content": "我是学计算机的"}])
        assert len(candidates) == 1
        assert candidates[0].subject == "用户"
        assert candidates[0].confidence == pytest.approx(0.95)

    @respx.mock
    def test_retrieve_context_parses_budgeted_ids(self, client: JavaEvalClient) -> None:
        respx.post(f"{BASE}/api/v1/eval/retrieve-context").mock(
            return_value=httpx.Response(
                200,
                json=_envelope("0000", {"formatted": "- 记忆A", "tokenCount": 42, "budgetedIds": [1, 2]}),
            )
        )

        r = client.retrieve_context(USER, "咖啡")
        assert r.token_count == 42
        assert r.budgeted_ids == [1, 2]

    @respx.mock
    def test_fencing_state_without_row_is_not_an_error(self, client: JavaEvalClient) -> None:
        """无行返回 (0, null) 是正常的「尚未 acquire」，不能当失败。"""
        respx.get(FENCING_GET_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"fencingVersion": 0, "activeRunId": None}))
        )

        state = client.get_fencing(USER)
        assert state.fencing_version == 0
        assert state.active_run_id is None

    @respx.mock
    def test_fencing_acquire_conflict_is_normal_result(self, client: JavaEvalClient) -> None:
        """``acquired=False`` 是正常返回值而非异常，调用方据此做 alignment。"""
        respx.post(FENCING_ACQUIRE_URL).mock(
            return_value=httpx.Response(200, json=_envelope("0000", {"acquired": False, "version": 9}))
        )

        result = client.acquire_fencing(USER, expected_version=3, run_id=RUN)
        assert result.acquired is False
        assert result.version == 9

    @respx.mock
    def test_release_returns_bool(self, client: JavaEvalClient) -> None:
        respx.post(f"{BASE}/api/v1/eval/fencing/release").mock(
            return_value=httpx.Response(200, json=_envelope("0000", False))
        )
        assert client.release_fencing(USER, RUN) is False

    @respx.mock
    def test_governance_samples_parses_four_buckets(self, client: JavaEvalClient) -> None:
        bucket = {
            "total": 1,
            "items": [
                {
                    "memoryId": 11,
                    "userId": USER,
                    "type": "fact",
                    "content": "x",
                    "status": "active",
                    "subject": "s",
                    "predicate": "p",
                    "value": "v",
                    "confidence": 0.8,
                }
            ],
        }
        respx.post(f"{BASE}/api/v1/eval/governance/samples").mock(
            return_value=httpx.Response(
                200,
                json=_envelope(
                    "0000",
                    {
                        "duplicates": bucket,
                        "consistency": {"total": 0, "items": []},
                        "expired": {"total": 0, "items": []},
                        "hallucination": {"total": 0, "items": []},
                    },
                ),
            )
        )

        samples = client.governance_samples(USER)
        assert samples.duplicates.total == 1
        assert samples.duplicates.items[0].memory_id == 11
        assert samples.expired.total == 0

    @respx.mock
    def test_governance_replay_sends_flags_and_parses_decisions(self, client: JavaEvalClient) -> None:
        route = respx.post(f"{BASE}/api/v1/eval/governance/replay").mock(
            return_value=httpx.Response(
                200,
                json=_envelope(
                    "0000",
                    [
                        {
                            "action": "MERGE",
                            "mergedIntoId": 100,
                            "reason": "相似度 0.94",
                            "items": [{"memoryId": 101, "beforeStatus": "active", "afterStatus": "merged"}],
                        }
                    ],
                ),
            )
        )

        decisions = client.governance_replay(USER, duplicates=True, consistency=False, expired=False, hallucination=False)

        body = json.loads(route.calls[0].request.content)
        assert body["duplicates"] is True
        assert body["consistency"] is False
        assert decisions[0].action == "MERGE"
        assert decisions[0].merged_into_id == 100
        assert decisions[0].items[0].after_status == "merged"

    @respx.mock
    def test_metrics_parses_both_gauges(self, client: JavaEvalClient) -> None:
        respx.get(f"{BASE}/api/v1/eval/metrics").mock(
            return_value=httpx.Response(
                200,
                json=_envelope("0000", {"extractionRejectRate": 0.12, "vectorSyncPendingCount": 3}),
            )
        )

        m = client.metrics()
        assert m.extraction_reject_rate == pytest.approx(0.12)
        assert m.vector_sync_pending_count == 3


# ---------------------------------------------------------------------------
# 可观测性：EP-3 验收要求日志含 run_id / eval_user_id / endpoint / status
# ---------------------------------------------------------------------------


class TestLogging:
    @respx.mock
    def test_failure_log_carries_correlation_fields(
        self, client: JavaEvalClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        respx.post(RESET_URL).mock(
            return_value=httpx.Response(200, json=_envelope("E0403", None, "拒绝"))
        )

        with caplog.at_level(logging.ERROR, logger="app.connector.client"), pytest.raises(
            JavaEvalFencingError
        ):
            client.reset(USER, run_id=RUN)

        record = next(r for r in caplog.records if r.name == "app.connector.client")
        # 这四个字段是 EP-3 明文验收项；少了任何一个，线上排障就得靠猜。
        assert record.run_id == RUN
        assert record.eval_user_id == USER
        assert record.endpoint == "/api/v1/eval/reset"
        assert record.status == 200
        assert record.code == "E0403"
        assert record.retryable is False

    @respx.mock
    def test_retry_logs_attempt_number(self, client: JavaEvalClient, caplog: pytest.LogCaptureFixture) -> None:
        respx.get(f"{BASE}/api/v1/eval/metrics").mock(
            side_effect=[
                httpx.Response(503, text="x"),
                httpx.Response(200, json=_envelope("0000", {"extractionRejectRate": 0.0, "vectorSyncPendingCount": 0})),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="app.connector.client"):
            client.metrics()

        retry_logs = [r for r in caplog.records if "退避后重试" in r.getMessage()]
        assert len(retry_logs) == 1
        assert retry_logs[0].attempt == 1


# ---------------------------------------------------------------------------
# 熔断行为（HTTP 层集成）
# ---------------------------------------------------------------------------


class TestCircuitIntegration:
    @respx.mock
    def test_circuit_opens_after_consecutive_transient_failures(self) -> None:
        c = JavaEvalClient(
            base_url=BASE,
            rate_limiter=RateLimiter(10_000.0, burst=100),
            circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_timeout=999.0),
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0.0, jitter=0.0, sleep=_noop_sleep),
        )
        try:
            route = respx.get(f"{BASE}/api/v1/eval/metrics").mock(
                return_value=httpx.Response(500, text="boom")
            )

            for _ in range(2):
                with pytest.raises(JavaEvalTransientError):
                    c.metrics()
            assert len(route.calls) == 2

            # 第三次：熔断已打开，应快速失败且**不发请求**。
            with pytest.raises(JavaEvalCircuitOpenError):
                c.metrics()
            assert len(route.calls) == 2
        finally:
            c.close()

    @respx.mock
    def test_business_rejection_does_not_open_circuit(self) -> None:
        """一个 fencing 配置错的 run 不该把熔断器打开、连带阻断其他健康 run。"""
        c = JavaEvalClient(
            base_url=BASE,
            rate_limiter=RateLimiter(10_000.0, burst=100),
            circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_timeout=999.0),
            retry_policy=RetryPolicy(max_attempts=1, base_delay=0.0, jitter=0.0, sleep=_noop_sleep),
        )
        try:
            respx.post(RESET_URL).mock(
                return_value=httpx.Response(200, json=_envelope("E0403", None, "拒绝"))
            )

            for _ in range(5):
                with pytest.raises(JavaEvalFencingError):
                    c.reset(USER, run_id=RUN)

            assert c._circuit.state.value == "closed"
        finally:
            c.close()
