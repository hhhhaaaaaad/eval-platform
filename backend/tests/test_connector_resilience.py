"""connector 韧性组件单测：熔断、限流、重试策略。

全部用**注入的假时钟**，不真 sleep——否则「冷却 30 秒后恢复」这类用例每次要跑半分钟，
测试会慢到没人愿意跑，最后被跳过。
"""

from __future__ import annotations

import pytest

from app.connector.errors import (
    JavaEvalCircuitOpenError,
    JavaEvalConflictError,
    JavaEvalFencingError,
    JavaEvalTransientError,
)
from app.connector.resilience import CircuitBreaker, CircuitState, RateLimiter, RetryPolicy


class _FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RecordingSleep:
    """记录 sleep 时长而不真的等待，**同时推进配对的假时钟**。

    推进时钟不是可选的：``RateLimiter.acquire()`` 靠 sleep 期间流逝的时间补令牌，
    只记录不推进会让它永远补不满，循环不终止（会把测试跑成 MemoryError）。
    真实 ``time.sleep`` 天然推进 ``time.monotonic``，这里如实模拟同一个契约。
    """

    def __init__(self, clock: _FakeClock) -> None:
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    def test_only_retryable_errors_are_retried(self) -> None:
        """第 13 节明文「禁止对所有异常统一 retry」——这里就是那条约束的实现点。"""
        policy = RetryPolicy(max_attempts=3)

        assert policy.should_retry(JavaEvalTransientError("超时"), attempt=1) is True
        # fencing 拒绝重试毫无意义：版本不会因为重试而变对。
        assert policy.should_retry(JavaEvalFencingError("拒绝"), attempt=1) is False
        assert policy.should_retry(JavaEvalConflictError("冲突"), attempt=1) is False

    def test_stops_at_max_attempts(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(JavaEvalTransientError("x"), attempt=2) is True
        assert policy.should_retry(JavaEvalTransientError("x"), attempt=3) is False

    def test_exponential_backoff_without_jitter(self) -> None:
        policy = RetryPolicy(base_delay=0.5, multiplier=2.0, max_delay=8.0, jitter=0.0)
        assert [policy.delay_for(n) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]

    def test_backoff_is_capped_at_max_delay(self) -> None:
        """不设上限的话，第 10 次退避会到 256 秒，比 run 的 lease 还长。"""
        policy = RetryPolicy(base_delay=0.5, multiplier=2.0, max_delay=8.0, jitter=0.0)
        assert policy.delay_for(20) == 8.0

    def test_jitter_stays_bounded_and_non_negative(self) -> None:
        policy = RetryPolicy(base_delay=2.0, multiplier=1.0, max_delay=10.0, jitter=0.25)
        # 随机源推到两个极端，验证抖动幅度不越界（下界不会被压成负数）。
        policy.random = lambda: 1.0
        assert policy.delay_for(1) == pytest.approx(2.5)
        policy.random = lambda: 0.0
        assert policy.delay_for(1) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed_and_allows_calls(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        assert breaker.state is CircuitState.CLOSED
        breaker.before_call()  # 不应抛

    def test_opens_after_threshold_failures(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            breaker.on_failure()
        assert breaker.state is CircuitState.CLOSED
        breaker.on_failure()
        assert breaker.state is CircuitState.OPEN

    def test_open_rejects_calls_fast(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0)
        breaker.on_failure()
        with pytest.raises(JavaEvalCircuitOpenError):
            breaker.before_call()

    def test_half_open_after_recovery_timeout(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, clock=clock)
        breaker.on_failure()

        clock.advance(29.0)
        with pytest.raises(JavaEvalCircuitOpenError):
            breaker.before_call()

        clock.advance(2.0)  # 累计 31s > 30s
        breaker.before_call()  # 放探针
        assert breaker.state is CircuitState.HALF_OPEN

    def test_half_open_allows_only_one_probe(self) -> None:
        """恢复瞬间若放全部流量进来，刚缓过来的 Java 侧会被二次打垮。"""
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
        breaker.on_failure()
        clock.advance(11.0)

        breaker.before_call()  # 第一个探针放行
        with pytest.raises(JavaEvalCircuitOpenError):
            breaker.before_call()  # 第二个被拒

    def test_probe_success_closes_circuit(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
        breaker.on_failure()
        clock.advance(11.0)
        breaker.before_call()
        breaker.on_success()

        assert breaker.state is CircuitState.CLOSED
        breaker.before_call()  # 恢复后正常放行

    def test_probe_failure_reopens_immediately(self) -> None:
        """half-open 下探针失败要立刻回到 open，不必再凑满阈值。"""
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=10.0, clock=clock)
        for _ in range(5):
            breaker.on_failure()
        clock.advance(11.0)
        breaker.before_call()

        breaker.on_failure()
        assert breaker.state is CircuitState.OPEN

    def test_success_resets_failure_counter(self) -> None:
        """「连续失败」语义：中间成功一次就该清零，否则稀疏流量下会慢性开闸。"""
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.on_failure()
        breaker.on_failure()
        breaker.on_success()
        breaker.on_failure()
        breaker.on_failure()
        assert breaker.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_rejects_non_positive_rate(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(rate_per_second=0)

    def test_burst_tokens_are_consumed_without_waiting(self) -> None:
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = RateLimiter(rate_per_second=10.0, burst=3, clock=clock, sleep=sleep)

        for _ in range(3):
            limiter.acquire()

        assert sleep.calls == []

    def test_waits_when_tokens_exhausted(self) -> None:
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = RateLimiter(rate_per_second=10.0, burst=1, clock=clock, sleep=sleep)

        limiter.acquire()  # 用掉唯一的令牌
        limiter.acquire()  # 需要等 1/10 秒

        assert len(sleep.calls) == 1
        assert sleep.calls[0] == pytest.approx(0.1)

    def test_tokens_refill_over_time(self) -> None:
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = RateLimiter(rate_per_second=10.0, burst=2, clock=clock, sleep=sleep)

        limiter.acquire()
        limiter.acquire()
        clock.advance(0.5)  # 补 5 个令牌，但上限是 burst=2
        limiter.acquire()
        limiter.acquire()
        assert sleep.calls == []
