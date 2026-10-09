"""阈值扫描与校准。

Threshold 校准在面试口径里是**风险决策**而不是纯技术问题：阈值调高，
precision 升、recall 降 —— 少误杀但可能漏检；调低则反过来。对银行客服
场景，FP（误杀）影响用户体验，FN（漏检）可能造成合规事故，两边都不能
只看一头。这里提供：

* :func:`scan_thresholds` —— 看整条 P/R/F1 曲线（趋势比单点更有信息量）；
* :func:`calibrate_threshold` —— 按业务约束自动选阈值：默认最大化 F1，
  或给定 ``min_recall``（如"召回率不得低于 0.95"）后取满足约束下
  F1 最高的阈值 —— 这正好对应"访问核心数据的 Agent，FN 可能导致合规
  事故"的场景。

注意：阈值校准针对**单个检测器的连续 score**（如嵌入相似度），
pipeline 级评估走的是策略引擎的综合决策（action），没有单一阈值可扫。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from eval_harness.metrics import compute_metrics


@dataclass(frozen=True)
class ThresholdPoint:
    """某个阈值下的指标。``threshold`` 是判定边界（score >= t 判拦截）。"""

    threshold: float
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    tn: int
    fn: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "threshold": round(self.threshold, 3),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
        }


def scan_thresholds(
    scores: Sequence[float], labels: Sequence[bool], thresholds: Sequence[float]
) -> list[ThresholdPoint]:
    """对每个阈值计算指标。阈值需升序排列（否则结果没法读）。"""
    if len(scores) != len(labels):
        raise ValueError(
            f"score 与标签长度不一致: scores={len(scores)} labels={len(labels)}"
        )
    ordered = list(thresholds)
    if ordered != sorted(ordered):
        raise ValueError("thresholds 必须升序排列")
    if not ordered:
        return []

    points: list[ThresholdPoint] = []
    for t in ordered:
        pred = [s >= t for s in scores]
        m = compute_metrics(labels, pred)
        points.append(
            ThresholdPoint(
                threshold=t,
                precision=m.precision,
                recall=m.recall,
                f1=m.f1,
                tp=m.confusion.tp,
                fp=m.confusion.fp,
                tn=m.confusion.tn,
                fn=m.confusion.fn,
            )
        )
    return points


def calibrate_threshold(
    scores: Sequence[float],
    labels: Sequence[bool],
    thresholds: Sequence[float],
    *,
    min_recall: float | None = None,
) -> ThresholdPoint:
    """在给定阈值集合里选最优阈值。

    Args:
        min_recall: 业务约束。为 ``None`` 时最大化 F1；给定时先过滤
            ``recall >= min_recall`` 的点，再在其中取 F1 最高 —— 漏检
            不可接受（合规）时用它，宁可比 F1 最优点多拦截一些。
    """
    points = scan_thresholds(scores, labels, thresholds)
    if not points:
        raise ValueError("阈值集合为空，无法校准")

    if min_recall is not None:
        if not 0.0 <= min_recall <= 1.0:
            raise ValueError(f"min_recall 必须在 [0,1] 内: {min_recall}")
        candidates = [p for p in points if p.recall >= min_recall]
        if not candidates:
            raise ValueError(
                f"没有任何阈值达到 min_recall={min_recall}；"
                "需要放宽约束或改进检测器，而不是硬选一个不达标的阈值"
            )
    else:
        candidates = points

    return max(candidates, key=lambda p: p.f1)


__all__ = ["ThresholdPoint", "scan_thresholds", "calibrate_threshold"]
