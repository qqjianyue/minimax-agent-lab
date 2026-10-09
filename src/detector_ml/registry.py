"""检测器分层注册表（C5）。

分层触发语义（架构方案 §5.2）：L1 规则 → L2 Presidio PII → L3 嵌入相似度
→ L4 LLM judge。**后层只在前层未命中时触发** —— 延迟预算与计费（judge
只在边界案例触发）都由这条语义承担，而不是由"跑全部检测器"承担。

配合 :class:`~policy_engine.pipeline.GuardPipeline` 的 ``short_circuit`` 模式：
检测器按层排序传入，命中即停。**失败不短路** —— fail-closed 需要失败信息
参与决策，检测器挂了不能假装"没跑过"（attempted_detectors 会如实反映）。

Registry 只负责**排序与合法性校验**，不重复 pipeline 的执行职责：
`detectors` 按层展开后直接交给 GuardPipeline，执行 / 异常分类 / 求值
仍是 pipeline 的事。
"""

from __future__ import annotations

from collections.abc import Sequence

from guard_contract.enums import GuardStage
from guard_contract.port import DetectorPort


class DetectorRegistry:
    """按层维护检测器，产出 pipeline 可消费的顺序列表。

    Args:
        layers: 按执行顺序排列的 ``(层名, 检测器)``。层名只用于可读性与
            校验，不参与执行逻辑。
    """

    def __init__(self, layers: Sequence[tuple[str, DetectorPort]]) -> None:
        seen_layers: set[str] = set()
        seen_names: set[str] = set()
        for layer, detector in layers:
            if layer in seen_layers:
                raise ValueError(f"层名重复: {layer!r}")
            if detector.name in seen_names:
                raise ValueError(f"检测器名重复: {detector.name!r}")
            seen_layers.add(layer)
            seen_names.add(detector.name)
        self._layers = tuple(layers)

    @property
    def detectors(self) -> tuple[DetectorPort, ...]:
        """按层展开的全部检测器（顺序 = 触发顺序）。"""
        return tuple(detector for _, detector in self._layers)

    @property
    def names(self) -> list[str]:
        return [detector.name for _, detector in self._layers]

    def for_stage(self, stage: GuardStage) -> list[DetectorPort]:
        """该检测点实际参与的检测器（按层排序，过滤不支持的 stage）。"""
        return [
            detector
            for _, detector in self._layers
            if stage in detector.supported_stages
        ]


__all__ = ["DetectorRegistry"]
