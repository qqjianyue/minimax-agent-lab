"""版本信息。

版本是回退机制的基础设施：冒烟测试要能确认"目标机上跑的确实是我刚发布的那个版本"，
否则 symlink 切换失败时会被误判成部署成功。

版本号格式::

    <semver>+g<git-short-sha>          正常
    <semver>+g<git-short-sha>.dirty    工作区有未提交改动

semver 的唯一来源是 ``pyproject.toml``（由 hatchling 打包进 distribution metadata），
git sha 与构建时间由部署脚本通过环境变量注入 —— 不在构建期改写源码，
这样"代码内容"和"版本信息"是分离的，releases/ 下的目录可以做到可校验的不可变。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

from agent_core.errors import VersionError

DISTRIBUTION_NAME = "minimax-agent"

ENV_GIT_SHA = "MINIMAX_AGENT_GIT_SHA"
ENV_GIT_DIRTY = "MINIMAX_AGENT_GIT_DIRTY"
ENV_BUILD_TIME = "MINIMAX_AGENT_BUILD_TIME"

UNKNOWN_SEMVER = "0.0.0+unknown"

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


@dataclass(frozen=True, slots=True)
class VersionInfo:
    semver: str
    git_sha: str = ""
    dirty: bool = False
    build_time: str = ""

    @property
    def display(self) -> str:
        """人类可读、且可被冒烟测试精确比对的版本字符串。"""
        text = self.semver
        if self.git_sha:
            text = f"{text}+g{self.git_sha}"
        if self.dirty:
            text = f"{text}.dirty"
        return text

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "version": self.display,
            "semver": self.semver,
            "git_sha": self.git_sha,
            "dirty": self.dirty,
            "build_time": self.build_time,
        }


def parse_semver(value: str) -> tuple[int, int, int]:
    """解析 semver 主版本号三元组。

    供版本比较与"是否需要重建 venv"的判断使用（例如 0.2 -> 0.3 通常意味着
    依赖指纹变化）。
    """
    match = _SEMVER_RE.match(value.strip())
    if match is None:
        raise VersionError(f"不是合法的 semver: {value!r}")
    return int(match["major"]), int(match["minor"]), int(match["patch"])


def is_newer(candidate: str, baseline: str) -> bool:
    """candidate 是否严格新于 baseline（忽略 prerelease 与 build 段）。"""
    return parse_semver(candidate)[:3] > parse_semver(baseline)[:3]


def get_version_info() -> VersionInfo:
    """读取当前运行版本。优先取 distribution metadata，回退到环境变量。"""
    try:
        semver = _dist_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        # 未安装（例如直接从源码目录跑）时不应崩溃，冒烟测试会因此拿到可辨识的
        # 哨兵值并明确失败，而不是静默通过。
        semver = os.environ.get("MINIMAX_AGENT_SEMVER", UNKNOWN_SEMVER)

    return VersionInfo(
        semver=semver,
        git_sha=os.environ.get(ENV_GIT_SHA, ""),
        dirty=os.environ.get(ENV_GIT_DIRTY, "") == "1",
        build_time=os.environ.get(ENV_BUILD_TIME, ""),
    )


__all__ = [
    "DISTRIBUTION_NAME",
    "VersionInfo",
    "get_version_info",
    "is_newer",
    "parse_semver",
]
