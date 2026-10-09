"""评估报告：指标聚合、分类明细与 JSON 落盘。

报告回答三件事：
1. **整体** —— 整套 guard 在红队集上的 P/R/F1/准确率（回归对比的主键）；
2. **分类** —— 直接注入 / 数据泄露 / 工具滥用 / 良性各自的表现
   （定位"是哪个信任边界出了问题"，只看整体会掩盖分类差异）；
3. **误报明细** —— 良性样本被拦截的列表（FP 是用户体验损失，逐条可追）；
   漏检明细同理（FN 是合规风险，逐条可追）。

``disabled`` 样本单独列出但**不计入指标**：数据集收录了但当前架构还不能
承诺拦截（如 B8 之前的间接注入），报告里保留存在感但不污染指标 ——
这正是"测试集覆盖四类，但承诺按能力解锁"的工程化表达。
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval_harness.dataset import EvalDataset
from eval_harness.metrics import ClassificationMetrics, compute_metrics
from eval_harness.runner import CaseVerdict


@dataclass(frozen=True)
class EvaluationReport:
    """一次评估的完整结果。``to_dict`` 即 JSON 落盘形状。"""

    target_name: str
    dataset_name: str
    mode: str
    generated_at: str
    enabled: int
    disabled: int
    overall: ClassificationMetrics
    by_category: dict[str, ClassificationMetrics]
    latency_ms_mean: float
    latency_ms_p95: float
    false_positives: tuple[CaseVerdict, ...] = ()
    false_negatives: tuple[CaseVerdict, ...] = ()
    disabled_cases: tuple[CaseVerdict, ...] = ()
    threshold_scan: tuple[dict[str, float | int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target_name,
            "dataset": self.dataset_name,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "cases": {"enabled": self.enabled, "disabled": self.disabled},
            "overall": self.overall.to_dict(),
            "by_category": {
                cat: m.to_dict() for cat, m in sorted(self.by_category.items())
            },
            "latency_ms": {
                "mean": round(self.latency_ms_mean, 1),
                "p95": round(self.latency_ms_p95, 1),
            },
            "false_positives": [v.to_dict() for v in self.false_positives],
            "false_negatives": [v.to_dict() for v in self.false_negatives],
            "disabled_cases": [v.to_dict() for v in self.disabled_cases],
            "threshold_scan": list(self.threshold_scan),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(0.95 * len(ordered)))
    return ordered[idx]


def build_report(
    *,
    target_name: str,
    dataset: EvalDataset,
    mode: str,
    verdicts: list[CaseVerdict],
    threshold_scan: list[dict[str, float | int]] | None = None,
) -> EvaluationReport:
    """从跑分结果聚合出报告。

    ``verdicts`` 应为 :meth:`EvalRunner.run` 的返回（只含 enabled 样本）。
    """
    y_true = [v.expected_intercept for v in verdicts]
    y_pred = [v.intercepted for v in verdicts]
    overall = compute_metrics(y_true, y_pred)

    by_category: dict[str, ClassificationMetrics] = {}
    groups: dict[str, list[CaseVerdict]] = defaultdict(list)
    for v in verdicts:
        groups[v.category].append(v)
    for category, cases in groups.items():
        by_category[category] = compute_metrics(
            [c.expected_intercept for c in cases], [c.intercepted for c in cases]
        )

    latencies = [v.latency_ms for v in verdicts]
    fps = tuple(v for v in verdicts if not v.expected_intercept and v.intercepted)
    fns = tuple(v for v in verdicts if v.expected_intercept and not v.intercepted)
    disabled = tuple(
        v
        for v in _verdicts_for_disabled(dataset, verdicts)
    )

    return EvaluationReport(
        target_name=target_name,
        dataset_name=dataset.name,
        mode=mode,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        enabled=len(verdicts),
        disabled=len(disabled),
        overall=overall,
        by_category=by_category,
        latency_ms_mean=statistics.mean(latencies) if latencies else 0.0,
        latency_ms_p95=_p95(latencies),
        false_positives=fps,
        false_negatives=fns,
        disabled_cases=disabled,
        threshold_scan=tuple(threshold_scan or ()),
    )


def _verdicts_for_disabled(
    dataset: EvalDataset, enabled_verdicts: list[CaseVerdict]
) -> list[CaseVerdict]:
    """给 disabled 样本生成占位 verdict（保留存在感，不计指标）。"""
    existing = {v.case_id for v in enabled_verdicts}
    out: list[CaseVerdict] = []
    for case in dataset.disabled_cases:
        if case.id in existing:
            continue
        out.append(
            CaseVerdict(
                case_id=case.id,
                category=case.category,
                expected=case.expected,
                expected_intercept=case.expected != "allow",
                action=None,
                intercepted=False,
                score=0.0,
            )
        )
    return out


def render_report(report: EvaluationReport) -> str:
    """人类可读的报告文本（终端 / 部署日志打印用）。"""
    lines: list[str] = []
    o = report.overall
    lines.append(f"[eval] {report.target_name} @ {report.dataset_name} (mode={report.mode})")
    lines.append(
        f"[eval]  整体  P={o.precision:.3f}  R={o.recall:.3f}  "
        f"F1={o.f1:.3f}  Acc={o.accuracy:.3f}  "
        f"(TP={o.confusion.tp} FP={o.confusion.fp} TN={o.confusion.tn} FN={o.confusion.fn})"
    )
    for cat in sorted(report.by_category):
        m = report.by_category[cat]
        lines.append(
            f"[eval]    {cat:<20} P={m.precision:.3f} R={m.recall:.3f} "
            f"F1={m.f1:.3f} (n={m.confusion.total})"
        )
    lines.append(
        f"[eval]  延迟  mean={report.latency_ms_mean:.0f}ms  p95={report.latency_ms_p95:.0f}ms  "
        f"样本数={report.enabled}（另有 {report.disabled} 条 disabled 不计分）"
    )
    if report.false_positives:
        ids = ", ".join(v.case_id for v in report.false_positives)
        lines.append(f"[eval]  误报(FP，良性被拦): {ids}")
    if report.false_negatives:
        ids = ", ".join(v.case_id for v in report.false_negatives)
        lines.append(f"[eval]  漏检(FN，攻击放行): {ids}")
    if report.threshold_scan:
        lines.append("[eval]  阈值扫描（threshold -> P/R/F1）:")
        for row in report.threshold_scan:
            lines.append(
                f"[eval]    t={row['threshold']:.3f}  P={row['precision']:.3f} "
                f"R={row['recall']:.3f}  F1={row['f1']:.3f}"
            )
    return "\n".join(lines)


def save_report(report: EvaluationReport, path: str | Path) -> Path:
    """报告 JSON 落盘（评估产物，供历史对比）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(report.to_json(), encoding="utf-8")
    return p


__all__ = ["EvaluationReport", "build_report", "render_report", "save_report"]
