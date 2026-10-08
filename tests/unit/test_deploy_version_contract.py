"""部署脚本与 Python 之间的版本号契约。

## 为什么这个契约值得单独测

版本号是回退机制的**验证依据**：冒烟测试拿 ``/healthz`` 返回的版本和本次发布的
版本做**精确比对**，不一致就判失败（防止 symlink 切换失败却误报成功）。

而版本号是**两边各算一半**的：

* bash 侧 ``derive_version()`` 生成 ``<semver>+g<sha>``，``export_release_env()``
  把它拆成 ``MINIMAX_AGENT_GIT_SHA`` 等环境变量写进 release 目录
* Python 侧 :class:`VersionInfo.display` 再用这些环境变量把字符串拼回来

只要有一边的拆解规则变了，冒烟就会**恒定失败**，而且失败信息是"版本不匹配"——
看不出是格式分叉。已发生过的真实 bug：``${version#*+g}`` 在版本串不含 ``+g`` 时
通配不匹配，**原样返回整个字符串**，导致 GIT_SHA 变成 "0.1.0"、最终 display
拼成 ``0.1.0+g0.1.0``。

这些用例直接调真 bash，不复刻 shell 逻辑 —— 复刻了就测不到真正的 bug。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core.version import VersionInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON_SH = REPO_ROOT / "deploy" / "lib" / "common.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="需要 bash 才能执行部署脚本",
)


def _shell_path(path: Path) -> str:
    """把 Windows 路径转成 MSYS 能识别的形式。

    Git Bash 认识 ``C:/a/b``，但对 ``C:\\a\\b`` 里的反斜杠处理不可靠。
    """
    text = str(path)
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:].replace(chr(92), '/')}"
    return text


def _pyproject_semver() -> str:
    """从 pyproject.toml 读出版本号。"""
    import re

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "pyproject.toml 里找不到 version 字段"
    return match.group(1)


def _bash(script: str, **env: str) -> str:
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "COMMON_SH": _shell_path(COMMON_SH),
            "REPO": _shell_path(REPO_ROOT),
            **env,
        },
    )
    return result.stdout


def _write_release_env(rel: Path, version: str) -> dict[str, str]:
    """跑真实的 ``export_release_env``，返回它写出的键值。"""
    _bash(
        'set -euo pipefail; source "$COMMON_SH"; export_release_env "$VERSION" "$RELDIR"',
        VERSION=version,
        RELDIR=_shell_path(rel),
    )
    parsed: dict[str, str] = {}
    for line in (rel / ".release-env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
    return parsed


class TestReleaseEnv:
    def test_semver_without_git_produces_empty_sha(self, tmp_path: Path) -> None:
        """无 git 提交时版本是纯 semver，GIT_SHA 必须为空。

        这是踩过的坑：``${version#*+g}`` 遇到不含 ``+g`` 的字符串会
        **原样返回整个版本串**，于是 GIT_SHA 变成 "0.1.0"。
        """
        env = _write_release_env(tmp_path, "0.1.0")

        assert env["MINIMAX_AGENT_GIT_SHA"] == ""

    def test_semver_is_always_written(self, tmp_path: Path) -> None:
        """项目本体不装进 venv，dist metadata 取不到版本。

        ``get_version_info()`` 会回退到 ``MINIMAX_AGENT_SEMVER``；不写这个变量，
        /healthz 就会报 ``0.0.0+unknown``，冒烟的版本比对必然失败。
        """
        env = _write_release_env(tmp_path, "0.1.0+gabc1234")

        assert env["MINIMAX_AGENT_SEMVER"] == "0.1.0"

    def test_semver_env_has_no_build_suffix(self, tmp_path: Path) -> None:
        """SEMVER 只放 semver 本体；"+g..." 由 git_sha 单独承载。"""
        env = _write_release_env(tmp_path, "2.3.4+gdeadbee")

        assert env["MINIMAX_AGENT_SEMVER"] == "2.3.4"

    def _version_info_from(self, rel: Path, version: str) -> VersionInfo:
        """按 ``get_version_info()`` 的真实优先级还原版本。

        它先试 dist metadata，失败才回退到环境变量。venv 里不装项目本体，
        所以目标机上走的一定是回退分支 —— 测试必须模拟同一条路径。
        """
        env = _write_release_env(rel, version)
        return VersionInfo(
            semver=env["MINIMAX_AGENT_SEMVER"],
            git_sha=env["MINIMAX_AGENT_GIT_SHA"],
            dirty=env["MINIMAX_AGENT_GIT_DIRTY"] == "1",
            build_time=env["MINIMAX_AGENT_BUILD_TIME"],
        )

    def test_semver_without_git_round_trips_to_same_display(self, tmp_path: Path) -> None:
        """端到端契约：bash 写出的东西，Python 必须能还原成同一个版本串。"""
        version = "0.1.0"

        assert self._version_info_from(tmp_path, version).display == version

    def test_git_sha_is_extracted_from_version(self, tmp_path: Path) -> None:
        env = _write_release_env(tmp_path, "0.1.0+gabc1234")

        assert env["MINIMAX_AGENT_GIT_SHA"] == "abc1234"

    def test_git_version_round_trips_to_same_display(self, tmp_path: Path) -> None:
        version = "0.1.0+gabc1234"

        assert self._version_info_from(tmp_path, version).display == version

    def test_build_time_is_always_present(self, tmp_path: Path) -> None:
        """构建时间为空会让 /healthz 少一个排障字段。"""
        env = _write_release_env(tmp_path, "0.1.0")

        assert env["MINIMAX_AGENT_BUILD_TIME"], "构建时间不应为空"

    def test_derived_version_round_trips(self, tmp_path: Path) -> None:
        """最强的契约：**真实部署流程产出的版本必须能原样还原**。

        复刻 install.sh / update.sh 的实际顺序：
        ``derive_version`` → ``release_identity`` → ``export_release_env`` → Python。

        无论仓库当前处于什么 git 状态（无提交 / 有提交 / 工作区脏），
        最终都要能被 Python 侧精确还原 —— 否则冒烟恒失败，而冒烟比对版本号
        正是防止"symlink 切换失败却误报成功"的安全网。
        """
        derived = _bash(
            'set -euo pipefail; source "$COMMON_SH"; derive_version "$REPO"'
        ).strip()
        version = _bash(
            'set -euo pipefail; source "$COMMON_SH"; release_identity "$VERSION"',
            VERSION=derived,
        ).strip()
        info = self._version_info_from(tmp_path, version)

        assert ".dirty" not in version, "release 身份不应带 .dirty 后缀"
        assert info.display == version, (
            f"部署流程产出 {version!r}，但 Python 还原成了 {info.display!r} —— "
            "冒烟测试会比对失败"
        )


class TestVenvFingerprint:
    """依赖指纹必须只覆盖依赖，不能被项目版本号牵着走。

    ``uv.lock`` 内嵌了项目自身版本，所以直接哈希整份 lock 的话，
    **每次发版指纹必变** —— venv 复用的快路径就废了，日志还会误报
    "重建 venv（约需数分钟）"而实际只花几十毫秒。
    """

    def test_missing_lock_yields_sentinel(self, tmp_path: Path) -> None:
        out = _bash(
            'set -euo pipefail; source "$COMMON_SH"; venv_fingerprint "$RELDIR"',
            RELDIR=_shell_path(tmp_path),
        ).strip()

        assert out == "no-lock"

    def test_fingerprint_ignores_project_version_bump(self, tmp_path: Path) -> None:
        """只改项目版本号（不动依赖）时，指纹必须保持不变。

        这里直接构造两份 uv.lock：除项目自身 version 外完全相同。
        """
        base = _write_uvlock(tmp_path / "a", project_version="0.1.1")
        bumped = _write_uvlock(tmp_path / "b", project_version="0.9.9")

        assert base != "", "fixture 生成失败"
        assert bumped != ""

        fp_a = self._fingerprint(tmp_path / "a")
        fp_b = self._fingerprint(tmp_path / "b")

        assert fp_a == fp_b, "仅项目版本变化不应改变依赖指纹"

    def test_fingerprint_changes_when_dependency_changes(self, tmp_path: Path) -> None:
        """依赖真的变了时，指纹**必须**变 —— 否则会复用错误的 venv。"""
        first = _write_uvlock(tmp_path / "c", project_version="0.1.1", extra_dep="httpx")
        second = _write_uvlock(tmp_path / "d", project_version="0.1.1", extra_dep="requests")

        assert first and second

        assert self._fingerprint(tmp_path / "c") != self._fingerprint(tmp_path / "d")

    def _fingerprint(self, rel: Path) -> str:
        return _bash(
            'set -euo pipefail; source "$COMMON_SH"; venv_fingerprint "$RELDIR"',
            RELDIR=_shell_path(rel),
        ).strip()


def _write_uvlock(
    rel: Path,
    *,
    project_version: str,
    extra_dep: str | None = None,
) -> str:
    """写一份最小可用的 uv.lock + pyproject.toml。

    指纹只解析 ``[[package]]`` 块里的 name/version，所以不需要 uv 参与生成。
    """
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "pyproject.toml").write_text(
        '[project]\nname = "minimax-agent"\nversion = '
        f'"{project_version}"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    packages = [
        '[[package]]\nname = "minimax-agent"\nversion = '
        f'"{project_version}"\nsource = {{ editable = "." }}\n',
        '[[package]]\nname = "httpx"\nversion = "0.28.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
    ]
    if extra_dep:
        packages.append(
            f'[[package]]\nname = "{extra_dep}"\nversion = "1.0.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
        )
    text = 'version = 1\nrequires-python = ">=3.12"\n\n' + "\n".join(packages)
    (rel / "uv.lock").write_text(text, encoding="utf-8")
    return text


class TestReleaseIdentity:
    def test_strips_dirty_suffix(self) -> None:
        assert (
            _bash(
                'set -euo pipefail; source "$COMMON_SH"; release_identity "0.1.0+gabc.dirty"'
            ).strip()
            == "0.1.0+gabc"
        )

    def test_leaves_clean_version_untouched(self) -> None:
        assert (
            _bash(
                'set -euo pipefail; source "$COMMON_SH"; release_identity "0.1.0+gabc"'
            ).strip()
            == "0.1.0+gabc"
        )

    def test_leaves_bare_semver_untouched(self) -> None:
        assert (
            _bash(
                'set -euo pipefail; source "$COMMON_SH"; release_identity "0.1.0"'
            ).strip()
            == "0.1.0"
        )


class TestDeriveVersion:
    def test_reads_semver_from_pyproject(self) -> None:
        """semver 必须来自 pyproject.toml，且与文件里写的完全一致。

        刻意**不硬编码版本号** —— 版本一改就把测试改红，久了就没人看红灯了。
        这里直接读 pyproject 断言，版本怎么变都不会假失败。
        """
        expected = _pyproject_semver()

        version = _bash(
            'set -euo pipefail; source "$COMMON_SH"; derive_version "$REPO"'
        ).strip()

        assert version.startswith(expected), f"pyproject 里是 {expected}，实际得到 {version!r}"

    def test_never_produces_double_g(self) -> None:
        """防御 '+g' 被重复拼接或错位切分。"""
        version = _bash(
            'set -euo pipefail; source "$COMMON_SH"; derive_version "$REPO"'
        ).strip()

        assert version.count("+g") <= 1
        if "+g" in version:
            assert version.split("+g")[1], "+g 后面必须有 sha"
