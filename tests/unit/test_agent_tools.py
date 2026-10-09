"""L0 单元测试 · C6 工具层（agent_tools）。

覆盖：注册表（查重 / 暴露 spec）、JSON Schema 校验（合法 / 非法 / 枚举 /
聚合错误）、执行安全链（未注册 / 参数非法 / 高危拦截且 handler 不被调 /
权限边界 / handler 失败）、银行示例工具的声明与风险元数据。
"""

from __future__ import annotations

import pytest

from agent_core.ports import ToolSpec
from agent_tools import (
    RegisteredTool,
    ToolExecutor,
    ToolRegistry,
    build_bank_tools,
    validate_arguments,
)
from agent_tools.errors import ToolArgumentError


def make_tool(
    name: str,
    *,
    schema: dict | None = None,
    dangerous: bool = False,
    scope: str | None = None,
    handler=None,
) -> RegisteredTool:
    from agent_tools.spec import ToolRisk

    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=name,
            parameters=schema
            or {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        handler=handler or (lambda args: f"ran:{name}:{args}"),
        risk=ToolRisk(dangerous=dangerous, required_scope=scope),
    )


# --- 注册表 ----------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry([make_tool("a"), make_tool("b")])
        assert "a" in registry
        assert registry.get("a") is not None
        assert registry.get("missing") is None
        assert registry.names == ["a", "b"]
        assert len(registry) == 2

    def test_duplicate_registration_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(make_tool("a"))
        with pytest.raises(ValueError, match="重复注册"):
            registry.register(make_tool("a"))

    def test_specs_expose_only_declarations(self) -> None:
        registry = ToolRegistry([make_tool("a")])
        specs = registry.specs()
        assert len(specs) == 1
        assert specs[0].name == "a"
        assert specs[0].parameters["type"] == "object"


# --- 参数校验 --------------------------------------------------------------


class TestValidation:
    def test_valid_arguments_pass(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        validate_arguments(schema, {"n": 3})  # 不抛即通过

    def test_missing_required_field_fails(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        with pytest.raises(ToolArgumentError, match="参数校验失败"):
            validate_arguments(schema, {})

    def test_wrong_type_fails(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        with pytest.raises(ToolArgumentError, match="参数校验失败"):
            validate_arguments(schema, {"n": "not-a-number"})

    def test_enum_constraint_enforced(self) -> None:
        schema = {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["a", "b"]}},
            "required": ["kind"],
        }
        validate_arguments(schema, {"kind": "a"})
        with pytest.raises(ToolArgumentError):
            validate_arguments(schema, {"kind": "c"})

    def test_aggregates_all_violations(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
            "required": ["a", "b"],
        }
        with pytest.raises(ToolArgumentError) as exc:
            validate_arguments(schema, {"a": "x"})  # a 类型错 + b 缺失
        message = str(exc.value)
        assert "参数校验失败" in message
        assert message.count(";") >= 1

    def test_unsupported_dialect_rejected(self) -> None:
        schema = {"$schema": "https://json-schema.org/draft/04/schema#", "type": "object"}
        with pytest.raises(ToolArgumentError, match="方言"):
            validate_arguments(schema, {})


# --- 执行安全链 ------------------------------------------------------------


class TestToolExecutor:
    def test_successful_execution(self) -> None:
        calls: list[tuple[str, dict]] = []
        tool = make_tool(
            "echo",
            handler=lambda args: (calls.append(("echo", args)) or f"ok:{args['value']}"),
        )
        executor = ToolExecutor(ToolRegistry([tool]))
        outcome = executor.execute("echo", {"value": "hi"})

        assert outcome.status == "ok"
        assert outcome.output == "ok:hi"
        assert calls == [("echo", {"value": "hi"})]

    def test_unknown_tool(self) -> None:
        executor = ToolExecutor(ToolRegistry())
        outcome = executor.execute("ghost", {})
        assert outcome.status == "not_found"
        assert outcome.output is None

    def test_invalid_arguments_not_executed(self) -> None:
        calls: list[str] = []
        tool = make_tool("strict", handler=lambda args: calls.append("ran"))
        executor = ToolExecutor(ToolRegistry([tool]))
        outcome = executor.execute("strict", {"value": 123})  # 类型错误

        assert outcome.status == "invalid_arguments"
        assert calls == []  # handler 未被调用

    def test_dangerous_tool_never_executes_handler(self) -> None:
        calls: list[str] = []
        tool = make_tool(
            "delete_all",
            dangerous=True,
            handler=lambda args: calls.append("ran"),
        )
        executor = ToolExecutor(ToolRegistry([tool]))
        outcome = executor.execute("delete_all", {"value": "x"})

        assert outcome.status == "requires_approval"
        assert outcome.error  # 有危险说明
        assert calls == []  # 永不执行

    def test_permission_boundary_enforced(self) -> None:
        tool = make_tool("balance", scope="account:read", handler=lambda args: "42")
        executor = ToolExecutor(ToolRegistry([tool]))

        denied = executor.execute("balance", {"value": "x"}, caller_scopes=("customer_service",))
        assert denied.status == "permission_denied"
        assert "account:read" in denied.error

        allowed = executor.execute("balance", {"value": "x"}, caller_scopes=("account:read",))
        assert allowed.status == "ok"

    def test_no_scope_means_unrestricted(self) -> None:
        tool = make_tool("public", handler=lambda args: "open")
        executor = ToolExecutor(ToolRegistry([tool]))
        assert executor.execute("public", {"value": "x"}).status == "ok"

    def test_handler_failure_reported_not_raised(self) -> None:
        def boom(_: dict) -> str:
            raise RuntimeError("backend down")

        executor = ToolExecutor(ToolRegistry([make_tool("flaky", handler=boom)]))
        outcome = executor.execute("flaky", {"value": "x"})
        assert outcome.status == "handler_error"
        assert "RuntimeError" in outcome.error


# --- 银行示例工具 -----------------------------------------------------------


class TestBankTools:
    def test_three_tools_declared(self) -> None:
        tools = build_bank_tools()
        names = [t.name for t in tools]
        assert names == ["get_product_rate", "get_account_balance", "delete_customer_records"]

    def test_dangerous_tool_marked(self) -> None:
        tools = {t.name: t for t in build_bank_tools()}
        assert tools["delete_customer_records"].risk.dangerous is True
        assert tools["delete_customer_records"].risk.danger_reason
        assert tools["get_product_rate"].risk.dangerous is False

    def test_balance_tool_has_scope_boundary(self) -> None:
        tools = {t.name: t for t in build_bank_tools()}
        assert tools["get_account_balance"].risk.required_scope == "account:read"

    def test_rate_tool_rejects_unknown_product(self) -> None:
        registry = ToolRegistry(build_bank_tools())
        executor = ToolExecutor(registry)
        outcome = executor.execute("get_product_rate", {"product_type": "crypto"})
        assert outcome.status == "invalid_arguments"

    def test_rate_tool_success(self) -> None:
        registry = ToolRegistry(build_bank_tools())
        outcome = ToolExecutor(registry).execute("get_product_rate", {"product_type": "deposit"})
        assert outcome.status == "ok"
        assert "1.85%" in outcome.output

    def test_delete_requires_approval_via_executor(self) -> None:
        registry = ToolRegistry(build_bank_tools())
        outcome = ToolExecutor(registry).execute("delete_customer_records", {"confirm": True})
        assert outcome.status == "requires_approval"
