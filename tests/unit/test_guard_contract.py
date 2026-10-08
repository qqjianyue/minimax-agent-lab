"""C2 guard-contract · 枚举与 DetectorResult 单元测试。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from guard_contract.enums import (
    DEFAULT_FAIL_MODE,
    FailMode,
    GuardStage,
    PolicyAction,
    Severity,
)
from guard_contract.result import DetectorResult, decision_record
from tests.fakes import make_result


class TestPolicyActionSemantics:
    @pytest.mark.parametrize(
        "action, is_dataflow",
        [
            (PolicyAction.ALLOW, False),
            (PolicyAction.ESCALATE, False),
            (PolicyAction.REDACT, True),
            (PolicyAction.BLOCK, True),
            (PolicyAction.REWRITE, True),
            (PolicyAction.REQUIRE_APPROVAL, True),
        ],
    )
    def test_dataflow_distinction(self, action: PolicyAction, is_dataflow: bool) -> None:
        """架构方案的核心区分：拦截改数据流，审计不改。

        ALLOW/ESCALATE 不改写或截断内容，其余四种会 —— 这条属性
        决定了该动作必须放在管道哪一侧。
        """
        assert action.is_dataflow_action is is_dataflow

    @pytest.mark.parametrize(
        "action, is_terminal",
        [
            (PolicyAction.ALLOW, True),
            (PolicyAction.BLOCK, True),
            (PolicyAction.REDACT, True),
            (PolicyAction.REWRITE, False),
            (PolicyAction.ESCALATE, False),
            (PolicyAction.REQUIRE_APPROVAL, False),
        ],
    )
    def test_terminal_distinction(self, action: PolicyAction, is_terminal: bool) -> None:
        """REWRITE 会再走一轮生成，ESCALATE/REQUIRE_APPROVAL 会挂起等人。"""
        assert action.is_terminal is is_terminal

    def test_str_value_roundtrip(self) -> None:
        for action in PolicyAction:
            assert str(action) == action.value
            assert PolicyAction(action.value) is action

    def test_parse_rejects_unknown_with_helpful_message(self) -> None:
        from agent_core.errors import ConfigurationError

        with pytest.raises(ConfigurationError) as exc:
            PolicyAction.parse("obliterate")
        message = str(exc.value)
        assert "obliterate" in message
        # 错误信息要能直接告诉运营可选项是什么
        assert "require_approval" in message

    def test_all_six_actions_defined(self) -> None:
        """与架构方案 §5.6 的动作集合保持一致，缺一个都是行为变更。"""
        assert {a.value for a in PolicyAction} == {
            "allow",
            "redact",
            "block",
            "rewrite",
            "escalate",
            "require_approval",
        }


class TestOtherEnums:
    def test_stages_cover_architecture_detection_points(self) -> None:
        assert {s.value for s in GuardStage} == {"input", "retrieval", "tool", "output"}

    def test_default_fail_mode_is_closed_for_banking(self) -> None:
        """银行场景默认 fail-closed：误杀成本低于漏检成本。"""
        assert DEFAULT_FAIL_MODE is FailMode.CLOSED

    def test_severity_levels(self) -> None:
        assert [s.value for s in Severity] == ["info", "low", "medium", "high", "critical"]


class TestDetectorResultValidation:
    def test_accepts_well_formed_result(self) -> None:
        result = make_result(label="prompt_injection", score=0.95, evidence=["ignore previous"])
        assert result.score == 0.95
        assert result.evidence == ("ignore previous",)

    @pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
    def test_score_boundaries_inclusive(self, score: float) -> None:
        assert make_result(score=score).score == score

    @pytest.mark.parametrize("score", [-0.01, 1.01, 2.0])
    def test_score_out_of_range_rejected(self, score: float) -> None:
        with pytest.raises(ValidationError):
            make_result(score=score)

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_result(confidence=1.5)

    def test_negative_latency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_result(latency_ms=-0.1)

    @pytest.mark.parametrize("field", ["detector", "version", "label"])
    def test_blank_identifier_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            make_result(**{field: "   "})

    def test_unknown_field_rejected(self) -> None:
        """契约演进时新增字段必须显式声明，避免拼写错误被静默吞掉。"""
        payload = make_result().to_audit_record()
        payload["scroe"] = 0.9  # 故意拼错
        with pytest.raises(ValidationError):
            DetectorResult.from_audit_record(payload)

    def test_result_is_frozen(self) -> None:
        """审计记录一旦生成就不能被下游改写 —— append-only 的前提。"""
        result = make_result()
        with pytest.raises(ValidationError):
            result.score = 0.99  # type: ignore[misc]

    def test_evidence_default_is_empty(self) -> None:
        assert make_result().evidence == ()


class TestSerialization:
    def test_audit_record_is_json_serializable(self) -> None:
        result = make_result(
            label="pii_leak",
            score=0.8,
            evidence=("身份证 110101...",),
            metadata={"pii_types": ["CN_ID"]},
        )
        record = result.to_audit_record()
        # tuple / Mapping 转成 JSON 原生类型，否则审计落盘会炸
        assert isinstance(record["evidence"], list)
        assert isinstance(record["metadata"], dict)
        json.dumps(record)  # 不抛异常即通过

    def test_roundtrip_preserves_all_fields(self) -> None:
        original = make_result(
            detector="presidio.l2",
            version="2.2.364",
            stage=GuardStage.OUTPUT,
            label="pii_leak",
            score=0.77,
            confidence=0.91,
            evidence=("phone: 138****",),
            latency_ms=42.5,
            metadata={"count": 1},
        )
        restored = DetectorResult.from_audit_record(
            json.loads(json.dumps(original.to_audit_record()))
        )
        assert restored == original

    def test_stage_serializes_as_plain_string(self) -> None:
        assert make_result(stage=GuardStage.TOOL).to_audit_record()["stage"] == "tool"

    def test_is_flagged_uses_label(self) -> None:
        assert make_result(label="safe").is_flagged is False
        assert make_result(label="SAFE").is_flagged is False
        assert make_result(label="prompt_injection").is_flagged is True

    def test_is_flagged_does_not_consult_score(self) -> None:
        """高 score 但标签为 safe 时仍不算 flag —— 阈值判断属于 policy engine。"""
        assert make_result(label="safe", score=0.99).is_flagged is False

    def test_summary_is_single_line_and_informative(self) -> None:
        text = make_result(label="prompt_injection", score=0.93).summary()
        assert "\n" not in text
        assert "rules.l1" in text
        assert "prompt_injection" in text


class TestDecisionRecord:
    def test_record_carries_full_audit_context(self) -> None:
        record = decision_record(
            request_id="req-1",
            use_case="bank-assistant-demo",
            tenant="acme-bank",
            results=(make_result(label="pii_leak", score=0.9), make_result(detector="judge.l4")),
            action=PolicyAction.REDACT,
            reason="命中 PII，替换为占位符后放行",
        )
        assert record["request_id"] == "req-1"
        assert record["tenant"] == "acme-bank"
        assert record["action"] == "redact"
        assert len(record["detector_results"]) == 2
        json.dumps(record)

    def test_fail_mode_only_present_on_degraded_path(self) -> None:
        normal = decision_record(
            request_id="r",
            use_case="u",
            tenant="t",
            results=(make_result(),),
            action=PolicyAction.ALLOW,
            reason="ok",
        )
        degraded = decision_record(
            request_id="r",
            use_case="u",
            tenant="t",
            results=(),
            action=PolicyAction.BLOCK,
            reason="detector timeout",
            fail_mode="closed",
        )
        assert normal["fail_mode"] is None
        assert degraded["fail_mode"] == "closed"
