"""C1 agent-core · 重试策略单元测试。

退避曲线是必须做边界断言的东西：封顶是否生效、抖动是否越界、
第 N 次之后是否停止重试。这里全部是纯函数调用，毫秒级且无 flaky 风险。
"""

from __future__ import annotations

import random

import pytest

from agent_core.errors import (
    ConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    TransientError,
)
from agent_core.retry import RetryPolicy


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"max_attempts": 0}, "max_attempts"),
            ({"base_delay_s": 0}, "base_delay_s"),
            ({"max_delay_s": 0.1}, "不能小于"),
            ({"multiplier": 0.5}, "multiplier"),
            ({"jitter_ratio": 1.5}, "jitter_ratio"),
        ],
    )
    def test_invalid_config_rejected(self, kwargs: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            RetryPolicy(**kwargs)


class TestBackoff:
    def test_exponential_growth(self) -> None:
        policy = RetryPolicy(base_delay_s=0.5, multiplier=2.0, max_delay_s=100.0, jitter_ratio=0.0)
        assert policy.base_delay_for(1) == pytest.approx(0.5)
        assert policy.base_delay_for(2) == pytest.approx(1.0)
        assert policy.base_delay_for(3) == pytest.approx(2.0)
        assert policy.base_delay_for(4) == pytest.approx(4.0)

    def test_caps_at_max_delay(self) -> None:
        policy = RetryPolicy(base_delay_s=1.0, multiplier=10.0, max_delay_s=8.0, jitter_ratio=0.0)
        assert policy.base_delay_for(1) == pytest.approx(1.0)
        assert policy.base_delay_for(2) == pytest.approx(8.0)
        assert policy.base_delay_for(30) == pytest.approx(8.0)

    def test_attempt_is_one_based(self) -> None:
        policy = RetryPolicy(jitter_ratio=0.0)
        with pytest.raises(ValueError, match="attempt 从 1 开始"):
            policy.base_delay_for(0)

    def test_no_jitter_matches_base_exactly(self) -> None:
        policy = RetryPolicy(base_delay_s=0.25, jitter_ratio=0.0)
        rng = random.Random(1234)
        for attempt in range(1, 5):
            assert policy.delay_for(attempt, rng=rng) == policy.base_delay_for(attempt)

    def test_jitter_stays_within_bounds(self) -> None:
        """抖动后的延迟必须落在 base*(1±jitter) 区间内，且非负。"""
        jitter = 0.2
        policy = RetryPolicy(
            base_delay_s=1.0, multiplier=2.0, max_delay_s=100.0, jitter_ratio=jitter
        )
        rng = random.Random(20261008)
        for attempt in range(1, 6):
            base = policy.base_delay_for(attempt)
            for _ in range(200):
                delay = policy.delay_for(attempt, rng=rng)
                assert delay >= 0.0
                assert base * (1 - jitter) - 1e-9 <= delay <= base * (1 + jitter) + 1e-9

    def test_jitter_actually_varies(self) -> None:
        """抖动必须真的在分散延迟，否则并发重试仍会打在同一点上。"""
        policy = RetryPolicy(jitter_ratio=0.3)
        rng = random.Random(7)
        values = {policy.delay_for(1, rng=rng) for _ in range(50)}
        assert len(values) > 1


class TestShouldRetry:
    def test_retries_transient_errors(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(1, LLMTimeoutError("timeout")) is True
        assert policy.should_retry(1, LLMRateLimitError("429")) is True
        assert policy.should_retry(1, TransientError("blip")) is True

    def test_does_not_retry_permanent_errors(self) -> None:
        """重放一个必然再次失败的请求只是浪费额度 —— 这类错误不进重试。"""
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(1, LLMResponseError("malformed json")) is False
        assert policy.should_retry(1, ConfigurationError("bad config")) is False
        assert policy.should_retry(1, ValueError("nope")) is False

    def test_stops_at_max_attempts(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(2, LLMTimeoutError("t")) is True
        assert policy.should_retry(3, LLMTimeoutError("t")) is False
        assert policy.should_retry(4, LLMTimeoutError("t")) is False

    def test_max_attempts_one_means_no_retry(self) -> None:
        assert RetryPolicy(max_attempts=1).should_retry(1, LLMTimeoutError("t")) is False

    def test_retry_on_is_configurable(self) -> None:
        policy = RetryPolicy(retry_on=(LLMError,))
        assert policy.should_retry(1, LLMResponseError("x")) is True


def test_hierarchy_allows_transient_classification() -> None:
    """类型即契约：重试策略靠 isinstance 判断，不靠布尔标记。"""
    assert issubclass(LLMTimeoutError, TransientError)
    assert issubclass(LLMRateLimitError, TransientError)
    assert not issubclass(LLMResponseError, TransientError)
