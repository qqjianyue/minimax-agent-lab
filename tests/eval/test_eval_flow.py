"""C11 eval_harness · L4 评估层端到端流程。

完整评估工作流在 Fake 依赖下跑通：数据集 → 运行器（pipeline 模式）→
指标聚合 → 报告渲染/落盘。同时覆盖"良性误伤监控"（FP 必须被量化）与
"阈值校准"（单检测器模式）两条业务主线 —— 这两条正是 B7 交付物
"评估回归 + 阈值校准报告"的核心。

全部离线：检测器是 tests.fakes 的桩，socket 由根 conftest 禁用。
"""

from __future__ import annotations

import json

import pytest

from eval_harness.dataset import EvalCase, EvalDataset, builtin_dataset
from eval_harness.report import build_report, render_report, save_report
from eval_harness.runner import EvalRunner
from eval_harness.threshold import calibrate_threshold, scan_thresholds
from guard_contract.default_policy import load_default_policy
from guard_contract.enums import GuardStage
from policy_engine import GuardPipeline, PolicyEngine
from tests.fakes import StubDetector, make_result


class _PerfectDetector(StubDetector):
    """按文本前缀命中：``EVIL:`` 返回注入命中，其余（良性）不命中。"""

    def detect(self, text: str, *, stage: GuardStage):
        if text.startswith("EVIL:"):
            return super().detect(text, stage=stage)
        return ()


def _pipeline(detector: StubDetector) -> GuardPipeline:
    return GuardPipeline(PolicyEngine(load_default_policy()), [detector])


def _injection_result():
    return (
        make_result(
            detector="stub.l1",
            label="prompt_injection",
            score=0.95,
            confidence=0.9,
            stage="input",
        ),
    )


class TestEndToEndPipelineRun:
    def test_full_flow_perfect_detector(self, tmp_path) -> None:
        """完整流程：完美检测器在内置红队集上 P=R=F1=1，报告落盘可读。"""
        ds = EvalDataset(
            name="e2e",
            cases=tuple(
                [
                    EvalCase(
                        id=f"a{i}",
                        text=f"EVIL:{i}",
                        category="direct_injection",
                        expected="block",
                    )
                    for i in range(5)
                ]
                + [
                    EvalCase(id=f"b{i}", text=f"OK:{i}", category="benign", expected="allow")
                    for i in range(5)
                ]
            ),
        )
        runner = EvalRunner(
            _pipeline(_PerfectDetector(name="perfect", results=_injection_result()))
        )
        verdicts = runner.run(ds)
        report = build_report(
            target_name="perfect", dataset=ds, mode=runner.mode, verdicts=verdicts
        )

        assert report.overall.f1 == 1.0
        assert report.overall.recall == 1.0
        assert report.overall.precision == 1.0
        assert report.enabled == 10

        text = render_report(report)
        assert "F1=1.000" in text

        out = save_report(report, tmp_path / "r" / "e2e.json")
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed["overall"]["f1"] == 1.0
        assert parsed["by_category"]["direct_injection"]["recall"] == 1.0

    def test_benign_false_positive_monitoring(self) -> None:
        """全命中检测器：良性全误杀 —— 报告必须点名 FP 明细（用户体验损失可追）。"""
        ds = builtin_dataset()
        det = StubDetector(name="all-hit", results=_injection_result())
        runner = EvalRunner(_pipeline(det))
        report = build_report(
            target_name="all-hit", dataset=ds, mode=runner.mode, verdicts=runner.run(ds)
        )
        assert len(report.false_positives) == 6  # 6 条良性全被拦
        assert {v.case_id for v in report.false_positives} == {
            "bg-01", "bg-02", "bg-03", "bg-04", "bg-05", "bg-06"
        }
        # 攻击全拦 → recall=1，但 precision 被 FP 拉低
        assert report.overall.recall == 1.0
        assert report.overall.precision == pytest.approx(12 / 18)

    def test_uncovered_indirect_injection_excluded_from_metrics(self) -> None:
        """disabled 样本（多模态注入）不进指标，但报告保留存在感。

        B8 起 ii-01/ii-02 已解锁进指标；只有全链路没有能力覆盖的那条
        留在 disabled —— 「不能兑现的承诺不写进数据集的 enabled」。
        """
        ds = builtin_dataset()
        runner = EvalRunner(_pipeline(StubDetector(name="never", results=None)))
        report = build_report(
            target_name="never", dataset=ds, mode=runner.mode, verdicts=runner.run(ds)
        )
        assert report.enabled == 18
        assert report.disabled == 1
        assert "indirect_injection" in report.by_category
        assert [v.case_id for v in report.disabled_cases] == ["ii-03"]

    def test_indirect_injection_cases_run_on_their_own_stage(self) -> None:
        """ii-01 走 retrieval、ii-02 走 tool —— 不能都塞进 input。

        用 input 去跑它们等于换了一把尺子：要么被输入侧规则误打成高分，
        要么因为那个检测点上没有对应检测器而必然放行。两种都让指标失真。
        """
        ds = builtin_dataset()
        runner = EvalRunner(_pipeline(StubDetector(name="rec", results=None)))
        verdicts = runner.run(ds)
        stages = {v.case_id: v.stage for v in verdicts}
        assert stages["ii-01"] == "retrieval"
        assert stages["ii-02"] == "tool"
        # 其余样本仍落在运行器默认的 input
        assert {v.stage for v in verdicts if v.case_id.startswith("bg-")} == {"input"}


class TestThresholdCalibrationFlow:
    def test_calibrate_returns_best_f1(self) -> None:
        """单检测器模式：扫描整条 P/R/F1 曲线后给出校准阈值。"""
        scores = [0.95, 0.85, 0.75, 0.2, 0.1]
        labels = [True, True, False, False, False]
        points = scan_thresholds(scores, labels, [0.5, 0.8, 0.9])
        best = calibrate_threshold(scores, labels, [0.5, 0.8, 0.9])
        # t=0.8: TP=2 FP=0 → F1=1
        assert best.threshold == 0.8
        assert best.f1 == 1.0
        assert len(points) == 3

    def test_min_recall_constraint(self) -> None:
        """合规场景约束：recall 不达标（漏检不可接受）的阈值直接排除。"""
        scores = [0.95, 0.85, 0.75, 0.2]
        labels = [True, True, False, False]
        best = calibrate_threshold(
            scores, labels, [0.5, 0.8, 0.9], min_recall=0.6
        )
        # t=0.9 的 R=0.5 不达标被排除；t=0.8（R=1, F1=1）胜出
        assert best.threshold == 0.8

    def test_detector_mode_score_is_max_result_score(self) -> None:
        """detector 模式：多个命中结果取最高 score 作为样本得分。"""
        det = StubDetector(
            results=(
                make_result(detector="s", label="pii_leak", score=0.3, stage="input"),
                make_result(detector="s", label="prompt_injection", score=0.88, stage="input"),
            )
        )
        runner = EvalRunner(det, detector_threshold=0.8)
        ds = EvalDataset(
            name="t",
            cases=(EvalCase(id="a", text="x", category="direct_injection", expected="block"),),
        )
        verdict = runner.run(ds)[0]
        assert verdict.score == pytest.approx(0.88)
        assert verdict.intercepted is True
