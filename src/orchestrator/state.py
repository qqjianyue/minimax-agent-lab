"""B6 C7 编排状态定义。

编排状态是 LangGraph ``StateGraph`` 的节点间契约 —— 每个节点读取/写入
哪些字段必须显式声明，图才不会在运行期悄悄丢字段。
"""

from __future__ import annotations

from typing import Any, TypedDict

from agent_core.ports import LLMMessage, TokenUsage, ToolCall
from policy_engine.decision import Decision


class ToolCallInfo(TypedDict):
    """一次工具调用的完整记录（进响应与审计）。"""

    name: str
    arguments: dict[str, Any]
    #: executor 结果状态：ok / blocked / requires_approval / not_found /
    #: invalid_arguments / permission_denied / handler_error
    status: str
    #: status=ok 时的输出文本；其余情况为说明文本
    output: str
    #: TOOL 阶段 guard 决策（拦截时非 None；审计追溯"为什么没执行"）
    decision: Decision | None


class OrchestratorState(TypedDict, total=False):
    """编排图的状态。

    字段刻意用 ``total=False``：不同分支只写入自己需要的字段，图框架
    在节点间传递的是同一份 dict，未写的键保持缺席 —— 节点读取时必须
    ``.get()`` 而不是直接索引，防止把上一轮的残留值当成这轮的结果。
    """

    request_id: str
    user_message: str
    #: 输入 guard 处理后的文本（REDACT 后送入模型的版本）
    guarded_input: str
    input_decision: Decision
    output_decision: Decision
    #: 发给 LLM 的完整消息历史（system / user / assistant / tool）
    messages: list[LLMMessage]
    #: 待执行的工具调用队列（LLMResponse.tool_calls → 状态；LLMMessage 不承载）
    pending_tool_calls: list[ToolCall]
    #: 本次对话实际调用的工具（按执行顺序）
    tools_called: list[ToolCallInfo]
    #: LLM 调用轮数（max_steps 兜底的计数口径）
    steps: int
    #: 全部 LLM 调用的 token 用量累计（响应展示成本口径）
    total_usage: TokenUsage
    #: 出现需人工审批/被拦截的工具调用（HITL 语义标记）
    interrupted: bool
    #: 输入侧被拦截（不调模型，直接返回）
    blocked: bool
    #: 最终交给用户的文本（已按动作处理）
    response: str
    #: 是否发生了脱敏改写
    redacted: bool


__all__ = ["OrchestratorState", "ToolCallInfo"]
