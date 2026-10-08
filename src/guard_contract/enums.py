"""契约枚举。

枚举值刻意用 :class:`~enum.StrEnum`（直接是字符串）：审计日志和 policy YAML
里最终存的都是这些字符串值，类型和值统一可以省掉一层转换，也避免手写
``str, Enum`` 时在 f-string 里变成 ``PolicyAction.BLOCK`` 这种难读的输出。

关于 ``PolicyAction.is_dataflow_action``：架构方案里有一条关键区分 ——
"拦截"（block / redact / rewrite / require_approval）会同步改变数据流，
所以必须位于管道的前后两侧（环绕拦截）；"审计"只记录已发生的决策，
是横切面，不改变数据流。把它建模成枚举上的属性而不是散落在文档里的口头约定，
是为了让这条区分在写 policy 逻辑时随时可查、也可测。
"""

from __future__ import annotations

from enum import StrEnum

from agent_core.errors import ConfigurationError


class GuardStage(StrEnum):
    """检测阶段 —— 对应架构方案 §1.2 的检测点。"""

    INPUT = "input"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    OUTPUT = "output"


class Severity(StrEnum):
    """威胁严重度。与 ``score``（连续风险分）不同，severity 是离散档位。"""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyAction(StrEnum):
    """策略动作。"""

    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"
    REWRITE = "rewrite"
    ESCALATE = "escalate"
    REQUIRE_APPROVAL = "require_approval"

    @property
    def is_dataflow_action(self) -> bool:
        """是否改变数据流（即属于「环绕拦截」而非「审计」）。

        ``ALLOW`` 与 ``ESCALATE`` 不改变数据流：前者原样放行，后者放行但打标记
        送人工复核。其余四种都会实质改写或截断数据流。
        """
        return self in _DATA_FLOW_ACTIONS

    @property
    def is_terminal(self) -> bool:
        """是否会终止本次请求。

        ``REWRITE`` 会再走一轮生成，``ESCALATE`` / ``REQUIRE_APPROVAL`` 会挂起
        等待人工，因此都不是终结。
        """
        return self in _TERMINAL_ACTIONS

    @classmethod
    def parse(cls, value: str) -> PolicyAction:
        """从配置字符串解析，失败时给出可操作的错误信息。"""
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(a.value for a in cls)
            raise ConfigurationError(f"非法的 policy action: {value!r}，可选值: {valid}") from exc


class FailMode(StrEnum):
    """检测器不可用时的降级策略。对应架构方案 §5.7。"""

    #: 放行 + 告警。适合低风险信息查询，用户体验优先。
    OPEN = "open"
    #: 阻断 + 告警。适合高危操作，安全优先。
    CLOSED = "closed"
    #: 只跑 L1 规则层，保持基础防护。
    DEGRADED = "degraded"


_DATA_FLOW_ACTIONS = frozenset(
    {
        PolicyAction.REDACT,
        PolicyAction.BLOCK,
        PolicyAction.REWRITE,
        PolicyAction.REQUIRE_APPROVAL,
    }
)

_TERMINAL_ACTIONS = frozenset({PolicyAction.ALLOW, PolicyAction.BLOCK, PolicyAction.REDACT})

#: 银行场景默认 fail-closed：安全优先，误杀成本低于漏检成本。
DEFAULT_FAIL_MODE = FailMode.CLOSED

__all__ = [
    "DEFAULT_FAIL_MODE",
    "FailMode",
    "GuardStage",
    "PolicyAction",
    "Severity",
]
