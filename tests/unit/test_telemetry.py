"""C9 `telemetry` 单元测试。

覆盖三块：

* **属性脱敏** —— 这是本组件的安全边界，trace 会落盘
* **span 树** —— 架构方案 §4.5 要求的父子结构
* **成本口径** —— FT-11 要求 ``llm_call`` span 带 token_usage 与 cost
"""

from __future__ import annotations

import pytest

from detector_rules.redactor import RegexRedactor
from guard_contract.enums import GuardStage, PolicyAction
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision
from telemetry import (
    FORBIDDEN_ATTRIBUTES,
    PROMPT_ATTRIBUTES,
    REDACTED_PLACEHOLDER,
    Instrumentation,
    NoOpTelemetry,
    OtelTelemetry,
    build_telemetry,
    estimate_cost,
    sanitize_attributes,
)
from tests.fakes import InMemoryTelemetry

VALID_CN_ID = "11010519491231002X"


def make_decision(
    *,
    action: PolicyAction = PolicyAction.ALLOW,
    matched: bool = False,
    label: str = "safe",
    results: tuple[DetectorResult, ...] = (),
    failed: tuple = (),
    fail_mode=None,
) -> Decision:
    return Decision(
        action=action,
        reason="测试",
        rule_name=None if not matched else "some_rule",
        matched=matched,
        request_id="req-1",
        tenant="default",
        use_case="bank-assistant-demo",
        stage=GuardStage.INPUT,
        policy_version="1.0.0",
        results=results,
        attempted_detectors=["rules.l1"],
        failed_detectors=failed,
        fail_mode=fail_mode,
    )


def make_result(label: str = "pii_leak", score: float = 0.9) -> DetectorResult:
    return DetectorResult(
        detector="rules.l1",
        version="1.0.0",
        stage=GuardStage.INPUT,
        label=label,
        score=score,
        confidence=0.9,
        evidence=("evidence-text",),
    )


class TestAttributeSanitization:
    def test_credential_attributes_always_dropped(self) -> None:
        """凭据类属性无论 capture_prompts 如何都不该进 trace。"""
        attrs = dict.fromkeys(FORBIDDEN_ATTRIBUTES, "sk-should-never-appear")
        safe = sanitize_attributes(attrs, capture_prompts=True)

        assert safe == {}
        assert "sk-should-never-appear" not in str(safe)

    def test_prompt_dropped_when_capture_disabled(self) -> None:
        attrs = dict.fromkeys(PROMPT_ATTRIBUTES, "用户输入的原文")
        safe = sanitize_attributes(attrs, capture_prompts=False)

        assert set(safe) == PROMPT_ATTRIBUTES
        assert all(v == REDACTED_PLACEHOLDER for v in safe.values())

    def test_prompt_kept_when_capture_enabled(self) -> None:
        safe = sanitize_attributes(
            {"prompt": "用户输入的原文"}, capture_prompts=True
        )
        assert safe["prompt"] == "用户输入的原文"

    def test_placeholder_keeps_the_key(self) -> None:
        """保留键名、只丢内容 —— 排查要能看出"这里本来有段文本"。"""
        safe = sanitize_attributes({"prompt": "x"}, capture_prompts=False)
        assert "prompt" in safe
        assert "REDACTED" in safe["prompt"]

    def test_pii_in_attribute_value_is_redacted(self) -> None:
        safe = sanitize_attributes(
            {"custom_field": f"身份证 {VALID_CN_ID}"},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert VALID_CN_ID not in safe["custom_field"]

    def test_non_pii_string_untouched(self) -> None:
        """没有命中 PII 的普通文本要原样保留，否则 trace 可读性会被毁掉。"""
        safe = sanitize_attributes(
            {"note": "这是一段普通说明"}, capture_prompts=True, redactor=RegexRedactor()
        )
        assert safe["note"] == "这是一段普通说明"

    def test_non_string_values_pass_through(self) -> None:
        safe = sanitize_attributes(
            {"count": 3, "flag": True, "ratio": 0.5, "items": [1, 2]},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert safe == {"count": 3, "flag": True, "ratio": 0.5, "items": [1, 2]}

    def test_nested_dict_pii_is_redacted(self) -> None:
        """埋点代码可能把结构化数据写进一个属性，嵌套层里的 PII 不能漏。"""
        safe = sanitize_attributes(
            {"results": {"text": f"身份证 {VALID_CN_ID}"}},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert VALID_CN_ID not in str(safe)

    def test_nested_list_pii_is_redacted(self) -> None:
        safe = sanitize_attributes(
            {"items": [{"content": f"身份证 {VALID_CN_ID}"}, "普通文本"]},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert VALID_CN_ID not in str(safe)
        assert safe["items"][1] == "普通文本"

    def test_non_pii_nested_structure_untouched(self) -> None:
        safe = sanitize_attributes(
            {"results": {"ok": True, "note": "普通说明", "tags": ["a", "b"]}},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert safe == {"results": {"ok": True, "note": "普通说明", "tags": ["a", "b"]}}

    def test_input_not_mutated(self) -> None:
        original = {"prompt": "原文"}
        sanitize_attributes(original, capture_prompts=False)
        assert original == {"prompt": "原文"}

    # --- 嵌套层的**键名**过滤 ---
    #
    # 下面这组是真实漏过的口子：值脱敏做了递归，键名过滤却只做顶层，
    # 于是 set_attribute("llm.cfg", {"api_key": "sk-live-..."}) 会把凭据
    # 原样送进可被调阅的观测库 —— 而且 redactor 兜不住，它只认 PII 模式。

    def test_nested_credential_key_is_dropped(self) -> None:
        """嵌套 dict 里的凭据键必须和顶层一样被丢掉。"""
        safe = sanitize_attributes(
            {"llm.cfg": {"api_key": "sk-live-SUPERSECRET", "model": "MiniMax-M3"}},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert "SUPERSECRET" not in str(safe)
        assert safe["llm.cfg"] == {"model": "MiniMax-M3"}

    def test_deeply_nested_credential_key_is_dropped(self) -> None:
        safe = sanitize_attributes(
            {"a": {"b": {"c": {"password": "hunter2"}}}},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert "hunter2" not in str(safe)

    def test_credential_key_inside_list_item_is_dropped(self) -> None:
        safe = sanitize_attributes(
            {"calls": [{"token": "tok-secret", "id": "c1"}]},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert "tok-secret" not in str(safe)
        assert safe["calls"] == [{"id": "c1"}]

    def test_nested_prompt_key_is_replaced_when_capture_disabled(self) -> None:
        """嵌套层里叫 prompt 的键，capture 关闭时同样只能是占位符。

        键名才是判断依据，与它出现在第几层无关。
        """
        safe = sanitize_attributes(
            {"payload": {"prompt": "用户输入原文"}},
            capture_prompts=False,
            redactor=RegexRedactor(),
        )
        assert safe["payload"]["prompt"] == REDACTED_PLACEHOLDER
        assert "用户输入原文" not in str(safe)

    def test_nested_prompt_key_kept_when_capture_enabled(self) -> None:
        safe = sanitize_attributes(
            {"payload": {"prompt": "用户输入原文"}},
            capture_prompts=True,
            redactor=RegexRedactor(),
        )
        assert safe["payload"]["prompt"] == "用户输入原文"


class TestNoOpTelemetry:
    def test_start_span_returns_usable_context_manager(self) -> None:
        telemetry = NoOpTelemetry()
        with telemetry.start_span("anything", a=1) as span:
            span.set_attribute("b", 2)
            span.record_exception(ValueError("x"))
            span.end()

    def test_preserves_control_flow_shape(self) -> None:
        """关闭遥测不能改变控制流 —— 嵌套语义与真实实现一致。"""
        telemetry = NoOpTelemetry()
        with telemetry.start_span("parent"), telemetry.start_span("child"):
            pass

    def test_satisfies_the_port(self) -> None:
        from agent_core.ports import TelemetryPort

        assert isinstance(NoOpTelemetry(), TelemetryPort)


class TestGuardSpan:
    def test_records_action_and_stage(self) -> None:
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.guard("input_guard", GuardStage.INPUT) as g:
            g.record(make_decision(action=PolicyAction.BLOCK, matched=True), latency_ms=1.234)

        [span] = telemetry.find("input_guard")
        assert span.attributes["guard.action"] == "block"
        assert span.attributes["guard.stage"] == "input"
        assert span.attributes["guard.matched"] is True
        assert span.attributes["guard.latency_ms"] == 1.234

    def test_attempted_detectors_is_not_result_count(self) -> None:
        """"跑过的检测器"与"报出风险的检测器"必须分开记录。

        混淆这两者会让"所有检测器都挂了"被读成"什么都没跑" ——
        这正是 B2 修过的引擎 bug 的同类问题，不能在埋点层重犯。
        """
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.guard("input_guard", GuardStage.INPUT) as g:
            g.record(make_decision(results=()), latency_ms=0.1)

        [span] = telemetry.find("input_guard")
        assert span.attributes["guard.detectors"] == "rules.l1"
        assert span.attributes["guard.detector_result_count"] == 0
        assert span.attributes["guard.hit_labels"] == "<none>"

    def test_hit_labels_from_results(self) -> None:
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.guard("output_guard", GuardStage.OUTPUT) as g:
            g.record(make_decision(results=(make_result(),)), latency_ms=0.1)

        [span] = telemetry.find("output_guard")
        assert span.attributes["guard.hit_labels"] == "pii_leak"
        assert span.attributes["guard.detector_result_count"] == 1

    def test_fail_mode_is_recorded_when_present(self) -> None:
        from guard_contract.enums import FailMode

        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.guard("input_guard", GuardStage.INPUT) as g:
            g.record(make_decision(fail_mode=FailMode.CLOSED), latency_ms=0.1)

        [span] = telemetry.find("input_guard")
        assert span.attributes["guard.fail_mode"] == "closed"

    def test_fail_mode_absent_when_normal(self) -> None:
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.guard("input_guard", GuardStage.INPUT) as g:
            g.record(make_decision(), latency_ms=0.1)

        [span] = telemetry.find("input_guard")
        assert "guard.fail_mode" not in span.attributes


class TestLlmCallSpan:
    """FT-11：``llm_call`` span 必须含 token_usage 与 cost。"""

    def test_records_tokens_and_cost(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class Usage:
            prompt_tokens: int
            completion_tokens: int

        @dataclass
        class Completion:
            usage: Usage
            model: str = "MiniMax-M3"

        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry, price_input_per_million=1.0, price_output_per_million=2.0)

        with inst.llm_call(model="MiniMax-M3") as call:
            call.record(Completion(usage=Usage(prompt_tokens=1000, completion_tokens=500)))

        [span] = telemetry.find("llm_call")
        assert span.attributes["llm.prompt_tokens"] == 1000
        assert span.attributes["llm.completion_tokens"] == 500
        assert span.attributes["llm.token_usage_present"] is True
        # (1000*1 + 500*2) / 1_000_000 = 0.002
        assert span.attributes["llm.cost"] == pytest.approx(0.002)
        assert span.attributes["llm.model"] == "MiniMax-M3"

    def test_missing_usage_is_flagged_not_crashed(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class Completion:
            usage: None = None
            model: str = "MiniMax-M3"

        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.llm_call(model="MiniMax-M3") as call:
            call.record(Completion())

        [span] = telemetry.find("llm_call")
        assert span.attributes["llm.token_usage_present"] is False

    def test_cost_is_zero_when_price_unconfigured(self) -> None:
        """未配置单价 → cost 为 0。

        这是"没有价格信息"，不是"这次调用免费" —— 两者在排障时含义完全不同。
        """
        from dataclasses import dataclass

        @dataclass
        class Usage:
            prompt_tokens: int = 100
            completion_tokens: int = 100

        @dataclass
        class Completion:
            usage: Usage
            model: str = "m"

        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.llm_call(model="m") as call:
            call.record(Completion(usage=Usage()))

        [span] = telemetry.find("llm_call")
        assert span.attributes["llm.cost"] == 0.0


class TestCostEstimation:
    def test_mixed_pricing(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class Usage:
            prompt_tokens: int = 10_000
            completion_tokens: int = 20_000

        cost = estimate_cost(
            Usage(), price_input_per_million=2.0, price_output_per_million=3.0
        )
        assert cost == pytest.approx((10_000 * 2.0 + 20_000 * 3.0) / 1_000_000)

    def test_zero_tokens(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class Usage:
            prompt_tokens: int = 0
            completion_tokens: int = 0

        assert (
            estimate_cost(
                Usage(), price_input_per_million=5.0, price_output_per_million=5.0
            )
            == 0.0
        )


class TestSpanTree:
    """架构方案 §4.5 的父子结构。"""

    def test_nesting_produces_parent_child(self) -> None:
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with inst.guard("input_guard", GuardStage.INPUT), telemetry.start_span(
            "prompt_injection_check"
        ):
            pass

        assert telemetry.children_of("input_guard") == telemetry.find("prompt_injection_check")

    def test_tree_matches_the_design_shape(self) -> None:
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with telemetry.start_span("agent.request"):
            with inst.guard("input_guard", GuardStage.INPUT):
                pass
            with inst.llm_call(model="MiniMax-M3"):
                pass
            with inst.guard("output_guard", GuardStage.OUTPUT):
                pass

        assert telemetry.tree() == [
            ("agent.request", 0),
            ("input_guard", 1),
            ("llm_call", 1),
            ("output_guard", 1),
        ]

    def test_span_ends_even_when_body_raises(self) -> None:
        """异常路径下 span 也必须关闭。

        手工 start/end 时中间抛异常就会让 span 悬着不关 —— 这是
        SpanPort 被要求实现上下文管理器的根本原因。
        """
        telemetry = InMemoryTelemetry()
        inst = Instrumentation(telemetry)

        with pytest.raises(RuntimeError), inst.llm_call(model="m"):
            raise RuntimeError("boom")

        telemetry.assert_all_ended()
        [span] = telemetry.find("llm_call")
        assert any("RuntimeError" in e for e in span.exceptions)

    def test_stack_is_unwound_after_exit(self) -> None:
        """退出后当前 span 必须恢复，否则后续 span 会挂错父节点。"""
        telemetry = InMemoryTelemetry()

        with telemetry.start_span("parent"):
            pass
        with telemetry.start_span("sibling"):
            pass

        assert telemetry.roots() == telemetry.spans
        assert telemetry.children_of("parent") == []


# ---------------------------------------------------------------------------
# 真实 OTel 路径
# ---------------------------------------------------------------------------


def make_otel(capture_prompts: bool = False, redactor=None):
    """构造一个导出到内存的 OTel 实现。

    用 ``SimpleSpanProcessor`` 而不是 ``BatchSpanProcessor``：后者会起后台
    线程，测试退出后线程还在刷日志，而且 ``build_telemetry`` 不返回 provider，
    测试拿不到句柄去 shutdown。
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from telemetry import OtelTelemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    telemetry = OtelTelemetry(
        tracer, capture_prompts=capture_prompts, redactor=redactor
    )
    return telemetry, exporter


class TestOtelTelemetry:
    """真实 SDK 行为。NoOp 与内存 fake 都证明不了这里。"""

    def test_start_attributes_are_sanitized(self) -> None:
        telemetry, exporter = make_otel()

        with telemetry.start_span(
            "s", **{"prompt": "用户原文", "llm.api_key": "sk-secret", "ok": 1}
        ):
            pass

        [span] = exporter.get_finished_spans()
        attrs = dict(span.attributes or {})
        assert attrs["prompt"] == REDACTED_PLACEHOLDER
        assert "llm.api_key" not in attrs, "凭据类属性必须在出口被丢掉，不能只脱敏"
        assert attrs["ok"] == 1

    def test_mid_span_attribute_is_sanitized(self) -> None:
        """出口拦截必须覆盖 span 生命周期里**后写**的属性。

        start 时过滤一次是不够的：埋点代码常常先开 span、拿到判定结果后再
        写属性。只在 start 过滤等于给中途写入留了后门。
        """
        telemetry, exporter = make_otel()

        with telemetry.start_span("s") as span:
            span.set_attribute("output.text", "身份证 11010519491231002X")

        [finished] = exporter.get_finished_spans()
        assert dict(finished.attributes or {})["output.text"] == REDACTED_PLACEHOLDER

    def test_redactor_masks_pii_in_string_attributes(self) -> None:
        telemetry, exporter = make_otel(redactor=RegexRedactor())

        with telemetry.start_span("s", note=f"身份证 {VALID_CN_ID}"):
            pass

        [span] = exporter.get_finished_spans()
        assert VALID_CN_ID not in dict(span.attributes or {})["note"]

    def test_capture_prompts_allows_raw_text(self) -> None:
        telemetry, exporter = make_otel(capture_prompts=True)

        with telemetry.start_span("s", prompt="用户原文"):
            pass

        [span] = exporter.get_finished_spans()
        assert dict(span.attributes or {})["prompt"] == "用户原文"

    def test_end_is_idempotent(self) -> None:
        """``with`` 退出调一次 end、业务再显式调一次，不能导出两个 span。

        OTel 对重复 end 会打一条 warning，而重复导出在 Phoenix 里表现为
        同一条 trace 里凭空多出节点 —— 排查时非常费劲，所以宁可自己吞掉。
        """
        telemetry, exporter = make_otel()

        with telemetry.start_span("s") as span:
            span.end()

        assert len(exporter.get_finished_spans()) == 1

    def test_exception_is_recorded_and_span_ends(self) -> None:
        telemetry, exporter = make_otel()

        with pytest.raises(RuntimeError), telemetry.start_span("s"):
            raise RuntimeError("boom")

        [span] = exporter.get_finished_spans()
        events = [e for e in span.events if e.name == "exception"]
        assert events, "异常路径必须留下 exception 事件，否则 trace 上看不出哪条链路炸了"
        assert events[0].attributes["exception.type"] == "RuntimeError"

    def test_context_is_restored_after_exit(self) -> None:
        """退出后必须还原 context，否则后续 span 会挂到已结束的 span 下。"""
        from opentelemetry import trace as otel_trace

        telemetry, exporter = make_otel()
        before = otel_trace.get_current_span().get_span_context().span_id

        with telemetry.start_span("s"):
            pass
        with pytest.raises(RuntimeError), telemetry.start_span("boom"):
            raise RuntimeError("boom")

        after = otel_trace.get_current_span().get_span_context().span_id
        assert after == before, "context 没被还原"
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert spans["boom"].parent is None, "异常退出的 span 不能成为下一个 span 的 parent"

    def test_nesting_builds_real_parent_child(self) -> None:
        telemetry, exporter = make_otel()

        with telemetry.start_span("agent.request"), telemetry.start_span("input_guard"):
            pass

        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert spans["input_guard"].parent is not None
        assert spans["input_guard"].parent.span_id == spans["agent.request"].context.span_id

    def test_noop_control_flow_is_preserved(self) -> None:
        """NoOp 也必须支持 ``with ... as span`` 并接受异常退出。"""
        telemetry = NoOpTelemetry()

        with pytest.raises(RuntimeError), telemetry.start_span("s") as span:
            span.set_attribute("prompt", "x")
            span.record_exception(RuntimeError("boom"))
            raise RuntimeError("boom")


class TestBuildTelemetry:
    @pytest.fixture(autouse=True)
    def _no_real_exporter(self, monkeypatch):
        """把真 exporter 与后台线程替换掉。

        真跑 ``BatchSpanProcessor`` 会起一个后台线程去连 OTLP 端点，而
        ``build_telemetry`` 又不返回 provider，测试没法 shutdown —— 线程会
        一直挂在解释器退出时刷失败日志，还会去碰 socket（本仓库禁网）。
        """
        import opentelemetry.exporter.otlp.proto.http.trace_exporter as http_mod
        import opentelemetry.sdk.trace.export as export_mod

        created: dict[str, object] = {}

        class FakeExporter:
            def __init__(self, *, endpoint: str, **_: object) -> None:
                created["endpoint"] = endpoint

        class FakeProcessor:
            def __init__(self, exporter: object, *_: object, **__: object) -> None:
                created["processor_arg"] = exporter

            # TracerProvider 在解释器退出时会对每个 processor 调 shutdown，
            # 缺这个方法会在 atexit 里抛 AttributeError（不影响断言，但会
            # 刷一屏 traceback，看起来像测试挂了）。
            def shutdown(self) -> None:
                return None

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return True

        monkeypatch.setattr(http_mod, "OTLPSpanExporter", FakeExporter)
        monkeypatch.setattr(export_mod, "BatchSpanProcessor", FakeProcessor)
        return created

    def test_disabled_returns_noop(self) -> None:
        telemetry = build_telemetry(
            enabled=False,
            service_name="s",
            endpoint="http://localhost:6006",
            capture_prompts=False,
        )
        assert isinstance(telemetry, NoOpTelemetry)

    def test_enabled_returns_otel(self) -> None:
        telemetry = build_telemetry(
            enabled=True,
            service_name="s",
            endpoint="http://localhost:6006",
            capture_prompts=False,
        )
        assert isinstance(telemetry, OtelTelemetry)

    @pytest.mark.parametrize(
        "endpoint",
        ["http://phoenix:6006", "http://phoenix:6006/"],
        ids=["no_trailing_slash", "trailing_slash"],
    )
    def test_endpoint_is_normalized(self, _no_real_exporter, endpoint: str) -> None:
        """带不带尾斜杠都得拼出同一个 OTLP 路径。

        配置里的地址是手写的，带 ``/`` 很常见；直接 f-string 拼接会产出
        末尾多一个斜杠的路径。
        """
        build_telemetry(
            enabled=True,
            service_name="s",
            endpoint=endpoint,
            capture_prompts=False,
        )
        assert _no_real_exporter["endpoint"] == "http://phoenix:6006/v1/traces"

    def test_missing_otel_sdk_falls_back_to_noop(self, monkeypatch) -> None:
        """OTel 装不上时退回 NoOp，而不是让服务起不来。

        可观测性是可选的。观测缺依赖却让业务进程崩掉，等于把可选能力
        写成了硬依赖。
        """
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("opentelemetry"):
                raise ImportError("simulated missing otel")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        telemetry = build_telemetry(
            enabled=True,
            service_name="s",
            endpoint="http://localhost:6006",
            capture_prompts=False,
        )
        assert isinstance(telemetry, NoOpTelemetry)
