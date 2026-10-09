"""工具执行编排（C6）。

执行前检查链（顺序即安全优先级，先到先拒）：

1. **查找** —— 工具未注册 → ``not_found``（模型幻觉工具名也要兜住）
2. **Schema 校验** —— 参数非法 → ``invalid_arguments``（非法参数不值得
   占用人工审批资源，直接拒）
3. **高危拦截** —— ``dangerous`` 工具 → ``requires_approval``，**永不
   调用 handler**（"删除客户记录"这类操作没有自动执行这个选项）
4. **权限边界** —— caller 不满足 ``required_scope`` → ``permission_denied``
5. **执行** —— handler 失败 → ``handler_error``（业务性失败，不重试）

executor **永不抛异常**：所有失败都进 :class:`ToolOutcome.status`。
端点 / 编排层按状态决定动作语义（requires_approval 与 TOOL 阶段 guard 的
判定一致后合并）。handler 的异常在步骤 5 被捕获并转 ``handler_error``，
调用方拿到的是结构化结果而不是 traceback。
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_tools.errors import ToolArgumentError
from agent_tools.registry import ToolRegistry
from agent_tools.spec import ToolOutcome
from agent_tools.validation import validate_arguments

#: 无权限边界工具的默认 scope 校验：None 表示不检查
_NO_SCOPE = None


class ToolExecutor:
    """把注册表 + 校验 + 风险策略串成一次安全执行。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def execute(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        caller_scopes: Sequence[str] = (),
    ) -> ToolOutcome:
        tool = self._registry.get(name)
        if tool is None:
            return ToolOutcome(status="not_found", tool=name)

        # 步骤 2：Schema 校验（见模块 docstring 的顺序理由）
        try:
            validate_arguments(tool.spec.parameters, dict(arguments))
        except ToolArgumentError as exc:
            return ToolOutcome(status="invalid_arguments", tool=name, error=str(exc))

        # 步骤 3：高危拦截 —— 危险操作没有自动执行这个选项
        if tool.risk.dangerous:
            return ToolOutcome(
                status="requires_approval",
                tool=name,
                error=tool.risk.danger_reason or "高危工具，需人工确认",
            )

        # 步骤 4：权限边界
        scope = tool.risk.required_scope
        if scope is not _NO_SCOPE and scope not in caller_scopes:
            return ToolOutcome(
                status="permission_denied",
                tool=name,
                error=f"缺少权限: 需要 scope {scope!r}",
            )

        # 步骤 5：执行（handler 已保证参数合法，内部失败转 handler_error）
        try:
            output = tool.handler(dict(arguments))
        except Exception as exc:  # noqa: BLE001 - handler 是外部代码，必须兜住
            return ToolOutcome(
                status="handler_error",
                tool=name,
                error=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(status="ok", tool=name, output=output)


__all__ = ["ToolExecutor"]
