"""Windows 上的 bash 定位与路径转换。

## 为什么值得单独写一个模块

Windows 上有**三个**叫 bash 的东西，语义完全不同：

===================================  ==================================
``C:\\Program Files\\Git\\bin\\bash.exe``  Git Bash，能跑仓库里的 .sh
``C:\\msys64\\usr\\bin\\bash.exe``         MSYS2，同样能跑
``C:\\Windows\\System32\\bash.exe``       **WSL 入口，跑不了**
===================================  ==================================

WSL 那个的路径语义是 Linux 的：``/c/workspace/...`` 在它眼里是"根目录下的
c 目录"，根本不存在。而且 GitHub 的 windows runner 镜像里 WSL 只装了组件、
**没装发行版**，直接执行会报错退出。

而 PATH 的命中顺序在不同机器上并不一致（有人装 MSYS2、有人把 WSL 排前面、
有人用 PortableGit），**不能信 ``shutil.which("bash")``**。

所以这里的策略是：显式排除 WSL 那个，再逐个探测候选是不是真的能执行命令，
用第一个能用的。找不到就返回 None —— 那表示"这台机器上没法跑 .sh 测试"，
用例会**显式 skip** 而不是假失败。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: WSL 入口。它不是 GNU bash 的 POSIX 兼容层，用它跑仓库脚本必错。
_WSL_BASH = os.path.normcase(r"C:\Windows\System32\bash.exe")

#: Windows 上已知的 GNU bash 位置，按"最可能可用"排序。
#: PortableGit 这类非标准安装位置靠 ``shutil.which`` 兜底。
_KNOWN_BASH = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\msys64\usr\bin\bash.exe",
)

#: 探测用的标记串。放在这里避免和被测脚本的输出撞车。
_PROBE = "minimax-bash-probe"


def shell_path(path: Path | str) -> str:
    """把 Windows 路径转成 MSYS / Git Bash 能识别的形式。

    Git Bash 认识 ``C:/a/b``，但对 ``C:\\a\\b`` 里的反斜杠处理不可靠 ——
    直接把 Windows 路径喂给 ``bash`` 可能得到 WinError 5（拒绝访问），
    因为 MSYS 把带反斜杠的路径当成了转义序列。
    """
    text = str(path)
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:].replace(chr(92), '/')}"
    return text


def _usable(candidate: str) -> bool:
    """探测候选 bash 是不是真能执行命令。

    只看文件存在是不够的：WSL 入口存在但不可用，安全软件拦截时文件也在。
    """
    try:
        probe = subprocess.run(
            [candidate, "-c", f"echo {_PROBE}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # OSError：进程起不来（权限/安全软件）；SubprocessError：超时
        return False
    return probe.returncode == 0 and _PROBE in (probe.stdout or "")


def _is_excluded(candidate: str) -> bool:
    """WSL 入口不能选 —— 它的路径语义是 Linux 的，跑不了仓库里的 .sh。"""
    return os.path.normcase(candidate) == _WSL_BASH


def find_bash() -> str | None:
    """找一个能真正执行仓库脚本的 GNU bash；找不到返回 None。"""
    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend(_KNOWN_BASH)
    found = shutil.which("bash")
    if found:
        candidates.append(found)

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(candidate)
        if key in seen or _is_excluded(candidate):
            continue
        seen.add(key)
        if not Path(candidate).is_file():
            continue
        if _usable(candidate):
            return candidate
    return None


#: 加载时探一次。探不到就是"这台机器没法跑 .sh 测试"，用例会显式 skip。
BASH = find_bash()
