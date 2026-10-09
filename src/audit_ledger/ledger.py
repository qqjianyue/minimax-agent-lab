"""审计账本对外门面：写入、检索、保留期清理。

三件事刻意放在一个类里，因为它们共享同一个不变式 ——
**记录一旦写入就不可更改**。拆成三个各自带状态的对象，
迟早会出现"清理逻辑不小心重写了文件"这种事。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_core.ports import Clock
from audit_ledger.record import AuditRecord
from audit_ledger.sink import JsonlAuditSink
from policy_engine.decision import Decision
from policy_engine.redactor import Redactor

ClockLike = Callable[[], datetime]


class SystemClockAdapter:
    """把 :class:`~agent_core.ports.Clock` 适配成"取当前时间"的 callable。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def __call__(self) -> datetime:
        return self._clock.now()


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """一次保留期清理的结果。

    ``removed`` / ``kept`` 都要报出来：清理是**破坏性**操作，
    运维需要知道到底动了多少东西，而不是只看到一个"完成"。
    """

    total_before: int
    removed: int
    kept: int
    cutoff: str

    @property
    def changed(self) -> bool:
        return self.removed > 0


class AuditLedger:
    """append-only 审计账本。

    **进程模型**：账本设计为**单进程独占**。``enforce_retention`` 的整文件
    重写与并发追加存在竞态窗口（重写读到的是旧快照，期间新追加的记录可能
    被覆盖），因此**不支持多 worker 共享同一文件**。当前部署为单实例
    systemd 服务，无此问题；若未来启用多进程（如 ``uvicorn --workers>1``），
    需要引入文件锁或改用 SQLite（决策 Q8 预留了替换路径）。
    """

    def __init__(
        self,
        sink: JsonlAuditSink,
        *,
        clock: ClockLike,
        redactor: Redactor,
        retention_days: int = 90,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._redactor = redactor
        self._retention_days = retention_days

    @property
    def path(self) -> Path:
        return self._sink.path

    def record(
        self,
        decision: Decision,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """记录一次策略判定。"""
        record = AuditRecord.from_decision(
            decision,
            recorded_at=self._clock(),
            redactor=self._redactor,
            metadata=metadata,
        )
        self._sink.append(record)
        return record

    def records(self) -> Iterator[AuditRecord]:
        return self._sink.read_all()

    def count(self) -> int:
        return self._sink.count()

    def query(
        self,
        *,
        request_id: str | None = None,
        tenant: str | None = None,
        action: Any | None = None,
        stage: Any | None = None,
        since: datetime | None = None,
    ) -> list[AuditRecord]:
        """按条件检索。

        刻意支持"按 request_id 查完整链路" —— 这是排障最常见的入口：
        一次请求的所有检测点记录应当能一次捞出来。
        """
        action_value = getattr(action, "value", action)
        stage_value = getattr(stage, "value", stage)

        out: list[AuditRecord] = []
        for record in self.records():
            if request_id is not None and record.request_id != request_id:
                continue
            if tenant is not None and record.tenant != tenant:
                continue
            if action_value is not None and record.action.value != action_value:
                continue
            if stage_value is not None and record.stage.value != stage_value:
                continue
            if since is not None and record.recorded_at < since.isoformat():
                continue
            out.append(record)
        return out

    def enforce_retention(self, *, now: datetime | None = None) -> RetentionReport:
        """删除超出保留期的记录。

        这是**唯一**会删除数据的操作，且只删整条记录、不修改内容 ——
        删除是保留期策略的一部分，不违反 append-only（append-only 约束的是
        "已写入的记录不得被改写"，过期删除是明确的生命周期策略）。

        实现上重写整个文件。这是 append-only 格式的固有代价：
        JSONL 没有"就地删除"。因此这里**必须**原子替换 ——
        先写临时文件再 ``os.replace``，中断时原文件保持完整，
        绝不能出现"清理过程中账本不可读"的中间态。
        """
        reference = now or self._clock()
        cutoff_dt = reference - timedelta(days=self._retention_days)
        cutoff = cutoff_dt.isoformat()

        kept: list[AuditRecord] = []
        removed = 0
        total = 0
        for record in self.records():
            total += 1
            # 字符串比较即可：ISO-8601 在同一时区偏移下字典序 == 时间序。
            # 账本写入时统一用 Clock.now()，时区偏移一致。
            if record.recorded_at < cutoff:
                removed += 1
            else:
                kept.append(record)

        if removed:
            self._rewrite(kept)

        return RetentionReport(
            total_before=total,
            removed=removed,
            kept=len(kept),
            cutoff=cutoff,
        )

    def _rewrite(self, records: list[AuditRecord]) -> None:
        import json

        target = self._sink.path
        tmp = target.with_suffix(target.suffix + ".tmp")
        # 临时文件是新建的，权限随 umask（通常 644）；审计文件含脱敏后的
        # 证据片段，权限必须与原文一致（如 600），否则一次清理就把
        # 整本账本的访问控制悄悄放宽了。
        try:
            original_mode = target.stat().st_mode & 0o777
        except OSError:  # pragma: no cover - 目标存在才会走到这里
            original_mode = 0o600
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for record in records:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, original_mode)
        os.replace(tmp, target)


__all__ = ["AuditLedger", "RetentionReport", "SystemClockAdapter"]
