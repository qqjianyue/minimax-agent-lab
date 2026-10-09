"""append-only JSONL 存储。

## 为什么是 JSONL 而不是 SQLite

决策 Q8 已定：先简单版。但"简单"不等于"随便" —— JSONL 恰好**天然满足**
append-only：文件只能往后加，没有 UPDATE 语句可用，也就不存在"谁改了这一行"
的问题。等真需要复杂查询时换成 SQLite，届时**换的只是这一层**，
:mod:`audit_ledger.record` 的契约已经钉死了。

## 并发与持久性

* 单条记录一次 ``write()`` 追加：``O_APPEND`` 保证每次写都落在文件末尾，
  单次 write 在多进程并发追加时不会互相截断
* 每次写入后 ``flush`` + ``fsync``：审计记录丢了就等于没记，宁可慢一点
* **文件权限**：新建文件显式设为 ``file_mode``（默认 600），目录显式设为 700
  —— 两者都不依赖 umask 碰巧给对。只保护文件是不够的：默认 umask 建出来的
  目录通常是 775，同组用户读不了文件内容，却能删除或替换它，而"删掉证据"
  比"读到证据"更糟

## 进程模型

**单进程独占**。``AuditLedger.enforce_retention`` 会整文件重写，与并发
追加存在竞态窗口；启用多 worker 前必须引入文件锁或换 SQLite。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from audit_ledger.record import AuditRecord


class JsonlAuditSink:
    """JSONL 落盘。

    Args:
        path: 账本文件路径。
        fsync: 每次写入后是否 fsync。审计可靠性场景保持默认 True。
        file_mode: **新建**文件时设置的权限位。只在文件首次创建时生效
            （已存在的文件不重复 chmod，避免无谓 syscall）。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        fsync: bool = True,
        file_mode: int = 0o600,
    ) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._file_mode = file_mode

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: AuditRecord) -> None:
        """追加一条记录。

        刻意**没有任何更新/删除方法** —— append-only 的保证不靠调用方自觉，
        而靠这个类压根不提供改写入口。
        """
        parent = self._path.parent
        # 目录和文件都要管：只把文件设成 0600 是不够的 ——
        # 默认 umask 建出来的目录通常是 0775，同组用户虽然读不了文件内容，
        # 却能**删除或替换**它。对审计账本来说，"删掉证据"比"读到证据"更糟，
        # 而 append-only 的保证正是靠文件不被外部动过。
        # 只在本次创建时收紧：已存在的目录可能承载别的东西（比如 shared/）。
        created_dir = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if created_dir:
            os.chmod(parent, 0o700)

        created = not self._path.exists()
        line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
        # newline="" 之外的参数都不给：我们要的是"原样字节追加"，
        # 任何换行转换都会让跨平台读回来的内容对不上。
        with self._path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            if self._fsync:
                os.fsync(fh.fileno())
        if created:
            # 首次创建后收紧权限。放在 open() 之后：open 本身用 umask 建文件，
            # 这里显式覆盖成审计要求的最小权限。
            os.chmod(self._path, self._file_mode)

    def read_all(self) -> Iterator[AuditRecord]:
        """按写入顺序读回。"""
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield _record_from_dict(json.loads(line))

    def count(self) -> int:
        return sum(1 for _ in self.read_all())


def _record_from_dict(data: dict) -> AuditRecord:
    from guard_contract.enums import GuardStage, PolicyAction

    return AuditRecord(
        request_id=data["request_id"],
        recorded_at=data["recorded_at"],
        tenant=data["tenant"],
        use_case=data["use_case"],
        stage=GuardStage(data["stage"]),
        action=PolicyAction(data["action"]),
        rule_name=data.get("rule_name"),
        matched=bool(data.get("matched", False)),
        policy_version=data["policy_version"],
        detectors=tuple(data.get("detectors", ())),
        hit_labels=tuple(data.get("hit_labels", ())),
        evidence=tuple(data.get("evidence", ())),
        metadata=dict(data.get("metadata", {})),
    )


__all__ = ["JsonlAuditSink"]
