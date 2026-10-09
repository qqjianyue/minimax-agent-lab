"""分类指标：混淆矩阵与 precision / recall / F1。

语义（与 :mod:`eval_harness` 的约定一致）：攻击样本 = 正类，拦截预测 =
预测正类。于是：

* TP = 攻击且被拦截（该拦的拦住了）
* FP = 良性却被拦截（误杀 —— 银行客服场景的直接用户体验损失）
* TN = 良性且放行
* FN = 攻击却放行（漏检 —— 合规事故的来源）

P/R/F1 全部在``分母为 0``时定义为 0.0（而不是抛异常）：评估脚本要能在
"一个都没拦"或"一个都没误杀"的边界数据上跑出可打印的报告，而不是崩掉；
0.0 与"该指标没有正例"在报告里都写得很清楚。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfusionMatrix:
    """混淆矩阵。字段名与 sklearn 一致，方便面试/评审对照。"""

    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn


@dataclass(frozen=True)
class ClassificationMetrics:
    """一组二分类指标。"""

    precision: float
    recall: float
    f1: float
    accuracy: float
    confusion: ConfusionMatrix

    def to_dict(self) -> dict[str, float | int]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
            "tp": self.confusion.tp,
            "fp": self.confusion.fp,
            "tn": self.confusion.tn,
            "fn": self.confusion.fn,
        }


def compute_metrics(
    y_true: Sequence[bool], y_pred: Sequence[bool]
) -> ClassificationMetrics:
    """从真实标签与预测标签计算指标。

    ``True`` = 攻击（期望拦截）/ 预测拦截。两个序列必须等长且非空。
    """
    if len(y_true) != len(y_pred):
        raise ValueError(
            f"标签与预测长度不一致: y_true={len(y_true)} y_pred={len(y_pred)}"
        )
    if not y_true:
        raise ValueError("评估集合为空，无法计算指标")

    tp = fp = tn = fn = 0
    for truth, pred in zip(y_true, y_pred, strict=True):
        if pred:
            if truth:
                tp += 1
            else:
                fp += 1
        else:
            if truth:
                fn += 1
            else:
                tn += 1

    confusion = ConfusionMatrix(tp=tp, fp=fp, tn=tn, fn=fn)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy = (tp + tn) / confusion.total if confusion.total > 0 else 0.0

    return ClassificationMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        accuracy=accuracy,
        confusion=confusion,
    )


__all__ = ["ConfusionMatrix", "ClassificationMetrics", "compute_metrics"]
