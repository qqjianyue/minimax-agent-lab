"""llm-minimax：MiniMax 大模型后端的真实实现（C1 ``LLMPort`` 的一个实现）。

依赖方向：``llm_minimax`` → ``agent_core``。它不知道任何策略或检测器的存在。
"""

from __future__ import annotations

from llm_minimax.client import (
    CLIENT_NAME,
    MinimaxLLM,
    build_payload,
    map_http_error,
    parse_response,
)

__all__ = [
    "CLIENT_NAME",
    "MinimaxLLM",
    "build_payload",
    "map_http_error",
    "parse_response",
]
