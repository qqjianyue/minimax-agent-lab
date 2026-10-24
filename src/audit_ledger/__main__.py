"""C8 审计账本命令行入口。

定位：给 demo 与排障一个**不用先写 Python 脚本**就能查账本的入口。

## 为什么需要这个 CLI

审计账本此前只有 Python API（:meth:`AuditLedger.query`），服务侧也没有对外的
账本接口（``app.py`` 只有 healthz / guard/inspect / chat / tools/execute 四个
路由）。结果是"这次请求在账本里到底记了什么"这个问题，在演示现场和排障现场
都做不成 —— 想看就得先写一段脚本。

本模块提供三个子命令：

* ``summary`` —— 概览：总条数、按 action / stage 分布、时间范围
* ``query``   —— 按 request_id / tenant / action / stage / since 检索
* ``prune``   —— 手动触发保留期清理

## 三条设计约束

1. **不重写过滤逻辑**：检索直接调用 :meth:`AuditLedger.query`，CLI 只做参数
   解析与输出格式化。过滤语义只有一份实现，不会两边漂移。
2. **不改 C8 契约**：``AuditRecord`` / ``JsonlAuditSink`` / ``AuditLedger``
   一个字都没动。CLI 是新增的**旁路入口**，不是新的写入路径。
3. **只有 ``prune`` 会写文件**，且走既有的原子重写（临时文件 + ``os.replace``），
   中断时账本不会处于不可读状态。

## 用法

::

    python -m audit_ledger summary
    python -m audit_ledger query --request-id req-abc
    python -m audit_ledger query --tenant default --action block --since 2026-10-24
    python -m audit_ledger prune --days 30

根目录与账本路径的解析顺序（两者一致）：

    显式参数 > 环境变量 > 内置默认值

环境变量沿用服务侧同一套：``MINIMAX_AGENT_AUDIT__ROOT`` / ``MINIMAX_AGENT_AUDIT__PATH``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.clock import SystemClock
from agent_core.config import AuditSettings
from audit_ledger.ledger import AuditLedger, SystemClockAdapter
from audit_ledger.record import AuditRecord
from audit_ledger.sink import JsonlAuditSink
from guard_contract.enums import GuardStage, PolicyAction
from policy_engine.redactor import RedactionResult

#: 与服务侧同一套环境变量 —— CLI 不另造一套配置口径。
ENV_ROOT = "MINIMAX_AGENT_AUDIT__ROOT"
ENV_PATH = "MINIMAX_AGENT_AUDIT__PATH"

#: 默认值直接取自 :class:`~agent_core.config.AuditSettings`，不另写一份常量：
#: 配置改了就跟着变，不会出现"CLI 认为 365 天、配置认为 30 天"的分裂。
_DEFAULTS = AuditSettings()
DEFAULT_PATH = _DEFAULTS.path
DEFAULT_RETENTION_DAYS = _DEFAULTS.retention_days

#: ``query`` 默认最多显示多少条。账本可能有成千上万条，一次全打印既刷屏
#: 又看不出重点；真的要看全部用 ``--limit 0``。
DEFAULT_LIMIT = 50


class _NoopRedactor:
    """构造 :class:`AuditLedger` 用的占位脱敏器。

    ``AuditLedger`` 的构造签名要求一个脱敏器，但**只有写入路径**会用到它
    （:meth:`AuditRecord.from_decision`）。CLI 的三条命令都不写入新记录：
    ``query`` / ``summary`` 只读，``prune`` 只搬运**落盘时已经脱敏**的记录。

    刻意不用 ``RegexRedactor``：那会让 C8 依赖 ``detector_rules``（C4 规则层），
    违反依赖方向 —— C8 不该为了一个永远不会被调用的脱敏实现而依赖检测器层。
    """

    name = "noop"

    def redact(self, text: str) -> RedactionResult:
        return RedactionResult(text=text)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def resolve_root(explicit: str | None) -> Path:
    """账本根目录：显式参数 > 环境变量 > 当前目录。

    环境变量这一档是给**手工执行**准备的：systemd 注入的
    ``MINIMAX_AGENT_AUDIT__ROOT`` 只在服务进程的环境里生效，SSH 上去手动跑
    时拿不到，所以必须能显式传 ``--root``。
    """
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_ROOT)
    if from_env:
        return Path(from_env).expanduser()
    return Path.cwd()


def resolve_ledger_path(root: Path, explicit: str | None) -> Path:
    """账本文件路径：显式参数 > 环境变量 > 配置默认值（相对路径挂在 root 下）。"""
    raw = explicit or os.environ.get(ENV_PATH) or str(DEFAULT_PATH)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root / path


def _open_ledger(ledger_path: Path, *, retention_days: int) -> AuditLedger:
    """打开账本（**只读意图**：不触发任何文件创建）。

    ``JsonlAuditSink`` 的构造函数不碰文件系统，只有 ``append`` 才会创建文件。
    因此对不存在的账本执行 ``query`` 不会凭空造出一个空账本 —— 那会让
    "账本还没接上"和"账本是空的"变成同一件事。
    """
    return AuditLedger(
        JsonlAuditSink(ledger_path),
        clock=SystemClockAdapter(SystemClock()),
        redactor=_NoopRedactor(),
        retention_days=retention_days,
    )


# ---------------------------------------------------------------------------
# 参数解析辅助
# ---------------------------------------------------------------------------


def _parse_since(text: str) -> datetime:
    """解析 ``--since``，统一归一到 UTC。

    账本里的 ``recorded_at`` 是 ``datetime.now(UTC).isoformat()`` 的产物
    （形如 ``2026-10-24T09:00:00+00:00``），而 :meth:`AuditLedger.query` 的
    ``since`` 比较是**字符串比较** —— 只有两边格式与时区偏移一致，字典序才
    等于时间序。这里若产出 naive 时间戳，两边长度不同就会比错。

    不带时区信息时按 UTC 解释，与账本写入口径一致。
    """
    raw = text.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        valid = "示例: 2026-10-24 或 2026-10-24T09:00:00"
        raise ValueError(f"无法解析 --since 时间: {text!r}（{valid}）") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_action(text: str | None) -> str | None:
    """校验 ``--action`` 并归一成枚举值。

    刻意校验：``AuditLedger.query`` 拿字符串直接和 ``action.value`` 比，
    传错大小写（如 ``BLOCK``）会**静默返回空结果** —— 一个拼错就什么都查
    不到的 CLI，比直接报错的 CLI 糟糕得多。
    """
    if text is None:
        return None
    try:
        return PolicyAction(text.strip().lower()).value
    except ValueError:
        valid = ", ".join(a.value for a in PolicyAction)
        raise ValueError(f"非法的 action: {text!r}，可选值: {valid}") from None


def _parse_stage(text: str | None) -> str | None:
    """校验 ``--stage`` 并归一成枚举值（理由同 :func:`_parse_action`）。"""
    if text is None:
        return None
    try:
        return GuardStage(text.strip().lower()).value
    except ValueError:
        valid = ", ".join(s.value for s in GuardStage)
        raise ValueError(f"非法的 stage: {text!r}，可选值: {valid}") from None


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _verdict_kind(record: AuditRecord) -> str:
    """这条记录属于哪一种判定语义。

    **读取时现算，不落盘** —— 落盘只存事实（``fail_mode`` / ``matched`` /
    ``action``），解释是会变的：policy 语义一改，烤进数据的标记就过期了。

    ==========  ========  ====================================
    fail_mode   matched  语义
    ==========  ========  ====================================
    空          True     正常判定命中（判有害）
    空          False    正常放行（检测器都跑完了，都说安全）
    非空        True     判到了，但同时有检测器挂了（命中有效）
    非空        False    **判不了**（系统判定自己判不了）
    ==========  ========  ====================================

    最后一类在 fail-closed 策略下会表现为"阻断" —— 也就是最容易被误读成
    "模型判定内容有害"的那一类。修复 P1-1 之前，账本里这一类与一次普通
    ``allow`` 的形状**完全一样**。
    """
    if record.fail_mode is None:
        return "命中" if record.matched else "正常放行"
    if record.matched:
        return "命中（有检测器失败）"
    return "判不了"


def _print_summary_text(
    ledger_path: Path,
    total: int,
    first: str | None,
    last: str | None,
    by_action: Counter[str],
    by_stage: Counter[str],
    by_kind: Counter[str],
    by_failed_detector: Counter[str],
) -> None:
    print(f"[audit] 账本:     {ledger_path}")
    print(f"[audit] 总记录:   {total}")
    if total:
        print(f"[audit] 时间范围: {first} ~ {last}")
    else:
        print("[audit] 时间范围: （空账本）")
    for title, counter in (("action", by_action), ("stage", by_stage)):
        print(f"[audit] 按 {title}:")
        if not counter:
            print("               （无）")
            continue
        for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"               {key:<20}{count}")

    print("[audit] 按判定语义:")
    if not by_kind:
        print("               （无）")
    else:
        for key, count in sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"               {key:<20}{count}")

    if by_failed_detector:
        print("[audit] 挂掉的检测器:")
        for key, count in sorted(by_failed_detector.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"               {key:<20}{count}")


def _print_record(index: int, record: AuditRecord) -> None:
    rule = record.rule_name or "-"
    print(
        f"[{index}] {record.request_id} | {record.recorded_at} | "
        f"{record.stage.value} | {record.action.value} | rule={rule} "
        f"| {_verdict_kind(record)}"
    )
    print(
        f"    tenant: {record.tenant}   use_case: {record.use_case}   "
        f"policy={record.policy_version}"
    )
    if record.reason:
        print(f"    reason: {record.reason}")
    print(f"    detectors: {', '.join(record.detectors) or '-'}")
    print(f"    hits: {', '.join(record.hit_labels) or '-'}")
    if record.failed_detectors:
        parts = [
            f"{item.get('detector', '?')}({item.get('reason', '?')})"
            for item in record.failed_detectors
        ]
        print(
            f"    ⚠ 判不了：fail_mode={record.fail_mode}  失败检测器: {', '.join(parts)}"
        )
    if record.evidence:
        print("    evidence:")
        for item in record.evidence:
            print(f"      - {item}")
    extra = {k: v for k, v in record.metadata.items() if k not in ("start", "end")}
    if extra:
        print(f"    metadata: {json.dumps(extra, ensure_ascii=False, sort_keys=True)}")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_summary(args: argparse.Namespace) -> int:
    ledger_path = resolve_ledger_path(resolve_root(args.root), args.path)
    if not ledger_path.is_file():
        print(f"[audit] 账本不存在: {ledger_path}", file=sys.stderr)
        print(
            "[audit] 提示: 手动执行时 systemd 注入的环境变量不生效，"
            "请用 --root 指定账本根目录",
            file=sys.stderr,
        )
        return 1

    ledger = _open_ledger(ledger_path, retention_days=DEFAULT_RETENTION_DAYS)
    records = list(ledger.records())
    by_action = Counter(r.action.value for r in records)
    by_stage = Counter(r.stage.value for r in records)
    by_kind = Counter(_verdict_kind(r) for r in records)
    by_failed_detector = Counter(
        item.get("detector", "?")
        for r in records
        for item in r.failed_detectors
    )
    stamps = sorted(r.recorded_at for r in records)
    first = stamps[0] if stamps else None
    last = stamps[-1] if stamps else None

    if args.json:
        _print_json(
            {
                "ledger": str(ledger_path),
                "total": len(records),
                "first_recorded_at": first,
                "last_recorded_at": last,
                "by_action": dict(by_action),
                "by_stage": dict(by_stage),
                "by_kind": dict(by_kind),
                "by_failed_detector": dict(by_failed_detector),
            }
        )
    else:
        _print_summary_text(
            ledger_path,
            len(records),
            first,
            last,
            by_action,
            by_stage,
            by_kind,
            by_failed_detector,
        )
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    ledger_path = resolve_ledger_path(resolve_root(args.root), args.path)
    if not ledger_path.is_file():
        print(f"[audit] 账本不存在: {ledger_path}", file=sys.stderr)
        print(
            "[audit] 提示: 手动执行时 systemd 注入的环境变量不生效，"
            "请用 --root 指定账本根目录",
            file=sys.stderr,
        )
        return 1

    try:
        action = _parse_action(args.action)
        stage = _parse_stage(args.stage)
        since = _parse_since(args.since) if args.since else None
    except ValueError as exc:
        print(f"[audit] 参数错误: {exc}", file=sys.stderr)
        return 1

    ledger = _open_ledger(ledger_path, retention_days=DEFAULT_RETENTION_DAYS)
    matched = ledger.query(
        request_id=args.request_id,
        tenant=args.tenant,
        action=action,
        stage=stage,
        since=since,
    )
    if args.failed_only:
        # 只看"有检测器没能给出结论"的记录。**在 CLI 层过滤**，不给
        # AuditLedger.query 加参数 —— 那是 C8 的对外契约，不该为一次排查需求
        # 长出新参数（长出来的参数就再也长不回去了）。
        matched = [r for r in matched if r.fail_mode is not None]
    shown = matched if args.limit <= 0 else matched[: args.limit]

    if args.json:
        _print_json(
            {
                "ledger": str(ledger_path),
                "matched": len(matched),
                "shown": len(shown),
                "records": [r.to_dict() for r in shown],
            }
        )
        return 0

    if not matched:
        print(f"[audit] 账本: {ledger_path}")
        print("[audit] 无匹配记录")
        return 0

    print(f"[audit] 账本: {ledger_path}")
    shown_note = f"（显示前 {len(shown)} 条）" if len(shown) != len(matched) else ""
    print(f"[audit] 命中 {len(matched)} 条{shown_note}")
    for index, record in enumerate(shown, start=1):
        _print_record(index, record)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    ledger_path = resolve_ledger_path(resolve_root(args.root), args.path)
    if not ledger_path.is_file():
        print(f"[audit] 账本不存在: {ledger_path}", file=sys.stderr)
        return 1

    ledger = _open_ledger(ledger_path, retention_days=args.days)
    report = ledger.enforce_retention()

    if args.json:
        _print_json(
            {
                "ledger": str(ledger_path),
                "total_before": report.total_before,
                "removed": report.removed,
                "kept": report.kept,
                "cutoff": report.cutoff,
            }
        )
        return 0

    print(f"[audit] 账本:     {ledger_path}")
    print(f"[audit] 保留期:   {args.days} 天（截止 {report.cutoff}）")
    print(
        f"[audit] 清理前 {report.total_before} 条 → "
        f"删除 {report.removed} 条，保留 {report.kept} 条"
    )
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        default=None,
        help=f"账本根目录（默认取环境变量 {ENV_ROOT}，再兜底当前目录）",
    )
    parser.add_argument(
        "--path",
        default=None,
        help=f"相对 root 的账本路径（默认取环境变量 {ENV_PATH}，再兜底 {DEFAULT_PATH}）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本处理）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_ledger",
        description="审计账本查询工具（C8：概览 / 检索 / 手动保留期清理）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_summary = sub.add_parser("summary", help="概览：总条数 / 按 action 与 stage 的分布")
    _add_common(p_summary)
    p_summary.set_defaults(func=cmd_summary)

    p_query = sub.add_parser("query", help="按条件检索记录")
    p_query.add_argument("--request-id", default=None, help="按请求 ID 精确匹配（排障最常用）")
    p_query.add_argument("--tenant", default=None, help="按租户过滤")
    p_query.add_argument("--action", default=None, help="按动作过滤，如 block / redact / allow")
    p_query.add_argument(
        "--stage",
        default=None,
        help="按检测点过滤，如 input / retrieval / tool / output",
    )
    p_query.add_argument("--since", default=None, help="只看该时间之后的记录，如 2026-10-24")
    p_query.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"最多显示多少条（0 = 不限，默认 {DEFAULT_LIMIT}）",
    )
    p_query.add_argument(
        "--failed-only",
        action="store_true",
        help="只看有检测器没能给出结论的记录（判不了 / 有检测器挂掉）",
    )
    _add_common(p_query)
    p_query.set_defaults(func=cmd_query)

    p_prune = sub.add_parser("prune", help="手动执行保留期清理（原子重写）")
    p_prune.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"保留多少天（默认 {DEFAULT_RETENTION_DAYS}）",
    )
    _add_common(p_prune)
    p_prune.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
