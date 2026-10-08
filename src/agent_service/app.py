"""FastAPI 应用与路由。

## 单轮 /chat 的数据流

```
用户消息
   │
   ├─→ INPUT 检测（环绕拦截，同步）──→ Decision
   │        BLOCK / 需人工 → 直接返回，不调模型
   │        REDACT          → 先脱敏用户消息再送入模型
   ▼
   MiniMax 生成（外部资产，调用侧 llm_call 埋点）
   │
   ├─→ OUTPUT 检测（环绕拦截，必须发生在返回用户之前）──→ Decision
   │        BLOCK → 返回预设响应，绝不把原文交出去
   │        REDACT → 脱敏后返回
   ▼
   交给用户
```

B3 阶段是**单轮、无工具**的垂直切片；多轮记忆与工具调用由 B6 的编排层接管。
检测点的位置已经在位，B6 只是把中间那段换成 LangGraph 图。

## 为什么要有 /guard/inspect

L3 功能测试需要能**单独验证检测与策略**，不必经过模型。用它可以在
没有 API 额度的情况下回归 FT-02 ~ FT-07 的判定逻辑，也让排查
"为什么这次被拦"时有一个不消耗额度的入口。

## 错误处理纪律

上游错误一律转成不含内部细节的 502/503。凭据配置问题在日志里记录，
但**不记录 key 本身、配置文件路径或请求体**。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agent_core.errors import ConfigurationError, LLMError
from agent_core.ports import LLMMessage, LLMRequest, TokenUsage
from agent_service.container import AppContainer
from agent_service.models import (
    ChatRequest,
    ChatResponse,
    DecisionModel,
    GuardInspectRequest,
    GuardInspectResponse,
    HealthResponse,
    VersionModel,
)
from guard_contract.enums import GuardStage, PolicyAction
from policy_engine.actions import apply_action

logger = logging.getLogger("agent_service")

#: 各动作的严格程度。输入侧与输出侧取更严格的一侧作为整体结论 ——
#: 任何一侧要求阻断，整体就不能是放行。
_ACTION_SEVERITY = {
    PolicyAction.ALLOW: 0,
    PolicyAction.ESCALATE: 1,
    PolicyAction.REQUIRE_APPROVAL: 2,
    PolicyAction.REWRITE: 3,
    PolicyAction.REDACT: 4,
    PolicyAction.BLOCK: 5,
}


def stricter(left: PolicyAction, right: PolicyAction) -> PolicyAction:
    """返回两者中更严格的动作。"""
    return left if _ACTION_SEVERITY[left] >= _ACTION_SEVERITY[right] else right


def get_container(request: Request) -> AppContainer:
    """从 app.state 取容器。

    必须是**模块级**函数：``from __future__ import annotations`` 会把路由的
    注解变成字符串，FastAPI 再用 ``get_type_hints`` 还原 —— 它只查模块全局
    命名空间，定义在 ``create_app`` 内部的依赖别名会解析失败，于是 FastAPI
    把参数当成普通 query 参数，返回 422。
    """
    return request.app.state.container


#: Annotated 依赖注入。FastAPI 官方推荐写法：相比在参数默认值里写 Depends()，
#: 它不会让 B008 这类静态检查误报，也不必在每个路由上重复 Depends 调用。
ContainerDep = Annotated[AppContainer, Depends(get_container)]


def _lifespan(container: AppContainer) -> Callable[[FastAPI], AsyncIterator[None]]:
    """应用生命周期。

    启动时只记录"是否配置了 API Key"，**绝不记录 key 本身、配置文件路径
    或请求体** —— 启动日志常被收集到集中式日志系统，等于对外可见。
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service starting: version=%s policy=%s detectors=%s llm_configured=%s",
            container.version.display,
            container.policy.version,
            list(container.detectors),
            bool(container.settings.llm.api_key.get_secret_value()),
        )
        yield

    return lifespan


def create_app(container: AppContainer) -> FastAPI:
    """构造应用。所有依赖显式注入，不使用全局单例（便于测试整体替换）。"""

    app = FastAPI(
        title="minimax-agent",
        version=container.version.display,
        docs_url="/docs",
        lifespan=_lifespan(container),
    )
    app.state.container = container

    @app.get("/healthz", response_model=HealthResponse)
    def healthz(dep: ContainerDep) -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=VersionModel(**dep.version.to_dict()),
            policy={
                "version": dep.policy.version,
                "use_case": dep.policy.use_case,
                "tenant": dep.policy.tenant,
                "fail_mode": dep.policy.fail_mode.value,
                "default_action": dep.policy.default_action.value,
            },
            detectors=list(dep.detectors),
            llm_configured=bool(dep.settings.llm.api_key.get_secret_value()),
        )

    @app.post("/guard/inspect", response_model=GuardInspectResponse)
    def guard_inspect(
        payload: GuardInspectRequest,
        dep: ContainerDep,
    ) -> GuardInspectResponse:
        """只跑检测与策略，不调用模型。"""
        decision = dep.pipeline.run(payload.text, stage=payload.stage)
        return GuardInspectResponse(
            request_id=decision.request_id,
            decision=DecisionModel.from_decision(decision),
            # 审计记录里的 evidence 是命中的**原文片段**。这个响应会被
            # 调用方留存（它就是 C8 审计账本的数据源），所以必须脱敏 ——
            # 否则账本会变成一个更难清理的 PII 数据库。
            audit=decision.to_audit_record(redactor=dep.redactor),
        )

    @app.post("/chat", response_model=ChatResponse)
    def chat(
        payload: ChatRequest,
        dep: ContainerDep,
    ) -> ChatResponse:
        return handle_chat(dep, payload)

    @app.exception_handler(ConfigurationError)
    def _configuration_error(_: Request, exc: ConfigurationError) -> JSONResponse:
        # 配置问题不把内部细节带给调用方，但服务端留痕
        logger.error("configuration error: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "服务未正确配置"})

    @app.exception_handler(LLMError)
    def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
        logger.warning("llm error: %s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"detail": "上游模型服务暂时不可用"})

    return app


def handle_chat(container: AppContainer, payload: ChatRequest) -> ChatResponse:
    """单轮对话：输入检测 → 生成 → 输出检测。

    B6 之前不含工具调用与多轮记忆，但检测点位置与最终动作合成逻辑已就位，
    届时只需把中间段替换为 LangGraph 节点，判定语义不变。
    """
    # --- 1. 输入检测（环绕拦截）---
    input_decision = container.pipeline.run(payload.message, stage=GuardStage.INPUT)
    input_outcome = apply_action(
        input_decision.action, payload.message, redactor=container.redactor
    )

    # 被拦下或需人工时，不调用模型 —— 省额度，也避免危险内容进入模型上下文
    if input_decision.action is PolicyAction.BLOCK or input_decision.requires_human:
        return ChatResponse(
            request_id=input_decision.request_id,
            response=input_outcome.text,
            action=input_decision.action,
            input_guard=DecisionModel.from_decision(input_decision),
            output_guard=None,
            requires_human=input_decision.requires_human,
            redacted=input_outcome.changed,
            llm_called=False,
        )

    # --- 2. 生成（外部资产；遥测在调用侧闭环）---
    completion = container.llm.complete(
        LLMRequest(
            model=container.settings.llm.model,
            messages=(
                LLMMessage(role="system", content=container.system_prompt),
                LLMMessage(role="user", content=input_outcome.text),
            ),
            temperature=1.0,
        )
    )

    # --- 3. 输出检测（必须发生在返回用户之前，此位置不可后移）---
    output_decision = container.pipeline.run(completion.content, stage=GuardStage.OUTPUT)
    output_outcome = apply_action(
        output_decision.action, completion.content, redactor=container.redactor
    )

    return ChatResponse(
        request_id=input_decision.request_id,
        response=output_outcome.text,
        action=stricter(input_decision.action, output_decision.action),
        input_guard=DecisionModel.from_decision(input_decision),
        output_guard=DecisionModel.from_decision(output_decision),
        requires_human=input_decision.requires_human or output_decision.requires_human,
        redacted=input_outcome.changed or output_outcome.changed,
        usage=_usage_dict(completion.usage),
        llm_called=True,
    )


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


__all__ = ["create_app", "handle_chat", "stricter"]
