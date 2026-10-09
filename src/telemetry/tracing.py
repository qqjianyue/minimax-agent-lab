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

from collections.abc import Iterator
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
) -> float:
    """按每百万 token 单价估算一次调用的成本。

    刻意叫 ``estimate``：单价来自**部署配置**，可能滞后于官方调价，
    所以它是个估算口径而不是账单。单价为 0 时返回 0.0，
    表示"未配置单价"而不是"这次调用免费"—— 两者在排障时含义完全不同，
    所以宁可返回 0 也不要编一个看起来合理的数字。
    """
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    return (prompt * price_input_per_million + completion * price_output_per_million) / 1_000_000


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

    __slots__ = ("_span", "_price_in", "_price_out")

    def __init__(self, span: SpanPort, *, price_in: float, price_out: float) -> None:
        self._span = span
        self._price_in = price_in
        self._price_out = price_out

    def record(self, completion: Any) -> None:
        usage = getattr(completion, "usage", None)
        if usage is None:
            self._span.set_attribute("llm.token_usage_present", False)
            return

        self._span.set_attribute("llm.token_usage_present", True)
        self._span.set_attribute("llm.prompt_tokens", usage.prompt_tokens)
        self._span.set_attribute("llm.completion_tokens", usage.completion_tokens)
        self._span.set_attribute(
            "llm.cost",
            round(
                estimate_cost(
                    usage,
                    price_input_per_million=self._price_in,
                    price_output_per_million=self._price_out,
                ),
                8,
            ),
        )
        model = getattr(completion, "model", "") or ""
        if model:
            self._span.set_attribute("llm.model", model)


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
    ) -> None:
        self._telemetry = telemetry
        self._price_in = price_input_per_million
        self._price_out = price_output_per_million
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
            "llm_call", **{"llm.model": model, **attributes}
        ) as span:
            yield _LlmCallRecorder(
                span,
                price_in=self._price_in,
                price_out=self._price_out,
            )

    @contextmanager
    def trace(self, request_id: str, **attributes: Any) -> Iterator[None]:
        """最外层 trace span。"""
        attrs = {"request.id": request_id, **attributes}
        with self._telemetry.start_span("agent.request", **attrs):
            yield

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
