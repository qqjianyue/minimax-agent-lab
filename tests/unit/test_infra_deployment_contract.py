"""部署配置的**契约不变式**（遥测开关与基础设施镜像固定）。

## 为什么值得单独测

这两条都不是"功能"，而是**前提**。它们坏掉时的共同特征是：

**系统看起来完全正常，但有一整个能力静默消失了。**

- 遥测开关不在模板里 → ``shared/.env`` 重建时遥测被关掉，
  服务照常起、``/healthz`` 照常 ``ok``、审计账本照常写，
  但**一个 span 都不导出**，Phoenix 在旁边空转。
  曾经真实发生过：目标机上这一行是手工加的，仓库模板里没有，
  于是"机器重建配置"就足以让可观测性无声消失。
- 镜像 tag 跟 ``latest`` → 一次 ``compose pull`` 就把生产环境换成
  上游新推的未验证版本。实测上游 ``latest`` 在 2026-10-23 已从
  ``20.20.0`` 漂到 ``20.26.0``。

两者都不会让任何测试失败 —— 跑测试的人不会想到"遥测是不是开着"。
所以这里把它们钉成不变式：**配置形状一旦回退，测试立刻红。**

## 与 test_deploy_entrypoints.py 的分工

那份测"脚本有没有真的执行"（入口完整性、调用顺序）；
这份测"配置里该有的东西在不在"（内容契约）。两者互补，不重叠。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"
INFRA_DIR = REPO_ROOT / "infra"

#: 环境变量赋值行的匹配（只取 KEY=VALUE，忽略注释与空行）
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _read_env_assignments(path: Path) -> dict[str, str]:
    """解析 dotenv 风格的赋值。注释行与空行忽略。"""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN_RE.match(raw)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# 1. 遥测开关必须在模板里，且必须是打开的
# ---------------------------------------------------------------------------


def test_env_template_enables_telemetry() -> None:
    """``deploy/env.template`` 必须显式打开遥测。

    代码默认值是 ``TelemetrySettings.enabled = False`` —— 关着时
    ``build_telemetry`` 返回 ``NoOpTelemetry``。所以模板里这一行不是
    "可有可无的示例"，而是**唯一**的开关点：它不在模板里，遥测就
    只能靠手工加，且 ``install.sh`` 重建 ``shared/.env`` 时会被抹掉。

    断言值必须是 ``true``：写成 ``false`` 同样满足"键存在"，
    但语义完全相反 —— 那是把观测关掉。
    """
    env = _read_env_assignments(DEPLOY_DIR / "env.template")
    assert "MINIMAX_AGENT_TELEMETRY__ENABLED" in env, (
        "deploy/env.template 缺少 MINIMAX_AGENT_TELEMETRY__ENABLED —— "
        "遥测默认关闭，shared/.env 重建时会静默丢失，"
        "而服务与 Phoenix 都表现得一切正常"
    )
    assert env["MINIMAX_AGENT_TELEMETRY__ENABLED"].lower() == "true", (
        f"MINIMAX_AGENT_TELEMETRY__ENABLED 应为 true，实际是 "
        f"{env['MINIMAX_AGENT_TELEMETRY__ENABLED']!r} —— "
        "关掉遥测会让 Phoenix 空转，而没有任何报错"
    )


def test_env_template_keeps_phoenix_endpoint() -> None:
    """遥测开关与 Phoenix 端点必须同时在模板里。

    只开开关、不指端点时，``build_telemetry`` 会往默认的
    ``http://127.0.0.1:6006`` 发 —— 本地开发碰巧也对，但生产机
    换了端口就静默指错。两行必须成对出现。
    """
    env = _read_env_assignments(DEPLOY_DIR / "env.template")
    assert "MINIMAX_AGENT_TELEMETRY__PHOENIX_ENDPOINT" in env, (
        "缺少 PHOENIX_ENDPOINT —— 遥测开了但不知道往哪发"
    )
    assert "MINIMAX_AGENT_TELEMETRY__CAPTURE_PROMPTS" in env, (
        "缺少 CAPTURE_PROMPTS —— 银行场景默认不得把 prompt/response "
        "原文写进 trace"
    )


# ---------------------------------------------------------------------------
# 2. 基础设施镜像必须固定到具体 tag
# ---------------------------------------------------------------------------


def test_phoenix_image_tag_is_pinned() -> None:
    """``infra/phoenix/env.template`` 的 tag 必须固定，不能是 ``latest``。

    实测：上游 ``latest`` 在 2026-10-23 已从 ``20.20.0`` 漂到
    ``20.26.0``（digest ``095f4987…``）。跟随 latest 意味着一次
    ``compose pull`` 就把生产环境换成没验证过的版本 ——
    而"回退到上个稳定版本"的前提正是"知道上个稳定版本是哪一版"。
    """
    env = _read_env_assignments(INFRA_DIR / "phoenix" / "env.template")
    assert "PHOENIX_IMAGE_TAG" in env, "infra/phoenix/env.template 缺少 PHOENIX_IMAGE_TAG"
    tag = env["PHOENIX_IMAGE_TAG"]
    assert tag and tag != "latest", (
        f"PHOENIX_IMAGE_TAG 必须是固定的具体版本，实际是 {tag!r} —— "
        "跟随 latest 会在一次 pull 里把生产环境换成未验证版本"
    )
    # 固定版本应当长得像版本号而不是别的东西
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag), (
        f"PHOENIX_IMAGE_TAG 看起来不像具体版本号: {tag!r}"
    )


def test_phoenix_compose_has_no_latest_fallback() -> None:
    """compose.yaml 里 tag **不能有默认值**。

    ``${PHOENIX_IMAGE_TAG:-latest}`` 的意思是"env 里这行被误删了就
    静默回落 latest"。而 ``PHOENIX_IMAGE`` 用的是 ``:?``（缺失即报错），
    两者的容错语义不一致 —— 恰好在最需要报错的那一项上给了兜底。

    宁可启动失败，也不要在生产机上悄悄换一个没验证过的版本。
    """
    compose = (INFRA_DIR / "phoenix" / "compose.yaml").read_text(encoding="utf-8")
    assert "PHOENIX_IMAGE_TAG:-latest" not in compose, (
        "compose.yaml 给 PHOENIX_IMAGE_TAG 留了 latest 兜底 —— "
        "env 里这行被误删时会静默回落未验证版本"
    )
    assert "PHOENIX_IMAGE_TAG:?" in compose, (
        "compose.yaml 的 PHOENIX_IMAGE_TAG 应使用 `:?`（缺失即报错），"
        "与 PHOENIX_IMAGE 的容错语义保持一致"
    )


# ---------------------------------------------------------------------------
# 3. BOM 与 env.template 必须一致（BOM 是版本的唯一来源）
# ---------------------------------------------------------------------------


def _bom_phoenix() -> dict:
    bom = yaml.safe_load((INFRA_DIR / "BILL_OF_MATERIALS.yaml").read_text(encoding="utf-8"))
    for comp in bom["components"]:
        if comp["name"] == "phoenix":
            return comp
    pytest.fail("BILL_OF_MATERIALS.yaml 里找不到 phoenix 组件")


def test_bom_image_tag_matches_env_template() -> None:
    """BOM 的 ``image_tag`` 必须与 ``env.template`` 里的实际取值一致。

    BOM 是基础设施版本的唯一来源（Q7）。两处一旦对不上，
    "当前跑的是哪一版"就有两个答案 —— 而其中一个只有机器知道。
    """
    env = _read_env_assignments(INFRA_DIR / "phoenix" / "env.template")
    bom_tag = str(_bom_phoenix()["image_tag"])
    assert bom_tag == env["PHOENIX_IMAGE_TAG"], (
        f"BOM 记的 image_tag={bom_tag!r}，env.template 里是 "
        f"{env['PHOENIX_IMAGE_TAG']!r} —— 版本来源必须唯一"
    )
    assert bom_tag != "latest", "BOM 的 image_tag 不得是 latest"


def test_bom_records_image_digest() -> None:
    """BOM 必须记录 registry digest，不只是镜像 ID。

    ``image_id``（config digest）与 ``image_digest``（manifest list digest）
    是**两个层级**的哈希，不能互相替代：镜像 ID 用于本地比对
    ``docker inspect``，registry digest 用于比对上游是否漂移。

    少了 registry digest 就没有"上游这个 tag 是不是还指向原来那个镜像"
    的判据 —— 那正是 ``latest`` 会悄悄换掉生产环境的那件事。
    """
    phoenix = _bom_phoenix()
    for key in ("image_id", "image_digest"):
        assert key in phoenix, f"BOM 的 phoenix 组件缺少 {key}"
        value = str(phoenix[key])
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", value), (
            f"{key} 不是合法的 sha256 digest: {value!r}"
        )
    assert phoenix["image_id"] != phoenix["image_digest"], (
        "image_id 与 image_digest 是不同层级的哈希，不应相同"
    )
