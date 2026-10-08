#!/usr/bin/env python3
"""读取 deploy/config.yaml —— 部署脚本的配置源。

## 为什么需要这个文件

``build.*`` 是**本机**路径，``runtime.*`` 是**目标机**路径。部署脚本要
同时读两侧声明，必须有一个统一的读取入口；目标机没有 ``jq``，而首次
安装时 venv 还不存在（PyYAML 装不进来），所以这里**只用标准库**。

## 为什么不用 PyYAML 解析

目标机系统 ``python3`` 不保证有 PyYAML。``deploy/config.yaml`` 又是固定的
两层扁平结构，用几十行标准库代码就能可靠解析，避免为一个配置文件引入依赖。
遇到缩进不一致或缺键一律**明确报错**，绝不静默回退到默认值 —— 路径配错
必须在部署前炸掉，而不是部署后才发现文件放错地方。

## 路径翻译（Windows 本机）

``build.mask-config: /workspace/mask-config.yaml`` 这种 POSIX 写法，在
Windows 上直接用会被解析到 ``C:\\Program Files\\Git\\workspace\\...``（MSYS
路径映射），而不是 ``C:\\workspace\\...``。``resolve --as local`` 会遍历
可用盘符做存在性检查，返回真实存在的那一个；都不存在时返回原值，
让错误信息如实显示"配置里写的是什么"。

## 用法

::

    deployconfig.py get build.mask-config          # 原始值
    deployconfig.py resolve build.mask-config --as local   # 本机可读路径
    deployconfig.py resolve runtime.mask-config --as remote # 目标机路径（原样）
    deployconfig.py list                            # 全部键值
"""

from __future__ import annotations

import argparse
import os
import re
import string
import sys
from pathlib import Path

KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class ConfigError(Exception):
    """配置读取或解析失败。消息只含键名与文件路径，不含任何内容。"""


def parse_two_level(text: str) -> dict[str, dict[str, str]]:
    """解析两层扁平 YAML。

    只支持本项目实际使用的子集：``section:`` 下若干 ``key: value``。
    刻意不支持嵌套更深、列表、锚点、多行标量 —— 遇到就报错，
    而不是悄悄按字面量处理。
    """
    result: dict[str, dict[str, str]] = {}
    current: str | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        if "\t" in line[:indent]:
            raise ConfigError(f"第 {lineno} 行使用了制表符缩进，请改用空格")

        match = KEY_RE.match(line.strip())
        if match is None:
            raise ConfigError(f"第 {lineno} 行无法解析: {line.strip()[:40]!r}")
        key, value = match.group(1), match.group(2).strip()

        if value and value not in ("{}",):
            # 同行有值：只允许是顶层键（`section: value` 形式不接受）
            if indent == 0:
                raise ConfigError(
                    f"第 {lineno} 行不接受 `key: value` 顶层写法: {key!r}。"
                    "请使用两层结构"
                )
            _strip_quotes(value)
            if current is None:
                raise ConfigError(f"第 {lineno} 行的 {key!r} 不在任何 section 下")
            result[current][key] = _strip_quotes(value)
            continue

        if indent == 0:
            current = key
            result.setdefault(current, {})
        else:
            if current is None:
                raise ConfigError(f"第 {lineno} 行的 {key!r} 不在任何 section 下")
            result[current][key] = ""

    return result


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_config(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        raise ConfigError(f"部署配置不存在: {path}")
    try:
        return parse_two_level(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"部署配置无法读取: {path}（{type(exc).__name__}）") from exc


def get_value(config: dict[str, dict[str, str]], dotted: str) -> str:
    parts = dotted.split(".")
    if len(parts) != 2:
        raise ConfigError(f"键名格式应为 section.key，收到: {dotted!r}")
    section, key = parts
    if section not in config:
        raise ConfigError(f"配置中缺少 section {section!r}（可用: {', '.join(sorted(config))}）")
    if key not in config[section]:
        raise ConfigError(
            f"配置中缺少 {section}.{key}（该 section 可用: {', '.join(sorted(config[section]))}）"
        )
    value = config[section][key]
    if not value:
        raise ConfigError(f"{section}.{key} 的值为空")
    return value


def translate_local(value: str) -> Path:
    """把 POSIX 风格的绝对路径翻译成本机真实路径。

    仅在 Windows 上需要翻译（Git Bash / MSYS 会把 ``/workspace/...``
    映射到 Git 安装目录下）。Unix 上原样返回。
    """
    if os.name != "nt":
        return Path(value)
    text = value.replace("\\", "/")
    if not text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return Path(value)

    for drive in _available_drives():
        candidate = Path(f"{drive}{text}")
        if candidate.exists():
            return candidate
    # 都不存在：返回原值，让上层错误信息如实显示配置内容
    return Path(value)


def _available_drives() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # pragma: no cover - 防御性
        return [f"{c}:\\" for c in string.ascii_uppercase]
    return [f"{chr(ord('A') + i)}:\\" for i in range(26) if mask & (1 << i)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取 deploy/config.yaml")
    parser.add_argument("action", choices=("get", "resolve", "list"))
    parser.add_argument("key", nargs="?", help="section.key")
    parser.add_argument(
        "--as",
        dest="side",
        choices=("local", "remote"),
        default="remote",
        help="local = 翻译成本机路径；remote = 目标机路径（原样输出）",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
        if args.action == "list":
            for section in sorted(config):
                for key in sorted(config[section]):
                    print(f"{section}.{key} = {config[section][key]}")
            return 0

        if not args.key:
            raise ConfigError("get / resolve 需要指定 section.key")

        value = get_value(config, args.key)
        if args.action == "get" or args.side == "remote":
            print(value)
        else:
            print(translate_local(value))
        return 0
    except ConfigError as exc:
        print(f"[deployconfig] 错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
