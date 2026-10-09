"""MiniMax 大模型后端客户端（OpenAI 兼容）。

## 为什么传输层可注入

``transport`` / ``sleep`` 都可注入，因此**请求构造、响应解析、错误映射、
重试退避全部可以在本地离线测试** —— 用 :class:`httpx.MockTransport` 喂假响应，
不需要 API key、不产生费用。真正需要联网的只有"端点是否可达"这一件事，
它被单独标记为 target 用例（B3 之后在目标机上跑）。

## 对齐 MiniMax 官方接口的几个要点

这些不是"兼容 OpenAI"就自然成立的差异，写错会直接 400：

* ``temperature`` 取值范围 ``[0, 2]``；
* ``n`` 仅支持 ``1``；
* ``presence_penalty`` / ``frequency_penalty`` / ``logit_bias`` **会被忽略**
  —— 本客户端因此根本不发这些字段，而不是发了再指望它生效；
* 已废弃的 ``function_call`` 不再使用，工具一律走 ``tools``。

## 凭据纪律

API Key 只在 :meth:`complete` 发请求的那一刻从
:class:`~agent_core.config.LLMSettings` 取出放进 header。
:class:`__repr__` 刻意不暴露 header，凭据也不会出现在任何日志里。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import TracebackType
from typing import Any

import httpx

from agent_core.config import LLMSettings
from agent_core.errors import (
    ConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    TransientError,
)
from agent_core.ports import (
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
)
from agent_core.retry import RetryPolicy

#: 客户端名，会出现在 llm_call span 上
CLIENT_NAME = "minimax"

#: 这些参数 MiniMax 会直接忽略，发过去只是浪费带宽并制造"生效了"的错觉
_IGNORED_PARAMS = ("presence_penalty", "frequency_penalty", "logit_bias")

#: 错误体最多带进异常消息的字符数。防止巨大的响应体灌满日志。
_MAX_ERROR_BODY_CHARS = 300


class MinimaxLLM:
    """MiniMax 的同步 LLM 客户端。"""

    name = CLIENT_NAME

    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Args:
        settings: LLM 配置（含 API Key）。**不要把 key 写进日志或响应体**。
        transport: httpx 传输层，测试时注入 ``httpx.MockTransport``。
        client: 已构造好的 httpx 客户端；与 transport 二选一。
        retry_policy: 重试策略，默认按 ``max_retries`` 构造。
        sleep: 休眠函数，测试时注入假实现以避免真实等待。
        """
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            transport=transport,
            timeout=settings.timeout_s,
        )
        self._retry = retry_policy or RetryPolicy(
            max_attempts=settings.max_retries + 1,
            retry_on=(TransientError,),
        )
        self._sleep = sleep
        self._model_used: str = ""

    # --- 上下文管理 ------------------------------------------------------
    def __enter__(self) -> MinimaxLLM:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __repr__(self) -> str:
        # 刻意不包含 base_url 之外的任何配置，更不包含 API Key
        return f"MinimaxLLM(model={self._settings.model!r}, key={self._settings.api_key_hint})"

    @property
    def settings(self) -> LLMSettings:
        return self._settings

    # --- 主入口 ----------------------------------------------------------
    def complete(self, request: LLMRequest) -> LLMResponse:
        """发起一次非流式补全，失败时抛 :class:`LLMError` 子类。"""
        api_key = self._settings.require_api_key()
        payload = build_payload(request)
        self._model_used = request.model

        last_error: LLMError | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                response = self._post(payload, api_key)
                return parse_response(response)
            except LLMError as exc:
                last_error = exc
                if not self._retry.should_retry(attempt, exc):
                    raise
                self._wait(self._retry.delay_for(attempt))
        # 循环必然以 return 或 raise 结束；这里只是让类型检查器满意
        raise last_error or LLMError("unreachable")

    def _post(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post("/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"MiniMax 请求超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"MiniMax 请求失败: {exc}") from exc

        if response.status_code >= 400:
            raise map_http_error(response.status_code, _safe_error_body(response))
        try:
            return response.json()
        except ValueError as exc:
            raise LLMResponseError(f"响应不是合法 JSON: {exc}") from exc

    def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
        else:  # pragma: no cover - 真实休眠只在生产路径发生
            import time

            time.sleep(seconds)


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------


def build_payload(request: LLMRequest) -> dict[str, Any]:
    """把 :class:`LLMRequest` 映射成 MiniMax 请求体。"""
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name:
            item["name"] = message.name
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            # assistant 的工具调用声明必须随消息下发，否则后续
            # role=tool 结果会因 tool_call_id 找不到匹配而被 API 拒绝
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        messages.append(item)

    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "temperature": request.temperature,
        "stream": request.stream,
        # MiniMax 明确 n 仅支持 1
        "n": 1,
    }
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    return payload


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def parse_response(data: dict[str, Any]) -> LLMResponse:
    """解析 MiniMax 响应。任何结构异常都归为 ``LLMResponseError``（不可重试）。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError(f"响应缺少 choices: {sorted(data)}")
    if len(choices) != 1:
        raise LLMResponseError(f"期望恰好 1 个 choice，实际 {len(choices)}")

    choice = choices[0]
    if not isinstance(choice, dict):
        raise LLMResponseError("choice 不是对象")

    message = choice.get("message")
    if not isinstance(message, dict):
        raise LLMResponseError("choice 缺少 message")

    content = message.get("content") or ""
    if not isinstance(content, str):
        raise LLMResponseError("message.content 不是字符串")

    tool_calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        if not isinstance(raw, dict):
            raise LLMResponseError("tool_call 不是对象")
        fn = raw.get("function")
        if not isinstance(fn, dict) or not fn.get("name"):
            raise LLMResponseError("tool_call 缺少 function.name")
        tool_calls.append(
            ToolCall(
                id=str(raw.get("id", "")),
                name=str(fn["name"]),
                arguments=_parse_arguments(fn.get("arguments")),
            )
        )

    return LLMResponse(
        model=str(data.get("model", "")),
        content=content,
        tool_calls=tuple(tool_calls),
        usage=_parse_usage(data.get("usage")),
        finish_reason=str(choice.get("finish_reason") or "stop"),
        response_id=str(data.get("id", "")),
    )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """工具参数是 JSON 字符串，偶有模型返回 dict。两种都要接住。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"工具参数不是合法 JSON: {raw!r}") from exc
        if not isinstance(parsed, dict):
            raise LLMResponseError(f"工具参数应为对象，实际 {type(parsed).__name__}")
        return parsed
    raise LLMResponseError(f"工具参数类型异常: {type(raw).__name__}")


def _parse_usage(raw: Any) -> TokenUsage:
    if not isinstance(raw, dict):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
    )


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------


def _safe_error_body(response: httpx.Response) -> str:
    """尽力读取错误体；读不到就返回标记，不让异常处理本身再抛一次。"""
    try:
        return response.text
    except Exception:  # pragma: no cover - 防御性
        return "<unreadable>"


def map_http_error(status_code: int, body: str = "") -> LLMError:
    """HTTP 状态码 → 异常类型。

    分类直接决定能否重试：

    * 401/403 —— 凭据问题，重试无意义；
    * 429、5xx —— 瞬时故障，可重试；
    * 其它 4xx —— 请求本身有问题，重试是浪费额度。

    错误体在**这里**截断而不是在调用点：截断属于"错误处理策略"的一部分，
    放在函数内才能保证无论谁调用都不会把巨大的响应体带进异常消息和日志。
    """
    message = f"MiniMax 返回 HTTP {status_code}"
    snippet = body[:_MAX_ERROR_BODY_CHARS]
    if snippet:
        message = f"{message}: {snippet}"
        if len(body) > _MAX_ERROR_BODY_CHARS:
            message = f"{message}…(truncated)"
    if status_code in (401, 403):
        return ConfigurationError(message)
    if status_code == 429:
        return LLMRateLimitError(message)
    if status_code >= 500:
        # 必须是 LLMError + TransientError 双重身份，否则不会进重试分支
        return LLMServerError(message)
    return LLMError(message)


__all__ = [
    "CLIENT_NAME",
    "MinimaxLLM",
    "build_payload",
    "map_http_error",
    "parse_response",
]
