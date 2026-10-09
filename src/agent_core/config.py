"""配置管理。

三条与本项目强相关的设计约定：

1. **API Key 不进代码、不进环境变量明文日志**。用 :class:`~pydantic.SecretStr`
   承载，序列化时自动变成 ``********``；``api_key_file`` 允许把密钥放在
   本机和目标机各自的一份密码配置文件里（决策 Q3），避免密钥进版本库。

2. **银行场景默认不落盘 prompt 原文**。``telemetry.capture_prompts`` 默认
   ``False``，对应"trace 会落盘 prompt 与 response 原文，落盘前必须脱敏"
   这条已知风险 —— 默认值先取安全的那一端。

3. **配置错误一律 fail-fast**。启动阶段抛 :class:`ConfigurationError`，
   而不是带默认值硬跑出半个可用的进程。
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from agent_core.errors import ConfigurationError
from agent_core.yaml_source import YamlSettingsSource, resolve_config_paths

ENV_PREFIX = "MINIMAX_AGENT_"

DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.cn/v1"


class LLMSettings(BaseModel):
    """MiniMax 大模型后端配置。"""

    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = "MiniMax-M3"
    # L4 LLM-as-Judge 用更便宜的小模型，延迟与成本的权衡点（面试可讲）
    judge_model: str = "MiniMax-M2.5"
    api_key: SecretStr = SecretStr("")
    api_key_file: Path | None = None
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)

    #: 每百万 token 单价，用于 `llm_call` span 的 cost 口径（FT-11）。
    #:
    #: 默认 0.0 表示**未配置**，不是"免费"。这样区分很重要：
    #: 成本为 0 的 trace 会被误读成"这次调用没花钱"，而真相是"我们没填单价"。
    #: 真实单价随官方调价变动，因此属于**部署配置**而非代码常量 ——
    #: 改价不需要改代码、不需要重新发版。
    price_input_per_million: float = Field(default=0.0, ge=0.0)
    price_output_per_million: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _load_api_key_from_file(self) -> LLMSettings:
        """``api_key`` 为空时从 ``api_key_file`` 读取。

        显式传入的 ``api_key`` 优先级更高 —— 这样同一份配置文件可以在
        本机和目标机复用，密钥本身按机器区分。
        """
        if self.api_key.get_secret_value():
            return self
        if self.api_key_file is None:
            return self

        path = self.api_key_file
        if not path.is_file():
            raise ConfigurationError(
                f"api_key_file 指向的文件不存在: {path}。请确认密码配置文件已就位。"
            )
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise ConfigurationError(f"api_key_file 内容为空: {path}")
        self.api_key = SecretStr(raw)
        return self

    def require_api_key(self) -> str:
        """取出明文 API Key，缺失时抛错。"""
        value = self.api_key.get_secret_value()
        if not value:
            raise ConfigurationError(
                "未配置 MiniMax API Key。请设置 "
                f"{ENV_PREFIX}LLM__API_KEY，或在配置中指定 api_key_file。"
            )
        return value

    @property
    def api_key_hint(self) -> str:
        """仅供日志展示的脱敏提示，绝不返回明文。"""
        value = self.api_key.get_secret_value()
        if not value:
            return "<unset>"
        if len(value) <= 8:
            return "********"
        return f"********{value[-4:]}"


class TelemetrySettings(BaseModel):
    """可观测性配置（横切面）。

    ``enabled`` 默认 **False**：可观测性依赖收集端（Phoenix）真实存在。
    Phoenix 尚未部署时若默认开启，服务会起一个后台导出线程，不断尝试连接
    一个没人监听的端点，失败日志能把真正的告警淹掉 —— 观测设施不可用
    反过来损害了可观测性。Phoenix 就位后按需在配置里打开即可。
    """

    enabled: bool = False
    service_name: str = "minimax-agent"
    phoenix_endpoint: str = "http://127.0.0.1:6006"
    # 银行场景默认不把 prompt/response 原文写入 trace
    capture_prompts: bool = False
    retention_days: int = Field(default=90, ge=1)


class AuditSettings(BaseModel):
    """审计账本配置（C8）。

    ``retention_days`` 与 :class:`TelemetrySettings` 的同名字段是两回事：
    审计账本是**合规留痕**，保留期通常更长，且不可被观测系统的策略带偏。

    ``root``：账本根目录。相对路径 ``path`` 会挂在 ``root`` 下。
    生产部署必须指向**跨版本共享目录**（如 ``%h/shared``，systemd 已注入
    ``MINIMAX_AGENT_AUDIT__ROOT``）—— 审计历史随版本走会丢：
    版本更新切 symlink 后新版本在新目录从零写账本，旧记录"消失"，
    回退时账本跳变，release 清理时历史被直接删除。
    ``root`` 为空时由容器决定（本地开发用当前工作目录，测试显式传
    ``audit_root`` 覆盖）。
    """

    enabled: bool = True
    root: Path | None = None
    #: 相对路径会挂在 root 下（生产为 shared/audit/，跨版本共享，回退不丢历史）
    path: Path = Path("audit/ledger.jsonl")
    retention_days: int = Field(default=365, ge=1)


class AppSettings(BaseModel):
    """服务进程自身配置。"""

    env: str = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    # ReAct 循环兜底步数，防止无限循环烧额度
    max_steps: int = Field(default=10, ge=1, le=100)


class Settings(BaseSettings):
    """应用根配置。

    配置来源与优先级（从高到低）::

        环境变量  >  私密 YAML (mask)  >  项目 YAML  >  .env  >  模型默认值

    嵌套字段在环境变量里用双下划线分隔，例如::

        MINIMAX_AGENT_LLM__MODEL=MiniMax-M3
        MINIMAX_AGENT_TELEMETRY__PHOENIX_ENDPOINT=http://127.0.0.1:6006

    两份 YAML 的位置本身也由环境变量指定，因此配置文件可以随机器不同而不同，
    而代码里不出现任何机器相关路径：

    * ``MINIMAX_AGENT_MASK_CONFIG_FILE`` —— 私密配置（独立于 release）
    * ``MINIMAX_AGENT_PROJECT_CONFIG_FILE`` —— 项目配置（默认 ``./config.yaml``，随 release）
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm: LLMSettings = Field(default_factory=LLMSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    app: AppSettings = Field(default_factory=AppSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        mask_path, project_path = resolve_config_paths()
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlSettingsSource(settings_cls, mask_path, label="私密配置"),
            YamlSettingsSource(settings_cls, project_path, label="项目配置"),
            file_secret_settings,
        )


def load_settings(
    *,
    secrets_file: str | os.PathLike[str] | None = None,
) -> Settings:
    """加载配置，可选指定 dotenv 格式的补充配置文件。

    优先级：真实环境变量 > 指定的 ``secrets_file`` > 默认 ``.env`` >
    私密 YAML > 项目 YAML > 模型默认值。

    环境变量优先于文件，因此可以在共享一份模板配置的同时按机器覆盖个别值。
    """
    if secrets_file is None:
        return Settings()

    path = Path(secrets_file)
    if not path.is_file():
        raise ConfigurationError(f"指定的密码配置文件不存在: {path}")
    return Settings(_env_file=path)


__all__ = [
    "DEFAULT_MINIMAX_BASE_URL",
    "ENV_PREFIX",
    "AppSettings",
    "AuditSettings",
    "LLMSettings",
    "Settings",
    "TelemetrySettings",
    "load_settings",
]
