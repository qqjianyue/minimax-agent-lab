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

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agent_core.errors import ConfigurationError, LLMError
from agent_core.ports import TokenUsage
from agent_service.container import AppContainer
from agent_service.models import (
    ChatRequest,
    ChatResponse,
    DecisionModel,
    GuardInspectRequest,
    GuardInspectResponse,
    HealthResponse,
    ToolCallResult,
    ToolExecuteRequest,
    ToolExecuteResponse,
    VersionModel,
)
from guard_contract.enums import GuardStage, PolicyAction
from policy_engine.decision import Decision

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
        try:
            yield
        finally:
            # 冲刷未导出的 span。放到 finally 是因为 uvicorn 收到 SIGTERM
            # 后正常退出也会走这里 —— 那正是最需要留住 trace 的时刻。
            container.shutdown()

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
        # 独立入口，自成一个请求，不与 /chat 共享 request_id。
        # 仍然开根 span：两个端点在 Phoenix 里应当长得一样，
        # 否则排查时得先想清楚"这条 trace 来自哪个接口"。
        request_id = f"req-{uuid4().hex[:12]}"
        with _trace_span(dep, request_id, **{"chat.has_tools": False}):
            decision, _ = _run_guarded(
                dep,
                span_name="guard_inspect",
                stage=payload.stage,
                text=payload.text,
                request_id=request_id,
                metadata={"endpoint": "/guard/inspect"},
            )
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

    @app.post("/tools/execute", response_model=ToolExecuteResponse)
    def tool_execute(
        payload: ToolExecuteRequest,
        dep: ContainerDep,
    ) -> ToolExecuteResponse:
        """工具执行（C6）：TOOL 阶段 guard + executor 安全链。

        数据流：

        1. **TOOL 阶段 guard**（环绕拦截）：检测文本 = 工具名 + 参数序列化，
           让 detector 看到与模型请求一致的调用意图 —— 危险工具调用
           （FT-07）与工具参数里的 PII 在到达 executor 之前被拦下；
        2. **executor 安全链**（:class:`~agent_tools.executor.ToolExecutor`）：
           Schema 校验 → 高危拦截（dangerous 永不自动执行）→ 权限边界
           （caller_scopes）→ handler。

        **guard 非放行即不执行**：B5 阶段 TOOL 阶段判 redact / rewrite 也
        直接不执行（结构化参数无法像文本一样"脱敏后继续"）；B6 编排层
        若需要"脱敏后重试"语义再细化。``caller_scopes`` 由服务端注入
        （B5 演示固定基础 scope），调用方不能自报权限 —— 见
        :class:`~agent_service.models.ToolExecuteRequest`。
        """
        # 独立入口，自成一个请求，不与 /chat 共享 request_id。
        request_id = f"req-{uuid4().hex[:12]}"
        with _trace_span(dep, request_id, **{"chat.has_tools": True}):
            guard_text = (
                f"{payload.tool}: {json.dumps(payload.arguments, ensure_ascii=False)}"
            )
            decision, _ = _run_guarded(
                dep,
                span_name="tool_guard",
                stage=GuardStage.TOOL,
                text=guard_text,
                request_id=request_id,
                metadata={"endpoint": "/tools/execute", "tool": payload.tool},
            )
            decision_model = DecisionModel.from_decision(decision)

            if decision.action is not PolicyAction.ALLOW:
                blocked = decision.action is PolicyAction.BLOCK
                return ToolExecuteResponse(
                    request_id=request_id,
                    decision=decision_model,
                    status="blocked" if blocked else "requires_approval",
                    requires_human=decision.requires_human,
                )

            outcome = dep.tool_executor.execute(
                payload.tool,
                payload.arguments,
                caller_scopes=("customer_service",),
            )
            return ToolExecuteResponse(
                request_id=request_id,
                decision=decision_model,
                status=outcome.status,
                output=outcome.output,
                requires_human=outcome.status == "requires_approval",
            )

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


def _audit(
    container: AppContainer,
    decision: Decision,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    """写一条审计记录。

    刻意**吞掉异常**：审计写不进去不能让业务请求失败。
    银行场景下账本不可写是需要告警的运维问题，但把它变成用户侧的 500
    只会让故障范围更大。写失败时记 warning，交由部署状态与监控去暴露。
    """
    if container.ledger is None:
        return
    try:
        container.ledger.record(decision, metadata=metadata)
    except Exception as exc:  # noqa: BLE001 - 见上方说明
        logger.warning("audit write failed: %s: %s", type(exc).__name__, exc)


# --- 埋点门面可空时的占位记录器 ---------------------------------------------
#
# `AppContainer.instrumentation` 允许为 None（容器被手工构造、尚未接遥测的
# 场景，例如部分单测）。**用占位对象而不是在业务代码里写 `if inst:` 分支**，
# 是为了让"有没有埋点"这件事在代码结构上不可见：一旦业务逻辑开始分叉，
# 两套路径迟早会走出不一样的语义，而这种差异只在关掉埋点时才暴露。


class _NullGuardRecorder:
    """遥测关闭时的检测点记录器。"""

    __slots__ = ()

    def record(self, decision: Decision, *, latency_ms: float) -> None:
        return None

    def __enter__(self) -> _NullGuardRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _NullLlmRecorder:
    """遥测关闭时的模型调用记录器。"""

    __slots__ = ()

    def record(self, completion: Any) -> None:
        return None

    def __enter__(self) -> _NullLlmRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@contextmanager
def _guard_span(
    container: AppContainer, name: str, stage: GuardStage
) -> Iterator[Any]:
    inst = container.instrumentation
    if inst is None:
        with _NullGuardRecorder() as recorder:
            yield recorder
        return
    with inst.guard(name, stage) as recorder:
        yield recorder


@contextmanager
def _llm_span(container: AppContainer, model: str) -> Iterator[Any]:
    inst = container.instrumentation
    if inst is None:
        with _NullLlmRecorder() as recorder:
            yield recorder
        return
    with inst.llm_call(model=model) as recorder:
        yield recorder


@contextmanager
def _trace_span(container: AppContainer, request_id: str, **attributes: Any) -> Iterator[None]:
    inst = container.instrumentation
    if inst is None:
        yield
        return
    with inst.trace(request_id, **attributes):
        yield


def _run_guarded(
    container: AppContainer,
    *,
    span_name: str,
    stage: GuardStage,
    text: str,
    request_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Decision, float]:
    """跑一个检测点，同时写出审计记录与 span。

    检测**发生在 span 内部**。早前的写法是先把 `pipeline.run` 跑完再补开
    一个 span 记录结果 —— 那样 span 里根本没有检测工作，只剩一个"报告结论"
    的壳子，时长恒等于零，出问题时 trace 上看不出是检测慢还是别处慢。

    Args:
        span_name: span 名。/chat 用 ``input_guard`` / ``output_guard``。
        request_id: 传给检测流水线。**同一次对话的多个检测点必须共用同一个**
            —— 否则账本里一次对话会散成几条互不相干的记录，出问题时
            无法把"这次为什么被拦"和"模型返回了什么"串起来。

    Returns:
        ``(决策, 检测耗时毫秒)``。
    """
    with _guard_span(container, span_name, stage) as recorder:
        started = time.perf_counter()
        decision = container.pipeline.run(text, stage=stage, request_id=request_id)
        latency_ms = (time.perf_counter() - started) * 1000.0
        recorder.record(decision, latency_ms=latency_ms)
    _audit(container, decision, metadata=metadata)
    return decision, latency_ms


def handle_chat(container: AppContainer, payload: ChatRequest) -> ChatResponse:
    """对话入口（B6 C7 编排版）。

    由 LangGraph 编排图驱动：输入检测 → 规划（带工具声明）→ 工具循环
    （TOOL guard + executor + 结果回填）→ 输出检测。检测点位置与最终
    动作合成语义与 B3 单轮版一致；工具审批/拦截标记 ``interrupted``。
    """
    # 整次对话共用一个 request_id：它是 trace 与审计账本的关联键，
    # 在最外层生成一次，下游检测点直接复用。
    request_id = f"req-{uuid4().hex[:12]}"

    with _trace_span(
        container, request_id, **{"chat.has_tools": True}
    ):
        result = container.orchestrator.invoke(
            {
                "request_id": request_id,
                "user_message": payload.message,
                "messages": [],
                "tools_called": [],
                "steps": 0,
                "pending_tool_calls": [],
                "interrupted": False,
                "blocked": False,
                "redacted": False,
            }
        )

    input_decision = result["input_decision"]
    output_decision = result.get("output_decision")
    tools_called = result.get("tools_called", [])
    interrupted = bool(result.get("interrupted", False))
    steps = result.get("steps", 0)
    total_usage = result.get("total_usage")

    return ChatResponse(
        request_id=input_decision.request_id,
        response=result.get("response", ""),
        action=(
            stricter(input_decision.action, output_decision.action)
            if output_decision is not None
            else input_decision.action
        ),
        input_guard=DecisionModel.from_decision(input_decision),
        output_guard=(
            DecisionModel.from_decision(output_decision) if output_decision is not None else None
        ),
        requires_human=(
            input_decision.requires_human
            or bool(output_decision is not None and output_decision.requires_human)
            or interrupted
        ),
        redacted=bool(result.get("redacted", False)),
        usage=_usage_dict(total_usage) if total_usage is not None else {},
        llm_called=steps > 0,
        tools_called=[
            ToolCallResult(name=tc["name"], status=tc["status"], output=tc["output"])
            for tc in tools_called
        ],
        steps=steps,
        interrupted=interrupted,
    )


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


__all__ = ["create_app", "handle_chat", "stricter"]
