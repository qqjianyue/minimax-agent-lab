"""C3 动作执行 + C4 脱敏器单元测试。

脱敏的测试重点是**位置正确性**：从左往右替换会让同类型实体多次出现时
后面几处偏移全部错位，这是这类实现最常见的 bug。
"""

from __future__ import annotations

import pytest

from detector_rules.redactor import RegexRedactor, placeholder_for
from guard_contract.enums import PolicyAction
from policy_engine.actions import BLOCKED_MESSAGE, apply_action
from policy_engine.redactor import RedactionResult, Redactor

VALID_CN_ID = "11010519491231002X"
VALID_CARD = "4539578763621486"


@pytest.fixture
def redactor() -> RegexRedactor:
    return RegexRedactor()


# --- 脱敏器 -----------------------------------------------------------------
class TestRegexRedactor:
    def test_satisfies_port(self, redactor: RegexRedactor) -> None:
        assert isinstance(redactor, Redactor)

    def test_cn_id_redacted(self, redactor: RegexRedactor) -> None:
        result = redactor.redact(f"我的身份证是 {VALID_CN_ID}")
        assert VALID_CN_ID not in result.text
        assert placeholder_for("CN_ID") in result.text
        assert result.changed is True
        assert result.kinds == ("CN_ID",)

    def test_mobile_redacted(self, redactor: RegexRedactor) -> None:
        result = redactor.redact("手机 13800138000")
        assert "13800138000" not in result.text
        assert result.kinds == ("CN_MOBILE",)

    def test_card_redacted_only_when_luhn_valid(self, redactor: RegexRedactor) -> None:
        ok = redactor.redact(f"卡号 {VALID_CARD}")
        assert VALID_CARD not in ok.text
        # 随机 18 位数字不过 Luhn，不应被脱敏 —— 否则审计噪音巨大
        noise = redactor.redact("订单 123456789012345678")
        assert noise.text == "订单 123456789012345678"
        assert noise.changed is False

    def test_multiple_entities_each_redacted(self, redactor: RegexRedactor) -> None:
        text = f"身份证 {VALID_CN_ID}，手机 13800138000，邮箱 a.b@example.com"
        result = redactor.redact(text)
        assert result.counts() == {"CN_ID": 1, "CN_MOBILE": 1, "EMAIL": 1}
        for secret in (VALID_CN_ID, "13800138000", "a.b@example.com"):
            assert secret not in result.text

    def test_repeated_entities_all_replaced(self, redactor: RegexRedactor) -> None:
        """回归：同一类型出现多次时，每一处都要被替换且不能错位。"""
        text = "13800138000 和 13900139000 都是我的号"
        result = redactor.redact(text)
        assert "13800138000" not in result.text
        assert "13900139000" not in result.text
        assert result.counts() == {"CN_MOBILE": 2}
        # 占位符不能互相吞掉
        assert result.text.count(placeholder_for("CN_MOBILE")) == 2
        assert "都是我的号" in result.text

    def test_benign_text_untouched(self, redactor: RegexRedactor) -> None:
        text = "请问定期存款的利率是多少？"
        result = redactor.redact(text)
        assert result.text == text
        assert result.changed is False
        assert result.summary() == "no redaction"

    def test_empty_text(self, redactor: RegexRedactor) -> None:
        assert redactor.redact("").text == ""

    def test_redaction_spans_are_sorted(self, redactor: RegexRedactor) -> None:
        result = redactor.redact(f"a {VALID_CN_ID} b 13800138000 c")
        starts = [r.start for r in result.redactions]
        assert starts == sorted(starts)

    def test_overlapping_spans_deduped(self) -> None:
        """两条规则命中同一段文字时只脱敏一次，避免出现嵌套占位符。"""
        import re

        from detector_rules.base import RuleSpec

        overlapping = RegexRedactor(
            rules=(
                RuleSpec(
                    name="wide",
                    label="x",
                    kind="WIDE",
                    pattern=re.compile(r"12345678901"),
                    score=0.5,
                    confidence=0.5,
                ),
                RuleSpec(
                    name="narrow",
                    label="x",
                    kind="NARROW",
                    pattern=re.compile(r"5678901"),
                    score=0.5,
                    confidence=0.5,
                ),
            )
        )
        result = overlapping.redact("号码 12345678901 结束")
        assert result.text.count(placeholder_for("WIDE")) == 1
        assert placeholder_for("NARROW") not in result.text

    def test_requires_at_least_one_rule(self) -> None:
        with pytest.raises(ValueError, match="至少需要一条规则"):
            RegexRedactor(rules=())

    def test_summary_counts_by_kind(self, redactor: RegexRedactor) -> None:
        result = redactor.redact("13800138000 13900139000")
        assert result.summary() == "CN_MOBILE x2"


# --- 动作执行 ---------------------------------------------------------------
class TestApplyAction:
    def test_allow_passes_text_through(self, redactor: RegexRedactor) -> None:
        outcome = apply_action(PolicyAction.ALLOW, "hello", redactor=redactor)
        assert outcome.text == "hello"
        assert outcome.changed is False
        assert outcome.await_approval is False

    def test_redact_actually_redacts(self, redactor: RegexRedactor) -> None:
        outcome = apply_action(PolicyAction.REDACT, f"我的号 {VALID_CN_ID}", redactor=redactor)
        assert VALID_CN_ID not in outcome.text
        assert outcome.changed is True
        assert outcome.redactions
        assert "CN_ID" in outcome.redaction_summary

    def test_redact_on_clean_text_is_a_noop(self, redactor: RegexRedactor) -> None:
        outcome = apply_action(PolicyAction.REDACT, "nothing sensitive", redactor=redactor)
        assert outcome.text == "nothing sensitive"
        assert outcome.changed is False

    def test_block_returns_preset_message(self, redactor: RegexRedactor) -> None:
        outcome = apply_action(PolicyAction.BLOCK, "dangerous content", redactor=redactor)
        assert outcome.text == BLOCKED_MESSAGE
        assert "dangerous" not in outcome.text

    def test_block_message_leaks_no_policy_detail(self, redactor: RegexRedactor) -> None:
        """告诉攻击者命中了哪条规则等于提供绕过反馈。"""
        text = BLOCKED_MESSAGE.lower()
        for leak in ("rule", "detector", "injection", "threshold", "score"):
            assert leak not in text

    def test_escalate_marks_for_review(self, redactor: RegexRedactor) -> None:
        outcome = apply_action(PolicyAction.ESCALATE, "answer", redactor=redactor)
        assert outcome.await_approval is True
        assert "人工复核" in outcome.text
        assert "answer" in outcome.text

    def test_require_approval_marks_for_review(self, redactor: RegexRedactor) -> None:
        outcome = apply_action(PolicyAction.REQUIRE_APPROVAL, "answer", redactor=redactor)
        assert outcome.await_approval is True

    def test_rewrite_signals_regeneration_instead_of_silently_passing(
        self, redactor: RegexRedactor
    ) -> None:
        """REWRITE 属于编排层，B6 之前绝不能悄悄当成放行。"""
        outcome = apply_action(PolicyAction.REWRITE, "draft", redactor=redactor)
        assert outcome.needs_regeneration is True
        assert outcome.await_approval is False
        assert outcome.text == "draft"

    def test_all_actions_handled(self, redactor: RegexRedactor) -> None:
        for action in PolicyAction:
            assert apply_action(action, "t", redactor=redactor) is not None


class TestRedactionResult:
    def test_empty_result_reports_no_change(self) -> None:
        result = RedactionResult(text="x")
        assert result.changed is False
        assert result.kinds == ()
        assert result.counts() == {}
