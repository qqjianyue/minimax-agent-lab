"""bash 定位器的单元测试（L0 离线）。

## 为什么值得测

``tests/bash_support.py`` 决定"用哪个 bash 去跑 .sh 测试"。这个选择错了，
症状非常隐蔽：

* 选到 WSL 的 bash → ``bash -n /c/workspace/...`` 报"文件不存在"，
  看起来像**脚本语法有问题**，实际是选错了解释器；
* 选到根本起不来的 bash → 报 WinError 5（拒绝访问），
  看起来像**环境坏了**，实际也是选错了。

两种都会伪装成"被测代码有问题"，所以定位规则本身要被钉住。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import bash_support
from tests.bash_support import shell_path


def test_shell_path_converts_windows_drive_to_msys() -> None:
    """Windows 上 ``C:\\a\\b`` 必须转成 ``/c/a/b``。

    反斜杠不能原样喂给 MSYS —— 它会被当成转义序列，
    实测表现为 WinError 5（拒绝访问），而不是"路径不存在"。
    """
    if os.name != "nt":
        pytest.skip("Windows 分支只在 Windows 上验证")
    assert shell_path(Path(r"C:\workspace\a\b.sh")) == "/c/workspace/a/b.sh"


def test_shell_path_keeps_posix_path_unchanged() -> None:
    """POSIX 上原样返回 —— 目标机是 Linux，路径不能被改写。"""
    if os.name == "nt":
        pytest.skip("POSIX 分支只在非 Windows 上验证")
    assert shell_path(Path("/data/workspace/x")) == "/data/workspace/x"


def test_wsl_bash_is_excluded() -> None:
    """WSL 入口必须被排除。

    ``C:\\Windows\\System32\\bash.exe`` 的路径语义是 Linux 的，
    GitHub 的 windows runner 镜像里它也只有组件、没有发行版。
    """
    assert bash_support._is_excluded(r"C:\Windows\System32\bash.exe")
    assert not bash_support._is_excluded(r"C:\Program Files\Git\bin\bash.exe")
    assert not bash_support._is_excluded("/usr/bin/bash")


def test_find_bash_never_returns_wsl() -> None:
    """无论 PATH 上有什么，定位器都不能返回 WSL 入口。"""
    found = bash_support.find_bash()
    assert found is None or not bash_support._is_excluded(found)


def test_bash_found_is_actually_usable() -> None:
    """定位到的 bash 必须真的能执行命令，不能只看文件存在。"""
    found = bash_support.find_bash()
    if found is None:
        pytest.skip("本机没有可用的 GNU bash")
    assert bash_support._usable(found)
