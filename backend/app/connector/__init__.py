"""AgentWrite（Java）HTTP connector 包（EP-3）。

对外只暴露三样东西，其余（schemas/resilience）按需从子模块导入：

- :class:`JavaEvalClient` —— typed 客户端，覆盖 ``/api/v1/eval/**`` 全部 13 个端点。
- :class:`JavaEvalError` 及子类 —— 按《独立平台项目工作》第 13 节的错误分类矩阵，
  通过 ``retryable`` 标志决定可否重试。
- :data:`errors` 里的业务码常量 —— 与 Java ``ResponseCode`` 枚举一一对应。

**调用方最需要记住的一点**：Java 端点对业务错误返回 **HTTP 200**，错误码在响应体
``code`` 字段。不要自己判断 ``response.status_code == 200`` 就当成功——请统一走本包，
由 :func:`app.connector.errors.classify_business_code` 判定。
"""

from app.connector import errors, schemas
from app.connector.client import JavaEvalClient
from app.connector.errors import (
    CODE_EVAL_CONFLICT,
    CODE_EVAL_FORBIDDEN,
    CODE_EVAL_INVALID,
    CODE_SUCCESS,
    JavaEvalAuthError,
    JavaEvalCircuitOpenError,
    JavaEvalConflictError,
    JavaEvalContractError,
    JavaEvalError,
    JavaEvalFencingError,
    JavaEvalTransientError,
    JavaEvalValidationError,
)
from app.connector.resilience import CircuitBreaker, CircuitState, RateLimiter, RetryPolicy

__all__ = [
    "CODE_EVAL_CONFLICT",
    "CODE_EVAL_FORBIDDEN",
    "CODE_EVAL_INVALID",
    "CODE_SUCCESS",
    "CircuitBreaker",
    "CircuitState",
    "JavaEvalAuthError",
    "JavaEvalCircuitOpenError",
    "JavaEvalClient",
    "JavaEvalConflictError",
    "JavaEvalContractError",
    "JavaEvalError",
    "JavaEvalFencingError",
    "JavaEvalTransientError",
    "JavaEvalValidationError",
    "RateLimiter",
    "RetryPolicy",
    "errors",
    "schemas",
]
