"""C8 `audit-ledger` —— append-only 决策记录。

对应架构方案 §1.2 的审计行（who / when / detector_version / verdict / evidence /
action）与 FT-12。按决策 Q8 先做简单版：JSONL 追加写入，
不做 SQLite 触发器之类的强化约束。

**不变式：已写入的记录不可改写。** 这一条由三处共同保证：
:meth:`AuditRecord.from_decision` 强制要求脱敏器（写入前就是对的）、
:class:`AuditRecord` 是 frozen dataclass（对象层不可变）、
:class:`JsonlAuditSink` 压根不提供更新入口（接口层没有改写路径）。
"""

from __future__ import annotations

from audit_ledger.ledger import AuditLedger, RetentionReport, SystemClockAdapter
from audit_ledger.record import AuditRecord
from audit_ledger.sink import JsonlAuditSink

__all__ = [
    "AuditLedger",
    "AuditRecord",
    "JsonlAuditSink",
    "RetentionReport",
    "SystemClockAdapter",
]
