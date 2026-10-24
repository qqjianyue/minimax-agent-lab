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
* ``PHOENIX_ENDPOINT`` —— 遥测收集端，默认 ``http://127.0.0.1:6006``。
  阶段二的验收用例要直接查它，因此这里也要能从部署流程注入同一份值。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_core.config import AuditSettings

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_AGENT_HOME = "/data/workspace/minimax-agent"
DEFAULT_PHOENIX_ENDPOINT = "http://127.0.0.1:6006"

#: 账本根目录的环境变量名。与 systemd unit 里注入的是同一个。
AUDIT_ROOT_ENV = "MINIMAX_AGENT_AUDIT__ROOT"

#: 已部署的 systemd user unit 的位置。install_systemd_unit 写在
#: ``${HOME}/.config/systemd/user/`` 下（见 deploy/lib/common.sh）。
UNIT_RELATIVE_PATH = Path(".config") / "systemd" / "user" / "minimax-agent.service"


def base_url() -> str:
    """被测服务地址。"""
    return os.environ.get("AGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def expected_version() -> str | None:
    """本次发布的版本串；未设置时冒烟测试的版本比对会跳过。"""
    return os.environ.get("EXPECTED_VERSION") or None


def agent_home() -> Path:
    """部署根目录。"""
    return Path(os.environ.get("AGENT_HOME") or DEFAULT_AGENT_HOME)


def deployed_unit_path() -> Path:
    """已部署的 systemd user unit 文件。"""
    return Path.home() / UNIT_RELATIVE_PATH


def deployed_audit_root() -> Path | None:
    """从**已部署的 unit** 里读 ``MINIMAX_AGENT_AUDIT__ROOT``。

    为什么读 unit 而不在测试里写死账本路径：

    账本落在哪由 unit 模板声明（``Environment=MINIMAX_AGENT_AUDIT__ROOT=...``），
    而 unit 模板是**随版本走的部署资产**。测试若自己写死一份，就等于多了一份
    真源 —— 改了模板忘了改测试，冒烟会在目标机上失败并触发自动回退。
    这不是假设：0.1.26 部署时把账本从 ``shared/audit/`` 挪到 ``shared/data/audit/``，
    写死旧路径的断言全数失败，L2 冒烟挂掉并自动回退。

    从 unit 读同一个值，两边永远一致；而且断言更强 —— 测的是**部署机上真实的
    运行配置**，不是测试自己的一份猜测。

    unit 不存在（本地跑、首次部署前）时返回 ``None``，由调用方退回兜底值。
    """
    unit = deployed_unit_path()
    if not unit.is_file():
        return None
    prefix = f"Environment={AUDIT_ROOT_ENV}="
    for raw_line in unit.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip().strip('"').strip("'")
        if not value:
            return None
        return Path(value).expanduser()
    return None


def audit_root() -> Path:
    """账本根目录：已部署 unit > 环境变量 > 部署根下的 shared/。

    三档的顺序有讲究：**已部署的 unit 排第一**。它才是服务进程真正读的那份
    配置；环境变量只有在有人显式导出时才有值，而它可能与 unit 不一致 ——
    那时以 unit 为准，才能断言"服务实际写在哪"。
    """
    from_unit = deployed_audit_root()
    if from_unit is not None:
        return from_unit
    from_env = os.environ.get(AUDIT_ROOT_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return agent_home() / "shared"


def audit_ledger_path() -> Path:
    """审计账本应当所在的位置。

    = 账本根目录（unit 声明）× 相对路径（:attr:`AuditSettings.path`）。
    两段分别取自各自的唯一真源，测试不再自己写死第三份。
    """
    relative = AuditSettings().path
    if relative.is_absolute():
        return relative
    return audit_root() / relative


def shared_dir() -> Path:
    """跨版本共享目录。"""
    return agent_home() / "shared"


def current_release_dir() -> Path:
    """当前 release 的实际目录（软链接已解析）。"""
    return (agent_home() / "current").resolve()


def phoenix_endpoint() -> str:
    """遥测收集端地址。"""
    return (os.environ.get("PHOENIX_ENDPOINT") or DEFAULT_PHOENIX_ENDPOINT).rstrip("/")


def chat_until_model_called(
    http: Any,
    message: str,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    """发一条业务提问，直到**真的调用了模型**为止。

    ## 为什么需要重采样

    上游偶发 fail-closed：输入侧 L4 judge 的响应解析不出来 → 检测器 ERROR →
    按 ``fail_mode=closed`` 阻断 → **这条良性提问根本没走到模型**。
    实测（0.1.26 部署）：judge 调用返回 HTTP 200，但账本记录是
    ``matched=false`` / ``rule_name=null`` / ``evidence=[]`` ——
    fail-closed 的形状，不是命中的形状。缺陷已登记为 P0-2 / P1-1。

    ## 什么时候用、什么时候不用

    **只用在"一次 flaky 就会误判为真回归、进而错误回退健康版本"的地方**：
    取样型 fixture（trace / 成本验收）与不在降级闸门范围内的断言。

    **不放宽任何断言**：重采样之后仍然要求"这次真的调用了模型"，
    拿不到有效样本就照常失败。区别只在于**多取几次样**，而不是
    把"取不到样本"当成通过。

    降级闸门覆盖范围内的用例（``TestFT01NormalQuery`` 等）**不要**用它 ——
    那些用例的 flaky 失败会被闸门记成 skip 并留下证据，重采样反而
    会把"偶发"掩盖成"一次通过"，让 P0-2 的发生频率无从统计。
    """
    last: dict[str, Any] = {}
    for _ in range(max(1, attempts)):
        response = http.post("/chat", json={"message": message}, timeout=120.0)
        assert response.status_code == 200, response.text
        last = response.json()
        if last.get("llm_called") is True:
            return last
    raise AssertionError(
        f"{attempts} 次采样都没走到模型（上游偶发 fail-closed，见 P0-2）: {last}"
    )
