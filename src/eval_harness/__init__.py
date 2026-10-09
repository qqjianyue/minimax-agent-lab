"""C11 eval_harness —— 安全评估回归工具集。

职责（对应开发方案 §2.2）：
    * 回归测试集执行器（数据集加载 + 对检测器 / 完整 guard pipeline 跑分）
    * 红队样本（直接注入 / 间接注入 / 数据泄露 / 工具滥用 + 良性样本）
    * 阈值扫描（threshold calibration：P/R/F1 随阈值的变化曲线）
    * precision / recall / F1 报告生成

定位：**开发工具，非常驻** —— 不随服务启动，只在评估 / 回归 / 阈值校准时
运行。它不引入任何新的运行期依赖（复用 pydantic），因此本地（Fake 检测器）
与目标机（真实检测器）都能跑同一份数据集与同一套指标逻辑 —— 指标语义一致
是"本地调阈值、目标机验证"这条工作流的前提。

指标语义约定（面试口径）：
    * 攻击样本 = 正类（positive）：期望被拦截（block / redact / rewrite /
      require_approval / escalate 都算"拦截"）；
    * 良性样本 = 负类（negative）：期望放行（allow）；
    * 拦截预测 = 预测正类。precision = 拦截对了多少 / 拦截了多少，
      recall = 该拦的拦住了多少。
"""

from __future__ import annotations

from eval_harness.dataset import EvalCase, EvalDataset, builtin_dataset, load_dataset
from eval_harness.metrics import ClassificationMetrics, ConfusionMatrix, compute_metrics
from eval_harness.report import EvaluationReport, render_report, save_report
from eval_harness.runner import CaseVerdict, EvalRunner
from eval_harness.threshold import ThresholdPoint, calibrate_threshold, scan_thresholds

__all__ = [
    "EvalCase",
    "EvalDataset",
    "builtin_dataset",
    "load_dataset",
    "ConfusionMatrix",
    "ClassificationMetrics",
    "compute_metrics",
    "ThresholdPoint",
    "scan_thresholds",
    "calibrate_threshold",
    "CaseVerdict",
    "EvalRunner",
    "EvaluationReport",
    "render_report",
    "save_report",
]
