"""时钟实现。

生产用 :class:`SystemClock`，测试用 ``tests/fakes.py`` 里的 ``FrozenClock``。
项目里所有涉及超时、退避、保留期的代码都应通过 :class:`~agent_core.ports.Clock`
协议取时间，不直接调 ``time.time()`` —— 这样才可能写出确定性的测试。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """真实时钟。统一使用 UTC，避免目标机（UTC）与本地（CST）时区混用。"""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


__all__ = ["SystemClock"]
