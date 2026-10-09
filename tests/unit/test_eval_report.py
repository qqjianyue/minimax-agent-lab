"""C11 eval_harness · 报告聚合单元测试。

报告要能回答"整体如何 / 哪个类别拖后腿 / 误报和漏检各是谁"三件事；
disabled 样本单列但**不计入指标**（B8 前的间接注入存在感保留、不污染数字）。
"""

from __future__ import annotations

import json

from eval_harness.dataset import builtin_dataset
from eval_harness.report import build_report, render_report, save_report
from eval_harness.runner import EvalRunner
from guard_contract.default_policy import load_default_policy
from policy_engine import GuardPipeline, PolicyEngine
from tests.fakes import StubDetector, make_result

#: 全拦截的 pipeline：对攻击样本命中 prompt_injection → block；良性也拦截 → FP。
_ALL_HIT = StubDetector(
    name="stub.l1",
    results=(
        make_result(
            detector="stub.l1",
            label="prompt_injection",
            score=0.95,
            confidence=0.9,
            stage="input",
        ),
    ),
)


def _pipeline(detector: StubDetector) -> GuardPipeline:
    return GuardPipeline(PolicyEngine(load_default_policy()), [detector])


class TestBuildReport:
    def test_perfect_pipeline_overall(self) -> None:
        """只命中攻击样本、良性放行 → P=R=F1=1。

        用前缀驱动的小数据集（``EVIL:``=攻击 / ``OK:``=良性）+ 按前缀命中的
        桩 —— 完全确定性，不依赖内置集的具体文本。
        """
        from eval_harness.dataset import EvalCase, EvalDataset

        ds = EvalDataset(
            name="perfect",
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

        class PrefixHit(StubDetector):
            def detect(self, text, *, stage):
                if not text.startswith("EVIL:"):
                    return ()
                return super().detect(text, stage=stage)

        det = PrefixHit(
            name="prefix",
            results=(
                make_result(
                    detector="prefix",
                    label="prompt_injection",
                    score=0.95,
                    confidence=0.9,
                    stage="input",
                ),
            ),
        )
        verdicts = EvalRunner(_pipeline(det)).run(ds)
        report = build_report(
            target_name="prefix", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        assert report.overall.precision == 1.0
        assert report.overall.recall == 1.0
        assert report.overall.f1 == 1.0
        assert not report.false_positives
        assert not report.false_negatives

    def test_benign_fp_tracked(self) -> None:
        """全命中桩：良性全被拦 → FP 明细里能点名误伤的是谁。"""
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(_ALL_HIT)).run(ds)
        report = build_report(
            target_name="all-hit", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        assert report.false_positives
        assert all(v.case_id.startswith("bg-") for v in report.false_positives)
        assert all(not v.expected_intercept and v.intercepted for v in report.false_positives)

    def test_missed_attacks_tracked(self) -> None:
        """从不命中桩：攻击全放行 → FN 明细点名漏检的是谁。"""
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(StubDetector(name="never", results=None))).run(ds)
        report = build_report(
            target_name="never", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        assert report.false_negatives
        assert all(v.expected_intercept and not v.intercepted for v in report.false_negatives)

    def test_by_category_present(self) -> None:
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(_ALL_HIT)).run(ds)
        report = build_report(
            target_name="x", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        cats = set(report.by_category)
        assert {"direct_injection", "data_leak", "tool_abuse", "benign"} <= cats
        # 间接注入是 disabled，不进 by_category（不计分）
        assert "indirect_injection" not in cats

    def test_disabled_cases_listed_but_not_counted(self) -> None:
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(StubDetector(name="never", results=None))).run(ds)
        report = build_report(
            target_name="x", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        assert report.enabled == 16
        assert report.disabled == 2
        assert [v.case_id for v in report.disabled_cases] == ["ii-01", "ii-02"]

    def test_to_json_serializable(self) -> None:
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(_ALL_HIT)).run(ds)
        report = build_report(
            target_name="x", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        parsed = json.loads(report.to_json())
        assert parsed["dataset"] == "redteam-v1"
        assert "overall" in parsed
        assert "by_category" in parsed

    def test_render_contains_key_lines(self) -> None:
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(_ALL_HIT)).run(ds)
        report = build_report(
            target_name="x", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        text = render_report(report)
        assert "整体" in text
        assert "误报(FP" in text
        assert "disabled" in text


class TestSaveReport:
    def test_writes_json(self, tmp_path) -> None:
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(_ALL_HIT)).run(ds)
        report = build_report(
            target_name="x", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        out = save_report(report, tmp_path / "reports" / "eval.json")
        assert out.is_file()
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed["target"] == "x"

    def test_accepts_plain_path(self, tmp_path) -> None:
        ds = builtin_dataset()
        verdicts = EvalRunner(_pipeline(_ALL_HIT)).run(ds)
        report = build_report(
            target_name="x", dataset=ds, mode="pipeline", verdicts=verdicts
        )
        save_report(report, str(tmp_path / "eval2.json"))
        assert (tmp_path / "eval2.json").is_file()
