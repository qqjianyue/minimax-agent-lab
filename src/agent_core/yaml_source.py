"""YAML 配置源。

## 为什么要 app 自己读，而不是交给 systemd

systemd 的 ``EnvironmentFile=`` **只解析 ``KEY=VALUE``**。把
``MINIMAX_AGENT_LLM__API_KEY: sk-xxx`` 这种 YAML 交给它，整行会被**静默跳过**：
服务照常启动、``/healthz`` 返回 200、看起来一切正常，但 ``/chat`` 全部 503。
这是最糟的失败模式 —— 延迟到真正发消息时才暴露，且没有任何报错。

所以本模块让 app 直接解析 YAML 配置文件。

## 优先级

::

    环境变量  >  私密 YAML (mask)  >  项目 YAML  >  模型默认值

环境变量仍然最高，因为它是最直接的临时覆盖手段（也保留了 systemd
``EnvironmentFile`` 的能力，用于覆盖非密钥参数）。私密配置高于项目配置：
项目配置随 release 走、可能滞后，而密钥文件是独立更新的。

## 键名两种写法都支持

* **环境变量式**（mask 文件当前的形式）：
  ``MINIMAX_AGENT_LLM__API_KEY: sk-xxx``
  去掉前缀后按 ``__`` 拆成嵌套：``{"llm": {"api_key": ...}}``
* **原生嵌套式**（项目配置更自然的写法）::

      llm:
        model: MiniMax-M2

## 未知键告警

拼错的键（比如 ``apikey`` 写成 ``api_key``）会被 pydantic 静默忽略，
服务照常启动但配置没生效。这里**按名字**（不按值）报告未知键，
把这类静默失效变成一条明确的 WARNING。键名不是机密，值才是。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from agent_core.errors import ConfigurationError

logger = logging.getLogger("agent_core.config")

#: 嵌套分隔符，与 ``env_nested_delimiter`` 保持一致
NESTED_DELIMITER = "__"


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """读取一个 YAML 映射文件。

    只含注释的文件（``#placeholder``）解析结果是 ``None``，按空配置处理。

    Raises:
        ConfigurationError: 文件不存在、无法解析、或顶层不是映射。
            错误信息**只含路径与原因，不含任何内容**。
    """
    if not path.is_file():
        raise ConfigurationError(f"配置文件不存在: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"配置文件无法读取: {path}（{type(exc).__name__}）") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # 只报行号，不回显出错行内容 —— 那可能含密钥
        mark = getattr(exc, "problem_mark", None)
        where = f"第 {mark.line + 1} 行" if mark is not None else "未知位置"
        raise ConfigurationError(f"配置文件 YAML 解析失败: {path}（{where}）") from exc

    if data is None:
        return {}  # 纯注释文件
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"配置文件顶层必须是映射结构，实际为 {type(data).__name__}: {path}"
        )
    return data


def to_nested(data: dict[str, Any], env_prefix: str) -> dict[str, Any]:
    """把"环境变量式"键名转成嵌套结构；已经是嵌套的键原样保留。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            out[str(key)] = value
            continue
        if key.startswith(env_prefix):
            path = key[len(env_prefix) :].lower().split(NESTED_DELIMITER)
            _assign(out, [p for p in path if p], value)
        else:
            # 原生嵌套写法，直接沿用
            _assign(out, [key], value)
    return out


def _assign(target: dict[str, Any], path: list[str], value: Any) -> None:
    if not path:
        return
    cursor = target
    for part in path[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    leaf = path[-1]
    if isinstance(cursor.get(leaf), dict) and isinstance(value, dict):
        cursor[leaf].update(value)
    else:
        cursor[leaf] = value


def find_unknown_keys(
    data: dict[str, Any], settings_cls: type[BaseSettings], env_prefix: str
) -> list[str]:
    """找出配置里模型不认识的键，返回点分路径（**只含键名，不含值**）。

    存在的意义：pydantic 对未知键是静默忽略的。``api_key`` 拼成 ``apikey``
    会让服务正常启动却拿不到密钥 —— 这种问题必须以告警形式暴露。
    """
    nested = to_nested(data, env_prefix)
    unknown: list[str] = []
    for key, value in nested.items():
        field = settings_cls.model_fields.get(key)
        if field is None:
            unknown.append(key)
            continue
        if not isinstance(value, dict):
            continue
        sub_model = _sub_model_of(field.annotation)
        if sub_model is None:
            continue
        sub_nested = to_nested(value, "") if _looks_env_style(value) else value
        for sub_key in sub_nested:
            if sub_key not in sub_model.model_fields:
                unknown.append(f"{key}.{sub_key}")
    return sorted(unknown)


def _looks_env_style(value: dict[str, Any]) -> bool:
    return any(k.startswith("MINIMAX_AGENT_") for k in value if isinstance(k, str))


def _sub_model_of(annotation: Any) -> type[BaseSettings] | None:
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


class YamlSettingsSource(PydanticBaseSettingsSource):
    """把一个 YAML 文件当作 pydantic-settings 的配置源。"""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        path: Path | None,
        *,
        label: str,
    ) -> None:
        super().__init__(settings_cls)
        self._label = label
        self._path = path
        self._data: dict[str, Any] = {}
        if path is None:
            return
        # 显式指定了路径却不存在的处理，和"根本没配置"必须区分：
        # 前者是配置事故（操作者明确要求读这个文件，文件却不在），必须报错 ——
        # 静默忽略会让服务带着缺失的密钥正常启动，直到真正发消息才 503。
        # 后者由调用方用 None 表示，走默认值。
        if not path.is_file():
            raise ConfigurationError(
                f"{label}文件被显式指定但不存在: {path}。"
                "请确认路径正确，或取消该环境变量以回退到默认值"
            )
        raw = load_yaml_mapping(path)
        if not raw:
            return
        prefix = settings_cls.model_config.get("env_prefix", "") or ""
        unknown = find_unknown_keys(raw, settings_cls, prefix)
        if unknown:
            logger.warning(
                "%s 配置 %s 中存在未知配置项: %s。"
                "它们会被忽略 —— 请检查是否拼写错误（只报告键名，不含值）",
                self._label,
                path,
                ", ".join(unknown),
            )
        self._data = to_nested(raw, prefix)

    @property
    def path(self) -> Path | None:
        return self._path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: ANN401
        # 本源按整体字典提供数据，不参与逐字段解析
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:
        return f"YamlSettingsSource(label={self._label!r}, keys={sorted(self._data)})"


def resolve_config_paths() -> tuple[Path | None, Path | None]:
    """定位两份 YAML 配置。

    Returns:
        ``(mask_path, project_path)``。任一项为 ``None`` 表示该层不存在，
        由上层按"没有这层配置"处理（而不是报错）。
    """
    mask_raw = os.environ.get("MINIMAX_AGENT_MASK_CONFIG_FILE", "").strip()
    mask = Path(mask_raw) if mask_raw else None

    project_raw = os.environ.get("MINIMAX_AGENT_PROJECT_CONFIG_FILE", "").strip()
    if project_raw:
        project = Path(project_raw)
    else:
        # 方案 A：项目配置固定从工作目录读，随 release 一起走
        default = Path("config.yaml")
        project = default if default.is_file() else None

    return mask, project


__all__ = [
    "YamlSettingsSource",
    "find_unknown_keys",
    "load_yaml_mapping",
    "resolve_config_paths",
    "to_nested",
]
