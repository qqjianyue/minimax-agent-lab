"""C7 编排图（LangGraph StateGraph）装配。

图拓扑（B6 切片，retrieve 节点为 B8 占位）：

::

    START
      │
      ▼
    input_guard ──blocked──► END（直接返回拦截响应，不调模型）
      │
      ▼
    retrieve（B8）──blocked──► END（不可信检索内容被判出注入）
      │
      ▼
    llm_plan ──tool_calls──► tool_loop ──┐
      │  ▲                              │
      │  └──── pending ──────────────────┘
      │                （steps < max_steps 时回 plan）
      ▼
    output_guard ──► END（respond 状态在 guard 内完成）

- ``retrieve`` 节点（B8）插在 ``input_guard`` 与 ``llm_plan`` 之间：检索内容
  过 ``RETRIEVAL`` 检测点后，以**不可信数据**标记作为参考块进入上下文
  （FT-09 间接注入）。B6/B7 阶段该节点是空操作。
- ``respond`` 即 output_guard 的产出（response 字段），不单设节点 ——
  状态在 guard 应用动作时已经完备。
- HITL（human-in-the-loop）：requires_approval / 被拦工具 → 标记
  ``interrupted``，响应带 requires_human；完整"暂停-恢复"（LangGraph
  interrupt + checkpointer 持久化）留 B7+，B6 用状态标记承载语义。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from agent_core.ports import LLMRequest, LLMResponse
from agent_tools.executor import ToolExecutor
from agent_tools.registry import ToolRegistry
from orchestrator.nodes import (
    GuardRunner,
    has_pending_tool_calls,
    input_guard_node,
    llm_plan_node,
    output_guard_node,
    retrieve_node,
    should_continue_loop,
    tool_loop_node,
)
from orchestrator.state import OrchestratorState


def build_orchestrator_graph(
    *,
    pipeline: Any,
    llm: Any,
    tools: ToolRegistry,
    tool_executor: ToolExecutor,
    redactor: Any,
    ledger: Any | None,
    instrumentation: Any | None,
    system_prompt: str,
    model: str,
    max_steps: int = 10,
    retriever: Any | None = None,
    top_k: int = 3,
) -> Any:
    """装配编排图。

    所有外部依赖显式注入（与 ``build_container`` 同纪律）：单测注入
    FakeLLM / 桩 pipeline / 桩工具，生产由容器装配时注入真实实现。

    Args:
        retriever: B8 检索器。不传则 ``retrieve`` 节点为空操作 ——
            保持 B6/B7 的行为不变，也让不装检索组件的部署不受影响。
        top_k: 每次请求检索的文档数。**每个数字都有代价**：每篇文档都要跑
            RETRIEVAL 检测点，其中检索 judge 是一次 LLM 调用。
    """
    runner = GuardRunner(
        pipeline,
        redactor=redactor,
        ledger=ledger,
        instrumentation=instrumentation,
    )

    def _input_guard(state: OrchestratorState) -> OrchestratorState:
        return input_guard_node(state, runner)

    def _llm_call(request: LLMRequest) -> LLMResponse:
        """带 llm_call 埋点的调用闭包（token/成本口径，FT-11）。"""
        inst = instrumentation
        if inst is None:
            return llm.complete(request)
        with inst.llm_call(model=request.model) as recorder:
            completion = llm.complete(request)
            recorder.record(completion)
            return completion

    def _llm_plan(state: OrchestratorState) -> OrchestratorState:
        return llm_plan_node(
            state,
            llm_complete=_llm_call,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
        )

    def _tool_loop(state: OrchestratorState) -> OrchestratorState:
        return tool_loop_node(state, executor=tool_executor, runner=runner)

    def _retrieve(state: OrchestratorState) -> OrchestratorState:
        return retrieve_node(state, runner, retriever=retriever, top_k=top_k)

    def _output_guard(state: OrchestratorState) -> OrchestratorState:
        return output_guard_node(state, runner)

    builder = StateGraph(OrchestratorState)
    builder.add_node("input_guard", _input_guard)
    builder.add_node("retrieve", _retrieve)
    builder.add_node("llm_plan", _llm_plan)
    builder.add_node("tool_loop", _tool_loop)
    builder.add_node("output_guard", _output_guard)

    builder.add_edge(START, "input_guard")

    # input_guard：被拦 → 直接结束（response 已在节点内产出）
    builder.add_conditional_edges(
        "input_guard",
        lambda state: "end" if state.get("blocked") else "retrieve",
        {"end": END, "retrieve": "retrieve"},
    )

    # retrieve（B8）：检索内容被判出注入且来源不可信 → 直接结束
    builder.add_conditional_edges(
        "retrieve",
        lambda state: "end" if state.get("blocked") else "plan",
        {"end": END, "plan": "llm_plan"},
    )

    # llm_plan：有待执行工具 → 工具循环；否则收尾检测
    builder.add_conditional_edges(
        "llm_plan",
        lambda state: "tool_loop" if has_pending_tool_calls(state) else "finalize",
        {"tool_loop": "tool_loop", "finalize": "output_guard"},
    )

    # tool_loop：未达上限且有新工具 → 回 plan；否则收尾（max_steps 兜底）
    builder.add_conditional_edges(
        "tool_loop",
        lambda state: should_continue_loop(state, max_steps=max_steps),
        {"plan": "llm_plan", "finalize": "output_guard"},
    )

    builder.add_edge("output_guard", END)

    return builder.compile()


__all__ = ["build_orchestrator_graph"]
