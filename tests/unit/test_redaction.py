"""C3 动作执行 + C4 脱敏器单元测试。

脱敏的测试重点是**位置正确性**：从左往右替换会让同类型实体多次出现时
后面几处偏移全部错位，这是这类实现最常见的 bug。
"""

from __future__ import annotations

import pytest

from detector_rules.redactor import RegexRedactor, placeholder_for
from guard_contract.enums import GuardStage, PolicyAction
from guard_contract.result import DetectorResult
from policy_engine.actions import BLOCKED_MESSAGE, apply_action
from policy_engine.redactor import (
    Redaction,
    RedactionResult,
    Redactor,
    apply_redactions,
    redactions_from_results,
)

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


# --- 决策驱动脱敏（B4 实测漏洞的回归） ---------------------------------------
#
# 这组用例对应的是一个**真实漏洞**：Presidio 判出 PERSON、policy 判 redact，
# 但执行脱敏的 RegexRedactor 只认 C4 那 4 条正则，于是 action=redact 而
# changed=False，原文原样进了模型又原样返回。
#
# 下面 test_redact_masks_entity_the_regex_redactor_cannot_see 就是当初该写、
# 却因为"只测了 RegexRedactor 自己"而漏掉的那一条。


def _pii_result(*, start: int, end: int, entity: str = "PERSON", detector: str = "pii.presidio"):
    return DetectorResult(
        detector=detector,
        version="2.2.358",
        stage=GuardStage.INPUT,
        label="pii_leak",
        score=0.85,
        confidence=0.85,
        evidence=(f"{entity}:{entity[:1]}***",),
        metadata={"entity": entity, "start": start, "end": end},
    )


class TestRedactionsFromResults:
    def test_extracts_span_and_entity_kind(self) -> None:
        spans = redactions_from_results([_pii_result(start=4, end=6)])
        assert len(spans) == 1
        assert (spans[0].start, spans[0].end, spans[0].kind) == (4, 6, "PERSON")

    def test_falls_back_to_label_when_entity_absent(self) -> None:
        result = _pii_result(start=0, end=3).model_copy(
            update={"metadata": {"start": 0, "end": 3}},
        )
        assert redactions_from_results([result])[0].kind == "pii_leak"

    @pytest.mark.parametrize(
        "metadata",
        [
            pytest.param({"entity": "PERSON"}, id="缺end"),
            pytest.param({"entity": "PERSON", "start": 2}, id="缺start"),
            pytest.param({"entity": "P", "start": 5, "end": 2}, id="区间倒置"),
            pytest.param({"entity": "P", "start": -1, "end": 4}, id="起点为负"),
            pytest.param({"entity": "P", "start": 3, "end": 3}, id="空区间"),
            pytest.param({"entity": "P", "start": "1", "end": "4"}, id="字符串偏移"),
            pytest.param({"entity": "P", "start": True, "end": 4}, id="bool偏移"),
        ],
    )
    def test_malformed_spans_are_skipped(self, metadata: dict) -> None:
        """宁可少遮也不能错位：偏移量非法时跳过，别拿它去切片。

        用错位的偏移量切片会**静默截断用户内容**，比漏遮更难发现。
        """
        result = _pii_result(start=0, end=1).model_copy(update={"metadata": metadata})
        assert redactions_from_results([result]) == ()

    def test_empty_results_yield_nothing(self) -> None:
        assert redactions_from_results([]) == ()


class TestDecisionDrivenRedaction:
    def test_redact_masks_entity_the_regex_redactor_cannot_see(
        self, redactor: RegexRedactor
    ) -> None:
        """**这条用例对应真实漏洞**：中文人名只有 Presidio 能认，
        RegexRedactor 完全看不见它。修复前这里会 changed=False。
        """
        text = "请帮我确认客户张伟的账户信息"
        name_start, name_end = 7, 9
        assert text[name_start:name_end] == "张伟"

        outcome = apply_action(
            PolicyAction.REDACT,
            text,
            redactor=redactor,
            results=[_pii_result(start=name_start, end=name_end)],
        )

        assert outcome.changed is True
        assert "张伟" not in outcome.text
        assert placeholder_for("PERSON") in outcome.text
        # 不该把整段打码 —— 精确到实体
        assert "请帮我确认客户" in outcome.text

    def test_regex_and_detector_spans_are_merged(self, redactor: RegexRedactor) -> None:
        """C4 正则与 Presidio 同时命中：两处都要遮。"""
        text = f"手机 {VALID_CN_ID} 客户张伟"
        idx = text.index("张伟")
        outcome = apply_action(
            PolicyAction.REDACT,
            text,
            redactor=redactor,
            results=[_pii_result(start=idx, end=idx + 2)],
        )
        assert VALID_CN_ID not in outcome.text
        assert "张伟" not in outcome.text

    def test_overlapping_regex_and_detector_spans_do_not_double_mask(
        self, redactor: RegexRedactor
    ) -> None:
        """同一段文字被两条路径命中：只遮一次，且不留嵌套占位符。"""
        text = f"身份证 {VALID_CN_ID}"
        start = text.index(VALID_CN_ID)
        outcome = apply_action(
            PolicyAction.REDACT,
            text,
            redactor=redactor,
            results=[_pii_result(start=start, end=start + len(VALID_CN_ID), entity="CN_ID")],
        )
        assert outcome.text.count(placeholder_for("CN_ID")) == 1
        assert VALID_CN_ID not in outcome.text

    def test_results_are_ignored_for_non_redact_actions(
        self, redactor: RegexRedactor
    ) -> None:
        """allow 不该因为带了 results 就去改文本。"""
        text = "请帮我确认客户张伟的账户信息"
        outcome = apply_action(
            PolicyAction.ALLOW, text, redactor=redactor, results=[_pii_result(start=7, end=9)]
        )
        assert outcome.text == text
        assert outcome.changed is False

    def test_redact_with_no_results_keeps_regex_behaviour(
        self, redactor: RegexRedactor
    ) -> None:
        """不传 results 时行为与修复前一致 —— C4 正则照常工作。"""
        outcome = apply_action(PolicyAction.REDACT, f"身份证 {VALID_CN_ID}", redactor=redactor)
        assert VALID_CN_ID not in outcome.text


class TestApplyRedactions:
    def test_multiple_replacements_keep_offsets_aligned(self) -> None:
        text = "张伟和李娜都是客户"
        # 张伟 = [0,2)，李娜 = [3,5)
        result = apply_redactions(
            text,
            [Redaction("PERSON", 0, 2), Redaction("PERSON", 3, 5)],
        )
        assert result.text == f"{placeholder_for('PERSON')}和{placeholder_for('PERSON')}都是客户"

    def test_empty_input_is_a_noop(self) -> None:
        assert apply_redactions("", []).text == ""
        assert apply_redactions("abc", []).text == "abc"
        assert apply_redactions("abc", []).changed is False
