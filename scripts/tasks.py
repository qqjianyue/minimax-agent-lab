"""跨平台任务入口。

Windows 本地开发机和 Linux 目标机共用同一套命令，避免维护两份 Makefile。
Makefile 只是这层接口的薄封装。

用法：
    uv run python scripts/tasks.py <command> [args...]

关于覆盖率口径（踩过的坑）
--------------------------
pytest-cov 默认**每次运行都会擦除**上一次的 ``.coverage``，所以分层执行时
后一层的报告会覆盖前一层的。``check`` 依次跑 L0 和 L1，如果各自打印报告，
最后看到的那份其实只有 L1 —— 看起来像整体覆盖率只有 68%，实际是 95%。

解决办法是让每层写自己的数据文件，跑完再合并：

* ``.coverage.l0`` / ``.coverage.l1`` —— 分层数据，互不覆盖
* ``.coverage``            —— 合并后的报告数据

因此 ``check`` 全程只打印**一份**报告，且明确标注是合并口径；
单层命令则明确标注"仅该层"，不留误读空间。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PY = REPO_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

#: 各测试层独立的覆盖率数据文件。分开存是"合并口径"的前提。
COV_UNIT = ".coverage.l0"
COV_INTEGRATION = ".coverage.l1"
#: 合并后的数据文件，供 coverage report 使用
COV_COMBINED = ".coverage"


def _run(args: list[str], env: dict[str, str] | None = None) -> int:
    """在仓库根目录执行命令，返回退出码。"""
    print(f"\n$ {' '.join(args)}\n", flush=True)
    # 覆盖而非替换：调用方只想改个别变量（主要是 COVERAGE_FILE）
    merged_env = {**os.environ, **env} if env else None
    return subprocess.call(args, cwd=REPO_ROOT, env=merged_env)


def _pytest(*extra: str, env: dict[str, str] | None = None) -> int:
    return _run([sys.executable, "-m", "pytest", *extra], env=env)


def cmd_setup(_: argparse.Namespace) -> int:
    """安装依赖（含 dev 组）。"""
    uv = shutil.which("uv")
    if uv is None:
        print("找不到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/", file=sys.stderr)
        return 1
    return _run([uv, "sync", "--all-groups"])


# --- L0 / L1：本地闸门 -------------------------------------------------------


def _run_layer(path: str, marker: str, *, cov_file: str, report: bool) -> int:
    """跑一层测试，写入**自己专属**的覆盖率数据文件。

    Args:
        report: 是否在这一层打印覆盖率报告。``check`` 传 False —— 分层报告
            必然是片面的，打印出来只会误导；改由最后统一合并后打印。

    这里固定关掉 ``--cov-fail-under``：pyproject 里的 ``fail_under`` 是
    **合并口径**的下限，而单层数字天然偏低（L1 单跑约 68%）。拿单层覆盖率
    当闸门只会制造无意义的红灯，真正执行门槛的是 check 与 test-offline。
    """
    extra = [
        path,
        "-m",
        marker,
        "--cov",
        f"--cov-report={'term-missing' if report else ''}",
        "--cov-fail-under=0",
    ]
    return _pytest(*extra, env={"COVERAGE_FILE": str(REPO_ROOT / cov_file)})


def cmd_test_unit(_: argparse.Namespace) -> int:
    """L0 单元测试。纯函数、离线、秒级。"""
    print("\n[i] 覆盖率口径：**仅 L0**。要看整体请跑 test-offline 或 check。\n")
    return _run_layer("tests/unit", "unit", cov_file=COV_UNIT, report=True)


def cmd_test_integration(_: argparse.Namespace) -> int:
    """L1 集成测试。Fake 依赖、离线。"""
    print("\n[i] 覆盖率口径：**仅 L1**。要看整体请跑 test-offline 或 check。\n")
    return _run_layer("tests/integration", "integration", cov_file=COV_INTEGRATION, report=True)


def cmd_test_offline(_: argparse.Namespace) -> int:
    """L0 + L1 一起跑（默认执行集合），覆盖率天然就是合并口径。"""
    print("\n[i] 覆盖率口径：L0 + L1 合并。\n")
    return _pytest(
        "tests/unit",
        "tests/integration",
        "-m",
        "unit or integration",
        "--cov",
        "--cov-report=term-missing",
        env={"COVERAGE_FILE": str(REPO_ROOT / COV_COMBINED)},
    )


def cmd_test_target(_: argparse.Namespace) -> int:
    """target 标记的测试。必须在目标机跑，且需要 --allow-network。"""
    return _pytest("-m", "target", "--allow-network")


def cmd_lint(_: argparse.Namespace) -> int:
    # ruff 是 dev group 依赖，恒定存在于当前 venv，不依赖 uv 是否在 PATH
    return _run([sys.executable, "-m", "ruff", "check", "src", "tests"])


def cmd_fmt(_: argparse.Namespace) -> int:
    return _run([sys.executable, "-m", "ruff", "format", "src", "tests"])


# --- 组合闸门 ----------------------------------------------------------------


def _report_combined_coverage() -> int:
    """合并各层覆盖率数据，打印**唯一一份**报告。"""
    data_files = [REPO_ROOT / COV_UNIT, REPO_ROOT / COV_INTEGRATION]
    available = [str(p) for p in data_files if p.exists()]
    missing = [p.name for p in data_files if not p.exists()]

    print("\n" + "=" * 66)
    print("[覆盖率] 合并口径 = L0 单元测试 + L1 集成测试")
    print("        这是两层合并后的结果，不是某一层单跑的数字。")
    if missing:
        print(f"        注意：缺少数据文件 {', '.join(missing)}，本次为部分合并。")
    print("=" * 66 + "\n", flush=True)

    if not available:
        print("[warn] 没有可合并的覆盖率数据，跳过报告。", file=sys.stderr)
        return 0

    env = {"COVERAGE_FILE": str(REPO_ROOT / COV_COMBINED)}
    code = _run([sys.executable, "-m", "coverage", "combine", *available], env=env)
    if code != 0:
        return code

    # coverage report 会读取 pyproject 的 fail_under，不达标时退出码为 2
    return _run([sys.executable, "-m", "coverage", "report", "--show-missing"], env=env)


def cmd_check(_: argparse.Namespace) -> int:
    """提交前完整闸门：lint -> L0 -> L1 -> 覆盖率门槛。任何一步失败立即停止。

    覆盖率**不在各层打印**，而是在两层都通过后统一合并打印一次 ——
    分层报告必然片面，打印出来只会让人误判整体质量。

    门槛值来自 ``pyproject.toml`` 的 ``[tool.coverage.report] fail_under``，
    是 **L0+L1 合并口径**的下限；单层不强制（见 :func:`_run_layer`）。
    """
    steps: list[tuple[str, object]] = [
        ("lint", cmd_lint),
        ("L0 单元测试", lambda ns: _run_layer("tests/unit", "unit", cov_file=COV_UNIT, report=False)),
        ("L1 集成测试", lambda ns: _run_layer("tests/integration", "integration", cov_file=COV_INTEGRATION, report=False)),
    ]
    for name, fn in steps:
        code = fn(argparse.Namespace())  # type: ignore[operator]
        if code != 0:
            print(f"\n[FAIL] {name} 未通过，停止后续步骤。", file=sys.stderr)
            return code
        print(f"\n[OK] {name} 通过。")

    cov_code = _report_combined_coverage()
    if cov_code != 0:
        # coverage 用退出码 2 表示"覆盖率低于门槛"，这是配置问题而非运行故障
        reason = "低于门槛" if cov_code == 2 else "报告生成失败"
        print(f"\n[FAIL] 覆盖率{reason}（门槛见 pyproject.toml 的 fail_under）。", file=sys.stderr)
        return cov_code

    print("\n[OK] 全部本地闸门通过，可以进入部署流程。")
    return 0


COMMANDS = {
    "setup": cmd_setup,
    "test-unit": cmd_test_unit,
    "test-int": cmd_test_integration,
    "test-offline": cmd_test_offline,
    "test-target": cmd_test_target,
    "lint": cmd_lint,
    "fmt": cmd_fmt,
    "check": cmd_check,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="minimax-agent 任务入口")
    parser.add_argument("command", choices=sorted(COMMANDS), help="要执行的任务")
    args = parser.parse_args()
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
