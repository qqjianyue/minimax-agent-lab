"""L1 集成测试 · 策略加载与配置组合。

验证的是"真实文件 + 真实解析器"这条路径：不是把 dict 直接喂给 model，
而是真的走 YAML 解析、字段映射、校验、构建。单元测试里我们用
``policy_set_from_mapping`` 跳过了 YAML 那一段，这里补上。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_core.config import load_settings
from guard_contract.default_policy import DEFAULT_POLICY_YAML, load_default_policy, load_policy_set
from guard_contract.enums import GuardStage, PolicyAction
from guard_contract.policy_schema import GuardSettings
from tests.fakes import make_result


class TestDefaultPolicy:
    @staticmethod
    @pytest.fixture(scope="module")
    def policy():
        return load_default_policy()

    def test_loads_from_yaml(self, policy) -> None:
        assert policy.version == "1.0.0"
        assert policy.use_case == "bank-assistant-demo"
        assert policy.fail_mode.value == "closed"

    def test_priorities_are_unique_and_sorted(self, policy) -> None:
        priorities = [r.priority for r in policy.sorted_rules()]
        assert priorities == sorted(priorities)
        assert len(priorities) == len(set(priorities))

    def test_covers_all_threat_categories(self, policy) -> None:
        """架构方案 §5.1 的威胁模型必须在默认策略里有对应规则。"""
        by_name = {r.name: r for r in policy.rules}
        assert by_name["block_critical_injection"].action is PolicyAction.BLOCK
        assert by_name["redact_pii"].action is PolicyAction.REDACT
        assert by_name["escalate_dangerous_tool"].action is PolicyAction.REQUIRE_APPROVAL
        assert by_name["escalate_borderline"].action is PolicyAction.ESCALATE

    def test_fallback_allow_is_default_action_not_a_rule(self, policy) -> None:
        """兜底放行必须用 default_action 表达。

        写成 min_score=0.0 的规则会让任何检测结果都命中规则，fail_mode
        就永远走不到 —— 降级机制形同虚设，而这种配置看上去完全正常。
        """
        assert policy.default_action is PolicyAction.ALLOW
        assert not any(
            r.action is PolicyAction.ALLOW and r.condition.min_score == 0.0 for r in policy.rules
        )

    def test_every_rule_condition_is_reachable(self, policy) -> None:
        """每条规则都至少能被某个构造出的检测结果命中。

        写死一条永远匹配不到的规则是常见的事故：配置看着有防护，
        实际是死代码。
        """
        samples = [
            make_result(label="prompt_injection", score=0.95, confidence=0.95),
            make_result(label="harmful_content", score=0.99, confidence=0.99),
            make_result(stage=GuardStage.TOOL, score=0.75, label="risky_tool", confidence=0.8),
            make_result(label="pii_leak", score=0.8, confidence=0.9),
            make_result(label="suspicious", score=0.65, confidence=0.6),
        ]
        for r in policy.rules:
            assert any(r.matches(sample) for sample in samples), f"规则 {r.name} 永远无法命中"

    def test_reasons_are_populated_for_audit(self, policy) -> None:
        for r in policy.rules:
            assert r.reason, f"规则 {r.name} 缺少 reason，审计记录会失去可解释性"

    def test_survives_yaml_roundtrip(self, policy) -> None:
        """policy-as-code 的前提是策略能被安全地存回去再读出来。"""
        as_dict = json.loads(policy.model_dump_json())
        reloaded = load_policy_set(as_dict)
        assert reloaded == policy


class TestPolicyLoadingErrors:
    def test_non_mapping_document_rejected(self) -> None:
        with pytest.raises(ValueError, match="不是映射结构"):
            load_policy_set("- just\n- a\n- list\n")

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_policy_set({"rules": [{"name": "r", "condition": {"min_score": 0.5}}]})

    def test_empty_rules_rejected_at_load_time(self) -> None:
        with pytest.raises(ValidationError, match="策略集不能为空"):
            load_policy_set({"version": "1.0.0", "rules": []})


class TestConfigurationComposition:
    def test_app_and_guard_settings_compose_without_cycles(self, tmp_path) -> None:
        """agent-core 与 guard-contract 各自持有配置，组合发生在服务层。

        两边可以独立加载、互不 import，测试确认组合结果符合预期。
        """
        secrets = tmp_path / "agent.env"
        secrets.write_text(
            "MINIMAX_AGENT_LLM__API_KEY=sk-integration-test\n"
            "MINIMAX_AGENT_TELEMETRY__CAPTURE_PROMPTS=true\n",
            encoding="utf-8",
        )
        app = load_settings(secrets_file=secrets)
        guard = GuardSettings(detector_timeout_ms=250.0)

        assert app.llm.require_api_key() == "sk-integration-test"
        assert app.telemetry.capture_prompts is True
        assert guard.detector_timeout_ms == 250.0

    def test_guard_settings_json_serialisable(self) -> None:
        json.dumps(GuardSettings().to_dict())

    def test_default_yaml_constant_matches_loaded_policy(self) -> None:
        """内置常量与 loader 必须一致，避免文档漂移。"""
        assert load_policy_set(DEFAULT_POLICY_YAML) == load_default_policy()
