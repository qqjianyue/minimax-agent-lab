"""L2 冒烟 / L3 功能测试的环境变量支持。

单独成模块而不是放在 conftest 里，是因为 conftest 只对其所在目录及子目录生效，
而 ``tests/smoke`` 与 ``tests/functional`` 都需要同一套取值逻辑。

环境变量：

* ``AGENT_BASE_URL`` —— 被测服务地址，默认 ``http://127.0.0.1:8080``
* ``EXPECTED_VERSION`` —— 本次发布的版本串。冒烟测试拿它与 ``/healthz``
  返回的版本做**精确比对**：symlink 切换失败却误报成功，是这类脚本最危险的
  失败模式。
* ``AGENT_HOME`` —— 部署根，默认 ``/data/workspace/minimax-agent``。
  由 ``deploy/lib/common.sh`` 在跑在线测试层时注入，测试**不自己硬编码**目标机
  路径 —— 两边写死两处，迟早会对不上，而对不上的后果是"测试断言了一个
  根本没人用的目录"。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_AGENT_HOME = "/data/workspace/minimax-agent"

#: 审计账本相对部署根的位置。systemd 注入的 MINIMAX_AGENT_AUDIT__ROOT
#: 指向 shared/，账本再挂在 audit/ 下 —— 必须与 AuditSettings.path 一致。
AUDIT_RELATIVE_PATH = Path("shared") / "audit" / "ledger.jsonl"


def base_url() -> str:
    """被测服务地址。"""
    return os.environ.get("AGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def expected_version() -> str | None:
    """本次发布的版本串；未设置时冒烟测试的版本比对会跳过。"""
    return os.environ.get("EXPECTED_VERSION") or None


def agent_home() -> Path:
    """部署根目录。"""
    return Path(os.environ.get("AGENT_HOME") or DEFAULT_AGENT_HOME)


def audit_ledger_path() -> Path:
    """审计账本应当所在的位置。"""
    return agent_home() / AUDIT_RELATIVE_PATH


def shared_dir() -> Path:
    """跨版本共享目录。"""
    return agent_home() / "shared"


def current_release_dir() -> Path:
    """当前 release 的实际目录（软链接已解析）。"""
    return (agent_home() / "current").resolve()
