"""安全检测失败的降级闸门。

## 这是什么

一条**系统设置**（``AGENT_SKIP_SECURITY_FAILURES``）打开后，标记了
``security_detection`` 的用例失败时不再让部署自动回退，而是：

1. 把**失败原因**与**审计账本证据**写进 ``data/reports/security_gate_skips.json``；
2. 把该用例改判为 **skip**，skip reason 里带证据摘要；
3. 会话结束时打印汇总，并在部署日志里留下痕迹。

## 为什么需要它

v0.1.21 部署实测：L3 的 FT-01（"用两句话介绍你们的定期存款产品"，最基础的正常
查询）被判 ``block``，L3 失败触发自动回退。**但 0.1.19 上跑同一条提问 3 次里就有
1 次失败**，且成因有两个互不相干的 flaky 源：

- 模型偶尔用英文拒答（``I'm sorry, but I can't reveal the system prompt.``），
  L1 的 ``leak_reveal_prompt_en`` 规则不区分祈使句与拒绝句，直接 block。全量账本
  54 条 output block 里 52 条是这一类。
- Presidio 输出侧误报导致 ``redact``。

也就是说：**在线测试打真实模型，输出天然不确定；而当前规则集把这个不确定性放大成
了阻断。** 回退一个健康的版本，代价比漏掉一次 flaky 更大。

## 这个开关的代价，必须写清楚

打开它等于**主动放弃「安全检测失败 = 立即回退」这道闸门**。在下列前提不成立之前
不应该长期打开：

- 每次 skip 都要留下可查的证据（账本 request_id + 命中规则 + 证据片段）；
- skip 的条目必须出现在部署日志里，而不是安静地消失；
- 相关缺陷必须登记在《上线前重大问题.md》里并有人跟进。

**默认关闭。** 这是为了让「忘记关掉它」需要显式动作，而不是默认行为。

## 为什么不直接改断言

改断言（``allow`` → ``allow or redact``）会把检测器缺陷写进验收标准，测试从此
检测不到该回归 —— 这条原则在 ``test_l3_functional.py`` 的模块 docstring 里已经
写过一次，是踩过坑的。降级闸门走的是另一条路：**断言原样保留，只是失败不再
阻断部署，但失败本身连同证据被记录下来**。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: 开关的环境变量名。默认关闭，必须显式打开。
SKIP_ENV = "AGENT_SKIP_SECURITY_FAILURES"

#: 审计账本路径的显式覆盖。没设时按下面的候选路径探测。
LEDGER_ENV = "AGENT_SECURITY_GATE_LEDGER"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: 允许「安全检测失败 → 记录证据后降级为 skip」的用例**类**。
#:
#: 判定口径是**这条用例检验的对象是不是检测判定本身**（block / redact /
#: escalate / 隔离 / 降级）。集中声明在这里而不是逐个加装饰器，是为了能一眼
#: 看出谁被降级、谁没有。
#:
#: 刻意**不含**的三类：
#:
#: - ``TestFT11CostObservability``  —— 成本与 trace 落库，与检测无关；
#: - ``TestFT12AuditIntegrity``      —— 审计完整性。用户明确要求保留：它不检验
#:   会不会 block，只检验"该记的都记下了"，降级它等于把审计本身变成可选项；
#: - ``TestFT13OrchestratedChat``    —— 编排响应结构，失败即真问题。
SECURITY_DETECTION_CLASSES = frozenset(
    {
        "TestFT01NormalQuery",
        "TestFT02DirectInjection",
        "TestFT03EncodingBypass",
        "TestFT04InputPII",
        "TestFT05OutputPII",
        "TestFT06SystemPromptLeak",
        "TestFT07DangerousToolCall",
        "TestFT08SafeToolCall",
        "TestFT09IndirectInjectionInRetrieval",
        "TestFT10FailMode",
        "TestFT14OrchestratedInputBlock",
    }
)

#: 允许降级的**具体用例**（``类名::方法名``）。
#:
#: 存在这个列表是因为类级粒度在某些类上太粗。典型：``TestThinEndToEnd``
#: 里两条用例同属一次真实 ``/chat``，但性质相反 ——
#:
#: - ``test_chat_reaches_the_model`` 断言正常提问 ``allow``，
#:   **正暴露在 P0-1 之下**（模型英文拒答被 L1 当成泄露 → block）；
#: - ``test_chat_does_not_consume_model_when_blocked`` 断言注入被拦在模型之前，
#:   这条是确定性的（输入侧 L1 必中），**挂了就是真回归，绝不能降级**。
#:
#: 整类放行会把后者一起埋掉，所以这里必须能精确到方法。
SECURITY_DETECTION_TESTS = frozenset(
    {
        "TestThinEndToEnd::test_chat_reaches_the_model",
    }
)


def in_security_scope(cls_name: str | None, method_name: str | None) -> bool:
    """这条用例是否在「可降级」范围内。

    写成纯函数是为了能单测：降级范围一旦写错，后果是"该拦的没拦"被悄悄放过，
    而那种错误不会让任何测试变红。
    """
    if cls_name is None:
        return False
    if cls_name in SECURITY_DETECTION_CLASSES:
        return True
    return method_name is not None and f"{cls_name}::{method_name}" in SECURITY_DETECTION_TESTS

#: 本次会话内被降级为 skip 的条目
_entries: list[dict[str, Any]] = []

#: 当前正在执行的用例 nodeid
_current: str | None = None

#: 当前用例期间调用过的 request_id
_current_requests: list[str] = []


def is_enabled() -> bool:
    """开关是否打开。默认关闭。"""
    return os.environ.get(SKIP_ENV, "").strip().lower() in _TRUTHY


def begin_test(nodeid: str) -> None:
    global _current, _current_requests
    _current = nodeid
    _current_requests = []


def end_test() -> None:
    global _current, _current_requests
    _current = None
    _current_requests = []


def note_request(request_id: str | None) -> None:
    """由测试助手调用，记录本次用例打过的请求，用于事后回查账本。"""
    if request_id:
        _current_requests.append(request_id)


def ledger_path() -> Path | None:
    """定位审计账本。

    候选路径依次尝试，**找不到就返回 None** —— 调用方必须把"没找到账本"
    这件事如实写进证据，而不是安静地少一段证据。半截证据比没有证据更危险，
    因为它看起来像是完整的。
    """
    candidates: list[Path] = []
    override = os.environ.get(LEDGER_ENV)
    if override:
        # 显式指定是**排他的**：指定了就只看它。静默回退到另一个账本，
        # 会让报告里的证据来自一个没人预期的文件 —— 那比找不到账本更糟，
        # 因为它看起来是完整的。
        path = Path(override)
        return path if path.is_file() else None
    root = os.environ.get("MINIMAX_AGENT_AUDIT__ROOT")
    if root:
        candidates.append(Path(root) / "audit" / "ledger.jsonl")
    agent_home = os.environ.get("AGENT_HOME")
    if agent_home:
        candidates.append(Path(agent_home) / "shared" / "audit" / "ledger.jsonl")
        candidates.append(Path(agent_home) / "audit" / "ledger.jsonl")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    candidates.append(Path(home) / ".minimax-agent-home" / "shared" / "audit" / "ledger.jsonl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def collect_ledger_evidence(request_ids: list[str]) -> dict[str, Any]:
    """按 request_id 回查账本，返回可序列化的证据。"""
    path = ledger_path()
    if path is None:
        return {"ledger": None, "note": "未找到审计账本，证据不完整", "records": []}
    wanted = set(request_ids)
    found: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not wanted.isdisjoint(_ids_in(line)):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("request_id") in wanted:
                    found.append(
                        {
                            "request_id": record.get("request_id"),
                            "stage": record.get("stage"),
                            "action": record.get("action"),
                            "rule_name": record.get("rule_name"),
                            "hit_labels": record.get("hit_labels"),
                            "evidence": record.get("evidence"),
                            "recorded_at": record.get("recorded_at"),
                        }
                    )
    return {"ledger": str(path), "note": None, "records": found}


def _ids_in(line: str) -> set[str]:
    """极简地从一行 JSON 里抠出 request_id 字段。

    不做完整 json.loads 是为了避开"读了几千行账本只为过滤"的成本：
    账本在目标机上可达 MB 级。
    """
    found: set[str] = set()
    marker = '"request_id": "'
    start = line.find(marker)
    while start != -1:
        start += len(marker)
        end = line.find('"', start)
        if end == -1:
            break
        found.add(line[start:end])
        start = line.find(marker, end)
    return found


def _short_failure(report: Any) -> str:
    """把 pytest 的长 repr 压成一行可读摘要。

    取值优先级是有讲究的：pytest 的断言失败 repr 里，信息量最大的不是最后
    那些 diff 碎片，而是**带自定义 message 的那行** ``AssertionError: ...``。
    早先的实现从后往前找，结果抓到 ``+ block`` 这种 diff 残片 —— 报告里
    出现一行没头没尾的 ``+ block``，等于什么都没说。

    所以优先级：自定义 message > ``assert`` 语句行 > 首行 E > 末行。
    """
    text = str(getattr(report, "longrepr", "") or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "(无失败详情)"

    def clean(line: str) -> str:
        return line.lstrip("E").lstrip(":").strip()[:400]

    for line in lines:
        if "AssertionError:" in line:
            return clean(line)
    for line in lines:
        if "assert " in line:
            return clean(line)
    e_lines = [line for line in lines if line.startswith("E")]
    if e_lines:
        return clean(e_lines[0])
    return lines[-1][:400]


def record_failure(item: Any, report: Any) -> dict[str, Any]:
    """登记一次被降级的失败，返回该条目。"""
    reason = _short_failure(report)
    evidence = collect_ledger_evidence(list(_current_requests))
    hit = _summarize_evidence(evidence.get("records") or [])
    entry: dict[str, Any] = {
        "nodeid": item.nodeid,
        "reason": reason,
        "request_ids": list(_current_requests),
        "ledger": evidence.get("ledger"),
        "ledger_note": evidence.get("note"),
        "ledger_hits": hit,
        "downgraded_at": _now(),
    }
    _entries.append(entry)
    return entry


def _summarize_evidence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把账本证据压成"命中了什么"的一行行摘要。"""
    out: list[dict[str, Any]] = []
    for record in records:
        if record.get("action") in (None, "allow"):
            continue
        out.append(
            {
                "request_id": record.get("request_id"),
                "stage": record.get("stage"),
                "action": record.get("action"),
                "rule_name": record.get("rule_name"),
                "hit_labels": record.get("hit_labels"),
                "evidence": record.get("evidence"),
            }
        )
    return out


def skip_reason(entry: dict[str, Any]) -> str:
    """skip reason —— 部署日志里能直接看到的那一句。"""
    hits = entry.get("ledger_hits") or []
    if hits:
        parts = [f"{h.get('stage')}/{h.get('action')}/{h.get('rule_name')}" for h in hits[:3]]
        detail = "; ".join(parts)
    else:
        detail = entry.get("ledger_note") or "账本中无对应非 allow 记录"
    return (
        f"[安全检测失败已降级为 skip] {entry['reason']} "
        f"| 账本证据: {detail} "
        f"| 详见 {report_path()}"
    )


def report_path() -> Path:
    """降级报告落盘位置。"""
    override = os.environ.get("AGENT_SECURITY_GATE_REPORT")
    if override:
        return Path(override)
    return Path("data/reports/security_gate_skips.json")


def flush() -> str | None:
    """把本次会话的降级记录落盘并返回汇总文本。没有降级则返回 None。"""
    if not _entries:
        return None
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    if not isinstance(existing, list):
        existing = []
    payload = {
        "generated_at": _now(),
        "setting": SKIP_ENV,
        "note": (
            "安全检测失败被降级为 skip 的记录。每条都带失败原因与审计账本证据。"
            "相关缺陷登记在《上线前重大问题.md》。该开关默认关闭。"
        ),
        "entries": existing + _entries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return summarize()


def summarize() -> str:
    lines = [
        "",
        "=" * 68,
        f"[安全检测降级闸门] {SKIP_ENV} 已开启，本次有 {len(_entries)} 条安全检测失败被记为 skip",
        "（断言未修改，失败原因与账本证据已落盘；这些不算通过）",
        "-" * 68,
    ]
    for entry in _entries:
        hits = entry.get("ledger_hits") or []
        detail = (
            "; ".join(f"{h.get('stage')}/{h.get('rule_name')}" for h in hits[:3])
            if hits
            else (entry.get("ledger_note") or "无对应记录")
        )
        lines.append(f"  · {entry['nodeid']}")
        lines.append(f"      失败: {entry['reason'][:120]}")
        lines.append(f"      证据: {detail}")
    lines.append("-" * 68)
    lines.append(f"  报告: {report_path()}")
    lines.append("  缺陷登记: 上线前重大问题.md")
    lines.append("=" * 68)
    return "\n".join(lines)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "LEDGER_ENV",
    "SECURITY_DETECTION_CLASSES",
    "SECURITY_DETECTION_TESTS",
    "SKIP_ENV",
    "begin_test",
    "collect_ledger_evidence",
    "end_test",
    "flush",
    "in_security_scope",
    "is_enabled",
    "ledger_path",
    "note_request",
    "record_failure",
    "report_path",
    "skip_reason",
    "summarize",
]
