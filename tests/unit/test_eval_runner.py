"""C11 eval_harness · 评估运行器单元测试。

两种模式：
* detector 模式 —— 单检测器 score 阈值判定（阈值校准的输入）；
* pipeline 模式 —— GuardPipeline 策略决策的拦截语义（block / redact /
  rewrite / require_approval / escalate 均算拦截，allow 放行）。

用真实 ``PolicyEngine + 默认策略`` 测 pipeline 模式，保证跑分逻辑吃的是
真实策略语义而不是测试自造的。
"""

from __future__ import annotations

import pytest

from eval_harness.dataset import EvalCase, EvalDataset
from eval_harness.runner import INTERCEPT_ACTIONS, EvalRunner
from guard_contract.default_policy import load_default_policy
from guard_contract.enums import GuardStage
from guard_contract.result import DetectorResult
from policy_engine import GuardPipeline, PolicyEngine
from tests.fakes import StubDetector, make_result


def _dataset(*cases: EvalCase) -> EvalDataset:
    return EvalDataset(name="t", cases=tuple(cases))


def _case(cid: str, *, expected: str = "allow", category: str = "benign") -> EvalCase:
    return EvalCase(id=cid, text=f"text-{cid}", category=category, expected=expected)


def _flagged_result(detector: str = "stub", score: float = 0.9, label: str = "prompt_injection"):
    return (
        DetectorResult(
            detector=detector,
            version="1.0.0",
            stage=GuardStage.INPUT,
            label=label,
            score=score,
            confidence=0.9,
        ),
    )


class TestDetectorMode:
    def test_flagged_above_threshold_intercepted(self) -> None:
        det = StubDetector(results=_flagged_result(score=0.9))
        runner = EvalRunner(det, detector_threshold=0.5)
        verdict = runner.run(_dataset(_case("a", expected="block", category="direct_injection")))[0]
        assert verdict.intercepted is True
        assert verdict.score == pytest.approx(0.9)
        assert verdict.action is None

    def test_below_threshold_not_intercepted(self) -> None:
        det = StubDetector(results=_flagged_result(score=0.4))
        runner = EvalRunner(det, detector_threshold=0.5)
        verdict = runner.run(_dataset(_case("a", expected="block")))[0]
        assert verdict.intercepted is False

    def test_no_hit_score_zero(self) -> None:
        det = StubDetector(results=None)
        runner = EvalRunner(det, detector_threshold=0.5)
        verdict = runner.run(_dataset(_case("a")))[0]
        assert verdict.intercepted is False
        assert verdict.score == 0.0

    def test_stage_is_passed_to_detector(self) -> None:
        det = StubDetector(results=_flagged_result(score=0.9), stages=frozenset({GuardStage.TOOL}))
        runner = EvalRunner(det, stage=GuardStage.INPUT)
        verdict = runner.run(_dataset(_case("a", expected="block")))[0]
        assert verdict.intercepted is False  # 检测器只在 TOOL 阶段命中


class TestPipelineMode:
    def _pipeline(self, detector: StubDetector) -> GuardPipeline:
        engine = PolicyEngine(load_default_policy())
        return GuardPipeline(engine, [detector])

    def test_block_action_intercepted(self) -> None:
        det = StubDetector(results=_flagged_result(score=0.95, label="prompt_injection"))
        runner = EvalRunner(self._pipeline(det))
        verdict = runner.run(_dataset(_case("a", expected="block")))[0]
        assert verdict.action is not None and verdict.intercepted is True
        assert verdict.rule_name == "block_critical_injection"

    def test_redact_action_is_intercept(self) -> None:
        """PII 走 redact —— 在指标里同样算拦截（PII 不会明文流出）。"""
        det = StubDetector(results=_flagged_result(score=0.8, label="pii_leak"))
        runner = EvalRunner(self._pipeline(det))
        verdict = runner.run(_dataset(_case("a", expected="redact", category="data_leak")))[0]
        assert verdict.action is not None
        assert verdict.action.value == "redact"
        assert verdict.intercepted is True

    def test_allow_default_not_intercepted(self) -> None:
        det = StubDetector(results=None)
        runner = EvalRunner(self._pipeline(det))
        verdict = runner.run(_dataset(_case("a")))[0]
        assert verdict.intercepted is False
        assert verdict.action is not None
        assert verdict.action.value == "allow"

    def test_safe_label_not_intercepted(self) -> None:
        det = StubDetector(results=(make_result(label="safe", score=0.0),))
        runner = EvalRunner(self._pipeline(det))
        verdict = runner.run(_dataset(_case("a")))[0]
        assert verdict.intercepted is False

    def test_disabled_cases_skipped(self) -> None:
        det = StubDetector(results=_flagged_result(score=0.95))
        runner = EvalRunner(self._pipeline(det))
        ds = _dataset(
            _case("a", expected="block"),
            EvalCase(
                id="off",
                text="x",
                category="indirect_injection",
                expected="block",
                enabled=False,
            ),
        )
        verdicts = runner.run(ds)
        assert [v.case_id for v in verdicts] == ["a"]


class TestTargetValidation:
    def test_invalid_target_rejected(self) -> None:
        with pytest.raises(TypeError, match="不支持的评估目标"):
            EvalRunner(object())  # type: ignore[arg-type]

    def test_mode_and_name(self) -> None:
        det = StubDetector(name="stub.l1")
        assert EvalRunner(det).mode == "detector"
        assert EvalRunner(det).target_name == "stub.l1"
        pipeline = GuardPipeline(PolicyEngine(load_default_policy()), [det])
        runner = EvalRunner(pipeline)
        assert runner.mode == "pipeline"
        assert runner.target_name == "guard-pipeline"

    def test_intercept_actions_cover_all_dataflow_actions(self) -> None:
        """拦截判定与策略引擎的 dataflow 语义对齐：改数据流的动作都算拦截。"""
        assert {"block", "redact", "rewrite", "require_approval", "escalate"} <= {
            a.value for a in INTERCEPT_ACTIONS
        }
