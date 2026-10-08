"""agent-core：跨组件共享的基础设施层。

只依赖 pydantic / pydantic-settings / pyyaml，不引入任何模型 SDK、HTTP 客户端
或 GPU 依赖 —— 这是保证"纯逻辑组件能在本地做真正单元测试"的必要条件。
"""

from __future__ import annotations

from agent_core.clock import SystemClock
from agent_core.config import (
    ENV_PREFIX,
    AppSettings,
    LLMSettings,
    Settings,
    TelemetrySettings,
    load_settings,
)
from agent_core.errors import (
    AgentCoreError,
    ConfigurationError,
    DependencyNotInstalledError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    TransientError,
    VersionError,
)
from agent_core.retry import RetryPolicy
from agent_core.version import VersionInfo, get_version_info, is_newer, parse_semver
from agent_core.yaml_source import (
    YamlSettingsSource,
    find_unknown_keys,
    load_yaml_mapping,
    resolve_config_paths,
    to_nested,
)

__version__ = get_version_info().semver

__all__ = [
    "ENV_PREFIX",
    "AgentCoreError",
    "AppSettings",
    "ConfigurationError",
    "DependencyNotInstalledError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMSettings",
    "LLMTimeoutError",
    "RetryPolicy",
    "Settings",
    "SystemClock",
    "TelemetrySettings",
    "TransientError",
    "VersionError",
    "VersionInfo",
    "YamlSettingsSource",
    "find_unknown_keys",
    "get_version_info",
    "is_newer",
    "load_settings",
    "load_yaml_mapping",
    "parse_semver",
    "resolve_config_paths",
    "to_nested",
]
