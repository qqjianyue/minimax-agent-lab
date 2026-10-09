"""银行示例工具（C6 的演示载体）。

三只工具覆盖 C6 的三个风险维度：

- :func:`get_product_rate` —— 安全只读，无权限边界（FT-08 走这条）
- :func:`get_account_balance` —— 只读但有**权限边界**（``account:read``）
- :func:`delete_customer_records` —— **高危**（FT-07 走这条）：executor
  永不调用其 handler，任何参数组合都先转人工确认

注意：handler 是纯函数示例（静态演示数据），真实部署时替换为对后端
系统的调用 —— 安全链（校验 → 高危 → 权限）不依赖 handler 的实现，
替换 handler 即可接入真实系统。
"""

from __future__ import annotations

from agent_core.ports import ToolSpec
from agent_tools.spec import RegisteredTool, ToolRisk

#: 演示利率表。真实数据应来自产品系统，此处仅为可执行示例。
_PRODUCT_RATES: dict[str, str] = {
    "deposit": "一年期定期存款年利率 1.85%",
    "loan": "个人消费贷款年利率 3.45%",
    "wealth": "稳健型理财产品近一年年化 2.90%",
}

#: 演示余额表。真实数据应来自核心系统，此处仅为可执行示例。
_ACCOUNT_BALANCES: dict[str, str] = {
    "10001": "人民币 45,230.00 元",
    "10002": "人民币 12,860.50 元",
}


def _get_product_rate(arguments: dict[str, object]) -> str:
    product_type = str(arguments["product_type"])
    return _PRODUCT_RATES[product_type]


def _get_account_balance(arguments: dict[str, object]) -> str:
    account_id = str(arguments["account_id"])
    balance = _ACCOUNT_BALANCES.get(account_id)
    if balance is None:
        return f"账户 {account_id} 不存在或无权查看"
    return f"账户 {account_id} 的余额为 {balance}"


def _delete_customer_records(arguments: dict[str, object]) -> str:  # pragma: no cover
    # executor 对 dangerous 工具永不调用 handler —— 该方法不会被真实执行，
    # 存在只是为了让工具定义完整（spec 可发布、审计可记录）。
    raise AssertionError("高危工具 handler 不应被执行")


def build_bank_tools() -> tuple[RegisteredTool, ...]:
    """构造银行演示工具集。"""
    return (
        RegisteredTool(
            spec=ToolSpec(
                name="get_product_rate",
                description=(
                    "查询银行产品当前利率。产品类型：deposit 存款 / "
                    "loan 贷款 / wealth 理财"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "product_type": {
                            "type": "string",
                            "enum": ["deposit", "loan", "wealth"],
                            "description": "产品类型",
                        }
                    },
                    "required": ["product_type"],
                },
            ),
            handler=_get_product_rate,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name="get_account_balance",
                description="查询指定账户的余额（只读）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "account_id": {
                            "type": "string",
                            "pattern": "^[0-9]{5}$",
                            "description": "5 位账户号",
                        }
                    },
                    "required": ["account_id"],
                },
            ),
            handler=_get_account_balance,
            risk=ToolRisk(
                required_scope="account:read",
                danger_reason="查询他人账户余额需要权限边界",
            ),
        ),
        RegisteredTool(
            spec=ToolSpec(
                name="delete_customer_records",
                description="删除客户记录。高危操作，需人工确认后执行。",
                parameters={
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": "是否确认执行删除",
                        }
                    },
                    "required": ["confirm"],
                },
            ),
            handler=_delete_customer_records,
            risk=ToolRisk(
                dangerous=True,
                danger_reason="批量删除客户记录，不可自动执行",
            ),
        ),
    )


__all__ = ["build_bank_tools"]
