"""C11 eval_harness · 分类指标单元测试。

边界穷举：全对 / 全错 / 混合矩阵 / 无正预测（分母 0）/ 无正样本 /
长度不一致 / 空集合。指标分母为 0 时**返回 0.0 而不是抛异常** ——
评估脚本要在"一个都没拦"的边界数据上也能出报告。
"""

from __future__ import annotations

import pytest

from eval_harness.metrics import ConfusionMatrix, compute_metrics


class TestConfusionMatrix:
    def test_counts(self) -> None:
        m = compute_metrics([True, True, False, False], [True, False, True, False])
        assert m.confusion == ConfusionMatrix(tp=1, fp=1, tn=1, fn=1)
        assert m.confusion.total == 4


class TestComputeMetrics:
    def test_all_correct(self) -> None:
        m = compute_metrics([True, False], [True, False])
        assert m.precision == 1.0
        assert m.recall == 1.0
        assert m.f1 == 1.0
        assert m.accuracy == 1.0

    def test_all_wrong(self) -> None:
        m = compute_metrics([True, False], [False, True])
        assert m.precision == 0.0
        assert m.recall == 0.0
        assert m.f1 == 0.0
        assert m.accuracy == 0.0

    def test_mixed_matrix(self) -> None:
        # tp=1 fp=1 fn=1 tn=1
        m = compute_metrics([True, True, False, False], [True, False, True, False])
        assert m.precision == pytest.approx(0.5)
        assert m.recall == pytest.approx(0.5)
        assert m.f1 == pytest.approx(0.5)
        assert m.accuracy == pytest.approx(0.5)

    def test_no_positive_predictions(self) -> None:
        """一个都没拦：precision 分母为 0 → 0.0，recall=0。"""
        m = compute_metrics([True, True], [False, False])
        assert m.precision == 0.0
        assert m.recall == 0.0
        assert m.f1 == 0.0

    def test_no_positive_labels(self) -> None:
        """没有攻击样本：FN 无来源，recall=0；全放行 → accuracy=1。"""
        m = compute_metrics([False, False], [False, False])
        assert m.precision == 0.0
        assert m.recall == 0.0
        assert m.f1 == 0.0
        assert m.accuracy == 1.0

    def test_imperfect_recall_f1_compromise(self) -> None:
        # 3 个攻击拦 2 个，误杀 1 个良性：R=2/3, P=2/3, F1=2/3
        m = compute_metrics([True, True, True, False], [True, True, False, True])
        assert m.recall == pytest.approx(2 / 3)
        assert m.precision == pytest.approx(2 / 3)
        assert m.f1 == pytest.approx(2 / 3)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="长度不一致"):
            compute_metrics([True], [True, False])

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="为空"):
            compute_metrics([], [])

    def test_to_dict_shape(self) -> None:
        m = compute_metrics([True, False], [True, False])
        d = m.to_dict()
        assert d["precision"] == 1.0
        assert d["recall"] == 1.0
        assert d["tp"] == 1
        assert d["fp"] == 0
