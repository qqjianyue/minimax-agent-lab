"""C1 agent-core · 配置管理单元测试。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_core.config import (
    DEFAULT_MINIMAX_BASE_URL,
    AppSettings,
    LLMSettings,
    Settings,
    load_settings,
)
from agent_core.errors import ConfigurationError


class TestDefaults:
    def test_llm_defaults_match_minimax_openai_compatible_endpoint(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.llm.base_url == DEFAULT_MINIMAX_BASE_URL
        assert settings.llm.model == "MiniMax-M3"
        # L4 judge 走更便宜的小模型，是刻意的成本/延迟权衡
        assert settings.llm.judge_model != settings.llm.model

    def test_api_key_unset_by_default(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.llm.api_key.get_secret_value() == ""
        with pytest.raises(ConfigurationError, match="未配置 MiniMax API Key"):
            settings.llm.require_api_key()

    def test_prompt_capture_disabled_by_default(self) -> None:
        """银行场景：trace 默认不落盘 prompt 原文，先取安全的那一端。"""
        assert Settings(_env_file=None).telemetry.capture_prompts is False

    def test_app_defaults(self) -> None:
        app = AppSettings()
        assert app.max_steps == 10
        assert app.env == "local"


class TestApiKeyHandling:
    def test_secret_never_leaks_into_serialized_output(self) -> None:
        secret = "sk-super-secret-value-9876"
        settings = LLMSettings(api_key=secret)
        assert secret not in settings.model_dump_json()
        assert secret not in json.dumps(settings.model_dump(mode="json"))
        assert secret not in repr(settings)

    def test_hint_only_exposes_last_four(self) -> None:
        assert LLMSettings(api_key="sk-abcdefghijkl").api_key_hint == "********ijkl"

    def test_hint_for_short_key_is_fully_masked(self) -> None:
        assert LLMSettings(api_key="short").api_key_hint == "********"

    def test_hint_when_unset(self) -> None:
        assert LLMSettings().api_key_hint == "<unset>"

    def test_key_read_from_file(self, tmp_path) -> None:
        key_file = tmp_path / "minimax.key"
        key_file.write_text("  sk-from-file-4321  \n", encoding="utf-8")
        settings = LLMSettings(api_key_file=key_file)
        assert settings.require_api_key() == "sk-from-file-4321"

    def test_explicit_key_wins_over_file(self, tmp_path) -> None:
        key_file = tmp_path / "minimax.key"
        key_file.write_text("sk-from-file", encoding="utf-8")
        settings = LLMSettings(api_key="sk-from-env", api_key_file=key_file)
        assert settings.require_api_key() == "sk-from-env"

    def test_missing_key_file_raises(self, tmp_path) -> None:
        with pytest.raises(ConfigurationError, match="api_key_file 指向的文件不存在"):
            LLMSettings(api_key_file=tmp_path / "nope.key")

    def test_empty_key_file_raises(self, tmp_path) -> None:
        key_file = tmp_path / "empty.key"
        key_file.write_text("   \n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="api_key_file 内容为空"):
            LLMSettings(api_key_file=key_file)

    def test_absent_key_file_leaves_key_empty(self) -> None:
        """没配置 api_key_file 时不应该报错 —— 启动期不强制，开发/测试环境可无 key。"""
        assert LLMSettings().api_key.get_secret_value() == ""


class TestEnvironmentOverrides:
    def test_nested_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_LLM__MODEL", "MiniMax-M2.5")
        settings = Settings(_env_file=None)
        assert settings.llm.model == "MiniMax-M2.5"

    def test_nested_env_override_deep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_TELEMETRY__PHOENIX_ENDPOINT", "http://10.0.0.5:6006")
        monkeypatch.setenv("MINIMAX_AGENT_APP__MAX_STEPS", "3")
        settings = Settings(_env_file=None)
        assert settings.telemetry.phoenix_endpoint == "http://10.0.0.5:6006"
        assert settings.app.max_steps == 3

    def test_invalid_env_value_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_APP__MAX_STEPS", "0")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


class TestLoadSettings:
    def test_reads_from_secrets_file(self, tmp_path) -> None:
        secrets = tmp_path / "agent.env"
        secrets.write_text(
            "MINIMAX_AGENT_LLM__API_KEY=sk-from-secrets-file\n"
            "MINIMAX_AGENT_LLM__MODEL=MiniMax-M2\n",
            encoding="utf-8",
        )
        settings = load_settings(secrets_file=secrets)
        assert settings.llm.require_api_key() == "sk-from-secrets-file"
        assert settings.llm.model == "MiniMax-M2"

    def test_real_env_beats_secrets_file(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量优先于文件：同一份模板配置可按机器覆盖个别值。"""
        secrets = tmp_path / "agent.env"
        secrets.write_text("MINIMAX_AGENT_LLM__MODEL=from-file\n", encoding="utf-8")
        monkeypatch.setenv("MINIMAX_AGENT_LLM__MODEL", "from-env")
        assert load_settings(secrets_file=secrets).llm.model == "from-env"

    def test_missing_secrets_file_raises(self, tmp_path) -> None:
        with pytest.raises(ConfigurationError, match="指定的密码配置文件不存在"):
            load_settings(secrets_file=tmp_path / "absent.env")

    def test_without_secrets_file_uses_defaults(self) -> None:
        assert load_settings().app.env == "local"
