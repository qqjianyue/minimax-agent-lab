"""llm-minimax 单元测试 —— 完全离线。

用 :class:`httpx.MockTransport` 喂假响应，因此**不需要 API key、不产生费用**
就能验证请求构造、响应解析、错误映射与重试退避。真正需要联网的只有
"端点可达"一件事，它被单独放在 target 用例里。
"""

from __future__ import annotations

import json

import httpx
import pytest

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
from agent_core.ports import LLMMessage, LLMRequest, TokenUsage, ToolCall, ToolSpec
from llm_minimax import MinimaxLLM, build_payload, map_http_error, parse_response

TEST_KEY = "sk-test-key-not-real-0000"


def settings(**overrides) -> LLMSettings:
    defaults = {"api_key": TEST_KEY, "model": "MiniMax-M3", "max_retries": 2}
    return LLMSettings(**{**defaults, **overrides})


def make_client(handler, *, sleep=None, **setting_overrides) -> MinimaxLLM:
    """构造一个走假传输层的客户端，并返回它。"""
    transport = httpx.MockTransport(handler)
    client = MinimaxLLM(
        settings(**setting_overrides),
        transport=transport,
        sleep=sleep or (lambda _s: None),
    )
    client.close = lambda: None  # type: ignore[method-assign]
    return client


def ok_response(content: str = "hello", **extra) -> dict:
    body = {
        "id": "resp-1",
        "model": "MiniMax-M3",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    body.update(extra)
    return body


def simple_request(**overrides) -> LLMRequest:
    defaults = {
        "model": "MiniMax-M3",
        "messages": (LLMMessage(role="user", content="hi"),),
    }
    return LLMRequest(**{**defaults, **overrides})


# --- 请求构造 ---------------------------------------------------------------
class TestBuildPayload:
    def test_basic_shape(self) -> None:
        payload = build_payload(simple_request())
        assert payload["model"] == "MiniMax-M3"
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert payload["temperature"] == 1.0
        assert payload["stream"] is False

    def test_n_is_always_one(self) -> None:
        """MiniMax 只支持 n=1，显式写出来比让服务端默认更清晰。"""
        assert build_payload(simple_request())["n"] == 1

    def test_ignored_params_never_sent(self) -> None:
        """这些参数 MiniMax 会静默忽略，发过去只会制造"生效了"的错觉。"""
        payload = build_payload(simple_request())
        for name in ("presence_penalty", "frequency_penalty", "logit_bias", "function_call"):
            assert name not in payload

    def test_max_tokens_omitted_when_unset(self) -> None:
        assert "max_tokens" not in build_payload(simple_request())
        assert build_payload(simple_request(max_tokens=256))["max_tokens"] == 256

    def test_tools_use_openai_compatible_shape(self) -> None:
        payload = build_payload(
            simple_request(
                tools=(
                    ToolSpec(
                        name="search", description="search web", parameters={"type": "object"}
                    ),
                )
            )
        )
        assert payload["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "search web",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def test_tools_omitted_when_empty(self) -> None:
        assert "tools" not in build_payload(simple_request())

    def test_optional_message_fields(self) -> None:
        payload = build_payload(
            simple_request(
                messages=(
                    LLMMessage(role="assistant", content="x", name="bot"),
                    LLMMessage(role="tool", content="y", tool_call_id="c1"),
                )
            )
        )
        assert payload["messages"][0]["name"] == "bot"
        assert payload["messages"][1]["tool_call_id"] == "c1"

    def test_assistant_tool_calls_serialized(self) -> None:
        """协议回归：assistant 必须携带 tool_calls 声明，否则工具循环被 API 400。"""
        payload = build_payload(
            simple_request(
                messages=(
                    LLMMessage(
                        role="assistant",
                        content="查一下",
                        tool_calls=(ToolCall(id="c1", name="search", arguments={"q": "abc"}),),
                    ),
                    LLMMessage(role="tool", content="结果", tool_call_id="c1"),
                )
            )
        )
        assert payload["messages"][0]["tool_calls"] == [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": "abc"}'},
            }
        ]
        assert payload["messages"][1]["tool_call_id"] == "c1"

    def test_assistant_without_tool_calls_no_key(self) -> None:
        payload = build_payload(
            simple_request(messages=(LLMMessage(role="assistant", content="x"),))
        )
        assert "tool_calls" not in payload["messages"][0]

    def test_temperature_passthrough(self) -> None:
        assert build_payload(simple_request(temperature=0.2))["temperature"] == 0.2


# --- 响应解析 ---------------------------------------------------------------
class TestParseResponse:
    def test_happy_path(self) -> None:
        response = parse_response(ok_response("你好"))
        assert response.content == "你好"
        assert response.model == "MiniMax-M3"
        assert response.usage.total_tokens == 18
        assert response.finish_reason == "stop"
        assert response.response_id == "resp-1"

    def test_tool_calls_parsed(self) -> None:
        body = ok_response(
            content="",
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "search", "arguments": '{"q": "abc"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        )
        response = parse_response(body)
        assert response.tool_calls[0].name == "search"
        assert response.tool_calls[0].arguments == {"q": "abc"}
        assert response.finish_reason == "tool_calls"

    def test_tool_arguments_as_dict_accepted(self) -> None:
        body = ok_response(
            choices=[
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "c", "function": {"name": "n", "arguments": {"a": 1}}}
                        ],
                    }
                }
            ]
        )
        assert parse_response(body).tool_calls[0].arguments == {"a": 1}

    @pytest.mark.parametrize(
        "body, message",
        [
            ({}, "choices"),
            ({"choices": []}, "choices"),
            (
                {"choices": [{"message": {"content": "a"}}, {"message": {"content": "b"}}]},
                "1 个 choice",
            ),
            ({"choices": [{"nope": 1}]}, "message"),
            ({"choices": [{"message": {"content": 123}}]}, "content"),
        ],
    )
    def test_malformed_responses_rejected(self, body: dict, message: str) -> None:
        with pytest.raises(LLMResponseError, match=message):
            parse_response(body)

    def test_broken_tool_arguments_rejected(self) -> None:
        body = ok_response(
            choices=[
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "c", "function": {"name": "n", "arguments": "{oops"}}
                        ],
                    }
                }
            ]
        )
        with pytest.raises(LLMResponseError, match="不是合法 JSON"):
            parse_response(body)

    def test_missing_usage_defaults_to_zero(self) -> None:
        body = ok_response()
        del body["usage"]
        assert parse_response(body).usage == TokenUsage()

    def test_garbage_usage_does_not_crash(self) -> None:
        body = ok_response(usage="not-a-dict")
        assert parse_response(body).usage.total_tokens == 0

    def test_negative_usage_clamped(self) -> None:
        assert parse_response(ok_response(usage={"prompt_tokens": -5})).usage.prompt_tokens == 0


# --- 错误映射 ---------------------------------------------------------------
class TestErrorMapping:
    @pytest.mark.parametrize(
        "status, expected",
        [
            (401, ConfigurationError),
            (403, ConfigurationError),
            (429, LLMRateLimitError),
            (500, LLMServerError),
            (502, LLMServerError),
            (503, LLMServerError),
            (400, LLMError),
            (404, LLMError),
        ],
    )
    def test_status_to_exception(self, status: int, expected: type) -> None:
        assert isinstance(map_http_error(status), expected)

    def test_retryability_matches_type(self) -> None:
        assert isinstance(map_http_error(429), TransientError)
        assert isinstance(map_http_error(503), TransientError)
        assert not isinstance(map_http_error(400), TransientError)
        assert not isinstance(map_http_error(401), TransientError)

    def test_server_error_is_also_an_llm_error(self) -> None:
        """回归：5xx 曾被映射成纯 TransientError，因不是 LLMError 子类而漏出
        `except LLMError`，导致"配了重试但 5xx 一次都不重"。"""
        assert isinstance(map_http_error(503), LLMError)

    def test_error_body_is_truncated(self) -> None:
        exc = map_http_error(500, "x" * 5000)
        assert len(str(exc)) < 500
        assert "truncated" in str(exc)


# --- 客户端行为（离线）------------------------------------------------------
class TestClientOffline:
    def test_sends_bearer_token(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_response("ok"))

        client = make_client(handler)
        response = client.complete(simple_request())
        assert response.content == "ok"
        assert seen["auth"] == f"Bearer {TEST_KEY}"
        # base_url 已含 /v1，httpx 会与相对路径拼接
        assert seen["path"] == "/v1/chat/completions"
        assert seen["body"]["model"] == "MiniMax-M3"

    def test_rate_limit_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=ok_response("third time lucky"))

        client = make_client(handler, max_retries=2)
        assert client.complete(simple_request()).content == "third time lucky"
        assert calls["n"] == 3

    def test_gives_up_after_max_retries(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, text="unavailable")

        client = make_client(handler, max_retries=1)
        with pytest.raises(LLMServerError):
            client.complete(simple_request())
        assert calls["n"] == 2  # 首次 + 1 次重试

    def test_no_retry_for_permanent_errors(self) -> None:
        """401 重试毫无意义，只是浪费额度。"""
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, text="bad key")

        client = make_client(handler, max_retries=3)
        with pytest.raises(ConfigurationError):
            client.complete(simple_request())
        assert calls["n"] == 1

    def test_backoff_is_injected_not_slept(self) -> None:
        """sleep 被注入，因此退避时长可断言、测试不真的等。"""
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if len(delays) < 2:
                return httpx.Response(429, text="slow down")
            return httpx.Response(200, json=ok_response())

        client = make_client(handler, sleep=delays.append, max_retries=3)
        client.complete(simple_request())
        assert len(delays) == 2
        assert all(d > 0 for d in delays)
        # 指数退避：第二次等待应长于第一次
        assert delays[1] > delays[0]

    def test_timeout_mapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        client = make_client(handler, max_retries=0)
        with pytest.raises(LLMTimeoutError):
            client.complete(simple_request())

    def test_non_json_response_rejected(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>oops</html>")

        client = make_client(handler, max_retries=0)
        with pytest.raises(LLMResponseError, match="不是合法 JSON"):
            client.complete(simple_request())

    def test_missing_key_raises_before_any_request(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=ok_response())

        client = MinimaxLLM(
            LLMSettings(api_key=""), transport=httpx.MockTransport(handler), sleep=lambda _s: None
        )
        client.close = lambda: None  # type: ignore[method-assign]
        with pytest.raises(ConfigurationError):
            client.complete(simple_request())
        assert calls["n"] == 0, "缺 key 时不应发出任何请求"

    def test_500_error_body_does_not_contain_key(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = make_client(handler, max_retries=0)
        with pytest.raises(LLMServerError) as exc:
            client.complete(simple_request())
        assert TEST_KEY not in str(exc.value)


# --- 凭据纪律 ---------------------------------------------------------------
class TestCredentialHygiene:
    def test_repr_does_not_leak_key(self) -> None:
        text = repr(MinimaxLLM(settings()))
        assert TEST_KEY not in text
        assert "sk-" not in text

    def test_repr_shows_only_masked_hint(self) -> None:
        assert "0000" in repr(MinimaxLLM(settings()))

    def test_transport_sees_key_but_nothing_else_does(self) -> None:
        """key 确实要发出去（否则调不通），但不该出现在任何其它地方。"""
        client = MinimaxLLM(settings())
        try:
            assert TEST_KEY not in repr(client.settings)
            assert TEST_KEY not in client.settings.model_dump_json()
            assert TEST_KEY not in json.dumps(client.settings.model_dump(mode="json"))
        finally:
            client.close()
