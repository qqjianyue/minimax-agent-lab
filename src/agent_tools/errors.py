"""agent_tools 领域的异常。

统一继承 :class:`~agent_core.errors.AgentCoreError`，调用方只捕获基类即可。
执行失败（handler 抛异常）是**业务性失败**，不继承 TransientError ——
重试一个注定再次失败的调用没有意义（与 errors.py 的约定一致）。
"""

from __future__ import annotations

from agent_core.errors import AgentCoreError


class ToolError(AgentCoreError):
    """工具层错误基类。"""


class ToolNotFoundError(ToolError):
    """请求的工具未注册。"""


class ToolArgumentError(ToolError):
    """参数未通过 JSON Schema 校验。"""


class ToolPermissionError(ToolError):
    """调用方 scope 不满足工具的权限边界。"""


class ToolExecutionError(ToolError):
    """handler 执行失败（业务性失败，不重试）。"""


__all__ = [
    "ToolArgumentError",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolPermissionError",
]
