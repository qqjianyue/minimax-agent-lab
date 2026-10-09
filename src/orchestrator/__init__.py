"""B6 C7 orchestrator：LangGraph 编排（多轮 + 工具循环）。

把 B3 的单轮 ``/chat`` 中间段（输入检测 → 生成 → 输出检测）替换为
LangGraph 状态图：

- ``input_guard`` → ``llm_plan``（带工具声明）→ ``tool_loop``（TOOL
  guard + executor + 结果回填）→ ``output_guard`` → 响应；
- ``max_steps`` 硬兜底（不无限烧模型额度）；
- HITL：requires_approval / 被拦工具 → 标记 ``interrupted``；
- ``retrieve`` 节点为 B8 RAG 占位，B6 不接检索路径。
"""

from __future__ import annotations

from orchestrator.graph import build_orchestrator_graph
from orchestrator.state import OrchestratorState, ToolCallInfo

__all__ = ["OrchestratorState", "ToolCallInfo", "build_orchestrator_graph"]
