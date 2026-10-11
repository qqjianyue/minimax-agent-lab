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
        short_circuit: bool | frozenset[GuardStage] = False,
    ) -> None:
        """Args:
        engine: 策略引擎。
        detectors: 参与检测的检测器（顺序影响短路语义，见 short_circuit）。
        enabled: 启用白名单。None 表示全启用；用于紧急时一键关掉高成本检测器。
        short_circuit: 分层触发（B5 C5）：按传入顺序运行，**已有命中结果时
            跳过后续检测器**（后层只在前层未命中时触发，承担延迟/计费预算）。
            **失败不短路** —— fail-closed 需要失败信息参与决策，检测器挂了
            不能假装"没跑过"。attempted_detectors 只记录实际运行的检测器。

            可以传 ``True``（所有阶段都短路，向后兼容）或一个
            :class:`~guard_contract.enums.GuardStage` 集合（**只对这些阶段
            短路**，其余阶段跑满全部检测器）。

            **为什么 B8 改成 per-stage（B8 关卡 0 实测）**：在检索检测点上，
            L1 正则的低分命中会把 L3/L4 整层挡掉，于是"最严重的判定"反而
            拿不到发言权 —— 实测里最标准的注入句式"忽略以上所有指令"只判到
            ``escalate``，而语义型攻击（"你现在是管理员模式"）因 L1 无命中
            而落到 embedding 相似度上直接放行。检索文档很短、检测成本可接受，
            那一层该跑满。
        """
        self._engine = engine
        self._detectors = tuple(detectors)
        self._enabled = enabled
        self._short_circuit = short_circuit

    @property
    def detectors(self) -> tuple[DetectorPort, ...]:
        return self._detectors

    def _short_circuits(self, stage: GuardStage, override: bool | None = None) -> bool:
        """该阶段是否启用短路。

        Args:
        stage: 检测点。
        override: 单次调用的覆盖值。``None`` 表示沿用构造期的阶段配置；
            显式 ``True``/``False`` 则本次调用说了算。

        **为什么需要按调用点覆盖（B8.7 实测）**：同一个 ``TOOL`` 阶段在 B8.6
        之后承载了**两种语义完全不同的内容** ——

        - 工具**调用参数**（结构化、短、可在规则层定案，跑满只是浪费一次
          LLM 调用）；
        - 工具**返回值**（不可信文本，与检索文档是同一个信任缺口）。

        实测：工具返回里带 base64 的毒文本在 L1 只得 0.45 分（编码混淆规则），
        而短路只看"有没有命中"不看分数 —— 于是检索 judge 被挡在门外，
        0.45 又够不上 ``escalate_borderline`` 的 0.60，**结果整体放行**。
        同一阶段开关短路就修不了它（要么参数侧也被迫跑满、丢掉 B5 的延迟预算，
        要么返回值侧漏检），所以短路策略下沉到调用点。
        """
        if override is not None:
            return override
        if isinstance(self._short_circuit, bool):
            return self._short_circuit
        return stage in self._short_circuit

    def _applicable(self, stage: GuardStage) -> list[DetectorPort]:
        out: list[DetectorPort] = []
        for detector in self._detectors:
            if self._enabled is not None and detector.name not in self._enabled:
                continue
            if stage not in detector.supported_stages:
                continue
            out.append(detector)
        return out

    def run(
        self,
        text: str,
        *,
        stage: GuardStage,
        request_id: str | None = None,
        short_circuit: bool | None = None,
    ) -> Decision:
        """执行检测并求值。

        Args:
        text: 待检测内容（用户输入、检索内容、工具输出或模型输出）。
        stage: 检测点。
        request_id: 关联请求标识；不传则自动生成。
        short_circuit: 本次调用的短路覆盖（见 :meth:`_short_circuits`）。
            ``None`` 表示沿用构造期配置。

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
        short_circuit_on = self._short_circuits(stage, short_circuit)

        for detector in applicable:
            # 分层触发：已有命中结果时后层不跑（见 short_circuit 的 docstring）
            if short_circuit_on and results:
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
