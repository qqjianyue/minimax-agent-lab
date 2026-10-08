"""L2 冒烟 / L3 功能测试的环境变量支持。

单独成模块而不是放在 conftest 里，是因为 conftest 只对其所在目录及子目录生效，
而 ``tests/smoke`` 与 ``tests/functional`` 都需要同一套取值逻辑。

环境变量：

* ``AGENT_BASE_URL`` —— 被测服务地址，默认 ``http://127.0.0.1:8080``
* ``EXPECTED_VERSION`` —— 本次发布的版本串。冒烟测试拿它与 ``/healthz``
  返回的版本做**精确比对**：symlink 切换失败却误报成功，是这类脚本最危险的
  失败模式。
"""

from __future__ import annotations

import os

DEFAULT_BASE_URL = "http://127.0.0.1:8080"


def base_url() -> str:
    """被测服务地址。"""
    return os.environ.get("AGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def expected_version() -> str | None:
    """本次发布的版本串；未设置时冒烟测试的版本比对会跳过。"""
    return os.environ.get("EXPECTED_VERSION") or None
