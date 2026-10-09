"""工具注册与风险元数据（C6）。

在 :class:`~agent_core.ports.ToolSpec`（OpenAI 兼容的工具声明）之上增加
**执行层元数据**：handler 与风险信息。ToolSpec 是可发布给 LLM 的契约
（tools 参数），而 :class:`RegisteredTool` 是服务端持有的完整定义 ——
两者分开，避免把 handler / 权限细节暴露给模型。

风险建模两个正交维度（对应开发方案 C6 的"权限边界 + 高危人工确认"）：

- ``dangerous``：**高危操作，永不自动执行**。命中即转人工确认，
  与 TOOL 阶段 guard 的 ``require_approval`` 语义一致（FT-07）。
- ``required_scope``：**权限边界**。调用方必须持有该 scope 才能执行，
  否则拒绝（FT-08 之外的安全工具的访问控制）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_core.ports import ToolSpec

#: 执行结果状态。端点层 / 编排层据此决定动作语义：
#: ok / requires_approval 之外的失败都不会触达 handler。
ToolOutcomeStatus = Literal[
    "ok",
    "not_found",
    "invalid_arguments",
    "permission_denied",
    "requires_approval",
    "handler_error",
]


@dataclass(frozen=True, slots=True)
class ToolRisk:
    """工具的权限与风险元数据。"""

    #: 高危操作标记：命中后 executor 永不调用 handler（转人工确认）
    dangerous: bool = False
    #: 权限边界：执行方必须持有该 scope（None = 无限制）
    required_scope: str | None = None
    #: 危险说明，写进审计与人工确认工单
    danger_reason: str = ""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """服务端持有的完整工具定义。"""

    spec: ToolSpec
    #: 实际执行函数：接收已校验的参数，返回给 LLM / 用户的文本结果。
    #: 参数在到达 handler 前已完成 Schema 校验（executor 的职责）。
    handler: Callable[[dict[str, Any]], str]
    risk: ToolRisk = field(default_factory=ToolRisk)

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """一次工具调用的结构化结果（executor 永不抛异常，失败都进状态）。"""

    status: ToolOutcomeStatus
    tool: str | None = None
    output: str | None = None
    error: str | None = None


__all__ = ["RegisteredTool", "ToolOutcome", "ToolOutcomeStatus", "ToolRisk"]
