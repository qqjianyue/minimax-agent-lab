"""评估数据集：样本模型、加载器与内置红队集。

四类攻击（对应面试口径"测试集包含直接注入、间接注入、数据泄露和工具滥用"）
外加**良性样本** —— 良性样本专门挑"长得像攻击但其实是正常业务"的用例，
用来监控 false positive：银行客服场景里误杀直接影响用户体验，FP 必须
和 FN 一样被量化，而不是假装不存在。

每个样本标注 ``expected``（期望动作）与 ``category``。跑分时指标按类别
分别统计 —— 只看整体 P/R/F1 会把"间接注入全漏"和"良性全误杀"混成一锅粥，
分类口径才能定位"是哪个信任边界出了问题"。
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

#: 期望动作：allow（放行）/ block（拦截）/ redact（脱敏后放行）。
#: redact 在指标上算"拦截"（改变了数据流，PII 不会明文流出）。
ALLOW = "allow"
BLOCK = "block"
REDACT = "redact"
EXPECTED_VALUES = frozenset({ALLOW, BLOCK, REDACT})

#: 攻击类别（面试口径四类 + 良性）。
CATEGORY_DIRECT_INJECTION = "direct_injection"
CATEGORY_INDIRECT_INJECTION = "indirect_injection"
CATEGORY_DATA_LEAK = "data_leak"
CATEGORY_TOOL_ABUSE = "tool_abuse"
CATEGORY_BENIGN = "benign"


class EvalCase(BaseModel):
    """单个评估样本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 稳定用例 id（报告/回归对比的键，不允许重复）
    id: str
    #: 待检测文本
    text: str
    #: 类别，见 CATEGORY_* 常量
    category: str
    #: 期望动作：allow / block / redact
    expected: str
    #: 说明（为什么这么标注 / 覆盖的绕过手法）
    note: str = ""
    #: 是否参与本次跑分。False 表示"数据集已收录但当前架构还不能承诺拦截"
    #: （如 B8 之前的间接注入），报告里单列、不计入 P/R/F1。
    enabled: bool = True

    @field_validator("id", "text", "category", "expected")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("字段不能为空")
        return value.strip()

    @field_validator("expected")
    @classmethod
    def _expected_in(cls, value: str) -> str:
        if value not in EXPECTED_VALUES:
            raise ValueError(
                f"非法的期望动作 {value!r}，可选值: {sorted(EXPECTED_VALUES)}"
            )
        return value


class EvalDataset(BaseModel):
    """一组评估样本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 数据集名（报告里可追溯用的来源）
    name: str
    cases: tuple[EvalCase, ...]

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, cases: tuple[EvalCase, ...]) -> tuple[EvalCase, ...]:
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError(f"数据集存在重复用例 id: {sorted(ids)}")
        return cases

    @property
    def enabled_cases(self) -> tuple[EvalCase, ...]:
        return tuple(c for c in self.cases if c.enabled)

    @property
    def disabled_cases(self) -> tuple[EvalCase, ...]:
        return tuple(c for c in self.cases if not c.enabled)

    @classmethod
    def from_file(cls, path: str | Path) -> EvalDataset:
        """从 JSON 文件加载数据集。

        文件形状：``{"name": "...", "cases": [{...}, ...]}``。
        """
        p = Path(path)
        try:
            raw: Any = __import__("json").loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(f"数据集文件不存在: {p}") from None
        except (UnicodeDecodeError, __import__("json").JSONDecodeError) as exc:
            raise ValueError(f"数据集文件不是合法 JSON: {p} ({exc})") from exc
        if not isinstance(raw, dict) or "cases" not in raw:
            raise ValueError(f"数据集文件缺少 'cases' 数组: {p}")
        return cls.model_validate(raw)


def builtin_dataset() -> EvalDataset:
    """加载包内内置红队数据集（``data/redteam.json``）。"""
    text = resources.files("eval_harness").joinpath("data/redteam.json").read_text(
        encoding="utf-8"
    )
    raw: dict[str, Any] = __import__("json").loads(text)
    return EvalDataset.model_validate(raw)


def load_dataset(path: str | Path | None) -> EvalDataset:
    """按路径加载；``None`` 时返回内置数据集。"""
    return builtin_dataset() if path is None else EvalDataset.from_file(path)


__all__ = [
    "ALLOW",
    "BLOCK",
    "REDACT",
    "EXPECTED_VALUES",
    "CATEGORY_DIRECT_INJECTION",
    "CATEGORY_INDIRECT_INJECTION",
    "CATEGORY_DATA_LEAK",
    "CATEGORY_TOOL_ABUSE",
    "CATEGORY_BENIGN",
    "EvalCase",
    "EvalDataset",
    "builtin_dataset",
    "load_dataset",
]
