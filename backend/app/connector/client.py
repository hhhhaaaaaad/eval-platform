"""AgentWrite（Java）评测端点 typed client。

调用链路的固定顺序（顺序本身是设计的一部分）：

    circuit.before_call()  →  rate_limiter.acquire()  →  httpx 请求  →  分类  →  按需退避重试

- 熔断在限流**之前**：熔断打开时直接快速失败，不该先排队等令牌再被拒。
- 限流在请求**之前**：即使重试也要占令牌，否则重试风暴会绕过限流保护 Java 侧。

熔断只统计**瞬态失败**（超时 / 5xx / 连接错误），业务性拒绝不计数。原因很实在：
某个 run 拿着过期 fencing 会被 Java 持续拒绝，若把这类 4xx 也算失败，
一个配置错误的 run 就会把熔断器打开、连带阻断其他健康 run——那是把局部配置问题
放大成全局故障。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self

import httpx
from pydantic import BaseModel, ValidationError

from app.connector import errors as err
from app.connector.errors import (
    CODE_SUCCESS,
    HEADER_FENCING,
    HEADER_RUN_ID,
    JavaEvalContractError,
    JavaEvalError,
)
from app.connector.resilience import CircuitBreaker, RateLimiter, RetryPolicy
from app.connector.schemas import (
    AcquireRequest,
    AcquireResult,
    ExtractCandidate,
    ExtractRequest,
    FencingState,
    GovernanceDecision,
    GovernanceReplayRequest,
    GovernanceSamplesRequest,
    GovernanceSamplesResponse,
    JavaResponse,
    MetricsResponse,
    ParamsResponse,
    ReleaseRequest,
    ResetRequest,
    ResetResponse,
    RetrieveContextRequest,
    RetrieveContextResponse,
    SearchRequest,
    SearchResponse,
    SeedItem,
    SeedRequest,
    SeedResponse,
)
from app.settings.config import Settings, get_settings
from app.settings.logging import get_logger

_logger = get_logger(__name__)

#: 响应摘要截断长度。失败要留证，但不该把整份语料写进日志/结果表。
_SUMMARY_LIMIT = 500


def _parse_model(model: type[BaseModel]) -> Callable[[Any], Any]:
    """构造严格的单对象解析器：结构不符即抛契约错误，绝不猜测字段。"""

    def _parse(raw: Any) -> Any:
        if raw is None:
            raise JavaEvalContractError(f"期望 {model.__name__} 结构，实际 data 为 null")
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise JavaEvalContractError(
                f"响应结构不兼容 {model.__name__}: {exc.error_count()} 处校验失败",
                response_summary=str(exc)[:_SUMMARY_LIMIT],
            ) from exc

    return _parse


def _parse_model_list(model: type[BaseModel]) -> Callable[[Any], Any]:
    """构造严格的数组解析器（Java 侧返回 ``List<X>`` 时 data 是数组）。"""

    def _parse(raw: Any) -> Any:
        if not isinstance(raw, list):
            raise JavaEvalContractError(f"期望 {model.__name__} 数组，实际为 {type(raw).__name__}")
        try:
            return [model.model_validate(item) for item in raw]
        except ValidationError as exc:
            raise JavaEvalContractError(
                f"响应结构不兼容 [{model.__name__}]: {exc.error_count()} 处校验失败",
                response_summary=str(exc)[:_SUMMARY_LIMIT],
            ) from exc

    return _parse


def _parse_bool(raw: Any) -> bool:
    """Java 的 ``Response<Boolean>``：data 必须是 JSON 布尔，不接受 "true"/1 之类的宽松等价。"""
    if not isinstance(raw, bool):
        raise JavaEvalContractError(f"期望布尔值，实际为 {type(raw).__name__}")
    return raw


class JavaEvalClient:
    """AgentWrite 评测端口客户端。

    线程安全（依赖 httpx.Client 自身的线程安全 + 各组件的锁），可被多个 Celery 任务共享。
    推荐用 :meth:`from_settings` 构造，以便配置集中管理。
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout: float = 30.0,
        max_retries: int = 3,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        trust_env: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        # 不要对评测端点做 URL 重定向跟随：redirect 会静默改变目标，
        # 破坏「请求的确打到了哪个端点」的可追溯性。
        #
        # trust_env=False 是**必须的**，不是可选的优化。httpx 默认会读取环境/系统代理，
        # 于是访问本机 Java 服务也会被绕进代理——在有代理的开发机上（国内很常见）
        # 表现为收到代理返回的 **502**，看起来像 Java 服务坏了，实际请求根本没到它。
        # 这个坑实跑时踩过：本地 8092 无监听，日志里却是 HTTP 502 而非连接拒绝。
        # 内部服务端点不该走环境代理；确有需要时用 java_eval_trust_env 显式打开。
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            trust_env=trust_env,
        )
        self._rate_limiter = rate_limiter or RateLimiter(rate_per_second=20.0, burst=10)
        self._circuit = circuit_breaker or CircuitBreaker()
        self._retry = retry_policy or RetryPolicy(max_attempts=max_retries)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **overrides: Any) -> JavaEvalClient:
        """按应用配置构造。``overrides`` 用于测试注入限流/熔断/时钟等。"""
        settings = settings or get_settings()
        kwargs: dict[str, Any] = {
            "base_url": settings.java_eval_base_url,
            "token": settings.java_eval_token,
            "timeout": settings.java_eval_timeout_seconds,
            "max_retries": settings.java_eval_max_retries,
            "trust_env": settings.java_eval_trust_env,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 内部请求核心 -----------------------------------------------------

    def _headers(self, run_id: str | None, fencing_version: int | None) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if run_id is not None:
            headers[HEADER_RUN_ID] = run_id
        if fencing_version is not None:
            headers[HEADER_FENCING] = str(fencing_version)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        parse: Callable[[Any], Any],
        json_body: Any = None,
        run_id: str | None = None,
        eval_user_id: int | None = None,
        fencing_version: int | None = None,
    ) -> Any:
        """发一次请求并按矩阵分类；仅瞬态失败重试。"""
        payload = json_body.model_dump(by_alias=True, exclude_none=True) if json_body is not None else None
        headers = self._headers(run_id, fencing_version)
        self._circuit.before_call(endpoint=path)

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send(method, path, payload, headers)
            except httpx.TimeoutException as exc:
                error: JavaEvalError = err.JavaEvalTransientError(
                    f"请求超时: {exc}", endpoint=path, run_id=run_id, eval_user_id=eval_user_id,
                    attempts=attempt,
                )
            except httpx.HTTPError as exc:
                # 连接拒绝 / reset / DNS 失败等，均为基础设施瞬态。
                error = err.JavaEvalTransientError(
                    f"连接失败: {type(exc).__name__}", endpoint=path, run_id=run_id,
                    eval_user_id=eval_user_id, attempts=attempt,
                )
            else:
                try:
                    data = self._unwrap(response, path, run_id, eval_user_id, attempt)
                    parsed = parse(data)
                except JavaEvalError as exc:
                    if isinstance(exc, JavaEvalContractError):
                        # 契约错误：立刻失败，且不计入熔断（不是基础设施问题）。
                        self._log_failure(exc, eval_user_id)
                        raise
                    error = exc
                else:
                    self._circuit.on_success()
                    self._log_success(path, response.status_code, eval_user_id)
                    return parsed

            self._handle_failure(error, attempt, eval_user_id)

    def _send(self, method: str, path: str, payload: Any, headers: dict[str, str]) -> httpx.Response:
        self._rate_limiter.acquire()
        return self._client.request(method, path, json=payload, headers=headers)

    def _unwrap(
        self,
        response: httpx.Response,
        path: str,
        run_id: str | None,
        eval_user_id: int | None,
        attempt: int,
    ) -> Any:
        """把 HTTP 响应解析为 ``data``；任何非成功码都转成对应异常。"""
        summary = response.text[:_SUMMARY_LIMIT]

        # 1) HTTP 层已是错误：此时响应体多半是 Spring Security / 容器的错误页，
        #    不是 Response 信封，故只按状态码分类。
        status_class = err.classify_http_status(response.status_code)
        if status_class is not None:
            raise status_class(
                f"HTTP {response.status_code}",
                endpoint=path,
                http_status=response.status_code,
                run_id=run_id,
                eval_user_id=eval_user_id,
                attempts=attempt,
                response_summary=summary,
            )
        if response.status_code >= 400:
            # 其余 4xx（404/405 等）：归类为不可重试的通用失败，别当瞬态重试。
            raise JavaEvalError(
                f"HTTP {response.status_code}",
                endpoint=path,
                http_status=response.status_code,
                run_id=run_id,
                eval_user_id=eval_user_id,
                attempts=attempt,
                response_summary=summary,
            )

        # 2) HTTP 200：业务结果在信封里。这是本 connector 最容易被写错的地方——
        #    200 不代表成功，只有 code=="0000" 才是。
        try:
            envelope = JavaResponse[Any].model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise JavaEvalContractError(
                f"响应不是合法的 Response 信封: {type(exc).__name__}",
                endpoint=path,
                http_status=response.status_code,
                run_id=run_id,
                eval_user_id=eval_user_id,
                attempts=attempt,
                response_summary=summary,
            ) from exc

        code_class = err.classify_business_code(envelope.code)
        if code_class is not None:
            raise code_class(
                f"业务失败 code={envelope.code}: {envelope.info or ''}".strip(),
                endpoint=path,
                http_status=response.status_code,
                code=envelope.code,
                info=envelope.info,
                run_id=run_id,
                eval_user_id=eval_user_id,
                attempts=attempt,
                response_summary=summary,
            )
        if envelope.code != CODE_SUCCESS:
            raise JavaEvalContractError(
                f"未知业务码 {envelope.code}", endpoint=path, code=envelope.code,
                run_id=run_id, eval_user_id=eval_user_id, attempts=attempt,
            )
        return envelope.data

    def _handle_failure(self, error: JavaEvalError, attempt: int, eval_user_id: int | None) -> None:
        """失败后的统一出口：决定重试还是抛出。绝不「所有异常统一 retry」。"""
        if not self._retry.should_retry(error, attempt):
            # 只有瞬态失败才计入熔断（详见类文档）。
            if error.retryable:
                self._circuit.on_failure()
            self._log_failure(error, eval_user_id)
            raise error

        delay = self._retry.delay_for(attempt)
        _logger.warning(
            "connector 瞬态失败，退避后重试",
            extra={
                "endpoint": error.endpoint,
                "status": error.http_status,
                "run_id": error.run_id,
                "eval_user_id": eval_user_id,
                "attempt": attempt,
                "delay": round(delay, 3),
            },
        )
        self._retry.sleep(delay)

    def _log_success(self, path: str, status: int, eval_user_id: int | None) -> None:
        _logger.debug(
            "connector 调用成功",
            extra={"endpoint": path, "status": status, "eval_user_id": eval_user_id},
        )

    def _log_failure(self, error: JavaEvalError, eval_user_id: int | None) -> None:
        _logger.error(
            "connector 调用失败",
            extra={
                "endpoint": error.endpoint,
                "status": error.http_status,
                "run_id": error.run_id,
                "eval_user_id": eval_user_id,
                "attempt": error.attempts,
                "code": error.code,
                "retryable": error.retryable,
            },
        )

    # -- seed / reset -----------------------------------------------------

    def seed(
        self,
        eval_user_id: int,
        items: list[SeedItem],
        *,
        run_id: str,
        fencing_version: int | None = None,
    ) -> SeedResponse:
        """幂等写入种子语料。重复内容不产生重复行，返回 ``existed``。"""
        body = SeedRequest(eval_user_id=eval_user_id, items=items)
        return self._request(
            "POST", "/api/v1/eval/seed", parse=_parse_model(SeedResponse),
            json_body=body, run_id=run_id, eval_user_id=eval_user_id,
            fencing_version=fencing_version,
        )

    def reset(
        self,
        eval_user_id: int,
        *,
        run_id: str,
        fencing_version: int | None = None,
    ) -> ResetResponse:
        """物理清空评测命名空间。需要 fencing 持有权（``run_id`` 必填）。"""
        body = ResetRequest(eval_user_id=eval_user_id)
        return self._request(
            "POST", "/api/v1/eval/reset", parse=_parse_model(ResetResponse),
            json_body=body, run_id=run_id, eval_user_id=eval_user_id,
            fencing_version=fencing_version,
        )

    # -- 只读观测 ---------------------------------------------------------

    def search(
        self,
        eval_user_id: int,
        query: str,
        *,
        top_k: int = 5,
        threshold: float | None = None,
        freeze_side_effects: bool = True,
        exact: bool | None = None,
        hnsw_ef: int | None = None,
    ) -> SearchResponse:
        body = SearchRequest(
            eval_user_id=eval_user_id,
            query=query,
            top_k=top_k,
            threshold=threshold,
            freeze_side_effects=freeze_side_effects,
            exact=exact,
            hnsw_ef=hnsw_ef,
        )
        return self._request(
            "POST", "/api/v1/eval/search", parse=_parse_model(SearchResponse),
            json_body=body, eval_user_id=eval_user_id,
        )

    def extract(self, eval_user_id: int, messages: list[dict[str, str]]) -> list[ExtractCandidate]:
        body = ExtractRequest(eval_user_id=eval_user_id, messages=messages)
        return self._request(
            "POST", "/api/v1/eval/extract", parse=_parse_model_list(ExtractCandidate),
            json_body=body, eval_user_id=eval_user_id,
        )

    def retrieve_context(
        self, eval_user_id: int, query_context: str, *, top_k: int = 5
    ) -> RetrieveContextResponse:
        body = RetrieveContextRequest(eval_user_id=eval_user_id, query_context=query_context, top_k=top_k)
        return self._request(
            "POST", "/api/v1/eval/retrieve-context", parse=_parse_model(RetrieveContextResponse),
            json_body=body, eval_user_id=eval_user_id,
        )

    # -- fencing ----------------------------------------------------------

    def get_fencing(self, eval_user_id: int) -> FencingState:
        """无行时返回 ``(0, None)``，不是错误——首次 acquire 前就是这个状态。"""
        return self._request(
            "GET", f"/api/v1/eval/fencing/{eval_user_id}",
            parse=_parse_model(FencingState), eval_user_id=eval_user_id,
        )

    def acquire_fencing(self, eval_user_id: int, expected_version: int, run_id: str) -> AcquireResult:
        """抢占 fencing。

        ``acquired=False`` 是**正常返回**而非异常：它表示期望版本已过期，
        响应里的 ``version`` 是当前权威版本，调用方据此做 alignment。
        注意别把 ``acquired=False`` 当成失败重试——版本不会因为重试而变对。
        """
        body = AcquireRequest(eval_user_id=eval_user_id, expected_version=expected_version, run_id=run_id)
        return self._request(
            "POST", "/api/v1/eval/fencing/acquire", parse=_parse_model(AcquireResult),
            json_body=body, run_id=run_id, eval_user_id=eval_user_id,
        )

    def release_fencing(self, eval_user_id: int, run_id: str) -> bool:
        body = ReleaseRequest(eval_user_id=eval_user_id, run_id=run_id)
        return self._request(
            "POST", "/api/v1/eval/fencing/release", parse=_parse_bool,
            json_body=body, run_id=run_id, eval_user_id=eval_user_id,
        )

    # -- 观测 -------------------------------------------------------------

    def params(self) -> ParamsResponse:
        """评测参数快照；平台据此算 ``config_fingerprint``。"""
        return self._request("GET", "/api/v1/eval/params", parse=_parse_model(ParamsResponse))

    def metrics(self) -> MetricsResponse:
        return self._request("GET", "/api/v1/eval/metrics", parse=_parse_model(MetricsResponse))

    def circuit_breaker_degraded(self) -> bool:
        """Java 侧注入是否已降级（与平台自己的熔断器无关，是远端状态）。"""
        return self._request("GET", "/api/v1/eval/circuit-breaker", parse=_parse_bool)

    # -- governance -------------------------------------------------------

    def governance_samples(self, eval_user_id: int) -> GovernanceSamplesResponse:
        body = GovernanceSamplesRequest(eval_user_id=eval_user_id)
        return self._request(
            "POST", "/api/v1/eval/governance/samples", parse=_parse_model(GovernanceSamplesResponse),
            json_body=body, eval_user_id=eval_user_id,
        )

    def governance_replay(
        self,
        eval_user_id: int,
        *,
        duplicates: bool = True,
        consistency: bool = True,
        expired: bool = True,
        hallucination: bool = True,
    ) -> list[GovernanceDecision]:
        """只调 compute 层，Java 侧绝不落库；同一批语料重放多次结果必须一致。"""
        body = GovernanceReplayRequest(
            eval_user_id=eval_user_id,
            duplicates=duplicates,
            consistency=consistency,
            expired=expired,
            hallucination=hallucination,
        )
        return self._request(
            "POST", "/api/v1/eval/governance/replay", parse=_parse_model_list(GovernanceDecision),
            json_body=body, eval_user_id=eval_user_id,
        )
