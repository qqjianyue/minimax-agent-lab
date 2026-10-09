"""检测流水线：把多个检测器跑起来，收集结果与失败，交给引擎求值。

**B2 的边界**：检测器按顺序**同步**执行。真实的按墙钟超时强制（线程池 /
asyncio.wait_for）随 B5 的真实检测器一起落地 —— 现在所有检测器都是纯计算，
秒级返回，加超时机制只会增加不确定性而不增加保护。

异常分类在这里完成，因为"哪个检测器为什么失败"必须在进入策略层之前就确定：

- :class:`~guard_contract.port.DetectorTimeoutError` → ``TIMEOUT``
- 其它异常 → ``ERROR``

分类完成之后交给引擎的只有结论（failures 列表），引擎不需要也不应该知道
异常的细节 —— 策略层关心的是"有没有检测器没给出结论"，不是"它是怎么挂的"。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from guard_contract.enums import GuardStage
from guard_contract.port import (
    DetectorFailure,
    DetectorFailureReason,
    DetectorPort,
    DetectorTimeoutError,
)
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision
from policy_engine.engine import PolicyEngine


class GuardPipeline:
    """在一个检测点上依次运行多个检测器，产出一个 :class:`Decision`。"""

    def __init__(
        self,
        engine: PolicyEngine,
        detectors: Sequence[DetectorPort],
        *,
        enabled: frozenset[str] | None = None,
        short_circuit: bool = False,
    ) -> None:
        """Args:
        engine: 策略引擎。
        detectors: 参与检测的检测器（顺序影响短路语义，见 short_circuit）。
        enabled: 启用白名单。None 表示全启用；用于紧急时一键关掉高成本检测器。
        short_circuit: 分层触发（B5 C5）：按传入顺序运行，**已有命中结果时
            跳过后续检测器**（后层只在前层未命中时触发，承担延迟/计费预算）。
            **失败不短路** —— fail-closed 需要失败信息参与决策，检测器挂了
            不能假装"没跑过"。attempted_detectors 只记录实际运行的检测器。
        """
        self._engine = engine
        self._detectors = tuple(detectors)
        self._enabled = enabled
        self._short_circuit = short_circuit

    @property
    def detectors(self) -> tuple[DetectorPort, ...]:
        return self._detectors

    def _applicable(self, stage: GuardStage) -> list[DetectorPort]:
        out: list[DetectorPort] = []
        for detector in self._detectors:
            if self._enabled is not None and detector.name not in self._enabled:
                continue
            if stage not in detector.supported_stages:
                continue
            out.append(detector)
        return out

    def run(self, text: str, *, stage: GuardStage, request_id: str | None = None) -> Decision:
        """执行检测并求值。

        Args:
        text: 待检测内容（用户输入、检索内容、工具输出或模型输出）。
        stage: 检测点。
        request_id: 关联请求标识；不传则自动生成。

        Returns:
            策略决策。**检测器自身抛出的异常不会向外传播** —— 异常已被转成
            :class:`DetectorFailure` 并交给引擎按 fail_mode 处理。调用方拿到的
            永远是一个可执行的决策，而不是一个异常。
        """
        rid = request_id or f"req-{uuid4().hex[:12]}"
        results: list[DetectorResult] = []
        failures: list[DetectorFailure] = []
        applicable = self._applicable(stage)
        attempted: list[str] = []

        for detector in applicable:
            # 分层触发：已有命中结果时后层不跑（见 short_circuit 的 docstring）
            if self._short_circuit and results:
                break
            attempted.append(detector.name)
            try:
                results.extend(detector.detect(text, stage=stage))
            except DetectorTimeoutError as exc:
                failures.append(
                    DetectorFailure(
                        detector=detector.name,
                        reason=DetectorFailureReason.TIMEOUT,
                        detail=str(exc),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 检测器不可信，必须兜住
                failures.append(
                    DetectorFailure(
                        detector=detector.name,
                        reason=DetectorFailureReason.ERROR,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )

        # attempted_detectors 必须传：引擎要靠它区分"检测器跑完了但没发现问题"
        # 与"检测器全挂了"。缺了这个信息，DEGRADED 模式会把前者误判成后者，
        # 降级机制就变成了阻断机制。short_circuit 下这里只含实际运行的检测器。
        return self._engine.evaluate(
            request_id=rid,
            stage=stage,
            results=results,
            failures=failures,
            attempted_detectors=attempted,
        )


__all__ = ["GuardPipeline"]
