"""业务埋点：把 §4.5 的 span 树落到真实控制流上。

## 为什么单独一层

``TelemetryPort`` 只知道"开个 span"，不知道"这是个检测点还是个模型调用"。
把 span 名字、属性名、成本口径写死在各处埋点里，会出现三个后果：
同一个 span 在不同调用点名字不一致、成本算法散落多处、
某个新调用点忘了加 ``llm_call`` 就丢掉了成本口径。

所以这里收一层：业务代码只调 :meth:`Instrumentation.guard` /
:meth:`Instrumentation.llm_call`，**span 名字与属性口径由本模块统一维护**。

## 与 §4.5 的对应关系

::

    trace
    ├── input_guard      ← guard("input_guard", INPUT)
    ├── generation_step_1 ← llm_call()
    └── output_guard     ← guard("output_guard", OUTPUT)

叶子粒度目前是"一个检测点 span + detector 汇总属性"，而不是 §4.5 里
``prompt_injection_check`` / ``pii_check`` 那种按语义拆分。原因是当前只有
一个检测器 ``rules.l1``，按语义拆出来的叶子必然是空的。B5 接入 Presidio
与嵌入检测后，自然会变成真正的多叶子 —— 树形结构本身已经就位。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from agent_core.ports import SpanPort, TelemetryPort
from guard_contract.enums import GuardStage
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision


def estimate_cost(
    usage: Any,
    *,
    price_input_per_million: float,
    price_output_per_million: float,
    price_cached_input_per_million: float | None = None,
) -> float:
    """按每百万 token 单价估算一次调用的成本（含缓存差异化计费）。

    MiniMax prompt 缓存是差异化计费：命中缓存的输入 token 按优惠的
    cache_read 单价计，未命中的新增输入按标准输入单价计。公式：

        cost = (prompt − cached) × 输入单价
             + cached × 缓存命中单价
             + completion × 输出单价

    没有缓存用量（或未配置缓存单价）时退化为原来的两档公式，
    其中缓存单价缺省按**输入单价**处理 —— 不低估成本（保守口径）。

    单价为 0 时返回 0.0，表示"未配置单价"而不是"这次调用免费"——
    两者在排障时含义完全不同，所以宁可返回 0 也不要编一个看起来合理的数字。
    """
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    cached = getattr(usage, "cached_prompt_tokens", 0) or 0
    cached = min(cached, prompt)  # 防御：缓存命中数不可能超过输入总量
    input_price = price_input_per_million
    cache_price = (
        price_cached_input_per_million
        if price_cached_input_per_million is not None
        else input_price
    )
    return (
        (prompt - cached) * input_price
        + cached * cache_price
        + completion * price_output_per_million
    ) / 1_000_000


class _GuardRecorder:
    """检测点 span 的出口记录器。"""

    __slots__ = ("_span",)

    def __init__(self, span: SpanPort) -> None:
        self._span = span

    def record(self, decision: Decision, *, latency_ms: float) -> None:
        """把判定结果写进 span。

        在**判定完成之后**调用，因此 span 的总时长由 ``with`` 保证，
        而这里的 ``latency_ms`` 是判定本身的耗时（不含 span 建立开销）。
        """
        self._span.set_attribute("guard.action", decision.action.value)
        self._span.set_attribute("guard.stage", decision.stage.value)
        self._span.set_attribute("guard.rule", decision.rule_name or "default_action")
        self._span.set_attribute("guard.matched", decision.matched)
        self._span.set_attribute("guard.policy_version", decision.policy_version)
        self._span.set_attribute("guard.latency_ms", round(latency_ms, 3))

        # detector 汇总。注意 ``results`` 只含**报出风险**的结果，
        # 而 ``attempted_detectors`` 才是真正跑过的检测器 —— 两者含义不同，
        # 混用会让"检测器全挂了"被误读成"什么都没跑"。
        self._span.set_attribute(
            "guard.detectors",
            ",".join(decision.attempted_detectors) or "<none>",
        )
        hit_labels = sorted({r.label for r in decision.results})
        self._span.set_attribute("guard.hit_labels", ",".join(hit_labels) or "<none>")
        self._span.set_attribute("guard.detector_result_count", len(decision.results))

        if decision.fail_mode is not None:
            # 降级路径要能一眼看出来：这是"检测器超时后 fail-closed"，
            # 还是"正常判定 block"。这两个问题的排查方向完全不同。
            self._span.set_attribute("guard.fail_mode", decision.fail_mode.value)

        if decision.failed_detectors:
            self._span.set_attribute(
                "guard.failed_detectors",
                ",".join(d.detector for d in decision.failed_detectors),
            )


class _LlmCallRecorder:
    """``llm_call`` span 的出口记录器。FT-11 的落点。"""

    __slots__ = ("_span", "_price_in", "_price_out", "_price_cached")

    def __init__(
        self,
        span: SpanPort,
        *,
        price_in: float,
        price_out: float,
        price_cached: float | None = None,
    ) -> None:
        self._span = span
        self._price_in = price_in
        self._price_out = price_out
        self._price_cached = price_cached

    def record(self, completion: Any) -> None:
        usage = getattr(completion, "usage", None)
        if usage is None:
            self._span.set_attribute("llm.token_usage_present", False)
            return

        self._span.set_attribute("llm.token_usage_present", True)
        # 属性名对齐 OpenInference 语义约定（Phoenix 据此识别 token 用量、
        # 计算 span_costs）：llm.token_count.prompt / completion /
        # prompt_details.cache_read / total。旧名 llm.prompt_tokens 不识别，
        # 导致 trace 落库但成本关联为 0（FT-11 实测）。
        cached = getattr(usage, "cached_prompt_tokens", 0) or 0
        self._span.set_attribute("llm.token_count.prompt", usage.prompt_tokens)
        self._span.set_attribute("llm.token_count.completion", usage.completion_tokens)
        self._span.set_attribute("llm.token_count.prompt_details.cache_read", cached)
        total = getattr(usage, "total_tokens", None)
        if total is None:
            total = int(usage.prompt_tokens) + int(usage.completion_tokens)
        self._span.set_attribute("llm.token_count.total", total)

        cost_total = round(
            estimate_cost(
                usage,
                price_input_per_million=self._price_in,
                price_output_per_million=self._price_out,
                price_cached_input_per_million=self._price_cached,
            ),
            8,
        )
        # llm.cost.* 显式上报业务侧自算的成本（人民币口径，含缓存优惠价）。
        # 注意：Phoenix v20 的 span_costs 表由 daemon 用**内置模型定价目录**
        # （美元价）从 token_count 另算，不读这里的值；llm.cost.total 用于
        # UI 展示与自定义成本公式（配自定义单价属性）。
        self._span.set_attribute("llm.cost.total", cost_total)
        if cost_total > 0:
            prompt_cost = round(
                (usage.prompt_tokens - cached) * self._price_in / 1_000_000, 8
            )
            cache_cost = round(cached * (self._price_cached or self._price_in) / 1_000_000, 8)
            completion_cost = round(
                usage.completion_tokens * self._price_out / 1_000_000, 8
            )
            self._span.set_attribute("llm.cost.prompt", prompt_cost)
            self._span.set_attribute(
                "llm.cost.prompt_details.cache_read", cache_cost
            )
            self._span.set_attribute("llm.cost.completion", completion_cost)
            # 自定义单价属性：供 Phoenix UI 做人民币口径成本公式/看板
            self._span.set_attribute("llm.token_price.input", self._price_in)
            self._span.set_attribute(
                "llm.token_price.cache_read", self._price_cached or self._price_in
            )
            self._span.set_attribute("llm.token_price.output", self._price_out)
        model = getattr(completion, "model", "") or ""
        if model:
            self._span.set_attribute("llm.model", model)
            # OpenInference 标准键。Phoenix 的 should_calculate_span_cost 用
            # llm.model_name 匹配内置模型定价目录（MiniMax-M3 在清单里，
            # 命中后才从 token_count 计算 span_costs）。
            self._span.set_attribute("llm.model_name", model)


class _ToolCallRecorder:
    """``tool_call`` span 的出口记录器。

    入参在 span **开始时**就写（调用前已知），出参与状态在**结束后**写
    ——所以 record 分两次调用，这是工具调用与模型调用最不一样的地方。
    """

    __slots__ = ("_span",)

    def __init__(self, span: SpanPort) -> None:
        self._span = span

    def record(self, *, status: str, latency_ms: float) -> None:
        """写入执行结果。

        Args:
            status: executor 的结构化状态（ok / not_found / invalid_arguments /
                permission_denied / requires_approval / handler_error）。
                **工具失败率就是按这个字段算的**，所以哪怕执行失败也必须写 ——
                没有 status 的 span 会让失败率被系统性低估。
        """
        self._span.set_attribute("tool.status", status)
        self._span.set_attribute("tool.duration_ms", round(latency_ms, 3))


class Instrumentation:
    """业务级埋点门面。

    持有一个 :class:`TelemetryPort`，把 §4.5 的 span 名字与属性口径固化下来。
    """

    def __init__(
        self,
        telemetry: TelemetryPort,
        *,
        price_input_per_million: float = 0.0,
        price_output_per_million: float = 0.0,
        price_cached_input_per_million: float | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._price_in = price_input_per_million
        self._price_out = price_output_per_million
        self._price_cached = price_cached_input_per_million
        self._shutdown = False

    @contextmanager
    def guard(
        self,
        name: str,
        stage: GuardStage,
        **attributes: Any,
    ) -> Iterator[_GuardRecorder]:
        """开启一个检测点 span。

        Args:
            name: span 名，如 ``"input_guard"`` / ``"output_guard"``
            stage: 检测阶段，作为属性记录
        """
        with self._telemetry.start_span(
            name, **{"guard.stage": stage.value, **attributes}
        ) as span:
            yield _GuardRecorder(span)

    @contextmanager
    def llm_call(self, *, model: str, **attributes: Any) -> Iterator[_LlmCallRecorder]:
        """开启一个模型调用 span。遥测在**调用侧**闭环（架构方案 §1）。"""
        with self._telemetry.start_span(
            "llm_call",
            **{
                "llm.model": model,
                # OpenInference 标准键：Phoenix 识别 LLM span 并计算成本
                # （should_calculate_span_cost 要求 openinference.span.kind=LLM
                # 且 llm.model_name 非空；llm.model_name 在 record 里补）。
                "openinference.span.kind": "LLM",
                **attributes,
            },
        ) as span:
            yield _LlmCallRecorder(
                span,
                price_in=self._price_in,
                price_out=self._price_out,
                price_cached=self._price_cached,
            )

    @contextmanager
    def trace(self, request_id: str, **attributes: Any) -> Iterator[None]:
        """最外层 trace span。"""
        attrs = {"request.id": request_id, **attributes}
        with self._telemetry.start_span("agent.request", **attrs):
            yield

    @contextmanager
    def tool_call(
        self, name: str, *, arguments: Mapping[str, Any] | None = None, **attributes: Any
    ) -> Iterator[_ToolCallRecorder]:
        """开启一个**工具执行** span。

        与 :meth:`tool_guard` 的区别要说清楚，否则很容易混：
        ``tool_guard`` 记的是"要不要执行"（策略判定），``tool_call`` 记的是
        "执行成没成"（参数 / 返回 / 耗时）。两者缺一，工具链路的排障就不完整：
        只有 guard 时，看到一条 tool_guard 记录无法区分"被拦了"和"执行了但失败"。

        Args:
            name: 工具名。
            arguments: 结构化入参。会整体 JSON 序列化后写入
                ``tool.arguments``，**受 capture_prompts 开关管辖**
                （默认只留键名，见 :mod:`telemetry.redaction`）。
        """
        payload = (
            json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True)
            if arguments is not None
            else ""
        )
        attrs = {"tool.name": name, "tool.arguments": payload, **attributes}
        with self._telemetry.start_span("tool_call", **attrs) as span:
            yield _ToolCallRecorder(span)

    def shutdown(self) -> None:
        """关闭底层遥测实现，冲刷未导出的 span。幂等。

        幂等性放在这一层而不是只依赖具体实现：关停路径可能被应用生命周期
        和信号处理器各调一次，而"关两次"不该比"关一次"更糟。
        """
        if self._shutdown:
            return
        self._shutdown = True
        self._telemetry.shutdown()


def summarize_detectors(results: tuple[DetectorResult, ...]) -> dict[str, Any]:
    """把检测结果汇总成 span 属性（供叶子 span 使用）。"""
    return {
        "detector.count": len(results),
        "detector.labels": ",".join(sorted({r.label for r in results})) or "<none>",
    }


__all__ = ["Instrumentation", "estimate_cost", "summarize_detectors"]
