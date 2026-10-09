"""工具包（C6）：工具注册表 / JSON Schema 校验 / 权限边界 / 高危人工确认。

批次范围（开发方案 §3.2）：在 :class:`~agent_core.ports.ToolSpec` 之上提供
**可执行工具层** —— 注册（registry）、参数校验（validation）、安全执行链
（executor，含高危拦截与权限边界）、银行示例工具（bank_tools）。

与 B6 orchestrator 的分工：本批次交付"工具层"，即给定工具名 + 参数，
安全地执行并返回结构化结果（``/tools/execute`` 端点暴露）；B6 负责"编排"
（模型生成 tool_call → 本层执行 → 回灌模型的多轮循环）。
"""

from agent_tools.bank_tools import build_bank_tools
from agent_tools.errors import (
    ToolArgumentError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionError,
)
from agent_tools.executor import ToolExecutor
from agent_tools.registry import ToolRegistry
from agent_tools.spec import RegisteredTool, ToolOutcome, ToolRisk
from agent_tools.validation import validate_arguments

__all__ = [
    "RegisteredTool",
    "ToolArgumentError",
    "ToolError",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolNotFoundError",
    "ToolOutcome",
    "ToolPermissionError",
    "ToolRegistry",
    "ToolRisk",
    "build_bank_tools",
    "validate_arguments",
]
