"""审计账本**落盘位置**的不变式测试。

## 为什么这组测试存在

账本落在哪由**三处**各自声明：

1. ``deploy/systemd/minimax-agent.service.template`` ——
   ``Environment=MINIMAX_AGENT_AUDIT__ROOT=...``
2. ``deploy/status.sh`` —— 「审计统计」去哪个目录找账本
3. ``tests/target_support.py`` —— 冒烟测试断言账本在哪

三处各写一份，**改一处忘另两处**就会出事，而且两种出事方式都很隐蔽：

* 测试写死了旧路径 → 冒烟在目标机上失败 → **自动回退**（0.1.26 部署实测踩过：
  账本从 ``shared/audit/`` 挪到 ``shared/data/audit/``，写死的断言全数失败）
* ``status.sh`` 指向另一个目录 → 部署成功、服务正常写账本，但运维**永远看不到**
  审计数据（本次方案梳理时实测发现）

所以这里把"三者必须指向同一个目录"钉成不变式：模板改了而另外两处没跟上，
本地 ``pytest`` 就会失败，不必等目标机冒烟才发现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_core.config import AuditSettings
from tests.target_support import (
    AUDIT_ROOT_ENV,
    audit_ledger_path,
    audit_root,
    deployed_audit_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_TEMPLATE = REPO_ROOT / "deploy" / "systemd" / "minimax-agent.service.template"
STATUS_SH = REPO_ROOT / "deploy" / "status.sh"
COMMON_SH = REPO_ROOT / "deploy" / "lib" / "common.sh"


def shell_vars(text: str) -> dict[str, str]:
    """抽出 shell 脚本里的 ``NAME="value"`` 形式的变量。

    允许 ``local `` 前缀 —— status.sh 里的 ``local audit_dir="..."`` 正是这种。
    """
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'^\s*(?:local\s+|export\s+)?([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"', text, re.M
        )
    }


def resolve(text: str, variables: dict[str, str]) -> str:
    """展开 ``${VAR}`` 引用（最多迭代若干轮，处理变量间的引用）。"""
    for _ in range(5):
        replaced = re.sub(
            r"\$\{([A-Z_][A-Z0-9_]*)\}",
            lambda m: variables.get(m.group(1), m.group(0)),
            text,
        )
        if replaced == text:
            break
        text = replaced
    return text


def unit_template_audit_root() -> Path:
    """unit 模板里声明的账本根目录（``%h`` 换成部署根的相对形式）。"""
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    prefix = f"Environment={AUDIT_ROOT_ENV}="
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            # 模板里是 ``%h/shared/data``；%h 由 deploy 的渲染脚本替换成
            # 指向部署根的软链，这里只取它后面的相对部分。
            return Path(value.replace("%h/", "", 1))
    raise AssertionError(f"unit 模板里没有声明 {AUDIT_ROOT_ENV}")


def status_sh_audit_dir() -> Path:
    """status.sh 的「审计统计」实际查的目录（相对部署根）。"""
    variables = shell_vars(COMMON_SH.read_text(encoding="utf-8"))
    variables["AGENT_HOME"] = ""
    for key in ("SHARED_DATA", "SHARED_DIR"):
        if key in variables:
            variables[key] = resolve(variables[key], variables)
    status_vars = shell_vars(STATUS_SH.read_text(encoding="utf-8"))
    raw = status_vars.get("audit_dir")
    assert raw, "status.sh 里找不到 audit_dir 变量"
    return Path(resolve(raw, variables))


def normalize(path: Path) -> str:
    """归一化成可比形式：统一分隔符、去掉首尾分隔符。

    status.sh 那边解析出来的是 ``${SHARED_DIR}/data``，而 SHARED_DIR 展开后是
    ``${AGENT_HOME}/shared`` —— AGENT_HOME 置空后开头多一个斜杠，
    于是"相对路径"和"绝对路径"只差一个前导斜杠。语义相同，但字符串不等。
    """
    return str(path).replace("\\", "/").strip("/")


class TestAuditLocationInvariants:
    def test_unit_template_declares_audit_root_under_shared(self) -> None:
        """账本根必须在共享目录下 —— 不能落在 release 里。

        落在 release 里，版本更新切 symlink 后新版本会在新目录从零写账本、
        旧记录"消失"，回退时账本跳变，release 清理时历史直接被删。
        """
        root = unit_template_audit_root()
        assert root.parts, "账本根目录不能为空"
        assert root.parts[0] == "shared", (
            f"账本根目录 {root} 不在 shared/ 下 —— 审计历史会随版本更新丢失"
        )

    def test_status_sh_audits_the_same_directory_as_the_unit(self) -> None:
        """status.sh 必须查 unit 声明的那个目录。

        对不上的后果不是报错，是**运维看不见**：服务照常写账本，
        部署照常成功，而「审计统计」那一栏永远是空的。
        """
        expected = unit_template_audit_root() / AuditSettings().path
        actual = status_sh_audit_dir()
        assert normalize(actual) == normalize(expected.parent), (
            f"status.sh 查 {actual}，unit 声明的账本在 {expected.parent} —— "
            "两处不一致，运维会看不到审计数据"
        )

    def test_audit_ledger_path_lands_under_the_declared_root(self) -> None:
        """账本路径 = 根目录 × 应用配置的相对路径。"""
        expected = unit_template_audit_root() / AuditSettings().path
        assert expected.name == "ledger.jsonl"
        assert expected.parent == unit_template_audit_root() / "audit"


class TestAuditRootResolution:
    def _write_unit(self, home: Path, root: str) -> Path:
        unit = home / ".config" / "systemd" / "user" / "minimax-agent.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Service]\n"
            "Environment=MINIMAX_AGENT_SECRETS_FILE=/x/.env\n"
            f"Environment={AUDIT_ROOT_ENV}={root}\n"
            "ExecStart=/x/python -m agent_service.main\n",
            encoding="utf-8",
        )
        return unit

    def test_reads_root_from_deployed_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self._write_unit(home, "/data/workspace/minimax-agent/shared/data")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert deployed_audit_root() == Path("/data/workspace/minimax-agent/shared/data")
        assert audit_root() == Path("/data/workspace/minimax-agent/shared/data")

    def test_unit_wins_over_environment_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量与 unit 不一致时以 unit 为准。

        断言的必须是**服务实际写的那份配置**。若以环境变量为准，
        测试就会去查一个服务根本没在写的地方 —— 断言通过，账本却在别处。
        """
        home = tmp_path / "home"
        home.mkdir()
        self._write_unit(home, "/data/workspace/minimax-agent/shared/data")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv(AUDIT_ROOT_ENV, "/somewhere/else")
        assert audit_root() == Path("/data/workspace/minimax-agent/shared/data")

    def test_falls_back_to_environment_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
        monkeypatch.setenv(AUDIT_ROOT_ENV, "/tmp/audit-root")
        assert audit_root() == Path("/tmp/audit-root")

    def test_falls_back_to_shared_under_agent_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
        monkeypatch.delenv(AUDIT_ROOT_ENV, raising=False)
        monkeypatch.setenv("AGENT_HOME", str(tmp_path / "agent"))
        assert audit_root() == tmp_path / "agent" / "shared"

    def test_ledger_path_composes_root_and_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self._write_unit(home, "/data/workspace/minimax-agent/shared/data")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert audit_ledger_path() == Path(
            "/data/workspace/minimax-agent/shared/data"
        ) / AuditSettings().path

    def test_missing_unit_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """本地跑（没有 unit）不能抛异常 —— 返回 None 走兜底即可。"""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
        assert deployed_audit_root() is None
