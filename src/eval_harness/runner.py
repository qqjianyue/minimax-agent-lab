"""评估运行器：对单个检测器或完整 guard pipeline 跑分。

两种模式对应评估的两个目的：

* **detector 模式**（阈值校准）：直接调 :class:`~guard_contract.port.DetectorPort`，
  取所有结果里最高的 ``score`` 作为样本得分，配合 :mod:`eval_harness.threshold`
  扫阈值 —— 回答"这个检测器自己的阈值该定在哪"；
* **pipeline 模式**（回归跑分）：跑 :class:`~policy_engine.pipeline.GuardPipeline`
  拿到策略决策 ``action``，按"是否拦截"（block / redact / rewrite /
  require_approval / escalate 均为拦截；allow 为放行）判定 —— 回答
  "整套 guard 在红队集上有没有回归"。

两种模式产出同一份 :class:`CaseVerdict` 结构，下游指标/报告完全共用 ——
检测器级与系统级评估跑的是同一套数据契约。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from eval_harness.dataset import EvalCase, EvalDataset
from guard_contract.enums import GuardStage, PolicyAction
from guard_contract.port import DetectorPort
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision
from policy_engine.pipeline import GuardPipeline

#: 视为"拦截"的动作：改变了数据流或挂起等待人工的动作。
INTERCEPT_ACTIONS = frozenset(
    {
        PolicyAction.BLOCK,
        PolicyAction.REDACT,
        PolicyAction.REWRITE,
        PolicyAction.REQUIRE_APPROVAL,
        PolicyAction.ESCALATE,
    }
)

#: pipeline / detector 二选一的最小协议（避免把 Protocol 写死成具体类型）
EvalTarget = GuardPipeline | DetectorPort


@dataclass(frozen=True)
class CaseVerdict:
    """单个样本的跑分结果。"""

    case_id: str
    category: str
    expected: str
    #: 期望是否"拦截类"（block/redact）—— 指标里的 y_true
    expected_intercept: bool
    #: 实际动作（pipeline 模式）或 None（detector 模式）
    action: PolicyAction | None
    #: 是否判定为拦截 —— 指标里的 y_pred
    intercepted: bool
    #: 样本得分（detector 模式 = 最高 score；pipeline 模式 = 最高命中 score，无命中 0）
    score: float
    #: 命中的检测器结果
    results: tuple[DetectorResult, ...] = field(default_factory=tuple)
    #: 检测耗时（毫秒）
    latency_ms: float = 0.0
    #: 命中规则（pipeline 模式可读）
    rule_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.case_id,
            "category": self.category,
            "expected": self.expected,
            "expected_intercept": self.expected_intercept,
            "action": self.action.value if self.action else None,
            "intercepted": self.intercepted,
            "score": round(self.score, 4),
            "latency_ms": round(self.latency_ms, 1),
            "detectors": [r.detector for r in self.results],
            "rule_name": self.rule_name,
        }


def _is_intercept(action: PolicyAction) -> bool:
    return action in INTERCEPT_ACTIONS


def _expected_intercept(expected: str) -> bool:
    """期望动作是否算"拦截"：allow 放行，block/redact 均拦截。"""
    return expected != "allow"


class EvalRunner:
    """对数据集跑分。用 ``pipeline`` 或 ``detector`` 构造（二选一）。"""

    def __init__(
        self,
        target: EvalTarget,
        *,
        stage: GuardStage = GuardStage.INPUT,
        detector_threshold: float = 0.5,
    ) -> None:
        """Args:
        target: GuardPipeline（回归跑分）或 DetectorPort（阈值校准）。
        stage: 检测点。默认 INPUT（红队集主要是输入注入）。
        detector_threshold: detector 模式判定拦截的阈值（score >= t）。
        """
        self._target = target
        self._stage = stage
        self._threshold = detector_threshold
        if isinstance(target, GuardPipeline):
            self._mode = "pipeline"
        elif isinstance(target, DetectorPort):  # runtime_checkable Protocol
            self._mode = "detector"
        else:
            raise TypeError(f"不支持的评估目标: {type(target)!r}")

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def target_name(self) -> str:
        if self._mode == "pipeline":
            return "guard-pipeline"
        return getattr(self._target, "name", type(self._target).__name__)

    def run(self, dataset: EvalDataset) -> list[CaseVerdict]:
        """对数据集全部 **enabled** 样本跑分（disabled 样本不参与指标）。"""
        verdicts: list[CaseVerdict] = []
        for case in dataset.cases:
            if not case.enabled:
                continue
            verdicts.append(self._run_one(case))
        return verdicts

    def _run_one(self, case: EvalCase) -> CaseVerdict:
        start = time.perf_counter()
        if self._mode == "pipeline":
            decision = self._run_pipeline(case)
            latency = (time.perf_counter() - start) * 1000.0
            return _verdict_from_decision(case, decision, latency)
        return self._run_detector(case, start)

    def _run_pipeline(self, case: EvalCase) -> Decision:
        pipeline = self._target  # type: GuardPipeline
        return pipeline.run(case.text, stage=self._stage)

    def _run_detector(self, case: EvalCase, start: float) -> CaseVerdict:
        detector = self._target  # type: DetectorPort
        results = detector.detect(case.text, stage=self._stage)
        latency = (time.perf_counter() - start) * 1000.0
        score = max((r.score for r in results), default=0.0)
        intercepted = score >= self._threshold
        return CaseVerdict(
            case_id=case.id,
            category=case.category,
            expected=case.expected,
            expected_intercept=_expected_intercept(case.expected),
            action=None,
            intercepted=intercepted,
            score=score,
            results=tuple(results),
            latency_ms=latency,
        )


def _verdict_from_decision(case: EvalCase, decision: Decision, latency_ms: float) -> CaseVerdict:
    return CaseVerdict(
        case_id=case.id,
        category=case.category,
        expected=case.expected,
        expected_intercept=_expected_intercept(case.expected),
        action=decision.action,
        intercepted=_is_intercept(decision.action),
        score=max((r.score for r in decision.results), default=0.0),
        results=decision.results,
        latency_ms=latency_ms,
        rule_name=decision.rule_name,
    )


__all__ = [
    "INTERCEPT_ACTIONS",
    "CaseVerdict",
    "EvalRunner",
    "EvalTarget",
]
