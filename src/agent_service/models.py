"""HTTP 请求 / 响应契约。

这些模型是**对外契约**，有两个硬性要求：

1. **不含任何凭据**。``HealthResponse`` 暴露版本、策略、检测器清单，
   但绝不暴露 base_url、API Key 或配置文件路径 —— ``/healthz`` 通常
   是唯一一个允许免鉴权访问的端点，它泄露的东西等于公开的东西。
2. **响应里带决策依据**。``ChatResponse`` 携带输入/输出两侧的 guard 决策摘要，
   这样冒烟与功能测试可以断言"确实经过了检测"，而不只是看响应文本像不像。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from guard_contract.enums import GuardStage, PolicyAction
from policy_engine.decision import Decision

# --- 请求 -------------------------------------------------------------------


class GuardInspectRequest(BaseModel):
    """调试端点：只跑检测与策略，不调用模型。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20_000, description="待检测文本")
    stage: GuardStage = GuardStage.INPUT


class ChatRequest(BaseModel):
    """单轮对话。B6 引入工具与多轮记忆后再扩展。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    request_id: str | None = Field(default=None, max_length=128)


class ToolExecuteRequest(BaseModel):
    """工具执行（C6）。

    调用方**不能自报权限 scope** —— ``caller_scopes`` 由服务端注入
    （B5 演示为固定基础 scope；B6 由编排层按用户身份注入），否则"权限
    边界"就退化成"客户端自证无罪"。危险操作由 TOOL 阶段 guard 与
    executor 的高危标记双重复核。
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


# --- 响应 -------------------------------------------------------------------


class VersionModel(BaseModel):
    version: str
    semver: str
    git_sha: str
    dirty: bool
    build_time: str


class HealthResponse(BaseModel):
    """``/healthz`` 响应。

    冒烟测试（L2）会拿 ``version.version`` 与本次发布版本做精确比对 ——
    symlink 切换失败时如果这里仍然报旧版本，部署会被判为失败而不是误报成功。
    """

    status: str
    version: VersionModel
    policy: dict[str, Any]
    detectors: list[str]
    llm_configured: bool


class DecisionModel(BaseModel):
    """决策摘要。完整审计记录见 ``Decision.to_audit_record()``。"""

    action: PolicyAction
    reason: str
    rule_name: str | None
    matched: bool
    fail_mode: str | None
    detector_count: int
    failed_detectors: list[str]

    @classmethod
    def from_decision(cls, decision: Decision) -> DecisionModel:
        return cls(
            action=decision.action,
            reason=decision.reason,
            rule_name=decision.rule_name,
            matched=decision.matched,
            fail_mode=decision.fail_mode.value if decision.fail_mode else None,
            detector_count=len(decision.results),
            failed_detectors=[f.detector for f in decision.failed_detectors],
        )


class GuardInspectResponse(BaseModel):
    request_id: str
    decision: DecisionModel
    audit: dict[str, Any]


class ChatResponse(BaseModel):
    request_id: str
    #: 实际交给用户的文本（已按动作处理）
    response: str
    #: 整体动作，取输入与输出两侧中更严格的一个
    action: PolicyAction
    input_guard: DecisionModel
    output_guard: DecisionModel | None
    requires_human: bool
    #: 是否因脱敏改写了用户可见内容
    redacted: bool
    usage: dict[str, int] = Field(default_factory=dict)
    llm_called: bool


class ToolExecuteResponse(BaseModel):
    """``/tools/execute`` 响应：guard 决策 + 执行结果。"""

    request_id: str
    #: TOOL 阶段 guard 的决策（危险调用 / PII 等在到达 executor 前被拦）
    decision: DecisionModel
    #: executor 的执行结果状态（ok / not_found / invalid_arguments /
    #: permission_denied / requires_approval / handler_error）
    status: str
    #: 执行成功的输出（status=ok 时非空）
    output: str | None = None
    requires_human: bool


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "DecisionModel",
    "GuardInspectRequest",
    "GuardInspectResponse",
    "HealthResponse",
    "ToolExecuteRequest",
    "ToolExecuteResponse",
    "VersionModel",
]
