"""L1 规则检测器：把关键词规则与 PII 规则合成一个 :class:`DetectorPort`。"""

from __future__ import annotations

import time

from detector_rules.base import RuleSet, RuleSpec
from detector_rules.patterns import KEYWORD_RULES
from detector_rules.pii import PII_RULES
from guard_contract.enums import GuardStage
from guard_contract.result import DetectorResult

#: L1 规则集版本。改动任何一条规则都必须同步 bump —— 审计记录要能回答
#: "上周还在用 l1 v1.0，误报降了多少"这类问题（对应契约里的 version 字段）。
L1_RULES_VERSION = "1.0.0"


def build_l1_rule_set(extra: tuple[RuleSpec, ...] = ()) -> RuleSet:
    """构建 L1 规则集。可传入额外规则（测试或按租户定制）。"""
    return RuleSet(version=L1_RULES_VERSION, rules=KEYWORD_RULES + PII_RULES + extra)


class RulesL1Detector:
    """基于正则的快速检测器。

    延迟预算：架构方案要求 L1 < 10ms。全部规则是一次线性扫描，
    实测在中位数机器上远低于该预算，但这一点由 L3 功能测试中的
    p50/p95 指标在目标机上确认，而不是在此处假设。
    """

    name = "rules.l1"

    def __init__(self, rule_set: RuleSet | None = None) -> None:
        self._rules = rule_set if rule_set is not None else build_l1_rule_set()

    @property
    def version(self) -> str:
        return self._rules.version

    @property
    def rule_set(self) -> RuleSet:
        return self._rules

    @property
    def supported_stages(self) -> frozenset[GuardStage]:
        return self._rules.stages

    def supports(self, stage: GuardStage) -> bool:
        return stage in self._rules.stages

    def detect(self, text: str, *, stage: GuardStage) -> tuple[DetectorResult, ...]:
        """扫描文本，返回全部命中。

        单遍扫描后，所有命中共享同一个扫描耗时 —— 它们确实是在同一次扫描中
        被发现的，不应伪造出"每条规则各自耗时"的假象。
        """
        if not text:
            return ()

        started = time.perf_counter()
        hits = self._rules.scan(text, stage=stage)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        return tuple(
            DetectorResult(
                detector=self.name,
                version=self._rules.version,
                stage=stage,
                label=hit.label,
                score=hit.score,
                confidence=hit.confidence,
                evidence=hit.evidence,
                latency_ms=elapsed_ms,
                metadata={"rule": hit.rule.name},
            )
            for hit in hits
        )


__all__ = ["L1_RULES_VERSION", "RulesL1Detector", "build_l1_rule_set"]
