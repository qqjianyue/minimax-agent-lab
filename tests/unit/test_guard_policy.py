"""C2 guard-contract · 策略 schema 单元测试。

重点在**阈值边界**。策略里最容易出事的就是"到底 >= 还是 >"写错一位，
这类 bug 在端到端演示里表现为偶发漏放，非常难查。所以边界值必须穷举。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from guard_contract.enums import FailMode, GuardStage, PolicyAction
from guard_contract.policy_schema import (
    GuardSettings,
    MatchCondition,
    PolicyRule,
    PolicySet,
    policy_set_from_mapping,
)
from tests.fakes import make_result


def rule(**kwargs) -> PolicyRule:
    defaults = {
        "name": "r",
        "action": PolicyAction.BLOCK,
        "condition": MatchCondition(min_score=0.8),
    }
    return PolicyRule(**{**defaults, **kwargs})


class TestMatchCondition:
    def test_empty_condition_rejected(self) -> None:
        """全空条件会匹配所有结果，几乎总是配置事故而非本意。"""
        with pytest.raises(ValidationError, match="至少需要一个约束条件"):
            MatchCondition()

    @pytest.mark.parametrize("threshold", [0.0, 0.5, 0.79, 0.8, 0.81, 1.0])
    def test_score_threshold_is_inclusive(self, threshold: float) -> None:
        """min_score 语义是「大于等于」—— 0.80 的阈值必须命中 0.80 本身。"""
        condition = MatchCondition(min_score=threshold)
        result = make_result(score=threshold)
        assert condition.matches(result) is True

    @pytest.mark.parametrize(
        "score, threshold, expected",
        [
            (0.79, 0.80, False),
            (0.80, 0.80, True),
            (0.81, 0.80, True),
            (0.0, 0.0, True),
            (0.01, 0.0, True),
        ],
    )
    def test_score_boundary_exact(self, score: float, threshold: float, expected: bool) -> None:
        assert MatchCondition(min_score=threshold).matches(make_result(score=score)) is expected

    def test_confidence_threshold_is_inclusive(self) -> None:
        condition = MatchCondition(min_confidence=0.5)
        assert condition.matches(make_result(confidence=0.5)) is True
        assert condition.matches(make_result(confidence=0.49)) is False

    def test_stages_filter(self) -> None:
        condition = MatchCondition(stages=frozenset({GuardStage.INPUT}))
        assert condition.matches(make_result(stage=GuardStage.INPUT)) is True
        assert condition.matches(make_result(stage=GuardStage.OUTPUT)) is False

    def test_labels_filter(self) -> None:
        condition = MatchCondition(labels=frozenset({"prompt_injection", "pii_leak"}))
        assert condition.matches(make_result(label="prompt_injection")) is True
        assert condition.matches(make_result(label="pii_leak")) is True
        assert condition.matches(make_result(label="safe")) is False

    def test_detectors_filter(self) -> None:
        condition = MatchCondition(detectors=frozenset({"judge.l4"}))
        assert condition.matches(make_result(detector="judge.l4")) is True
        assert condition.matches(make_result(detector="rules.l1")) is False

    def test_multiple_constraints_are_anded(self) -> None:
        condition = MatchCondition(
            stages=frozenset({GuardStage.INPUT}),
            labels=frozenset({"prompt_injection"}),
            min_score=0.9,
        )
        assert condition.matches(
            make_result(stage=GuardStage.INPUT, label="prompt_injection", score=0.9)
        )
        # 任一不满足即不命中
        assert not condition.matches(
            make_result(stage=GuardStage.OUTPUT, label="prompt_injection", score=0.9)
        )
        assert not condition.matches(make_result(stage=GuardStage.INPUT, label="safe", score=0.9))
        assert not condition.matches(
            make_result(stage=GuardStage.INPUT, label="prompt_injection", score=0.5)
        )

    @pytest.mark.parametrize("field", ["min_score", "min_confidence"])
    def test_out_of_range_thresholds_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            MatchCondition(**{field: 1.5})

    def test_describe_is_human_readable(self) -> None:
        condition = MatchCondition(
            min_score=0.8,
            stages=frozenset({GuardStage.INPUT, GuardStage.OUTPUT}),
            labels=frozenset({"pii_leak"}),
        )
        text = condition.describe()
        assert "score>=0.8" in text
        assert "input" in text and "output" in text
        assert "pii_leak" in text
        assert " AND " in text


class TestPolicyRule:
    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="规则名不能为空"):
            rule(name="  ")

    def test_defaults(self) -> None:
        r = rule()
        assert r.priority == 100
        assert r.enabled is True
        assert r.reason == ""

    def test_describe_includes_action_and_condition(self) -> None:
        text = rule(name="block_x", action=PolicyAction.BLOCK, priority=5).describe()
        assert "block_x" in text
        assert "-> block" in text
        assert "[5]" in text


class TestPolicySet:
    def test_empty_rules_rejected(self) -> None:
        """没有规则的策略集等于全部放行，必须在加载时就失败。"""
        with pytest.raises(ValidationError, match="策略集不能为空"):
            PolicySet(version="1.0.0", rules=())

    def test_blank_version_rejected(self) -> None:
        with pytest.raises(ValidationError, match="策略版本不能为空"):
            PolicySet(version="  ", rules=(rule(),))

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="规则名重复"):
            PolicySet(
                version="1.0.0", rules=(rule(name="a", priority=1), rule(name="a", priority=2))
            )

    def test_duplicate_priorities_rejected(self) -> None:
        """优先级重复会让求值顺序退化成依赖 YAML 书写顺序，审计时是灾难。"""
        with pytest.raises(ValidationError, match="规则优先级重复"):
            PolicySet(
                version="1.0.0", rules=(rule(name="a", priority=10), rule(name="b", priority=10))
            )

    def test_sorted_rules_ascending_by_priority(self) -> None:
        policy = PolicySet(
            version="1.0.0",
            rules=(
                rule(name="low", priority=900),
                rule(name="high", priority=10),
                rule(name="mid", priority=100),
            ),
        )
        assert [r.name for r in policy.sorted_rules()] == ["high", "mid", "low"]

    def test_enabled_rules_filters_disabled(self) -> None:
        policy = PolicySet(
            version="1.0.0",
            rules=(
                rule(name="on", priority=1, enabled=True),
                rule(name="off", priority=2, enabled=False),
            ),
        )
        assert [r.name for r in policy.enabled_rules()] == ["on"]

    def test_order_of_rules_in_source_does_not_matter(self) -> None:
        """同样两条规则，书写顺序不同不应改变求值顺序。"""
        a = rule(name="block", priority=10)
        b = rule(name="allow", priority=900)
        forward = PolicySet(version="1.0.0", rules=(a, b))
        backward = PolicySet(version="1.0.0", rules=(b, a))
        assert [r.name for r in forward.sorted_rules()] == [r.name for r in backward.sorted_rules()]

    def test_default_fail_mode_is_closed(self) -> None:
        policy = PolicySet(version="1.0.0", rules=(rule(),))
        assert policy.fail_mode is FailMode.CLOSED

    def test_from_mapping(self) -> None:
        data = {
            "version": "2.0.0",
            "use_case": "bank",
            "fail_mode": "degraded",
            "rules": [
                {"name": "r", "action": "escalate", "priority": 1, "condition": {"min_score": 0.5}}
            ],
        }
        policy = policy_set_from_mapping(data)
        assert policy.use_case == "bank"
        assert policy.fail_mode is FailMode.DEGRADED
        assert policy.rules[0].action is PolicyAction.ESCALATE

    def test_invalid_action_string_in_mapping_is_rejected(self) -> None:
        """配置里写错 action 必须在加载时就失败，而不是等到运行时才发现。"""
        with pytest.raises(ValidationError):
            policy_set_from_mapping(
                {
                    "version": "1.0.0",
                    "rules": [
                        {"name": "r", "action": "obliterate", "condition": {"min_score": 0.5}}
                    ],
                }
            )

    def test_describe_lists_rules_in_evaluation_order(self) -> None:
        policy = PolicySet(
            version="1.0.0",
            use_case="u",
            rules=(rule(name="second", priority=900), rule(name="first", priority=10)),
        )
        text = policy.describe()
        assert text.index("first") < text.index("second")
        assert "closed" in text


class TestGuardSettings:
    def test_all_detectors_enabled_by_default(self) -> None:
        assert GuardSettings().allows_detector("rules.l1") is True

    def test_allowlist_restricts_detectors(self) -> None:
        settings = GuardSettings(detectors_enabled=frozenset({"rules.l1"}))
        assert settings.allows_detector("rules.l1") is True
        assert settings.allows_detector("judge.l4") is False

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            GuardSettings(detector_timeout_ms=0)

    def test_judge_can_be_disabled_for_latency_sensitive_paths(self) -> None:
        """延迟敏感场景可关掉 L4，只保留前三层。"""
        assert GuardSettings(llm_judge_enabled=False).llm_judge_enabled is False

    def test_to_dict_is_json_friendly(self) -> None:
        import json

        payload = GuardSettings(detectors_enabled=frozenset({"b", "a"})).to_dict()
        assert payload["detectors_enabled"] == ["a", "b"]  # 已排序，便于 diff
        json.dumps(payload)
