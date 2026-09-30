"""connector 的韧性组件：限流、熔断、重试策略。

三者都用**可注入的时钟**（``clock``/``sleep``），使测试不必真的 sleep——
契约测试要能在毫秒级验证「指数退避 3 次后放弃」和「熔断打开后快速失败」，
靠 ``time.sleep`` 真实等待会让测试慢且不稳定。

线程安全：Celery worker 可能是 prefork（进程隔离），但 API 侧会用线程池调 connector，
故所有可变状态都加锁。
"""

from __future__ import annotations

import random as _random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from app.connector.errors import JavaEvalCircuitOpenError, JavaEvalError


def _default_random() -> float:
    """默认随机源；单列成模块级函数，避免在 dataclass 字段上塞 lambda。"""
    return _random.random()


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """连续失败达到阈值即打开；冷却期后放一个探针（half-open）决定是否恢复。

    为什么按「连续失败」而不是「窗口内失败率」：connector 的调用量在评测期间是
    脉冲式的（一个 run 集中打几千次），失败率窗口在这种稀疏流量下会长期失真。
    连续失败计数语义直白、无需维护时间窗口，且对「Java 侧整体挂了」这类
    真正需要熔断的场景响应更及时。
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _probe_in_flight: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def before_call(self, *, endpoint: str | None = None) -> None:
        """调用前检查。熔断打开且未到冷却期则直接抛错，不发请求。"""
        with self._lock:
            if self._state is CircuitState.OPEN:
                elapsed = self.clock() - self._opened_at
                if elapsed < self.recovery_timeout:
                    raise JavaEvalCircuitOpenError(
                        f"熔断打开中，拒绝调用（{elapsed:.1f}s / {self.recovery_timeout}s）",
                        endpoint=endpoint,
                    )
                # 冷却期已过：转 half-open，放一个探针试探恢复情况。
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                return

            if self._state is CircuitState.HALF_OPEN:
                # 已有探针在飞，其余调用继续快速失败，避免恢复瞬间被流量打垮。
                if self._probe_in_flight:
                    raise JavaEvalCircuitOpenError("熔断 half-open，已有探针在途", endpoint=endpoint)
                self._probe_in_flight = True

    def on_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False

    def on_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._consecutive_failures += 1
            if self._state is CircuitState.HALF_OPEN or self._consecutive_failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self.clock()

    def reset(self) -> None:
        """测试用：恢复到初始闭合态。"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0
            self._probe_in_flight = False


class RateLimiter:
    """令牌桶限流（每秒 ``rate_per_second`` 个，允许 ``burst`` 个突发）。

    评测会瞬间产生大量调用，直接打满 Java 侧会拖垮业务系统——限流是保护
    AgentWrite 而不是保护平台。默认 20/s 是个保守起点，可按实测调整。

    .. warning::

       注入 ``clock``/``sleep`` 时必须保持二者一致：``sleep(n)`` 要真的让 ``clock()``
       前进约 n 秒。``acquire()`` 是「算令牌 → 不足则睡 → 重算」的循环，靠 sleep 期间
       时间的流逝来补充令牌；若注入一个冻结的 ``clock`` 配一个空转的 ``sleep``，
       令牌永远不会补上，循环将不终止。真实 ``time.sleep`` + ``time.monotonic`` 天然满足，
       只有测试替身需要留意——测试助手见 ``tests/test_connector_resilience.py`` 的
       ``_RecordingSleep``，它会在记录时同步推进假时钟。
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second 必须为正数")
        self._rate = rate_per_second
        self._burst = max(1, burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self._burst)
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """取一个令牌；不足则按需要阻塞等待（调用方视角是「排队」而非失败）。"""
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            # 在锁外 sleep，否则会阻塞其他线程补充令牌。
            self._sleep(wait)


@dataclass
class RetryPolicy:
    """重试策略。

    **只对 ``exc.retryable`` 为 True 的异常重试**——第 13 节明文「禁止对所有异常统一 retry」。
    fencing 拒绝（``JavaEvalFencingError``）的 retryable 恒为 False，故这里天然排除，
    不需要额外的 if 分支；契约测试专门断言了这一点。
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    #: 退避倍数（指数退避）
    multiplier: float = 2.0
    #: 抖动比例：多个 worker 同时重试会形成尖峰，抖动把它们打散。
    jitter: float = 0.1
    sleep: Callable[[float], None] = time.sleep
    #: 随机源可注入，测试时传固定值使退避时间可断言。
    random: Callable[[], float] = _default_random

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后应等待的秒数（attempt 从 1 开始）。"""
        raw = min(self.max_delay, self.base_delay * (self.multiplier ** (attempt - 1)))
        if self.jitter <= 0:
            return raw
        # 对称抖动：raw * (1 ± jitter)，下界不为负。
        offset = raw * self.jitter * (2 * self.random() - 1)
        return max(0.0, raw + offset)

    def should_retry(self, exc: JavaEvalError, attempt: int) -> bool:
        return exc.retryable and attempt < self.max_attempts
