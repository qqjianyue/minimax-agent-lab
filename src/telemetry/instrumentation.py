"""遥测实现。

三种实现，同一个 :class:`~agent_core.ports.TelemetryPort` 端口：

* :class:`NoOpTelemetry` —— 遥测关闭时使用，零开销
* :class:`OtelTelemetry` —— 真实 OpenTelemetry，OTLP 导出到 Phoenix
* 内存版在 ``tests/fakes.py``（测试用，不是运行期实现）

三者都靠 ``with`` 的嵌套表达父子关系，与架构方案 §4.5 的 span 树一致。
"""

from __future__ import annotations

from typing import Any

from agent_core.ports import SpanPort, TelemetryPort
from telemetry.redaction import sanitize_attributes

_OTEL_TRACE: Any = None


def _otel_trace_api() -> Any:
    """懒加载 ``opentelemetry.trace``。

    刻意**不在模块顶层 import**：:class:`NoOpTelemetry` 必须能在完全没有 OTel
    SDK 的环境里构造出来 —— 那正是 ``build_telemetry`` 缺依赖时的回退路径。
    顶层 import 会把"没装 OTel"变成 ImportError，连 NoOp 都拿不到，
    容错代码自己先把服务打挂了。
    """
    global _OTEL_TRACE
    if _OTEL_TRACE is None:
        from opentelemetry import trace as otel_trace

        _OTEL_TRACE = otel_trace
    return _OTEL_TRACE


class _NoOpSpan:
    """什么都不做的 span。关闭遥测时连对象都不该分配。"""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def end(self) -> None:
        return None

    def __enter__(self) -> SpanPort:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class NoOpTelemetry:
    """遥测关闭时的实现。

    刻意保留完整的 span 树结构（``with`` 照样嵌套、照样进 ``__exit__``），
    这样关掉遥测不会改变任何控制流 —— 埋点代码不需要写
    ``if telemetry_enabled:`` 这种分支。
    """

    __slots__ = ()

    def start_span(self, name: str, **attributes: Any) -> SpanPort:
        return _NoOpSpan()

    def shutdown(self) -> None:
        return None


class OtelSpan:
    """OTel span 的薄适配层。

    只做三件事：属性出口脱敏、把异常记录到 span 上、把 span 挂进 OTel 的
    contextvar 以维持父子关系。

    关于第三件：子 span 的 parent 是由 ``tracer.start_span()`` 读**当前 context**
    推出来的，而"当前 span"只有被 ``attach`` 上去才存在。直接 ``start_span``
    而不 attach，子 span 会静默变成 root span —— 展平成一片互不相干的点，
    没有任何报错。所以 attach/detach 必须成对做，且要在 span 结束前后对齐。
    """

    __slots__ = ("_span", "_capture_prompts", "_redactor", "_ended", "_token")

    def __init__(
        self,
        span: Any,
        *,
        capture_prompts: bool,
        redactor: Any | None,
    ) -> None:
        self._span = span
        self._capture_prompts = capture_prompts
        self._redactor = redactor
        self._ended = False
        self._token: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        # 逐个属性脱敏而不是整批：埋点代码可能在 span 中途 set，
        # 整批过滤只在 start 时做是不够的。
        safe = sanitize_attributes(
            {key: value},
            capture_prompts=self._capture_prompts,
            redactor=self._redactor,
        )
        for safe_key, safe_value in safe.items():
            self._span.set_attribute(safe_key, safe_value)

    def record_exception(self, exc: BaseException) -> None:
        self._span.record_exception(exc)

    def end(self) -> None:
        # 幂等：with 退出时调用一次，显式 end() 可能又调一次。
        # OTel 对重复 end 会告警，这里直接吞掉。
        if self._ended:
            return
        self._ended = True
        self._span.end()

    def __enter__(self) -> SpanPort:
        trace_api = _otel_trace_api()
        self._token = trace_api.context_api.attach(
            trace_api.set_span_in_context(self._span)
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # detach 必须放在 end() 之前，且和 attach 成对：中途抛异常时也要
        # 还原 context，否则后续所有 span 都会挂到这个已结束的 span 下。
        try:
            if exc is not None:
                self.record_exception(exc)  # type: ignore[arg-type]
        finally:
            try:
                if self._token is not None:
                    _otel_trace_api().context_api.detach(self._token)
                    self._token = None
            finally:
                self.end()


class OtelTelemetry:
    """真实 OTel 实现。

    Phoenix（基础设施，按 Q7 版本独立）通过 OTLP 接收。这里刻意**不因为
    Phoenix 不可达而影响主流程**：OTel 的 BatchSpanProcessor 在后台线程里
    自己重试，导出失败不会冒泡到业务代码。

    这条很重要 —— 观测系统挂了不能拖垮被观测的业务，那是基本的可用性边界。
    """

    def __init__(
        self,
        tracer: Any,
        *,
        capture_prompts: bool = False,
        redactor: Any | None = None,
        provider: Any | None = None,
    ) -> None:
        self._tracer = tracer
        self._capture_prompts = capture_prompts
        self._redactor = redactor
        self._provider = provider
        self._shutdown = False

    def start_span(self, name: str, **attributes: Any) -> SpanPort:
        safe = sanitize_attributes(
            attributes,
            capture_prompts=self._capture_prompts,
            redactor=self._redactor,
        )
        span = self._tracer.start_span(name, attributes=safe)
        return OtelSpan(
            span,
            capture_prompts=self._capture_prompts,
            redactor=self._redactor,
        )

    def shutdown(self) -> None:
        """关闭 provider，把队列里没发出去的 span 冲刷掉。

        没有这一步，进程退出时 BatchSpanProcessor 队列里的最后几条
        （往往正是故障现场那几条）会随进程一起消失。幂等。
        """
        if self._shutdown:
            return
        self._shutdown = True
        if self._provider is not None:
            self._provider.shutdown()


def build_telemetry(
    *,
    enabled: bool,
    service_name: str,
    endpoint: str,
    capture_prompts: bool,
    redactor: Any | None = None,
) -> TelemetryPort:
    """按配置构造遥测实现。

    OTel SDK / exporter 缺失时**退回 NoOp**而不是抛异常：可观测性是可选项，
    不该成为服务启动的硬依赖。
    """
    if not enabled:
        return NoOpTelemetry()

    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return NoOpTelemetry()

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    tracer = otel_trace.get_tracer(service_name, tracer_provider=provider)
    return OtelTelemetry(
        tracer,
        capture_prompts=capture_prompts,
        redactor=redactor,
        provider=provider,
    )


__all__ = ["NoOpTelemetry", "OtelSpan", "OtelTelemetry", "build_telemetry"]
