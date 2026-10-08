"""C3 policy-engine · 决策与求值单元测试。

策略层是整个安全体系的判定核心，因此这里的测试策略是**穷举分支**而不是
抽样：fail_mode 的 3 个取值 × 有无失败 × 有无命中 × 降级后是否还有可用
检测器，每一种组合都要有一个明确的期望值。
"""

from __future__ import annotations

import json

import pytest

from guard_contract.enums import FailMode, GuardStage, PolicyAction
from guard_contract.policy_schema import MatchCondition, PolicyRule, PolicySet
from guard_contract.port import DetectorFailure, DetectorFailureReason
from policy_engine import Decision, PolicyEngine
from tests.fakes import make_result


def make_policy(**overrides) -> PolicySet:
    defaults = {
        "version": "1.0.0",
        "use_case": "test",
        "tenant": "t1",
        "rules": (
            PolicyRule(
                name="block_high",
                action=PolicyAction.BLOCK,
                priority=10,
                condition=MatchCondition(min_score=0.9),
                reason="高危",
            ),
            PolicyRule(
                name="escalate_mid",
                action=PolicyAction.ESCALATE,
                priority=20,
                condition=MatchCondition(min_score=0.6, min_confidence=0.5),
                reason="中危",
            ),
        ),
        "degraded_detectors": frozenset({"rules.l1"}),
    }
    return PolicySet(**{**defaults, **overrides})


def failure(name: str = "judge.l4", reason: DetectorFailureReason = DetectorFailureReason.TIMEOUT):
    return DetectorFailure(detector=name, reason=reason, detail="boom")


def evaluate(policy: PolicySet, results=(), failures=(), attempted=()):
    return PolicyEngine(policy).evaluate(
        request_id="req-1",
        stage=GuardStage.INPUT,
        results=tuple(results),
        failures=tuple(failures),
        attempted_detectors=tuple(attempted),
    )


class TestRuleEvaluation:
    def test_highest_priority_rule_wins(self) -> None:
        """同时命中 block_high 与 escalate_mid 时，优先级小者（数字小）胜出。"""
        decision = evaluate(
            make_policy(),
            [make_result(score=0.95), make_result(detector="judge.l4", score=0.7, confidence=0.8)],
        )
        assert decision.action is PolicyAction.BLOCK
        assert decision.rule_name == "block_high"
        assert decision.matched is True

    def test_lower_priority_rule_used_when_top_misses(self) -> None:
        decision = evaluate(make_policy(), [make_result(score=0.7, confidence=0.8)])
        assert decision.action is PolicyAction.ESCALATE
        assert decision.rule_name == "escalate_mid"

    def test_reason_comes_from_rule(self) -> None:
        assert evaluate(make_policy(), [make_result(score=0.95)]).reason == "高危"

    def test_result_order_does_not_change_outcome(self) -> None:
        """求值顺序只由优先级决定，与传入顺序无关。"""
        a = make_result(detector="x", score=0.7, confidence=0.8)
        b = make_result(detector="y", score=0.95, confidence=0.8)
        forward = evaluate(make_policy(), [a, b])
        backward = evaluate(make_policy(), [b, a])
        assert forward.action == backward.action == PolicyAction.BLOCK

    def test_disabled_rules_are_skipped(self) -> None:
        rules = (
            PolicyRule(
                name="block_high",
                action=PolicyAction.BLOCK,
                priority=10,
                condition=MatchCondition(min_score=0.9),
                enabled=False,
            ),
            PolicyRule(
                name="allow_benign",
                action=PolicyAction.ALLOW,
                priority=999,
                condition=MatchCondition(min_score=0.0, labels=frozenset({"safe"})),
            ),
        )
        decision = evaluate(make_policy(rules=rules), [make_result(label="safe", score=0.95)])
        assert decision.action is PolicyAction.ALLOW
        assert decision.rule_name == "allow_benign"

    def test_considered_detectors_deduped(self) -> None:
        decision = evaluate(
            make_policy(),
            [
                make_result(detector="rules.l1", score=0.95),
                make_result(detector="rules.l1", score=0.4),
            ],
        )
        assert decision.considered_detectors == ("rules.l1",)

    def test_policy_metadata_propagated(self) -> None:
        decision = evaluate(make_policy(), [make_result(score=0.95)])
        assert decision.policy_version == "1.0.0"
        assert decision.use_case == "test"
        assert decision.tenant == "t1"
        assert decision.stage is GuardStage.INPUT


class TestDefaultAction:
    def test_no_match_falls_back_to_default_action(self) -> None:
        rules = (
            PolicyRule(
                name="block_high",
                action=PolicyAction.BLOCK,
                priority=10,
                condition=MatchCondition(min_score=0.9),
            ),
        )
        decision = evaluate(make_policy(rules=rules), [make_result(score=0.1)])
        assert decision.action is PolicyAction.ALLOW
        assert decision.matched is False
        assert decision.rule_name is None

    def test_default_action_can_be_block_for_high_security_use_case(self) -> None:
        """高安全场景：没匹配到规则 ≠ 确认安全，应当阻断。"""
        rules = (
            PolicyRule(
                name="escalate_any",
                action=PolicyAction.ESCALATE,
                priority=10,
                condition=MatchCondition(min_score=0.0),
            ),
        )
        decision = evaluate(make_policy(rules=rules, default_action=PolicyAction.BLOCK), ())
        assert decision.action is PolicyAction.BLOCK
        assert decision.matched is False

    def test_default_action_and_rule_may_share_action_but_stay_distinguishable(self) -> None:
        """兜底与规则命中可以同为 BLOCK，但审计必须能区分是哪条生效。

        ``matched`` 与 ``rule_name`` 就是用来做这个区分的 —— 所以配置上
        不应该禁止两者动作相同，那只会逼着运营写出更别扭的配置。
        """
        rules = (
            PolicyRule(
                name="block_high",
                action=PolicyAction.BLOCK,
                priority=10,
                condition=MatchCondition(min_score=0.9),
            ),
        )
        policy = make_policy(rules=rules, default_action=PolicyAction.BLOCK)
        by_rule = evaluate(policy, [make_result(score=0.95)])
        by_default = evaluate(policy, ())

        assert by_rule.action is by_default.action is PolicyAction.BLOCK
        assert by_rule.matched is True and by_rule.rule_name == "block_high"
        assert by_default.matched is False and by_default.rule_name is None


class TestTrivialCatchAllRejected:
    def test_catch_all_allow_rule_rejected(self) -> None:
        """这类规则会让 fail_mode 永远失效，必须在加载时就拦下。"""
        rules = (
            PolicyRule(
                name="allow_everything",
                action=PolicyAction.ALLOW,
                priority=999,
                condition=MatchCondition(min_score=0.0),
            ),
        )
        with pytest.raises(Exception, match="fail_mode 永远失效"):
            make_policy(rules=rules)

    def test_constrained_allow_rule_is_fine(self) -> None:
        """带额外约束的放行规则是合法的，不应被误伤。"""
        rules = (
            PolicyRule(
                name="allow_safe_label",
                action=PolicyAction.ALLOW,
                priority=999,
                condition=MatchCondition(min_score=0.0, labels=frozenset({"safe"})),
            ),
        )
        assert make_policy(rules=rules).rules[0].name == "allow_safe_label"

    def test_catch_all_rejected_for_non_allow_actions(self) -> None:
        """只有 ALLOW 才有问题：全匹配后放行会掩盖检测缺失。"""
        rules = (
            PolicyRule(
                name="escalate_everything",
                action=PolicyAction.ESCALATE,
                priority=10,
                condition=MatchCondition(min_score=0.0),
            ),
        )
        assert make_policy(rules=rules).rules[0].action is PolicyAction.ESCALATE


class TestFailMode:
    def test_closed_blocks_when_unable_to_decide(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.CLOSED), [make_result(score=0.1)], [failure()]
        )
        assert decision.action is PolicyAction.BLOCK
        assert decision.degraded is True
        assert decision.fail_mode is FailMode.CLOSED
        assert "fail-closed" in decision.reason

    def test_open_allows_when_unable_to_decide(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.OPEN), [make_result(score=0.1)], [failure()]
        )
        assert decision.action is PolicyAction.ALLOW
        assert decision.fail_mode is FailMode.OPEN

    def test_positive_match_beats_fail_mode(self) -> None:
        """核心设计：检测器正常给出的信号，不该被"另一个检测器挂了"抹掉。

        这里的输入是"可用检测器判出高危 + 另一个检测器失败"。
        若实现成 fail-closed 一票否决，系统在任一检测器偶发故障时就完全不可用。
        """
        decision = evaluate(
            make_policy(fail_mode=FailMode.CLOSED), [make_result(score=0.95)], [failure()]
        )
        assert decision.action is PolicyAction.BLOCK
        assert decision.rule_name == "block_high"
        assert decision.matched is True
        # 失败事实仍被记录，审计不会丢失
        assert decision.fail_mode is FailMode.CLOSED
        assert len(decision.failed_detectors) == 1

    def test_no_failures_leaves_fail_mode_unset(self) -> None:
        decision = evaluate(make_policy(fail_mode=FailMode.CLOSED), [make_result(score=0.95)])
        assert decision.fail_mode is None
        assert decision.degraded is False


class TestDegradedMode:
    def test_untrusted_detector_results_are_dropped(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED),
            [make_result(detector="judge.l4", score=0.95)],
            [failure()],
        )
        assert decision.ignored_detectors == ("judge.l4",)
        assert decision.results == ()

    def test_trusted_detector_survives_and_can_block(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED),
            [
                make_result(detector="rules.l1", score=0.95),
                make_result(detector="judge.l4", score=0.95),
            ],
            [failure()],
        )
        assert decision.action is PolicyAction.BLOCK
        assert decision.ignored_detectors == ("judge.l4",)
        assert decision.considered_detectors == ("rules.l1",)

    def test_total_outage_blocks_even_in_degraded_mode(self) -> None:
        """降级后一个可用检测器都不剩 —— 这不是降级，是彻底不可用。"""
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED),
            [make_result(detector="judge.l4", score=0.1)],
            [failure()],
        )
        assert decision.action is PolicyAction.BLOCK
        assert "均不可用" in decision.reason

    def test_degraded_without_configured_trusted_detectors_is_outage(self) -> None:
        """未配置 degraded_detectors 时不得悄悄退化成放行。"""
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED, degraded_detectors=None),
            [make_result(detector="rules.l1", score=0.1)],
            [failure()],
        )
        assert decision.action is PolicyAction.BLOCK

    def test_trusts_in_degraded_mode_is_false_by_default(self) -> None:
        assert make_policy(degraded_detectors=None).trusts_in_degraded_mode("rules.l1") is False
        assert make_policy().trusts_in_degraded_mode("rules.l1") is True
        assert make_policy().trusts_in_degraded_mode("judge.l4") is False

    def test_successful_but_untrusted_detector_is_not_an_outage(self) -> None:
        """L1 跑完了、结论干净，L4 挂了 —— 这是一次**成功的降级检查**，不是不可用。

        回归用例：曾经因为引擎拿不到"谁成功执行过"这个信息，把这两种状态混为一谈，
        结果每次 L4 故障 + L1 判定安全都变成无故阻断，降级机制反成了阻断机制。
        """
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED),
            results=(),
            failures=[failure()],
            attempted=("rules.l1", "judge.l4"),
        )
        assert decision.action is PolicyAction.ALLOW
        assert decision.fail_mode is FailMode.DEGRADED
        assert "已降级" in decision.reason

    def test_all_detectors_failed_is_outage(self) -> None:
        """对照用例：没有任何检测器成功执行才是真正的不可用。"""
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED),
            results=(),
            failures=[failure("rules.l1"), failure("judge.l4")],
            attempted=("rules.l1", "judge.l4"),
        )
        assert decision.action is PolicyAction.BLOCK
        assert "均不可用" in decision.reason

    def test_successful_but_untrusted_detector_still_counts_as_outage(self) -> None:
        """白名单外的检测器即使跑成功，其"干净"结论在降级模式下也不作数。"""
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED, degraded_detectors=frozenset({"nonexistent"})),
            results=(),
            failures=[failure()],
            attempted=("judge.l4",),
        )
        assert decision.action is PolicyAction.BLOCK
        assert "均不可用" in decision.reason

    def test_attempted_detectors_recorded_for_audit(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.CLOSED),
            results=[make_result(score=0.95)],
            failures=[failure()],
            attempted=("rules.l1", "judge.l4"),
        )
        assert decision.attempted_detectors == ("rules.l1", "judge.l4")
        assert decision.to_audit_record()["attempted_detectors"] == ["rules.l1", "judge.l4"]

    def test_degraded_with_remaining_results_uses_default_action(self) -> None:
        rules = (
            PolicyRule(
                name="block_high",
                action=PolicyAction.BLOCK,
                priority=10,
                condition=MatchCondition(min_score=0.9),
            ),
        )
        decision = evaluate(
            make_policy(fail_mode=FailMode.DEGRADED, rules=rules),
            [make_result(detector="rules.l1", score=0.1)],
            [failure()],
        )
        assert decision.action is PolicyAction.ALLOW
        assert decision.fail_mode is FailMode.DEGRADED
        assert "已降级" in decision.reason


class TestDecisionProperties:
    @pytest.mark.parametrize(
        "action, human, regen",
        [
            (PolicyAction.ALLOW, False, False),
            (PolicyAction.BLOCK, False, False),
            (PolicyAction.REDACT, False, False),
            (PolicyAction.ESCALATE, True, False),
            (PolicyAction.REQUIRE_APPROVAL, True, False),
            (PolicyAction.REWRITE, False, True),
        ],
    )
    def test_action_properties(self, action, human: bool, regen: bool) -> None:
        decision = Decision(action=action, reason="r", stage=GuardStage.INPUT, request_id="x")
        assert decision.requires_human is human
        assert decision.needs_regeneration is regen

    def test_blank_request_id_rejected(self) -> None:
        with pytest.raises(Exception, match="request_id"):
            Decision(action=PolicyAction.ALLOW, reason="r", stage=GuardStage.INPUT, request_id=" ")


class TestAuditRecord:
    def test_record_is_json_serializable(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.CLOSED), [make_result(score=0.1)], [failure()]
        )
        record = decision.to_audit_record()
        json.dumps(record, ensure_ascii=False)

    def test_record_contains_full_provenance(self) -> None:
        """只看这一条记录就要能回答"为什么这么判"。"""
        decision = evaluate(make_policy(), [make_result(score=0.95, evidence=["x"])])
        record = decision.to_audit_record()
        assert record["action"] == "block"
        assert record["rule_name"] == "block_high"
        assert record["matched"] is True
        assert record["policy_version"] == "1.0.0"
        assert record["stage"] == "input"
        assert record["considered_detectors"] == ["rules.l1"]
        assert record["detector_results"][0]["evidence"] == ["x"]

    def test_failed_detectors_are_recorded_with_reason(self) -> None:
        decision = evaluate(
            make_policy(fail_mode=FailMode.CLOSED), [make_result(score=0.1)], [failure()]
        )
        record = decision.to_audit_record()
        assert record["fail_mode"] == "closed"
        assert record["failed_detectors"][0]["reason"] == "timeout"
        assert record["failed_detectors"][0]["detector"] == "judge.l4"


class TestFailureRecord:
    def test_blank_detector_name_rejected(self) -> None:
        with pytest.raises(Exception, match="检测器名"):
            DetectorFailure(detector=" ", reason=DetectorFailureReason.ERROR)

    def test_roundtrip(self) -> None:
        original = DetectorFailure(detector="d", reason=DetectorFailureReason.ERROR, detail="x")
        assert DetectorFailure.from_audit_record(original.to_audit_record()) == original
