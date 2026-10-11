"""基于正则的脱敏器。

复用 :mod:`detector_rules.pii` 里已经校验过的 PII 规则（身份证校验位、
银行卡 Luhn），因此**脱敏与检测用的是同一套判定** —— 不会出现"检测器
判定要脱敏、脱敏器却没找到"这种前后不一致的漏洞。

实现要点：从右往左替换。所有片段都按起始位置从后往前替换，前面的替换
不会影响后面片段的偏移量；若从左往右替换，同一实体多次出现时后续偏移
会全部错位。
"""

from __future__ import annotations

from collections.abc import Sequence

from detector_rules.base import RuleSpec
from detector_rules.pii import PII_RULES
from policy_engine.redactor import (
    Redaction,
    RedactionResult,
    apply_redactions,
    placeholder_for,
)

__all__ = ["RegexRedactor", "placeholder_for"]


class RegexRedactor:
    """用一组规则做脱敏。"""

    name = "regex-redactor"

    def __init__(self, rules: Sequence[RuleSpec] = PII_RULES) -> None:
        if not rules:
            raise ValueError("脱敏器至少需要一条规则")
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[RuleSpec, ...]:
        return self._rules

    def redact(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text=text)

        # (start, end, kind)
        spans: list[tuple[int, int, str]] = []
        for rule in self._rules:
            for match in rule.pattern.finditer(text):
                if rule.validator is not None and not rule.validator(match):
                    continue
                spans.append((match.start(), match.end(), rule.kind_or_label))

        if not spans:
            return RedactionResult(text=text)

        # 重叠与替换逻辑共用 policy_engine 的实现 —— 正则脱敏与决策驱动的
        # 区间脱敏必须在"重叠取最长、从右往左替换"上完全一致，否则同一个
        # 实体在两条路径下会得到不同的遮盖范围。
        return apply_redactions(
            text,
            [Redaction(kind=kind, start=start, end=end) for start, end, kind in spans],
        )
