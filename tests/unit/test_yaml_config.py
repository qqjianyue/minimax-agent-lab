"""C1 agent-core · YAML 配置源单元测试。

重点在三处：

1. **优先级**：环境变量 > 私密 YAML > 项目 YAML > 默认值。顺序错了会让
   "改了环境变量却没生效"这类问题极难排查。
2. **未知键告警**：pydantic 静默忽略未知键，拼错 `api_key` 会让服务正常
   启动却拿不到密钥。这里验证**按名字**报告。
3. **不泄露**：任何错误信息、日志、异常都不得包含配置值。
"""

from __future__ import annotations

import logging

import pytest

from agent_core.config import Settings
from agent_core.errors import ConfigurationError
from agent_core.yaml_source import (
    YamlSettingsSource,
    find_unknown_keys,
    load_yaml_mapping,
    resolve_config_paths,
    to_nested,
)

SECRET = "sk-super-secret-value-0000"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离外部配置，保证每个用例只看到自己造的文件。"""
    monkeypatch.delenv("MINIMAX_AGENT_MASK_CONFIG_FILE", raising=False)
    monkeypatch.delenv("MINIMAX_AGENT_PROJECT_CONFIG_FILE", raising=False)


# --- 解析 -------------------------------------------------------------------
class TestLoadYamlMapping:
    def test_reads_simple_mapping(self, tmp_path) -> None:
        f = tmp_path / "c.yaml"
        f.write_text("a: 1\nb: two\n", encoding="utf-8")
        assert load_yaml_mapping(f) == {"a": 1, "b": "two"}

    def test_comment_only_file_is_empty(self, tmp_path) -> None:
        """config.yaml 目前就是 `#placeholder`，必须按空配置处理而不是报错。"""
        f = tmp_path / "c.yaml"
        f.write_text("#placeholder\n", encoding="utf-8")
        assert load_yaml_mapping(f) == {}

    def test_empty_file_is_empty(self, tmp_path) -> None:
        f = tmp_path / "c.yaml"
        f.write_text("", encoding="utf-8")
        assert load_yaml_mapping(f) == {}

    def test_missing_file_raises_with_path_only(self, tmp_path) -> None:
        with pytest.raises(ConfigurationError, match="配置文件不存在") as exc:
            load_yaml_mapping(tmp_path / "nope.yaml")
        assert "nope.yaml" in str(exc.value)

    def test_malformed_yaml_error_has_no_content(self, tmp_path) -> None:
        """解析失败时**不能回显出错行** —— 那一行很可能就是密钥。"""
        f = tmp_path / "bad.yaml"
        f.write_text(f"key: {SECRET}\n  bad indent: [\n", encoding="utf-8")
        with pytest.raises(ConfigurationError) as exc:
            load_yaml_mapping(f)
        assert SECRET not in str(exc.value)
        assert "YAML 解析失败" in str(exc.value)

    def test_non_mapping_toplevel_rejected(self, tmp_path) -> None:
        f = tmp_path / "list.yaml"
        f.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="顶层必须是映射"):
            load_yaml_mapping(f)


# --- 键名转换 ---------------------------------------------------------------
class TestToNested:
    def test_env_style_split_on_double_underscore(self) -> None:
        out = to_nested({"MINIMAX_AGENT_LLM__API_KEY": "x"}, "MINIMAX_AGENT_")
        assert out == {"llm": {"api_key": "x"}}

    def test_deeply_nested(self) -> None:
        out = to_nested({"MINIMAX_AGENT_A__B__C": 1}, "MINIMAX_AGENT_")
        assert out == {"a": {"b": {"c": 1}}}

    def test_native_nested_kept_as_is(self) -> None:
        out = to_nested({"llm": {"model": "m"}}, "MINIMAX_AGENT_")
        assert out == {"llm": {"model": "m"}}

    def test_mixed_styles_coexist(self) -> None:
        out = to_nested(
            {"MINIMAX_AGENT_LLM__API_KEY": "x", "telemetry": {"enabled": False}},
            "MINIMAX_AGENT_",
        )
        assert out == {"llm": {"api_key": "x"}, "telemetry": {"enabled": False}}

    def test_key_without_prefix_after_strip(self) -> None:
        assert to_nested({"MINIMAX_AGENT_": "v"}, "MINIMAX_AGENT_") == {}


# --- 未知键检测 -------------------------------------------------------------
class TestUnknownKeys:
    def test_detects_typo_in_nested_field(self) -> None:
        """`apikey` 拼错会让服务正常启动却拿不到密钥 —— 必须显式报出来。"""
        unknown = find_unknown_keys({"MINIMAX_AGENT_LLM__apikey": "x"}, Settings, "MINIMAX_AGENT_")
        assert unknown == ["llm.apikey"]

    def test_detects_unknown_section(self) -> None:
        assert find_unknown_keys({"nope": {"a": 1}}, Settings, "MINIMAX_AGENT_") == ["nope"]

    def test_known_keys_not_reported(self) -> None:
        data = {
            "MINIMAX_AGENT_LLM__API_KEY": "x",
            "MINIMAX_AGENT_APP__PORT": 8080,
            "telemetry": {"enabled": True},
        }
        assert find_unknown_keys(data, Settings, "MINIMAX_AGENT_") == []

    def test_reports_names_not_values(self) -> None:
        data = {"MINIMAX_AGENT_LLM__apikey": SECRET}
        unknown = find_unknown_keys(data, Settings, "MINIMAX_AGENT_")
        assert all(SECRET not in u for u in unknown)


# --- 配置源 -----------------------------------------------------------------
class TestYamlSettingsSource:
    def test_env_style_yaml_feeds_settings(self, tmp_path) -> None:
        f = tmp_path / "mask.yaml"
        f.write_text(f"MINIMAX_AGENT_LLM__API_KEY: {SECRET}\n", encoding="utf-8")
        monkey = YamlSettingsSource(Settings, f, label="私密配置")
        assert monkey() == {"llm": {"api_key": SECRET}}

    def test_none_path_yields_empty(self) -> None:
        assert YamlSettingsSource(Settings, None, label="x")() == {}

    def test_comment_only_file_yields_empty(self, tmp_path) -> None:
        f = tmp_path / "c.yaml"
        f.write_text("#placeholder\n", encoding="utf-8")
        assert YamlSettingsSource(Settings, f, label="项目配置")() == {}

    def test_unknown_keys_logged_as_warning_with_names_only(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        f = tmp_path / "mask.yaml"
        f.write_text(f"MINIMAX_AGENT_LLM__apikey: {SECRET}\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="agent_core.config"):
            YamlSettingsSource(Settings, f, label="私密配置")
        assert any("llm.apikey" in r.message for r in caplog.records)
        assert all(SECRET not in r.getMessage() for r in caplog.records)

    def test_repr_has_no_values(self, tmp_path) -> None:
        f = tmp_path / "mask.yaml"
        f.write_text(f"MINIMAX_AGENT_LLM__API_KEY: {SECRET}\n", encoding="utf-8")
        assert SECRET not in repr(YamlSettingsSource(Settings, f, label="x"))


# --- 优先级 -----------------------------------------------------------------
class TestPrecedence:
    def test_mask_yaml_applies_when_nothing_else_set(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mask.yaml"
        f.write_text(f"MINIMAX_AGENT_LLM__API_KEY: {SECRET}\n", encoding="utf-8")
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", str(f))
        settings = Settings(_env_file=None)
        assert settings.llm.require_api_key() == SECRET

    def test_env_beats_mask_yaml(self, tmp_path, monkeypatch) -> None:
        """环境变量是临时覆盖手段，必须压过文件。"""
        f = tmp_path / "mask.yaml"
        f.write_text("MINIMAX_AGENT_LLM__MODEL: FromMask\n", encoding="utf-8")
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", str(f))
        monkeypatch.setenv("MINIMAX_AGENT_LLM__MODEL", "FromEnv")
        assert Settings(_env_file=None).llm.model == "FromEnv"

    def test_mask_beats_project_yaml(self, tmp_path, monkeypatch) -> None:
        mask = tmp_path / "mask.yaml"
        mask.write_text("MINIMAX_AGENT_LLM__MODEL: FromMask\n", encoding="utf-8")
        project = tmp_path / "config.yaml"
        project.write_text("llm:\n  model: FromProject\n", encoding="utf-8")
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", str(mask))
        monkeypatch.setenv("MINIMAX_AGENT_PROJECT_CONFIG_FILE", str(project))
        assert Settings(_env_file=None).llm.model == "FromMask"

    def test_project_yaml_applies_when_no_mask(self, tmp_path, monkeypatch) -> None:
        project = tmp_path / "config.yaml"
        project.write_text("llm:\n  model: FromProject\napp:\n  port: 9090\n", encoding="utf-8")
        monkeypatch.setenv("MINIMAX_AGENT_PROJECT_CONFIG_FILE", str(project))
        settings = Settings(_env_file=None)
        assert settings.llm.model == "FromProject"
        assert settings.app.port == 9090

    def test_defaults_when_no_files_configured(self, tmp_path, monkeypatch) -> None:
        """两个路径都没给时用默认值 —— 这是"没配置"，不是"配置错了"。"""
        monkeypatch.chdir(tmp_path)
        assert Settings(_env_file=None).llm.model == "MiniMax-M3"

    def test_explicitly_configured_but_missing_file_is_an_error(
        self, tmp_path, monkeypatch
    ) -> None:
        """显式指定了路径却不存在的处理，必须与"没配置"区别开。

        静默忽略的后果：服务带着缺失的密钥正常启动，/healthz 返回 200，
        直到真正发消息才 503 —— 典型的"看起来健康、实际不可用"。
        """
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", str(tmp_path / "absent.yaml"))
        with pytest.raises(ConfigurationError, match="被显式指定但不存在"):
            Settings(_env_file=None)

    def test_mask_key_never_serialised(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mask.yaml"
        f.write_text(f"MINIMAX_AGENT_LLM__API_KEY: {SECRET}\n", encoding="utf-8")
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", str(f))
        settings = Settings(_env_file=None)
        assert SECRET not in settings.model_dump_json()
        assert settings.llm.api_key_hint.endswith("0000")


class TestResolveConfigPaths:
    def test_reads_mask_from_env(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "m.yaml"
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", str(f))
        mask, _ = resolve_config_paths()
        assert mask == f

    def test_blank_env_treated_as_absent(self, monkeypatch) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_MASK_CONFIG_FILE", "   ")
        mask, _ = resolve_config_paths()
        assert mask is None

    def test_project_defaults_to_cwd_config(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("MINIMAX_AGENT_PROJECT_CONFIG_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("#placeholder\n", encoding="utf-8")
        _, project = resolve_config_paths()
        assert project is not None and project.name == "config.yaml"

    def test_project_none_when_absent(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("MINIMAX_AGENT_PROJECT_CONFIG_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        _, project = resolve_config_paths()
        assert project is None
