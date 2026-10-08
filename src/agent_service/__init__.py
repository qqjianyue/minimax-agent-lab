"""agent-service（C10）：进程入口与对外 HTTP 契约。

本包是**唯一知道所有组件存在的地方**：它加载配置、装好策略与检测器、
构造 LLM 客户端、把它们接成可用的服务。各组件之间只依赖契约，
因此任何一项都能在测试里被 Fake 单独替换。
"""

from __future__ import annotations

from agent_service.app import create_app, handle_chat, stricter
from agent_service.container import SYSTEM_PROMPT, AppContainer, build_container
from agent_service.models import (
    ChatRequest,
    ChatResponse,
    DecisionModel,
    GuardInspectRequest,
    GuardInspectResponse,
    HealthResponse,
    VersionModel,
)

__all__ = [
    "SYSTEM_PROMPT",
    "AppContainer",
    "ChatRequest",
    "ChatResponse",
    "DecisionModel",
    "GuardInspectRequest",
    "GuardInspectResponse",
    "HealthResponse",
    "VersionModel",
    "build_container",
    "create_app",
    "handle_chat",
    "stricter",
]
