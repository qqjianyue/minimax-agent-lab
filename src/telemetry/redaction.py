"""span 属性脱敏。

## 为什么需要它

trace 会**落盘**。架构方案 §6.4 把"trace 会落盘 prompt 与 response 原文，
落盘前必须脱敏"列为已知风险，对应的开关是 ``telemetry.capture_prompts``。

银行场景下默认值是 ``False``：宁可 trace 里少一点上下文，也不能让 prompt
原文与 PII 进了可被调阅的观测库。这不是"日志脱敏"的同一件事 —— 日志是
我们自己控制输出格式，trace 的**属性名由埋点代码决定**，只要某处顺手写了
一个 ``prompt`` 属性，明文就已经出去了，而且是静默出去的。

所以脱敏做成**span processor 而不是调用约定**：埋在业务代码里的
``set_attribute("prompt", text)`` 仍然可以照常写，由 processor 在出口处统一
拦截。靠约定迟早会漏，靠出口拦截才可靠。
"""

from __future__ import annotations

from typing import Any

from policy_engine.redactor import Redactor

#: **绝对不允许进 trace 的属性名**，无论 capture_prompts 如何设置。
#:
#: 这些是凭据类字段：即便调用方误写，也不该出现在可被调阅的观测库里。
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "llm.api_key",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
    }
)

#: 承载用户原文 / 模型输出的属性名。``capture_prompts=False`` 时替换成占位符。
PROMPT_ATTRIBUTES = frozenset(
    {
        "prompt",
        "input.text",
        "output.text",
        "completion",
        "messages",
        "system_prompt",
        "user_message",
        "retrieved_documents",
    }
)

#: capture_prompts=False 时写入的占位符。
#:
#: 刻意**保留键名、只丢内容**：排查时要能看出"这里本来有段文本、但出于合规
#: 没有记录"，而不是误以为这条链路根本没带文本。
REDACTED_PLACEHOLDER = "[REDACTED: capture_prompts=false]"


def _sanitize_value(value: Any, *, capture_prompts: bool, redactor: Redactor | None) -> Any:
    """递归脱敏单个属性值。

    埋点代码可能把结构化数据（dict / list）写进一个属性，例如
    ``{"results": [{"text": "身份证 110105..."}]}``。只对顶层字符串脱敏
    会漏掉嵌套层里的 PII —— 出口拦截要可靠，就必须递归到每一层。
    非字符串类型原样保留。
    """
    if isinstance(value, str):
        if redactor is not None:
            result = redactor.redact(value)
            if result.changed:
                return result.text
        return value
    if isinstance(value, dict):
        return _sanitize_mapping(
            value, capture_prompts=capture_prompts, redactor=redactor
        )
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_value(v, capture_prompts=capture_prompts, redactor=redactor)
            for v in value
        ]
    return value


def _sanitize_mapping(
    mapping: dict[str, Any], *, capture_prompts: bool, redactor: Redactor | None
) -> dict[str, Any]:
    """按键过滤一个属性字典。**顶层与嵌套共用同一套规则**。

    刻意不只在顶层过滤：如果 ``_sanitize_value`` 只管脱敏值不管键名，
    那么 ``set_attribute("llm.cfg", {"api_key": "sk-live-..."})`` 会把凭据
    原样送进可被调阅的观测库 —— 而且 redactor 兜不住，它只认 PII 模式
    （身份证/手机号/银行卡），对 ``sk-`` 开头的密钥一无所知。
    嵌套层的 ``prompt`` 同理：键名才是判断依据，与它出现在第几层无关。
    """
    safe: dict[str, Any] = {}

    for key, value in mapping.items():
        if key in FORBIDDEN_ATTRIBUTES:
            # 凭据类不留任何痕迹，连占位符都不给 —— 免得有人去猜是不是真的配了
            continue

        if key in PROMPT_ATTRIBUTES and not capture_prompts:
            safe[key] = REDACTED_PLACEHOLDER
            continue

        safe[key] = _sanitize_value(
            value, capture_prompts=capture_prompts, redactor=redactor
        )

    return safe


def sanitize_attributes(
    attributes: dict[str, Any],
    *,
    capture_prompts: bool,
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    """过滤单个 span 的属性。

    过滤**递归**到嵌套的 dict / list：属性名（尤其是凭据类与 prompt 类）
    在任意深度都要被同样对待。

    Args:
        capture_prompts: 是否允许把 prompt / 输出原文写进 trace。
        redactor: 用于脱敏 PII。给了就会对保留的属性做 PII 脱敏（含嵌套
            dict / list 内的字符串）；没给则只做"整段丢弃"，不尝试部分
            脱敏（宁可少信息，不要半吊子）。

    Returns:
        可安全写入 trace 的属性副本。**不修改入参**。
    """
    return _sanitize_mapping(
        attributes, capture_prompts=capture_prompts, redactor=redactor
    )


__all__ = [
    "FORBIDDEN_ATTRIBUTES",
    "PROMPT_ATTRIBUTES",
    "REDACTED_PLACEHOLDER",
    "sanitize_attributes",
]
