"""Java 评测端点的异常体系与错误分类。

设计依据是《独立平台项目工作》第 13 节的「Connector 错误分类和重试矩阵」，
但**该矩阵按 HTTP 状态码分类，与 Java 侧实际行为存在一处关键偏差，必须在此修正**：

    Java 的 ``MemoryEvalController.fail()`` 对业务错误一律返回 **HTTP 200**，
    真正的错误码在响应体 ``code`` 字段（``E0403`` / ``E0409`` / ``E0422``）。
    Spring Security 层的 401/403 才是真的 HTTP 状态码。

因此分类必须**同时看 HTTP 状态码和响应体 code**，两者任一命中即按对应类别处理。
若只看 HTTP 状态码，fencing 拒绝（HTTP 200 + code=E0403）会被当成成功解析，
或退一步被当成瞬态错误重试——而 fencing 拒绝重试是毫无意义的（版本不会自己变对），
这正是 EP-3 验收条款「fencing 403 不被误判为 transient retry」要防的。

分类结果通过 :attr:`JavaEvalError.retryable` 暴露，**上层禁止对异常统一 retry**，
只能依据该标志重试（第 13 节明文要求）。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Java 侧业务码（cn.sutone.ai.types.enums.ResponseCode）
# 与 Java 枚举一一对应，改动需同步；契约测试会断言这些常量与真实响应一致。
# ---------------------------------------------------------------------------
CODE_SUCCESS = "0000"
CODE_UN_ERROR = "0001"
CODE_ILLEGAL_PARAMETER = "0002"
CODE_UNAUTHORIZED = "0004"
CODE_EVAL_FORBIDDEN = "E0403"
CODE_EVAL_CONFLICT = "E0409"
CODE_EVAL_INVALID = "E0422"

#: 必须携带的请求头（seed/reset 的破坏性写守卫）
HEADER_RUN_ID = "X-Eval-Run-Id"
HEADER_FENCING = "X-Eval-Fencing"


class JavaEvalError(Exception):
    """connector 异常基类。

    携带足够的上报字段（endpoint / run_id / eval_user_id / code / status），
    使一条错误日志就能定位到「哪个 run 的哪个阶段打哪个端点被拒」。
    """

    #: 是否允许重试。**仅瞬态错误为 True**，其余一律 False。
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        http_status: int | None = None,
        code: str | None = None,
        info: str | None = None,
        run_id: str | None = None,
        eval_user_id: int | None = None,
        attempts: int = 1,
        response_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.http_status = http_status
        self.code = code
        self.info = info
        self.run_id = run_id
        self.eval_user_id = eval_user_id
        self.attempts = attempts
        # 保存响应摘要而非完整响应体：失败 case 要留证，但不该把可能很大的
        # 语料原样写进日志和 run 结果表（第 13 节「保存响应摘要」）。
        self.response_summary = response_summary

    def as_log_fields(self) -> dict[str, Any]:
        """产出可直接 ``extra=`` 给结构化日志的字段（对应 logging 的 _EXTRA_FIELDS）。"""
        return {
            "endpoint": self.endpoint,
            "status": self.http_status,
            "run_id": self.run_id,
            "case_id": None,
            "attempt": self.attempts,
        }

    def __str__(self) -> str:
        parts = [self.message]
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.code:
            parts.append(f"code={self.code}")
        if self.run_id:
            parts.append(f"run_id={self.run_id}")
        if self.attempts > 1:
            parts.append(f"attempts={self.attempts}")
        return " | ".join(parts)


class JavaEvalAuthError(JavaEvalError):
    """鉴权失败：HTTP 401/403，或业务码 0004。

    对应矩阵「401/403 → 否重试 → 告警并终止当前破坏性阶段」。
    """


class JavaEvalFencingError(JavaEvalAuthError):
    """fencing / 命名空间越界拒绝（业务码 E0403）。

    单列一个子类而不是复用 :class:`JavaEvalAuthError`，因为两者的上层处置不同：
    鉴权失败是「token 配错了」，fencing 失败是「这个 run 已经不是权威持有者了」，
    后者应当走 alignment protocol（读取权威状态 → 决定让位还是中止），而不是刷新 token。

    **retryable 恒为 False**：这是 EP-3 的硬性验收条款。
    """


class JavaEvalConflictError(JavaEvalError):
    """并发冲突（业务码 E0409）。

    矩阵记为「仅按场景」重试：重试与否取决于调用方能否先读到权威状态。
    connector 自身不重试，只把它如实抛出，由上层决定。
    """


class JavaEvalValidationError(JavaEvalError):
    """请求或数据错误：HTTP 400/422，或业务码 0002/E0422。不可重试。"""


class JavaEvalContractError(JavaEvalError):
    """返回结构不兼容（缺字段、类型不符、JSON 解析失败）。

    矩阵要求「立即失败，禁止猜测字段」——因此解析一律严格模式，
    宁可炸在 connector 也不让脏数据流进指标计算。
    """


class JavaEvalTransientError(JavaEvalError):
    """瞬态错误：HTTP 429/5xx、超时、连接重置。**这是唯一 retryable=True 的类别。**"""

    retryable = True


class JavaEvalCircuitOpenError(JavaEvalTransientError):
    """熔断器打开期间的快速失败。

    继承瞬态错误是因为其语义是「现在不行，等会儿可能行」；
    但调用方看到它时不该立刻重试（那正是熔断要阻止的），而应结束当前阶段等待恢复。
    """


def classify_http_status(status: int) -> type[JavaEvalError] | None:
    """按 HTTP 状态码给出异常类别；无法判定时返回 None（交给业务码判定）。"""
    if status in (401, 403):
        return JavaEvalAuthError
    if status == 400 or status == 422:
        return JavaEvalValidationError
    if status == 409:
        return JavaEvalConflictError
    if status == 429:
        return JavaEvalTransientError
    if status >= 500:
        return JavaEvalTransientError
    return None


def classify_business_code(code: str | None) -> type[JavaEvalError] | None:
    """按响应体业务码给出异常类别；``0000``（成功）返回 None。"""
    if code is None or code == CODE_SUCCESS:
        return None
    if code == CODE_EVAL_FORBIDDEN:
        return JavaEvalFencingError
    if code == CODE_EVAL_CONFLICT:
        return JavaEvalConflictError
    if code == CODE_EVAL_INVALID or code == CODE_ILLEGAL_PARAMETER:
        return JavaEvalValidationError
    if code == CODE_UNAUTHORIZED:
        return JavaEvalAuthError
    # 未知业务码：按不可重试的通用失败处理。宁可停下让人看，也不要盲目重试。
    return JavaEvalError
