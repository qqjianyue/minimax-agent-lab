"""工具参数校验（JSON Schema，C6）。

用 jsonschema 校验 OpenAI 格式的参数声明（``{"type": "object",
"properties": ..., "required": [...]}``）。引入 jsonschema 而不是手写
类型检查的理由：

- 工具的 ``parameters`` 声明是**数据**（ToolSpec 来自配置 / 工具作者），
  校验规则必须与声明同源 —— 手写 ``if isinstance(...)`` 会在声明与
  实现之间出现双份真相，迟早漂移；
- 枚举 / 嵌套对象 / 数组 / 最小长度等约束由 Schema 表达，天然可扩展。

校验失败抛 :class:`~agent_tools.errors.ToolArgumentError`，错误信息
聚合所有违例（一次调用给出全部问题，而不是挤牙膏式地逐个暴露）。
"""

from __future__ import annotations

from typing import Any

from agent_tools.errors import ToolArgumentError

#: 容忍的 $schema 方言。不声明时按 Draft 2020-12 处理（jsonschema 默认）。
_SUPPORTED_DIALECTS = {
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft-07/schema#",
}


def _validator_for(parameters: dict[str, Any]) -> Any:
    """按声明的 $schema 方言构造校验器；未声明用默认方言。"""
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - 惰性：主依赖但避免导入开销

    dialect = parameters.get("$schema")
    if dialect is not None and dialect not in _SUPPORTED_DIALECTS:
        raise ToolArgumentError(f"不支持的 JSON Schema 方言: {dialect!r}")
    return Draft202012Validator(parameters)


def validate_arguments(parameters: dict[str, Any], arguments: dict[str, Any]) -> None:
    """校验工具参数。

    Args:
        parameters: 工具的 JSON Schema（``ToolSpec.parameters``）。
        arguments: 实际参数。

    Raises:
        ToolArgumentError: 任一约束违例（错误信息聚合全部违例）。
    """
    validator = _validator_for(parameters)
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
    if not errors:
        return
    details = [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in errors
    ]
    raise ToolArgumentError("参数校验失败: " + "; ".join(details))


__all__ = ["validate_arguments"]
