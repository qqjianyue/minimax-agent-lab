"""C11 eval_harness · 阈值扫描与校准单元测试。

阈值校准是"风险决策"：min_recall 约束对应"漏检不可接受（合规）"的业务
场景 —— 先过滤不达标的阈值，再在达标者里取 F1 最高。
"""

from __future__ import annotations

import pytest

from eval_harness.threshold import calibrate_threshold, scan_thresholds


class TestScanThresholds:
    def test_high_threshold_means_high_precision_low_recall(self) -> None:
        scores = [0.95, 0.75, 0.55, 0.2]
        labels = [True, True, False, False]
        points = scan_thresholds(scores, labels, [0.5, 0.8, 0.95])
        # t=0.95: 只拦 0.95 那一条（TP），R=1/2, P=1
        assert points[2].threshold == 0.95
        assert points[2].precision == 1.0
        assert points[2].recall == pytest.approx(0.5)

    def test_low_threshold_means_low_precision(self) -> None:
        scores = [0.95, 0.75, 0.55, 0.2]
        labels = [True, True, False, False]
        points = scan_thresholds(scores, labels, [0.5])
        # t=0.5: 拦 0.95/0.75/0.55 三条（TP=2, FP=1），P=2/3, R=1
        assert points[0].recall == 1.0
        assert points[0].precision == pytest.approx(2 / 3)

    def test_unsorted_thresholds_rejected(self) -> None:
        with pytest.raises(ValueError, match="升序"):
            scan_thresholds([1.0], [True], [0.8, 0.5])

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="长度不一致"):
            scan_thresholds([1.0, 0.5], [True], [0.5])

    def test_empty_thresholds_returns_empty(self) -> None:
        assert scan_thresholds([1.0], [True], []) == []


class TestCalibrateThreshold:
    # 排序噪声场景：FP(0.95/0.93/0.92) 分数高于最低 TP(0.91)，
    # 无法完全分离 → 无约束时 F1 最优点是"只拦最高分那条"（t=0.98，
    # P=1 R=0.5）；min_recall 约束会把这个低召回点排除，改选 t=0.5。
    SCORES = [0.98, 0.95, 0.93, 0.92, 0.91, 0.2]
    LABELS = [True, False, False, False, True, False]

    def test_picks_max_f1_without_constraint(self) -> None:
        best = calibrate_threshold(self.SCORES, self.LABELS, [0.5, 0.95, 0.98])
        # t=0.98: TP=1 FP=0 → P=1, R=0.5, F1=0.667（胜过 t=0.5 的 0.571）
        assert best.threshold == 0.98
        assert best.f1 == pytest.approx(2 / 3)

    def test_min_recall_constraint_filters_below(self) -> None:
        """要求 R>=0.6：t=0.98 的 R=0.5 被排除，改选 t=0.5（R=1）。"""
        best = calibrate_threshold(
            self.SCORES, self.LABELS, [0.5, 0.95, 0.98], min_recall=0.6
        )
        assert best.threshold == 0.5
        assert best.recall == 1.0

    def test_min_recall_changes_the_choice(self) -> None:
        """约束确实**改变了选择**：无约束 t=0.98，有约束 t=0.5。"""
        plain = calibrate_threshold(self.SCORES, self.LABELS, [0.5, 0.95, 0.98])
        constrained = calibrate_threshold(
            self.SCORES, self.LABELS, [0.5, 0.95, 0.98], min_recall=0.6
        )
        assert plain.threshold == 0.98
        assert constrained.threshold == 0.5

    def test_min_recall_no_solution_raises(self) -> None:
        """只扫高阈值时最高 recall 只有 0.5，min_recall=0.6 无解。"""
        with pytest.raises(ValueError, match="没有任何阈值"):
            calibrate_threshold(
                self.SCORES, self.LABELS, [0.95, 0.98], min_recall=0.6
            )

    def test_invalid_min_recall_raises(self) -> None:
        with pytest.raises(ValueError, match=r"\[0,1\]"):
            calibrate_threshold(self.SCORES, self.LABELS, [0.5], min_recall=1.5)

    def test_empty_thresholds_raises(self) -> None:
        with pytest.raises(ValueError, match="为空"):
            calibrate_threshold([1.0], [True], [])

    def test_to_dict_roundtrip(self) -> None:
        best = calibrate_threshold(self.SCORES, self.LABELS, [0.5, 0.98])
        d = best.to_dict()
        assert d["threshold"] == 0.98
        assert d["f1"] == pytest.approx(2 / 3, abs=1e-3)  # to_dict 会 round 到 4 位
