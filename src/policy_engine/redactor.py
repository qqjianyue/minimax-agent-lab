"""脱敏端口。

``PolicyAction.REDACT`` 只是一条决策结论，真正把内容改掉的是脱敏器。
把它抽成 Protocol 有两个好处：

1. **依赖方向不倒挂**。知道"哪些字符是身份证/银行卡"的是 L1 规则层
   （``detector_rules``），而执行"按决策改写内容"是策略层
   （``policy_engine``）。让策略层 import 检测器层是错的；用 Protocol
   表达端口、由服务层组装，才是对的。
2. **可替换**。B5 接入 Presidio 时可以让它提供自己的脱敏实现，
   policy 层一行都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


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


__all__ = ["Redaction", "RedactionResult", "Redactor"]
