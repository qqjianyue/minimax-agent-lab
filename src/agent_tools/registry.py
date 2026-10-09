"""工具注册表（C6）。

职责：注册 / 查找 / 向 LLM 暴露工具声明。**不含执行逻辑** —— 执行编排
（校验 / 权限 / 高危拦截）在 :mod:`agent_tools.executor`。

name 唯一性在注册时强制：重名工具会让模型请求产生歧义，这是配置错误，
应该 fail-fast 而不是让 executor 随机挑一个。
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_core.ports import ToolSpec
from agent_tools.spec import RegisteredTool


class ToolRegistry:
    """按 name 维护已注册工具。"""

    def __init__(self, tools: Sequence[RegisteredTool] | None = None) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        if tools:
            for tool in tools:
                self.register(tool)

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具名重复注册: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def specs(self) -> tuple[ToolSpec, ...]:
        """全部工具声明（给 LLM 的 ``tools`` 参数，按注册顺序）。"""
        return tuple(tool.spec for tool in self._tools.values())

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)


__all__ = ["ToolRegistry"]
