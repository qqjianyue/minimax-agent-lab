"""C1 agent-core · 版本与端口单元测试。

版本测试的意义在于回退机制：冒烟测试要能确认"目标机上跑的确实是我刚发布的
那个版本"，所以 display 串的拼装规则必须是确定且可断言的。
"""

from __future__ import annotations

import pytest

from agent_core.errors import VersionError
from agent_core.ports import (
    Clock,
    EmbedderPort,
    LLMMessage,
    LLMPort,
    LLMRequest,
    LLMResponse,
    TelemetryPort,
    TokenUsage,
    ToolCall,
)
from agent_core.version import UNKNOWN_SEMVER, VersionInfo, get_version_info, is_newer, parse_semver
from tests.fakes import (
    FakeEmbedder,
    FakeLLM,
    FrozenClock,
    InMemoryTelemetry,
    cosine,
    make_response,
)


class TestSemver:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("1.2.3", (1, 2, 3)),
            ("0.1.0", (0, 1, 0)),
            ("10.0.11", (10, 0, 11)),
            ("1.2.3-rc.1", (1, 2, 3)),
            ("1.2.3+build.5", (1, 2, 3)),
        ],
    )
    def test_parse_valid(self, value: str, expected: tuple[int, int, int]) -> None:
        assert parse_semver(value) == expected

    @pytest.mark.parametrize("value", ["1.2", "v1.2.3", "1.2.3.4", "", "abc", "01.2.3"])
    def test_parse_invalid_raises(self, value: str) -> None:
        with pytest.raises(VersionError):
            parse_semver(value)

    def test_is_newer(self) -> None:
        assert is_newer("0.2.0", "0.1.9") is True
        assert is_newer("1.0.0", "0.9.9") is True
        assert is_newer("0.1.0", "0.1.0") is False
        assert is_newer("0.1.0", "0.2.0") is False

    def test_ignores_build_metadata_when_comparing(self) -> None:
        assert is_newer("0.1.0+gabc", "0.1.0+gdef") is False


class TestVersionInfo:
    def test_display_appends_git_sha(self) -> None:
        info = VersionInfo(semver="0.1.0", git_sha="1a2b3c4")
        assert info.display == "0.1.0+g1a2b3c4"

    def test_display_marks_dirty_working_tree(self) -> None:
        info = VersionInfo(semver="0.1.0", git_sha="1a2b3c4", dirty=True)
        assert info.display == "0.1.0+g1a2b3c4.dirty"

    def test_display_without_sha_is_bare_semver(self) -> None:
        assert VersionInfo(semver="0.1.0").display == "0.1.0"

    def test_to_dict_shape_is_stable(self) -> None:
        """`/healthz` 的返回结构由这里定下，冒烟测试依赖它的字段名。"""
        payload = VersionInfo(semver="0.1.0", git_sha="abc", dirty=False, build_time="T").to_dict()
        assert payload == {
            "version": "0.1.0+gabc",
            "semver": "0.1.0",
            "git_sha": "abc",
            "dirty": False,
            "build_time": "T",
        }

    def test_runtime_version_is_valid_semver(self) -> None:
        info = get_version_info()
        assert info.semver != ""
        # 未安装时返回哨兵值，也必须是合法 semver —— 冒烟测试据此明确失败而非静默通过
        parse_semver(info.semver)
        assert info.semver != "" or info.semver == UNKNOWN_SEMVER

    def test_runtime_version_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_GIT_SHA", "deadbee")
        monkeypatch.setenv("MINIMAX_AGENT_GIT_DIRTY", "1")
        info = get_version_info()
        assert info.git_sha == "deadbee"
        assert info.dirty is True
        assert info.display.endswith("+gdeadbee.dirty")


class TestPortsSatisfiedByFakes:
    def test_llm_port(self) -> None:
        assert isinstance(FakeLLM(), LLMPort)

    def test_embedder_port(self) -> None:
        assert isinstance(FakeEmbedder(), EmbedderPort)

    def test_telemetry_port(self) -> None:
        assert isinstance(InMemoryTelemetry(), TelemetryPort)

    def test_clock_port(self) -> None:
        assert isinstance(FrozenClock(), Clock)


class TestPortDataStructures:
    def test_message_rejects_unknown_role(self) -> None:
        with pytest.raises(ValueError, match="未知 role"):
            LLMMessage(role="wizard", content="hi")

    @pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
    def test_message_accepts_known_roles(self, role: str) -> None:
        assert LLMMessage(role=role, content="hi").role == role

    def test_temperature_bounds_match_minimax_docs(self) -> None:
        # MiniMax 官方文档：temperature ∈ [0, 2]
        for value in (0.0, 1.0, 2.0):
            assert LLMRequest(model="m", messages=(), temperature=value).temperature == value
        for value in (-0.1, 2.1):
            with pytest.raises(ValueError, match=r"\[0, 2\]"):
                LLMRequest(model="m", messages=(), temperature=value)

    def test_token_usage_total(self) -> None:
        assert TokenUsage(prompt_tokens=30, completion_tokens=12).total_tokens == 42

    def test_token_usage_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="不能为负数"):
            TokenUsage(prompt_tokens=-1)

    def test_tool_call_arguments_default_not_shared(self) -> None:
        """frozen dataclass 里可变默认值必须用 default_factory，否则实例间会串数据。"""
        a = ToolCall(id="1", name="t")
        b = ToolCall(id="2", name="t")
        a.arguments["x"] = 1
        assert b.arguments == {}

    def test_response_defaults_are_usable(self) -> None:
        resp = LLMResponse(model="m", content="c")
        assert resp.tool_calls == ()
        assert resp.usage.total_tokens == 0
        assert resp.finish_reason == "stop"


class TestFakeBehaviour:
    def test_fake_llm_returns_queued_responses_in_order(self) -> None:
        llm = FakeLLM(responses=[make_response("first"), make_response("second")])
        req = LLMRequest(model="m", messages=(LLMMessage(role="user", content="hi"),))
        assert llm.complete(req).content == "first"
        assert llm.complete(req).content == "second"
        assert llm.call_count == 2

    def test_fake_llm_holds_last_response_for_multiturn(self) -> None:
        """多轮 ReAct 循环里最后一条响应会被重复读取，不该抛 IndexError。"""
        llm = FakeLLM(responses=[make_response("only")])
        req = LLMRequest(model="m", messages=())
        for _ in range(5):
            assert llm.complete(req).content == "only"

    def test_fake_llm_records_requests_for_assertions(self) -> None:
        llm = FakeLLM(
            responses=[make_response("x", tool_calls=(ToolCall("c1", "search", {"q": "x"}),))]
        )
        llm.complete(LLMRequest(model="m", messages=(LLMMessage(role="user", content="q"),)))
        assert llm.calls[0].messages[0].content == "q"

    def test_fake_embedder_is_deterministic(self) -> None:
        emb = FakeEmbedder()
        assert emb.embed(["a", "b"]) == emb.embed(["a", "b"])
        assert emb.embed(["a"]) != emb.embed(["b"])

    def test_fake_embedder_honours_explicit_vectors(self) -> None:
        emb = FakeEmbedder(vectors={"attack": [1.0, 0.0], "benign": [0.0, 1.0]})
        assert emb.embed(["attack"]) == [[1.0, 0.0]]
        assert cosine(emb.embed(["attack"])[0], emb.embed(["benign"])[0]) == pytest.approx(0.0)

    def test_cosine_of_identical_vectors_is_one(self) -> None:
        emb = FakeEmbedder(vectors={"x": [0.3, 0.4, 0.5]})
        assert cosine(emb.embed(["x"])[0], emb.embed(["x"])[0]) == pytest.approx(1.0)

    def test_frozen_clock_advances_deterministically(self) -> None:
        clock = FrozenClock()
        first = clock.monotonic()
        clock.advance(12.5)
        assert clock.monotonic() - first == pytest.approx(12.5)
        assert (clock.now() - FrozenClock().now()).total_seconds() == pytest.approx(12.5)


def test_in_memory_telemetry_records_spans() -> None:
    tel = InMemoryTelemetry()
    with tel.start_span("outer", stage="input"), tel.start_span("inner", stage="input"):
        pass
    assert tel.span_names() == ["outer", "inner"]
    assert tel.find("inner")[0].attributes["stage"] == "input"
    tel.assert_all_ended()
