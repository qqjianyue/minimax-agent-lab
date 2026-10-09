"""部署脚本的**入口完整性**。

## 为什么值得单独测

``bash -n`` 只能验证语法，**验证不了脚本有没有真的执行**。本项目踩过这个坑：
``update.sh`` 定义完所有函数就结束了，文件末尾少了 ``main "$@"``。

后果比崩溃严重得多：脚本**静默退出、返回 0、零输出** —— 部署脚本报告"成功"，
而原子切换、L2/L3 门禁、自动回退**一件都没执行**。没人会注意到，因为
exit code 是 0。这种失败模式比报错危险得多：报错的部署至少会停下来。

所以这里对所有"函数式"部署脚本断言：**最后一行有效代码必须是入口调用**。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"

#: 采用 "source 公共库 + 定义函数 + main 调用" 结构的入口脚本。
#: 它们的最后一行有效代码必须是 main 入口，否则整个脚本是空转的。
#:
#: preflight.sh 不在此列：它是**线性脚本**，逻辑直接写在顶层，
#: 没有 main 这一层结构。
ENTRY_SCRIPTS = (
    "install.sh",
    "update.sh",
    "rollback.sh",
    "status.sh",
    "sync-mask.sh",
)


def _meaningful_lines(path: Path) -> list[str]:
    """去掉空行与注释后的有效代码行。"""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_actually_invokes_main(name: str) -> None:
    path = DEPLOY_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")

    lines = _meaningful_lines(path)

    assert lines, f"{name} 是空文件"
    assert lines[-1] == 'main "$@"', (
        f"{name} 的最后一行有效代码是 {lines[-1]!r}，应为 'main \"$@\"'。"
        "脚本定义完函数就退出会导致：零输出 + 退出码 0 + 什么也没执行，"
        "而部署流程会把它当成功。"
    )


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_strict_mode(name: str) -> None:
    """每个入口都要开 ``set -euo pipefail``。

    没有它时，脚本中间的失败会被忽略并继续往下走，最终以 0 退出 ——
    和上面那个"静默成功"是同一类事故。
    """
    path = DEPLOY_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")

    assert "set -euo pipefail" in path.read_text(encoding="utf-8"), f"{name} 未启用严格模式"


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
@pytest.mark.parametrize("name", (*ENTRY_SCRIPTS, "preflight.sh"))
def test_script_passes_syntax_check(name: str) -> None:
    path = DEPLOY_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")

    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{name} 语法错误:\n{result.stderr}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_common_sh_refuses_direct_execution() -> None:
    """公共库被误当脚本执行时要明确拒绝，而不是静默什么都不做。"""
    common = DEPLOY_DIR / "lib" / "common.sh"
    result = subprocess.run(
        ["bash", str(common)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, "common.sh 直接执行应当非零退出"
    assert "请勿直接执行" in result.stdout + result.stderr


#: 调用 ensure_venv 的脚本 —— 它们都会用到 uv。
#: 本项目踩过：rollback.sh 调了 ensure_venv 却没调 ensure_uv，
#: 而 uv 装在 ~/.local/bin，非交互 ssh 会话的 PATH 里没有它。
USES_VENV = ("install.sh", "update.sh", "rollback.sh")


@pytest.mark.parametrize("name", USES_VENV)
def test_script_using_venv_also_bootstraps_uv(name: str) -> None:
    """用了 ``ensure_venv`` 就必须先 ``ensure_uv``，否则非交互会话里 uv 找不到。

    实际后果不是"少装个东西"：uv 不在 PATH 时脚本会在 ``set -e`` 下中途死掉。
    如果那一步在 ``switch_release`` **之后**，就会留下 current 指向新版本、
    服务却还在跑旧版本的不一致状态。
    """
    path = DEPLOY_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")

    text = path.read_text(encoding="utf-8")
    if "ensure_venv" not in text:
        pytest.skip(f"{name} 不使用 ensure_venv")

    assert "ensure_uv" in text, (
        f"{name} 调用了 ensure_venv 但没有 ensure_uv —— "
        "uv 在 ~/.local/bin，非交互 ssh 会话的 PATH 里没有它"
    )


@pytest.mark.parametrize("name", USES_VENV)
def test_prepares_before_switching_symlink(name: str) -> None:
    """所有可能失败的准备都必须排在 ``switch_release`` 之前。

    否则中途失败会留下"符号链接已切、服务没重启"的不一致状态：
    看起来回退/更新成功了，实际运行的还是旧代码。
    """
    path = DEPLOY_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")

    lines = _meaningful_lines(path)
    switch_at = next((i for i, ln in enumerate(lines) if "switch_release" in ln), None)
    if switch_at is None:
        pytest.skip(f"{name} 不切换版本")

    for risky in ("ensure_venv", "run_layer"):
        first_use = next((i for i, ln in enumerate(lines) if risky in ln and "()" not in ln), None)
        if first_use is None:
            continue
        assert first_use < switch_at, (
            f"{name}: {risky} 出现在 switch_release 之后（第 {first_use + 1} 行 vs "
            f"第 {switch_at + 1} 行）。中途失败会留下 current 已切换、"
            "服务未重启的不一致状态。"
        )


#: 会切换 release 的入口 —— 它们都必须在切换前刷新 systemd unit。
SWITCHING_SCRIPTS = ("install.sh", "update.sh", "rollback.sh")


@pytest.mark.parametrize("name", SWITCHING_SCRIPTS)
def test_refreshes_systemd_unit_before_switching(name: str) -> None:
    """切换 release 就必须重渲染 systemd unit，且要排在切换之前。

    ## 这个不变式守住什么

    unit 模板里的 ``Environment=`` 是**版本相关资产**。模板新增一行而脚本
    不刷新时，目标机上跑的还是安装时留下的旧 unit —— 新配置静默失效，
    **没有任何报错**：服务健康、接口正常，只是那个配置压根没生效。

    B4 的 ``MINIMAX_AGENT_AUDIT__ROOT`` 就是这么丢的：unit 只由 install.sh
    渲染，于是"审计账本写进 release 目录、随版本更新消失"这个问题在本地
    看起来已修复（本地根本没走过 unit），一上目标机照旧。

    排在切换之前还有个理由：unit 渲染失败要发生在 symlink 切换之前，
    否则又会留下"切了一半"的中间状态。
    """
    path = DEPLOY_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")

    text = path.read_text(encoding="utf-8")
    assert "install_systemd_unit" in text, (
        f"{name} 会切换 release 但不刷新 systemd unit —— "
        "unit 里新增的 Environment= 不会生效，且不会报任何错"
    )

    lines = _meaningful_lines(path)
    unit_at = next(i for i, ln in enumerate(lines) if "install_systemd_unit" in ln)
    switch_at = next(i for i, ln in enumerate(lines) if "switch_release" in ln)
    assert unit_at < switch_at, f"{name}: unit 刷新必须排在 switch_release 之前"


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_unit_installer_lives_in_common_library() -> None:
    """``install_systemd_unit`` 必须定义在公共库里。

    它是装/更/退三条路径共用的：定义在某个入口脚本里，另外两条就静默拿不到。
    这是个纯结构性陷阱 —— bash 不会告诉你"这个函数不存在"，只会在调用点
    报 ``command not found``，而那已经是发布流程跑到一半了。
    """
    common = DEPLOY_DIR / "lib" / "common.sh"
    assert "install_systemd_unit()" in common.read_text(encoding="utf-8")

    for name in SWITCHING_SCRIPTS:
        # 允许**调用**，不允许**重新定义**
        defining = [
            ln
            for ln in _meaningful_lines(DEPLOY_DIR / name)
            if ln.startswith("install_systemd_unit()")
        ]
        assert not defining, f"{name} 重新定义了 install_systemd_unit，应只用公共库的"
