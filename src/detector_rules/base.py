"""L1 规则引擎的基础结构。

设计要点：

* **规则是数据**。每条 :class:`RuleSpec` 声明自己的 name / label / score /
  confidence / 适用阶段，运营可以据此回答"为什么这条被判成 0.9 分"。
  score 与 confidence 分开：前者是"有多危险"，后者是"我有多确定" ——
  两者混为一谈就无法做阈值校准（Q4 里那个 FP/FN 权衡的前提）。

* **校验器是可选的**。PII 类正则必须配校验器（身份证校验位、银行卡 Luhn），
  否则 16-19 位数字会把误报率推到不可用的水平。

* **正则必须保持简单**。本层处理的是**不可信输入**，且 Python 的 ``re``
  没有超时机制。一旦写出嵌套量词（``(a+)+`` 这类）就存在 ReDoS 风险，
  会让"5ms 内完成的 L1 层"变成拖垮整个服务的入口。
  新增规则时禁止嵌套量词、禁止回溯敏感的结构。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from guard_contract.enums import GuardStage

#: 单条规则最多保留多少条证据。避免日志/审计被超长匹配撑爆。
MAX_EVIDENCE = 5

MatchFn = Callable[[re.Match[str]], bool]


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """单条检测规则。"""

    name: str
    label: str
    pattern: re.Pattern[str]
    score: float
    confidence: float
    stages: frozenset[GuardStage] = field(default_factory=lambda: frozenset({GuardStage.INPUT}))
    #: 实体类型（PII 规则用，如 "CN_ID"）。留空则回退到 label。
    #: 脱敏占位符依赖它，因此它属于契约的一部分，改动会让历史审计记录含义漂移。
    kind: str = ""
    #: 可选的二次校验。返回 False 则该次匹配不算命中。
    #: PII 类规则必须提供；纯关键词类规则不需要。
    validator: MatchFn | None = None
    description: str = ""

    @property
    def kind_or_label(self) -> str:
        return self.kind or self.label

    def find(self, text: str) -> tuple[str, ...]:
        """返回所有匹配片段（已过 validator），去重且保持出现顺序。"""
        found: list[str] = []
        for match in self.pattern.finditer(text):
            if self.validator is not None and not self.validator(match):
                continue
            snippet = match.group(0)
            if snippet not in found:
                found.append(snippet)
            if len(found) >= MAX_EVIDENCE:
                break
        return tuple(found)

    def matches(self, text: str) -> bool:
        return bool(self.find(text))


@dataclass(frozen=True, slots=True)
class RuleHit:
    """一次规则命中。"""

    rule: RuleSpec
    evidence: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.rule.label

    @property
    def score(self) -> float:
        return self.rule.score

    @property
    def confidence(self) -> float:
        return self.rule.confidence


@dataclass(frozen=True, slots=True)
class RuleSet:
    """一组规则，按名称版本化管理。"""

    version: str
    rules: tuple[RuleSpec, ...]

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError("规则集不能为空")
        names = [r.name for r in self.rules]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"规则名重复: {sorted(duplicates)}")

    @property
    def stages(self) -> frozenset[GuardStage]:
        """本规则集覆盖的阶段。"""
        out: set[GuardStage] = set()
        for rule in self.rules:
            out |= rule.stages
        return frozenset(out)

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(r.label for r in self.rules)

    def rules_for(self, stage: GuardStage) -> tuple[RuleSpec, ...]:
        return tuple(r for r in self.rules if stage in r.stages)

    def scan(self, text: str, *, stage: GuardStage) -> tuple[RuleHit, ...]:
        """单遍扫描，返回全部命中。"""
        hits: list[RuleHit] = []
        for rule in self.rules_for(stage):
            evidence = rule.find(text)
            if evidence:
                hits.append(RuleHit(rule=rule, evidence=evidence))
        return tuple(hits)

    def names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.rules)


def build_rules(
    rows: Sequence[tuple[str, str, str, float, float, frozenset[GuardStage]]],
) -> tuple[RuleSpec, ...]:
    """从紧凑声明构建规则。

    Args:
        rows: ``(name, regex, label, score, confidence, stages)`` 元组序列。
    """
    out = []
    for name, regex, label, score, confidence, stages in rows:
        out.append(
            RuleSpec(
                name=name,
                label=label,
                pattern=re.compile(regex, re.IGNORECASE),
                score=score,
                confidence=confidence,
                stages=stages,
            )
        )
    return tuple(out)


__all__ = ["MAX_EVIDENCE", "MatchFn", "RuleHit", "RuleSet", "RuleSpec", "build_rules"]
