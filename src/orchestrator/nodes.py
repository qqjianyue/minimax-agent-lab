"""C7 编排节点实现（LangGraph 图的每个节点）。

节点的纪律（与项目"观测边界显式"的纪律一致）：

1. **guard 节点必须同时写审计与 span**：编排里的输入/输出/工具检测点
   与 B3/B5 的 ``_run_guarded`` 语义相同 —— 决策进账本、耗时进 trace。
   编排层注入 :class:`GuardRunner`（容器装配时带上 pipeline/redactor/
   ledger/instrumentation），节点不直接依赖应用层函数。
2. **LLM 调用必须走注入的端口**：节点不直接 import MinimaxLLM。
3. **工具执行必须过 TOOL guard**：guard 非 ALLOW 即不执行（B5 纪律），
   结构化参数不做"脱敏后继续"，直接标记中断（HITL）。
4. **tool_calls 走状态队列，不塞进消息**：``LLMMessage`` 没有 tool_calls
   字段（它在 ``LLMResponse`` 上）。待执行队列放 ``pending_tool_calls``，
   assistant 消息只存文本；工具结果按 ``tool_call_id`` 回填 tool 消息，
   模型下一轮才能看到执行结果。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence

from agent_core.ports import LLMMessage, LLMRequest, LLMResponse
from agent_tools.executor import ToolExecutor
from agent_tools.registry import ToolRegistry
from guard_contract.enums import GuardStage, PolicyAction
from orchestrator.state import OrchestratorState
from policy_engine.actions import apply_action
from policy_engine.decision import Decision
from policy_engine.pipeline import GuardPipeline
from policy_engine.redactor import Redactor

logger = logging.getLogger("orchestrator")

#: 编排层注入给工具执行的基础权限（B5 演示语义；后续按用户身份注入）
BASE_CALLER_SCOPES = ("customer_service",)

#: 工具调用被 guard 拦下时回填给模型的说明（模型据此调整策略）
_TOOL_BLOCKED_TEXT = "工具调用被安全策略拦截（需人工审批），未执行。"
_TOOL_NOT_FOUND_TEXT = "工具不存在，请勿再次调用。"
_TOOL_INVALID_ARGS_TEXT = "参数校验失败，请检查参数格式后重试。"


class GuardRunner:
    """一个检测点的执行器：pipeline 判定 + 审计 + 耗时返回。

    与 ``agent_service.app._run_guarded`` 同语义，但归属在编排层 ——
    避免 orchestrator 反向依赖应用层。instrumentation 为 None（纯单测）
    时退化为只跑 pipeline + 审计。
    """

    def __init__(
        self,
        pipeline: GuardPipeline,
        *,
        redactor: Redactor,
        ledger: object | None,
        instrumentation: object | None,
    ) -> None:
        self._pipeline = pipeline
        self.redactor = redactor
        self._ledger = ledger
        self._instrumentation = instrumentation

    def run(
        self,
        *,
        stage: GuardStage,
        text: str,
        request_id: str,
        span_name: str,
        metadata: dict[str, object] | None = None,
    ) -> Decision:
        """跑检测点：pipeline 判定 + 审计 + （有埋点时）span 与耗时记录。"""
        inst = self._instrumentation
        if inst is None:
            decision = self._pipeline.run(text, stage=stage, request_id=request_id)
        else:
            with inst.guard(span_name, stage) as recorder:
                started = time.perf_counter()
                decision = self._pipeline.run(text, stage=stage, request_id=request_id)
                recorder.record(decision, latency_ms=(time.perf_counter() - started) * 1000.0)
        self._audit(decision, metadata=metadata)
        return decision

    def _audit(self, decision: Decision, *, metadata: dict[str, object] | None) -> None:
        if self._ledger is None:
            return
        try:
            self._ledger.record(decision, metadata=metadata)
        except Exception as exc:  # noqa: BLE001 - 审计写失败不阻断业务（B4 纪律）
            logger.warning("audit write failed: %s: %s", type(exc).__name__, exc)


# --- 节点 ----------------------------------------------------------------


def input_guard_node(state: OrchestratorState, runner: GuardRunner) -> OrchestratorState:
    """INPUT 检测（环绕拦截）。非放行 → 标记 blocked 并直接产出响应文本。"""
    decision = runner.run(
        stage=GuardStage.INPUT,
        text=state["user_message"],
        request_id=state["request_id"],
        span_name="input_guard",
        metadata={"endpoint": "/chat"},
    )
    outcome = apply_action(decision.action, state["user_message"], redactor=runner.redactor)
    next_state = dict(state)
    next_state["input_decision"] = decision
    next_state["guarded_input"] = outcome.text
    next_state["redacted"] = outcome.changed
    if decision.action is PolicyAction.BLOCK or decision.requires_human:
        next_state["blocked"] = True
        next_state["response"] = outcome.text
    return next_state


def llm_plan_node(
    state: OrchestratorState,
    *,
    llm_complete: Callable[[LLMRequest], LLMResponse],
    tools: ToolRegistry,
    system_prompt: str,
    model: str,
) -> OrchestratorState:
    """一次 LLM 规划调用（带全部工具声明）。返回 tool_calls → 进入工具循环。

    ``llm_complete`` 是容器装配时包装好的调用闭包 —— 生产实现带
    ``llm_call`` span（token/成本埋点，FT-11 口径），测试直接传
    ``FakeLLM.complete``。节点只负责组装请求与消费响应。
    """
    next_state = dict(state)
    messages = list(state.get("messages", []))
    # system 提示只放首轮；工具结果由 tool_loop 以 role=tool 回填
    if not messages:
        messages.append(LLMMessage(role=LLMMessage.ROLE_SYSTEM, content=system_prompt))
        messages.append(LLMMessage(role=LLMMessage.ROLE_USER, content=state["guarded_input"]))

    request = LLMRequest(
        model=model,
        messages=tuple(messages),
        tools=tools.specs(),
        temperature=1.0,
    )
    completion = llm_complete(request)
    messages.append(
        LLMMessage(role=LLMMessage.ROLE_ASSISTANT, content=completion.content)
    )

    next_state["messages"] = messages
    next_state["steps"] = state.get("steps", 0) + 1
    # 待执行工具队列（LLMMessage 不承载 tool_calls，走状态队列）
    next_state["pending_tool_calls"] = list(getattr(completion, "tool_calls", ()))
    # token 用量累计（多轮编排的成本口径 = 各轮之和）
    prev = state.get("total_usage")
    usage = getattr(completion, "usage", None)
    if usage is not None:
        from agent_core.ports import TokenUsage  # noqa: PLC0415

        next_state["total_usage"] = TokenUsage(
            prompt_tokens=(prev.prompt_tokens if prev else 0) + usage.prompt_tokens,
            completion_tokens=(prev.completion_tokens if prev else 0) + usage.completion_tokens,
        )
    return next_state


def tool_loop_node(
    state: OrchestratorState,
    *,
    executor: ToolExecutor,
    runner: GuardRunner,
) -> OrchestratorState:
    """顺序执行待处理 tool_call（guard 非 ALLOW 即不执行）。

    工具结果以 ``role=tool`` + ``tool_call_id`` 回填消息；被拦/需审批的
    调用标记 ``interrupted``（HITL 语义），模型下一轮看到结果可调整。
    """
    pending = list(state.get("pending_tool_calls", ()))
    if not pending:
        return state

    next_state = dict(state)
    messages = list(state["messages"])
    called = list(state.get("tools_called", []))
    interrupted = bool(state.get("interrupted", False))

    for tc in pending:
        guard_text = f"{tc.name}: {json.dumps(tc.arguments, ensure_ascii=False)}"
        decision = runner.run(
            stage=GuardStage.TOOL,
            text=guard_text,
            request_id=state["request_id"],
            span_name="tool_guard",
            metadata={"endpoint": "/chat", "tool": tc.name},
        )
        if decision.action is not PolicyAction.ALLOW:
            blocked = decision.action is PolicyAction.BLOCK
            called.append(
                {
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "status": "blocked" if blocked else "requires_approval",
                    "output": _TOOL_BLOCKED_TEXT,
                    "decision": decision,
                }
            )
            interrupted = True
            messages.append(
                LLMMessage(
                    role=LLMMessage.ROLE_TOOL,
                    content=_TOOL_BLOCKED_TEXT,
                    tool_call_id=tc.id,
                )
            )
            continue

        outcome = executor.execute(tc.name, tc.arguments, caller_scopes=BASE_CALLER_SCOPES)
        status = outcome.status
        output = outcome.output or ""
        if status == "requires_approval":
            interrupted = True
            output = "该操作需人工审批，未执行。"
        elif status == "not_found":
            output = _TOOL_NOT_FOUND_TEXT
        elif status == "invalid_arguments":
            output = _TOOL_INVALID_ARGS_TEXT
        called.append(
            {
                "name": tc.name,
                "arguments": tc.arguments,
                "status": status,
                "output": output,
                "decision": decision,
            }
        )
        messages.append(
            LLMMessage(role=LLMMessage.ROLE_TOOL, content=output, tool_call_id=tc.id)
        )

    next_state["messages"] = messages
    next_state["tools_called"] = called
    next_state["interrupted"] = interrupted
    # 本轮已处理完，队列清空；是否需要再规划由条件边决定
    next_state["pending_tool_calls"] = []
    return next_state


def output_guard_node(state: OrchestratorState, runner: GuardRunner) -> OrchestratorState:
    """OUTPUT 检测（环绕拦截，发生在返回用户之前）。"""
    text = _final_assistant_text(state.get("messages", ()))
    if text is None:
        text = "（无回复）"
    decision = runner.run(
        stage=GuardStage.OUTPUT,
        text=text,
        request_id=state["request_id"],
        span_name="output_guard",
        metadata={"endpoint": "/chat"},
    )
    outcome = apply_action(decision.action, text, redactor=runner.redactor)
    next_state = dict(state)
    next_state["output_decision"] = decision
    next_state["response"] = outcome.text
    next_state["redacted"] = bool(state.get("redacted", False)) or outcome.changed
    return next_state


def _final_assistant_text(messages: Sequence[LLMMessage]) -> str | None:
    """最后一条**无待执行工具**的 assistant 文本（工具循环后的最终答复）。

    带 tool_calls 的 assistant 消息内容是"我要查一下…"这类过程文本，
    不是最终答复；工具结果（role=tool）也要跳过。
    """
    for msg in reversed(messages):
        if msg.role == LLMMessage.ROLE_ASSISTANT:
            return msg.content
    return None


def has_pending_tool_calls(state: OrchestratorState) -> bool:
    """条件边：是否还有待执行工具。"""
    return bool(state.get("pending_tool_calls", ()))


def should_continue_loop(state: OrchestratorState, *, max_steps: int) -> str:
    """条件边：工具循环后是否回规划。

    语义：**只要未达轮数上限就回 ``plan``** —— 工具结果必须让模型看到
    （它可能给出最终答复，也可能发起新一轮工具请求），"是否有新工具"
    由 ``llm_plan`` 的条件边判定；只有 ``max_steps`` 是硬兜底 —— 宁可不
    回复也不无限烧模型额度。
    """
    if state.get("steps", 0) >= max_steps:
        logger.warning("orchestrator reached max_steps=%s, force finalize", max_steps)
        return "finalize"
    return "plan"


__all__ = [
    "GuardRunner",
    "has_pending_tool_calls",
    "input_guard_node",
    "llm_plan_node",
    "output_guard_node",
    "should_continue_loop",
    "tool_loop_node",
]
