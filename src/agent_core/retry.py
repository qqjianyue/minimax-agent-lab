"""重试策略。

刻意做成**纯函数**（接收 rng、返回秒数、不自己 sleep），理由有两个：

1. 可测。退避曲线是一定要做边界断言的东西（封顶、抖动范围、第 N 次是否重试），
   自己 sleep 的实现只能靠等真实时间来验证，测试会慢且容易 flaky。

2. 可控。L4 LLM-as-Judge 在关键路径上，延迟预算有限；退避参数直接暴露给
   policy 配置，比藏在客户端重试逻辑里更容易讲清楚"为什么这条链路这么慢"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agent_core.errors import TransientError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """指数退避 + 抖动。

    Attributes:
        max_attempts: 总尝试次数（含首次）。1 表示不重试。
        base_delay_s: 首次失败后的基础等待秒数。
        max_delay_s: 退避上限（抖动前的封顶）。
        multiplier: 每次失败的放大倍数。
        jitter_ratio: 抖动比例，实际延迟落在 ``base * (1 ± jitter)`` 区间。
            抖动用于打散并发重试，避免多个请求在同一时刻一起重试。
        retry_on: 允许重试的异常类型。默认只重试 :class:`TransientError`。
    """

    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.1
    retry_on: tuple[type[Exception], ...] = field(default=(TransientError,))

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts 至少为 1，当前 {self.max_attempts}")
        if self.base_delay_s <= 0:
            raise ValueError(f"base_delay_s 必须为正，当前 {self.base_delay_s}")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError(
                f"max_delay_s ({self.max_delay_s}) 不能小于 base_delay_s ({self.base_delay_s})"
            )
        if self.multiplier < 1:
            raise ValueError(f"multiplier 至少为 1，当前 {self.multiplier}")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError(f"jitter_ratio 必须在 [0, 1]，当前 {self.jitter_ratio}")

    def base_delay_for(self, attempt: int) -> float:
        """不含抖动的退避值。``attempt`` 从 1 开始（第一次失败后）。"""
        if attempt < 1:
            raise ValueError(f"attempt 从 1 开始计数，当前 {attempt}")
        raw = self.base_delay_s * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_delay_s)

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """含抖动的实际等待秒数。

        结果落在 ``[base * (1 - jitter), base * (1 + jitter)]``，非负。
        """
        base = self.base_delay_for(attempt)
        if self.jitter_ratio == 0.0:
            return base
        source = rng if rng is not None else random
        factor = 1.0 + source.uniform(-self.jitter_ratio, self.jitter_ratio)
        return max(0.0, base * factor)

    def should_retry(self, attempt: int, exc: BaseException) -> bool:
        """已完成 ``attempt`` 次尝试且抛出 ``exc`` 后，是否还能再试。"""
        if attempt >= self.max_attempts:
            return False
        return isinstance(exc, self.retry_on)


__all__ = ["RetryPolicy"]
