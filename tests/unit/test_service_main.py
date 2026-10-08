"""C10 agent-service · 进程入口单元测试。

``main()`` 是 systemd ``ExecStart`` 直接调用的生产入口（``python -m agent_service.main``），
它只做三件事的串联：加载配置 → 组装容器 → 启动 HTTP 服务器。

这里之所以值得单独测，是因为存在两处**刻意的行为决策**，而不是顺手写出来的代码：

1. **配置错误 → 退出码 78（EX_CONFIG）**，而不是带半个可用的进程硬跑起来。
2. **缺 API Key → 不退出**，只告警。``/healthz`` 与 ``/guard/inspect`` 不需要 key，
   让服务能起来才排得了障。

这两条写错了不会有任何本地报错，只会在部署到目标机之后由 L2 冒烟暴露 ——
那时排障成本高得多。所以用 ``monkeypatch`` 拦掉 ``uvicorn.run``，在本地把它们锁住。

全程不建真实连接：``conftest.py`` 的离线闸门默认禁用 socket，
``httpx.Client`` 的构造本身不开连接，因此可以放心让 ``build_container`` 真跑一遍，
顺便证明"启动期不会误发请求"。
"""

from __future__ import annotations

import logging
import os

import pytest

from agent_core.errors import ConfigurationError
from agent_core.yaml_source import load_yaml_mapping
from agent_service import main as main_module
from agent_service.main import configure_logging, main

#: 一个明确是假的 key，仅用于断言"有 key 时不告警"，永远不会被发出去
FAKE_KEY = "sk-test-not-a-real-key-0000"

#: 入口用到的全部环境变量前缀，清空后才能保证测试不受本机环境影响
_ENV_PREFIX = "MINIMAX_AGENT_"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """隔离本机环境与仓库内的 config.yaml。

    两层隔离缺一不可：

    * 清 ``MINIMAX_AGENT_*`` —— 否则开发者本机设的变量会渗进用例
    * ``chdir`` 到 tmp_path —— 否则会读到仓库根的 ``config.yaml``，
      项目配置层就成了隐式输入
    """
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def uvicorn_calls(monkeypatch):
    """拦截 ``uvicorn.run``，记录调用参数而不是真的监听端口。"""
    calls: list[dict] = []

    def fake_run(app, **kwargs):
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)
    return calls


@pytest.fixture
def basic_config_args(monkeypatch):
    """拦截 ``logging.basicConfig``，捕获它收到的配置。

    不去动根 logger 的真实状态：pytest 自带的 logging 插件会在 fixture 之后
    重新接管根 logger（清 handler、强制级别），直接断言 ``root.level`` 会和插件打架。
    而 ``configure_logging`` 的职责就是"提出正确的配置要求"，断言要求本身更准确。
    """
    captured: dict = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: captured.update(kwargs))
    return captured


class TestConfigureLogging:
    def test_applies_requested_level(self, basic_config_args) -> None:
        configure_logging("DEBUG")
        assert basic_config_args["level"] == logging.DEBUG

    def test_unknown_level_falls_back_to_info(self, basic_config_args) -> None:
        """传进来的级别是用户可配的，写错了不该让服务起不来。"""
        configure_logging("NOT_A_REAL_LEVEL")
        assert basic_config_args["level"] == logging.INFO

    def test_level_is_case_insensitive(self, basic_config_args) -> None:
        configure_logging("warning")
        assert basic_config_args["level"] == logging.WARNING

    def test_writes_to_stderr(self, basic_config_args) -> None:
        """日志走 stderr，stdout 留给将来可能的结构化输出。"""
        import sys

        configure_logging("INFO")
        assert basic_config_args["stream"] is sys.stderr

    def test_format_has_no_request_body(self, basic_config_args) -> None:
        """刻意用最简格式：不含任何会把请求体带进日志的中间件。"""
        configure_logging("INFO")

        fmt = basic_config_args["format"]
        assert "%(asctime)s" in fmt
        assert "%(levelname)s" in fmt
        assert "%(message)s" in fmt


class TestConfigurationFailure:
    def test_missing_explicit_mask_file_exits_with_ex_config(
        self, monkeypatch, capsys, uvicorn_calls
    ) -> None:
        """显式指定了私密配置却读不到 → 启动即失败，绝不静默降级。

        这是"显式指定但不存在 → 报错"约定的端到端验证：配置事故如果被静默忽略，
        服务会带着缺失的密钥正常启动，直到真正发消息才 503。
        """
        monkeypatch.setenv(f"{_ENV_PREFIX}MASK_CONFIG_FILE", "/nonexistent/mask-config.yaml")

        assert main() == 78

        assert "配置错误" in capsys.readouterr().err
        # 失败路径绝不能去监听端口
        assert uvicorn_calls == []

    def test_configuration_error_is_not_swallowed_into_a_stack_trace(
        self, monkeypatch, capsys, uvicorn_calls
    ) -> None:
        """错误信息要可读，但不能变成裸栈回溯刷屏。"""
        monkeypatch.setenv(f"{_ENV_PREFIX}MASK_CONFIG_FILE", "/nonexistent/mask-config.yaml")

        main()

        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert "ConfigurationError" not in err


class TestMissingApiKey:
    def test_still_starts_without_key(self, monkeypatch, uvicorn_calls, caplog) -> None:
        """缺 key 不退出：/healthz 与 /guard/inspect 仍然可用，便于排障。"""
        with caplog.at_level(logging.WARNING, logger="agent_service"):
            assert main() == 0

        assert len(uvicorn_calls) == 1
        assert "未配置 MiniMax API Key" in caplog.text
        # 告警要说明后果，否则运维看到日志也不知道该做什么
        assert "503" in caplog.text

    def test_present_key_does_not_warn(self, monkeypatch, uvicorn_calls, caplog) -> None:
        monkeypatch.setenv(f"{_ENV_PREFIX}LLM__API_KEY", FAKE_KEY)

        with caplog.at_level(logging.WARNING, logger="agent_service"):
            assert main() == 0

        assert "未配置 MiniMax API Key" not in caplog.text
        assert len(uvicorn_calls) == 1

    def test_warning_never_contains_the_key(self, monkeypatch, uvicorn_calls, caplog) -> None:
        """有 key 的场景下，任何日志都不得出现 key 本身。"""
        monkeypatch.setenv(f"{_ENV_PREFIX}LLM__API_KEY", FAKE_KEY)

        with caplog.at_level(logging.DEBUG):
            main()

        assert FAKE_KEY not in caplog.text


class TestStartup:
    def test_uvicorn_receives_configured_address(self, monkeypatch, uvicorn_calls) -> None:
        monkeypatch.setenv(f"{_ENV_PREFIX}APP__HOST", "0.0.0.0")
        monkeypatch.setenv(f"{_ENV_PREFIX}APP__PORT", "9123")

        assert main() == 0

        call = uvicorn_calls[0]
        assert call["host"] == "0.0.0.0"
        assert call["port"] == 9123

    def test_access_log_disabled(self, monkeypatch, uvicorn_calls) -> None:
        """访问日志收益低、噪音高，还可能把路径带进日志。"""
        assert main() == 0

        assert uvicorn_calls[0]["access_log"] is False

    def test_log_level_passed_through_to_uvicorn(self, monkeypatch, uvicorn_calls) -> None:
        monkeypatch.setenv(f"{_ENV_PREFIX}APP__LOG_LEVEL", "DEBUG")

        main()

        assert uvicorn_calls[0]["log_level"] == "debug"

    def test_app_is_actually_wired_to_the_container(self, monkeypatch, uvicorn_calls) -> None:
        """传进 uvicorn 的必须是组装好的 FastAPI 实例，而不是 None。

        这条守的是"容器组装失败但服务照样起来"这类最难查的接线错误。
        """
        monkeypatch.setenv(f"{_ENV_PREFIX}LLM__API_KEY", FAKE_KEY)

        main()

        app = uvicorn_calls[0]["app"]
        assert app is not None
        routes = {getattr(r, "path", None) for r in app.routes}
        assert {"/healthz", "/chat", "/guard/inspect"} <= routes


class TestSecretsFileWiring:
    def test_env_secrets_file_is_honoured(self, monkeypatch, tmp_path, uvicorn_calls) -> None:
        """systemd 用 ``MINIMAX_AGENT_SECRETS_FILE`` 指向 shared/.env。"""
        secrets = tmp_path / "shared.env"
        secrets.write_text(
            f"{_ENV_PREFIX}APP__PORT=9321\n{_ENV_PREFIX}LLM__API_KEY={FAKE_KEY}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(f"{_ENV_PREFIX}SECRETS_FILE", str(secrets))

        assert main() == 0

        assert uvicorn_calls[0]["port"] == 9321

    def test_missing_secrets_file_fails_fast(self, monkeypatch, uvicorn_calls) -> None:
        monkeypatch.setenv(f"{_ENV_PREFIX}SECRETS_FILE", "/nonexistent/shared.env")

        assert main() == 78
        assert uvicorn_calls == []


def test_configuration_error_carries_no_secret_material(tmp_path) -> None:
    """兜底不变式：配置错误消息里不该混入任何值。

    ``YamlSettingsSource`` 只报路径与行号、不回显出错行内容 —— 那行可能正是密钥。
    """
    bad = tmp_path / "mask-config.yaml"
    # 故意让第二行成为语法错误，且该行长得像密钥
    bad.write_text(f"{_ENV_PREFIX}LLM__API_KEY: sk-leak-me\n  bad indent: [\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as excinfo:
        load_yaml_mapping(bad)

    assert "sk-leak-me" not in str(excinfo.value)
