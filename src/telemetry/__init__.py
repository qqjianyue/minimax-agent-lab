"""C9 `telemetry` —— 全链路可观测性。

对外只有 :class:`Instrumentation`（业务埋点门面）与 :func:`build_telemetry`
（按配置构造实现）。其余是内部细节，刻意不导出 —— 埋点的 span 名字与属性
口径应当**只有这一个入口**能改，否则各调用点会逐渐长出不一致的命名。

对应架构方案 §4.5 的 span 树；脱敏见 :mod:`telemetry.redaction`。
"""

from __future__ import annotations

from telemetry.instrumentation import NoOpTelemetry, OtelTelemetry, build_telemetry
from telemetry.redaction import (
    FORBIDDEN_ATTRIBUTES,
    PROMPT_ATTRIBUTES,
    REDACTED_PLACEHOLDER,
    sanitize_attributes,
)
from telemetry.tracing import Instrumentation, estimate_cost

__all__ = [
    "FORBIDDEN_ATTRIBUTES",
    "PROMPT_ATTRIBUTES",
    "REDACTED_PLACEHOLDER",
    "Instrumentation",
    "NoOpTelemetry",
    "OtelTelemetry",
    "build_telemetry",
    "estimate_cost",
    "sanitize_attributes",
]
