"""闸门脚本的控制台编码回归测试（L0 离线）。

## 为什么需要这个测试

Windows 上 Python 标准流的编码取自 ANSI 代码页：GitHub Actions 的
``windows-latest`` 是 cp1252，本地中文系统是 cp936（GBK）。本项目的闸门脚本
（``scripts/tasks.py`` 及两个排障脚本）全程用中文汇报进度，``print()`` 在
cp1252 下必然抛 ``UnicodeEncodeError``。

这不是假想问题：``windows-latest`` 上的 CI 从 v0.1.4 起长期红灯，失败点正是
``scripts/tasks.py`` 里打印 ``[OK] lint 通过。`` 的那一行 —— **连测试都还没开始
跑**。它被"Lint 挂了"或"测试挂了"掩盖了很久，因为报错出现在 lint 之后、
测试之前，而 CI 日志里上一行恰好是 lint 的成功输出。

所以这个测试把控制台编码强行固定成 cp1252，再跑一遍闸门流程：
哪天有人把 ``enable_utf8_console()`` 删了或改坏了，这里立刻红。

## 钉住的是"闸门不能崩"，不是"必须用哪种编码"

断言只检查两件事：中文能原样写出去、闸门流程能跑完。
换别的编码策略（比如把中文提示改成英文）同样能过 —— 那是另一种正确的修法。
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

#: 受测脚本。三个入口都会向 stdout/stderr 打中文，Windows 上都可能撞编码问题。
SCRIPT_NAMES = ("tasks", "fp_baseline", "phoenix_metrics")


def _load(name: str) -> ModuleType:
    """按路径加载脚本。

    ``scripts/`` 不是包（排障脚本刻意只用标准库、且要能单独拷走运行），
    所以用 importlib 按文件路径加载，而不是 ``import scripts.tasks``。
    """
    script = SCRIPTS_DIR / f"{name}.py"
    if not script.is_file():
        pytest.skip(f"脚本不存在: {script}")
    spec = importlib.util.spec_from_file_location(f"{name}_encoding_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=SCRIPT_NAMES)
def script(request: pytest.FixtureRequest) -> ModuleType:
    return _load(request.param)


def _cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """造一个 cp1252 的文本流，模拟 GitHub Actions 的 windows-latest 控制台。"""
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="cp1252", errors="strict")


def test_enable_utf8_console_reencodes_cp1252_stream(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cp1252 的流必须被切成 UTF-8，中文才写得出去。"""
    buf, stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    script.enable_utf8_console()

    assert stream.encoding == "utf-8"
    print("中文输出")
    stream.flush()
    assert "中文输出" in buf.getvalue().decode("utf-8")


def test_enable_utf8_console_tolerates_streams_without_reconfigure(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被 pytest 捕获或被重定向替换的流没有 ``reconfigure``，这时要跳过而不是炸。"""

    class _NoReconfigure:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", _NoReconfigure())

    script.enable_utf8_console()  # 不抛异常即通过


def test_check_survives_cp1252_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """复现 CI 失败现场：cp1252 控制台下跑完整闸门流程，不得抛 UnicodeEncodeError。

    失败点历史上是 ``cmd_check`` 打印 ``[OK] {name} 通过。`` 的那一行。
    """
    tasks = _load("tasks")
    buf, stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    # 只关心"打印"这一步，真实 lint / 测试由 CI 自己跑
    monkeypatch.setattr(tasks, "cmd_lint", lambda ns: 0)
    monkeypatch.setattr(tasks, "_run_layer", lambda *args, **kwargs: 0)
    monkeypatch.setattr(tasks, "_report_combined_coverage", lambda: 0)

    tasks.enable_utf8_console()
    assert tasks.cmd_check(argparse.Namespace()) == 0

    stream.flush()
    out = buf.getvalue().decode("utf-8")
    assert "[OK] lint 通过。" in out
    assert "[OK] 全部本地闸门通过，可以进入部署流程。" in out


def test_run_passes_utf8_to_child_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程（pytest / ruff / coverage）也必须用 UTF-8 输出。

    它们的输出直接写向同一个 fd：父进程用 UTF-8、子进程用 cp1252 的话，
    CI 日志里就是一段混合编码的乱码。
    """
    tasks = _load("tasks")
    calls: list[dict] = []

    class _FakeSubprocess:
        @staticmethod
        def call(args: list[str], cwd: object = None, env: dict | None = None) -> int:
            calls.append({"args": args, "env": env})
            return 0

    monkeypatch.setattr(tasks, "subprocess", _FakeSubprocess())
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    tasks._run(["python", "-c", "pass"])

    assert len(calls) == 1
    env = calls[0]["env"]
    assert env is not None
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_run_keeps_caller_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用方显式指定 PYTHONIOENCODING 时以调用方为准。"""
    tasks = _load("tasks")
    calls: list[dict] = []

    class _FakeSubprocess:
        @staticmethod
        def call(args: list[str], cwd: object = None, env: dict | None = None) -> int:
            calls.append({"args": args, "env": env})
            return 0

    monkeypatch.setattr(tasks, "subprocess", _FakeSubprocess())
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    tasks._run(["python", "-c", "pass"])

    env = calls[0]["env"]
    assert env is not None
    assert env["PYTHONIOENCODING"] == "latin-1"


def test_main_enables_utf8_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """入口必须切编码 —— 否则没人会记得在调用前手动调一次。"""
    tasks = _load("tasks")
    called: list[bool] = []
    monkeypatch.setattr(tasks, "enable_utf8_console", lambda: called.append(True))
    monkeypatch.setattr(tasks, "COMMANDS", {"noop": lambda ns: 0})
    monkeypatch.setattr(sys, "argv", ["tasks.py", "noop"])

    assert tasks.main() == 0
    assert called == [True]
