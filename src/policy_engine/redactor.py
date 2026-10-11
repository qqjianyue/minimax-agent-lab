"""脱敏端口。

``PolicyAction.REDACT`` 只是一条决策结论，真正把内容改掉的是脱敏器。
把它抽成 Protocol 有两个好处：

1. **依赖方向不倒挂**。知道"哪些字符是身份证/银行卡"的是 L1 规则层
   （``detector_rules``），而执行"按决策改写内容"是策略层
   （``policy_engine``）。让策略层 import 检测器层是错的；用 Protocol
   表达端口、由服务层组装，才是对的。
2. **可替换**。B5 接入 Presidio 时可以让它提供自己的脱敏实现，
   policy 层一行都不用改。

## 为什么脱敏必须跟着决策走（B4 之后实测得出的）

真实 ``/chat`` 实测抓到一个此前没人发现的漏洞：Presidio 判出 ``PERSON``、
policy 判了 ``redact``，但 :func:`policy_engine.actions.apply_action` 调用的
``RegexRedactor`` 只认 :data:`detector_rules.pii.PII_RULES` 那 4 条正则 ——
**Presidio 的实体从来不在脱敏器的知识范围里**。实测结果是
``action=redact`` 而 ``redacted=False``、占位符 0 个，原文一字未改地进了
模型、又原样返回给用户。

病根是**决策与执行用了两套检测**：决策看的是完整检测栈（含 Presidio），
执行却重新跑了一遍只有 C4 正则的脱敏器。"判了要脱敏"和"真的脱敏了"之间
没有任何强制关系 —— 只要新增一个检测器，这个洞就会再开一次。

修法是让脱敏**跟着决策走**：检测器把命中区间写进 ``metadata``（偏移量是
位置不是内容，落盘审计不构成泄露），:func:`redactions_from_results` 把它们
取成 :class:`Redaction`，再由 :func:`apply_redactions` 直接遮。这样
"判了就一定遮"从"指望两个检测器碰巧覆盖同一批实体"变成结构保证。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行期不引入
    from guard_contract.result import DetectorResult


def placeholder_for(kind: str) -> str:
    """占位符格式。实体类型（``CN_ID`` / ``PERSON`` / ``EMAIL``…）都走这里。

    刻意只有这一处定义：正则脱敏与决策驱动的区间脱敏必须产出**同一种**
    占位符，否则同一个实体在审计证据和用户可见文本里会长成两个样子。
    """
    return f"[REDACTED:{kind}]"


@dataclass(frozen=True, slots=True)
class Redaction:
    """一处被脱敏的片段。"""

    #: 实体类型，如 "CN_ID" / "BANK_CARD" / "EMAIL"
    kind: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """脱敏结果。"""

    text: str
    redactions: tuple[Redaction, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.redactions)

    @property
    def kinds(self) -> tuple[str, ...]:
        """去重后保序的实体类型。"""
        return tuple(dict.fromkeys(r.kind for r in self.redactions))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.redactions:
            out[r.kind] = out.get(r.kind, 0) + 1
        return out

    def summary(self) -> str:
        if not self.redactions:
            return "no redaction"
        return ", ".join(f"{kind} x{count}" for kind, count in self.counts().items())


@runtime_checkable
class Redactor(Protocol):
    """脱敏器端口。"""

    name: str

    def redact(self, text: str) -> RedactionResult:
        """返回脱敏后的文本与被脱敏的片段位置。"""
        ...


def redactions_from_results(results: Iterable[DetectorResult]) -> tuple[Redaction, ...]:
    """从检测结果里取出**可执行**的脱敏区间。

    只认 ``metadata`` 里成对出现的整数 ``start`` / ``end``。偏移量是**位置**
    不是内容，写进审计账本不构成泄露（原文仍在 ``evidence`` 里按各自规则
    脱敏）。

    缺任一端、类型不对、区间越界或空区间的结果一律跳过：宁可少遮也不能把
    文本改坏 —— 用错位的偏移量切片会静默截断用户内容，比漏遮更难发现。
    "每个检测器都必须产出合法区间"这条契约由 ``tests/unit/test_redaction.py``
    钉住。
    """
    out: list[Redaction] = []
    for result in results:
        meta = result.metadata or {}
        start, end = meta.get("start"), meta.get("end")
        # bool 是 int 的子类，显式排除，避免 True/False 被当成 1/0
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            continue
        if not (0 <= start < end):
            continue
        kind = meta.get("entity") or result.label
        out.append(Redaction(kind=str(kind), start=start, end=end))
    return tuple(out)


def apply_redactions(text: str, redactions: Sequence[Redaction]) -> RedactionResult:
    """按给定区间替换为占位符。

    重叠取**覆盖范围最大**的那个（同一段文字被两条规则命中只遮一次），
    并从右往左替换 —— 从左往右会让同一实体多次出现时后续偏移全部错位。
    """
    if not text or not redactions:
        return RedactionResult(text=text)

    ordered = sorted(redactions, key=lambda r: (r.start, -r.length))
    kept: list[Redaction] = [ordered[0]]
    for r in ordered[1:]:
        last = kept[-1]
        if r.start < last.end:
            if r.length > last.length:
                kept[-1] = r
            continue
        kept.append(r)

    result = text
    for r in sorted(kept, key=lambda r: r.start, reverse=True):
        result = result[: r.start] + placeholder_for(r.kind) + result[r.end :]

    return RedactionResult(
        text=result,
        redactions=tuple(sorted(kept, key=lambda r: r.start)),
    )


__all__ = [
    "Redaction",
    "RedactionResult",
    "Redactor",
    "apply_redactions",
    "placeholder_for",
    "redactions_from_results",
]
