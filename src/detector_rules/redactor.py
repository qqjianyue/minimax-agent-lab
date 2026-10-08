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
from policy_engine.redactor import Redaction, RedactionResult


def placeholder_for(kind: str) -> str:
    return f"[REDACTED:{kind}]"


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

        # 重叠片段：同一段文字被多条规则命中时只脱敏一次，保留最长的那个
        spans = _dedupe_overlaps(spans)

        # 从右往左替换，保证左侧片段的偏移量不受影响
        result = text
        for start, end, kind in sorted(spans, key=lambda s: s[0], reverse=True):
            result = result[:start] + placeholder_for(kind) + result[end:]

        redactions = tuple(
            Redaction(kind=kind, start=start, end=end)
            for start, end, kind in sorted(spans, key=lambda s: s[0])
        )
        return RedactionResult(text=result, redactions=redactions)


def _dedupe_overlaps(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """去掉重叠片段，保留覆盖范围最大的那个。"""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
    kept: list[tuple[int, int, str]] = [ordered[0]]
    for start, end, kind in ordered[1:]:
        last_start, last_end, _ = kept[-1]
        if start < last_end:
            # 重叠：若当前片段更长则替换掉上一个
            if (end - start) > (last_end - last_start):
                kept[-1] = (start, end, kind)
            continue
        kept.append((start, end, kind))
    return kept


__all__ = ["RegexRedactor", "placeholder_for"]
