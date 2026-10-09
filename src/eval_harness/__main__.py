"""C11 eval_harness 命令行入口。

定位是**离线评估工具**（数据集 / 指标 / 阈值校准），不组装真实检测器：
真实 guard pipeline 的跑分由目标机部署脚本（``deploy/eval.sh``）用
:func:`agent_service.container.build_container` 拿到组装好的 pipeline 后
调用 :class:`eval_harness.runner.EvalRunner` 完成 —— CLI 保持轻量，
不在入口里拖入服务装配依赖。

子命令：
    list         列出内置红队数据集概览（类别分布 / 期望动作分布）
    metrics      对已有逐样本预测算 P/R/F1（回归对比、CI 用）
    calibrate    对单检测器 score 做阈值扫描与校准（含 min_recall 约束）

``metrics`` / ``calibrate`` 读取的 JSON 形状：
    metrics:   {"labels": [true, false, ...], "predictions": [true, ...]}
    calibrate: {"scores": [0.9, 0.2, ...], "labels": [true, ...]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from eval_harness.dataset import builtin_dataset
from eval_harness.metrics import compute_metrics
from eval_harness.threshold import calibrate_threshold, scan_thresholds


def _load_data(path: str) -> dict[str, Any]:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取数据文件 {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"数据文件 {p} 必须是 JSON 对象")
    return raw


def cmd_list(_: argparse.Namespace) -> int:
    ds = builtin_dataset()
    by_cat: dict[str, int] = {}
    by_expected: dict[str, int] = {}
    for case in ds.cases:
        by_cat[case.category] = by_cat.get(case.category, 0) + 1
        by_expected[case.expected] = by_expected.get(case.expected, 0) + 1
    print(f"数据集: {ds.name}  共 {len(ds.cases)} 条 "
          f"（enabled={len(ds.enabled_cases)} disabled={len(ds.disabled_cases)}）")
    print("按类别:")
    for cat in sorted(by_cat):
        print(f"  {cat:<22} {by_cat[cat]}")
    print("按期望动作:")
    for exp in sorted(by_expected):
        print(f"  {exp:<22} {by_expected[exp]}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    data = _load_data(args.data)
    labels = data.get("labels")
    predictions = data.get("predictions")
    if not isinstance(labels, list) or not isinstance(predictions, list):
        raise SystemExit("数据文件需要 'labels' 与 'predictions' 两个布尔数组")
    m = compute_metrics(labels, predictions)
    print(
        f"P={m.precision:.4f}  R={m.recall:.4f}  F1={m.f1:.4f}  "
        f"Acc={m.accuracy:.4f}  "
        f"(TP={m.confusion.tp} FP={m.confusion.fp} TN={m.confusion.tn} FN={m.confusion.fn})"
    )
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    data = _load_data(args.data)
    scores = data.get("scores")
    labels = data.get("labels")
    if not isinstance(scores, list) or not isinstance(labels, list):
        raise SystemExit("数据文件需要 'scores' 与 'labels' 两个数组")
    thresholds = [round(t, 3) for t in args.thresholds]
    for point in scan_thresholds(scores, labels, thresholds):
        print(
            f"t={point.threshold:.3f}  P={point.precision:.4f}  "
            f"R={point.recall:.4f}  F1={point.f1:.4f}"
        )
    best = calibrate_threshold(
        scores, labels, thresholds, min_recall=args.min_recall
    )
    print(
        f"校准结果: t={best.threshold:.3f}  P={best.precision:.4f}  "
        f"R={best.recall:.4f}  F1={best.f1:.4f}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_harness", description="安全评估回归工具（C11）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出内置红队数据集概览")
    p_list.set_defaults(func=cmd_list)

    p_metrics = sub.add_parser("metrics", help="对已有预测算 P/R/F1")
    p_metrics.add_argument("--data", required=True, help="JSON: {labels, predictions}")
    p_metrics.set_defaults(func=cmd_metrics)

    p_cal = sub.add_parser("calibrate", help="阈值扫描与校准")
    p_cal.add_argument("--data", required=True, help="JSON: {scores, labels}")
    p_cal.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=[0.5, 0.6, 0.7, 0.8, 0.9, 0.95],
        help="扫描的阈值（升序）",
    )
    p_cal.add_argument(
        "--min-recall",
        type=float,
        default=None,
        help="业务约束：选满足 recall>=该值的 F1 最优阈值",
    )
    p_cal.set_defaults(func=cmd_calibrate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
