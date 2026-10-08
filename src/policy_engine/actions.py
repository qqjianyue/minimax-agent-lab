"""策略动作的执行。

``Decision`` 回答"应该怎么做"，本模块回答"那具体做什么"。

两者的边界是刻意的：决策层是纯函数、可穷举测试；执行层会真的改动内容，
必须用真实文本验证。所以 ``apply_action`` 同时返回**改写后的文本**和
**改了多少** —— 后者让调用方能断言"确实脱敏了"，而不是只相信策略说它脱敏了。

延迟到 B6 的动作：``REWRITE`` 需要再调一次模型，属于编排层的职责；
这里只把它显式标记出来，绝不静默地当作放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from guard_contract.enums import PolicyAction
from policy_engine.redactor import Redaction, RedactionResult, Redactor

#: 阻断时返回给用户的预设响应。
#: 刻意不包含任何策略细节 —— 告诉攻击者"你命中了 prompt_injection 规则"
#: 等于给他们提供了绕过反馈。
BLOCKED_MESSAGE = "抱歉，我无法处理这个请求。"

#: 需要人工复核时的附加说明
_ESCALATED_SUFFIX = "\n\n[该回复已标记为待人工复核]"


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """执行一个策略动作后的结果。"""

    #: 实际可以交付给用户（或交给下一层）的文本
    text: str
    #: 文本是否被改动过
    changed: bool = False
    #: 是否需要挂起等待人工
    await_approval: bool = False
    #: 是否需要重新生成（REACTION 由编排层处理）
    needs_regeneration: bool = False
    #: 脱敏详情，仅 REDACT 时非空
    redactions: tuple[Redaction, ...] = field(default_factory=tuple)

    @property
    def redaction_summary(self) -> str:
        return RedactionResult(text=self.text, redactions=self.redactions).summary()


def apply_action(
    action: PolicyAction,
    text: str,
    *,
    redactor: Redactor,
) -> ActionOutcome:
    """按动作处理文本。

    Args:
    action: 策略动作。
    text: 待处理文本（模型输出或用户输入）。
    redactor: 脱敏器。仅 ``REDACT`` 时会被调用。

    Returns:
        :class:`ActionOutcome`。
    """
    match action:
        case PolicyAction.ALLOW:
            return ActionOutcome(text=text)

        case PolicyAction.REDACT:
            result = redactor.redact(text)
            return ActionOutcome(
                text=result.text,
                changed=result.changed,
                redactions=result.redactions,
            )

        case PolicyAction.BLOCK:
            return ActionOutcome(text=BLOCKED_MESSAGE, changed=text != BLOCKED_MESSAGE)

        case PolicyAction.ESCALATE | PolicyAction.REQUIRE_APPROVAL:
            return ActionOutcome(text=text + _ESCALATED_SUFFIX, changed=True, await_approval=True)

        case PolicyAction.REWRITE:
            # B6 之前不能悄悄当成放行：调用方必须显式处理重新生成
            return ActionOutcome(text=text, needs_regeneration=True)

    raise AssertionError(f"未覆盖的 action: {action!r}")


__all__ = ["BLOCKED_MESSAGE", "ActionOutcome", "apply_action"]
