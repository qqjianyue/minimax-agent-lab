#!/usr/bin/env python3
"""指标汇总视图：从 Phoenix 拉最近 N 次运行，算出阶段二文档里的全部指标。

## 为什么要有这个脚本

阶段二验收标准里有一条很具体的话：

    "至少能回答：过去 10 次运行中，平均延迟多少？工具失败率多少？"

在 Phoenix UI 里回答这句话，需要：进 project → 切 Traces → 加过滤器 →
选时间段 → 手动看图。**能答，但答得慢，而且没法在 CI 里跑、没法演示。**

这个脚本把同一件事变成一条命令。它直接查 Phoenix 的 GraphQL，
因此不依赖 UI 版本，也不会因为 Phoenix 换皮而失效。

## 口径

字段与口径的权威定义在 ``docs/metrics.md``，本脚本只是它的可执行版本。
两边任何一处改了，另一处必须同步 —— 脚本里的字段名硬编码自
``src/telemetry/tracing.py``。

## 用法

    python scripts/phoenix_metrics.py --endpoint http://127.0.0.1:6006 --last 10
    python scripts/phoenix_metrics.py --group safety
    python scripts/phoenix_metrics.py --json          # 机器可读，便于接 CI

刻意只用标准库：这是个排障工具，不该因为"多装一个包"而在目标机上跑不起来。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_ENDPOINT = "http://127.0.0.1:6006"
DEFAULT_PROJECT = "default"


def enable_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8 —— Windows 控制台默认代码页装不下中文。

    完整背景见 ``scripts/tasks.py`` 里同名函数：Windows 的标准流编码取自 ANSI
    代码页（cp1252 / cp936），中文 ``print()`` 会抛 ``UnicodeEncodeError``。
    这个脚本刻意只用标准库、不引共享模块，所以这里内联一份。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # 流已关闭等极端情况下重编码会失败，但不该让闸门因此崩掉
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


#: 一次拉多少条 span。分母是"请求"而不是 span，一次请求约 6 个 span，
#: 所以默认 4000 条大约覆盖最近 660 次运行。
DEFAULT_SPAN_LIMIT = 4000

#: Phoenix 单页硬上限。``first`` 超过它会直接报 GraphQL 错，而不是截断。
PHOENIX_PAGE_MAX = 1000

#: span 名 → 指标分组。放在这里而不是散落在各处，是为了让"指标从哪来"
#: 在一个文件里就能看全。
SPAN_GROUPS = {
    "agent.request": "performance",
    "input_guard": "safety",
    "output_guard": "safety",
    "tool_guard": "safety",
    "guard_inspect": "safety",
    "llm_call": "cost",
    "tool_call": "quality",
}

_QUERY = """
{
  projects(first: 20) {
    edges {
      node {
        name
        spans(first: %(limit)d, timeRange: %(timeRange)s%(after)s) {
          pageInfo {
            hasNextPage
            endCursor
          }
          edges {
            cursor
            node {
              name
              startTime
              latencyMs
              attributes
              trace { id }
            }
          }
        }
      }
    }
  }
}
"""


class PhoenixError(RuntimeError):
    """Phoenix 不可达或返回了 GraphQL 错误。"""


def _post(endpoint: str, query: str) -> dict[str, Any]:
    body = json.dumps({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise PhoenixError(f"无法访问 Phoenix ({endpoint})：{exc}") from exc
    if payload.get("errors"):
        raise PhoenixError(f"GraphQL 错误：{payload['errors'][0].get('message')}")
    return payload


def _pick_project(payload: dict[str, Any], project: str) -> dict[str, Any]:
    projects = payload.get("data", {}).get("projects", {}).get("edges", [])
    target = next((e["node"] for e in projects if e["node"]["name"] == project), None)
    if target is None:
        names = [e["node"]["name"] for e in projects]
        raise PhoenixError(f"项目 {project!r} 不存在。现有项目：{names or '（无）'}")
    return target


def _fetch(endpoint: str, project: str, limit: int, since: datetime) -> list[dict[str, Any]]:
    """拉取指定 project 在 ``since`` 之后的 span，按 ``limit`` 封顶。

    **必须分页**：Phoenix 单页最多 1000 条，``first`` 超了直接报 GraphQL 错。
    更要紧的是把超出的部分**静默丢掉**更糟 —— 只取一页的话，项目跑久了
    汇总就会少算，而输出看上去仍然完整可信。
    """
    # Phoenix 的 TimeRange 用 RFC3339 字符串区间。留 1 小时重叠，
    # 避免"跨整点的请求"落在区间外。
    start = since - timedelta(hours=1)

    def _stamp(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    time_range = f'{{start: "{_stamp(start)}", end: "{_stamp(datetime.now(UTC))}"}}'

    spans: list[dict[str, Any]] = []
    cursor: str | None = None

    while len(spans) < limit:
        page_size = min(PHOENIX_PAGE_MAX, limit - len(spans))
        after = f", after: {json.dumps(cursor)}" if cursor else ""
        query = _QUERY % {"limit": page_size, "timeRange": time_range, "after": after}
        node = _pick_project(_post(endpoint, query), project)["spans"]

        edges = node.get("edges") or []
        for edge in edges:
            span = dict(edge["node"])
            raw = span.get("attributes")
            # Phoenix 把 attributes 存成 JSON 字符串；解析失败就退化成空 dict，
            # 不能让一条脏数据把整个汇总干掉。
            try:
                attrs = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                attrs = {}
            span["attributes"] = attrs if isinstance(attrs, dict) else {}
            spans.append(span)

        info = node.get("pageInfo") or {}
        if not info.get("hasNextPage") or not edges:
            break
        cursor = info.get("endCursor")

    return spans


def _latest_traces(spans: Sequence[dict[str, Any]], count: int) -> list[list[dict]]:
    """按 trace 分组，取最近 ``count`` 次运行。

    Phoenix 的 ``Span`` 上**没有** ``traceId``，但有 ``trace { id }`` ——
    用它归属，而不是拿时间窗口猜：并发请求下按时间切窗口会把两个请求的
    span 混成一组，算出来的延迟和失败率都是错的，而且**不会报错**。
    """
    by_trace: dict[str, list[dict]] = {}
    for span in spans:
        trace = span.get("trace") or {}
        key = trace.get("id") or "<no-trace>"
        by_trace.setdefault(key, []).append(span)

    def anchor_time(group: Sequence[dict]) -> str:
        """一次运行的起点 = 它的根 span（agent.request）的开始时间。"""
        roots = [s for s in group if s["name"] == "agent.request"]
        target = roots or list(group)
        return min((s.get("startTime") or "" for s in target), default="")

    ordered = sorted(by_trace.values(), key=anchor_time, reverse=True)
    return [g for g in ordered[:count] if any(s["name"] == "agent.request" for s in g)]


def _percentile(values: Sequence[float], pct: float) -> float:
    """最近秩（nearest-rank）分位数。

    刻意不用插值：小样本下插值出来的 p99 是个没人能复现的数字，
    而最近秩至少能指到某个真实观测值。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct * len(ordered) // 100))))
    return float(ordered[rank - 1])


def _flatten(groups: Sequence[Sequence[dict]], name: str) -> list[dict]:
    out = [s for g in groups for s in g if s["name"] == name]
    out.sort(key=lambda s: s.get("startTime") or "", reverse=True)
    return out


def _num(attrs: dict, path: str, default: float = 0.0) -> float:
    """按 ``llm.token_count.prompt`` 这样的点号路径取嵌套值。"""
    node: Any = attrs
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return float(node) if isinstance(node, (int, float)) else default


def rate(num: float, den: float) -> float:
    """比例。分母为 0 时返回 0 —— 没有任何样本时"比例"没有意义，
    返回 0 会让调用方误以为"失败率真的是 0"，所以展示层要同时看分母。"""
    return round(num / den, 4) if den else 0.0


def compute(spans: Sequence[dict], last: int) -> dict[str, Any]:
    """算出全部指标。返回结构同时供 --human 与 --json 使用。"""
    groups = _latest_traces(spans, last)
    if not groups:
        return {"traces": 0, "note": "最近窗口内没有 agent.request span"}

    roots = [g[0] for g in groups]
    e2e = [float(r.get("latencyMs") or 0.0) for r in roots]

    llm = _flatten(groups, "llm_call")
    tool = _flatten(groups, "tool_call")
    tool_guard = _flatten(groups, "tool_guard")
    guards = _flatten(groups, "input_guard") + _flatten(groups, "output_guard")

    tool_ok = sum(1 for s in tool if s["attributes"].get("tool", {}).get("status") == "ok")

    prompt = sum(_num(s["attributes"], "llm.token_count.prompt") for s in llm)
    completion = sum(_num(s["attributes"], "llm.token_count.completion") for s in llm)
    cached = sum(_num(s["attributes"], "llm.token_count.prompt_details.cache_read") for s in llm)
    cost = sum(_num(s["attributes"], "llm.cost.total") for s in llm)
    no_usage = sum(
        1 for s in llm if s["attributes"].get("llm", {}).get("token_usage_present") is False
    )

    return {
        "traces": len(groups),
        "performance": {
            "avg_latency_ms": round(sum(e2e) / len(e2e), 1) if e2e else 0.0,
            "p50_ms": _percentile(e2e, 50),
            "p95_ms": _percentile(e2e, 95),
            "p99_ms": _percentile(e2e, 99),
            "max_ms": round(max(e2e), 1) if e2e else 0.0,
            "guard_latency_avg_ms": round(
                sum(_num(s["attributes"], "guard.latency_ms") for s in guards + tool_guard)
                / max(1, len(guards) + len(tool_guard)),
                2,
            ),
            "tool_duration_avg_ms": round(
                sum(_num(s["attributes"], "tool.duration_ms") for s in tool) / max(1, len(tool)),
                2,
            ),
        },
        "quality": {
            "tool_calls": len(tool),
            "tool_success_rate": rate(tool_ok, len(tool)),
            "tool_status_breakdown": _count_by(
                s["attributes"].get("tool", {}).get("status", "<none>") for s in tool
            ),
        },
        "cost": {
            "llm_calls": len(llm),
            "prompt_tokens": int(prompt),
            "completion_tokens": int(completion),
            "cache_read_tokens": int(cached),
            "cache_hit_rate": rate(cached, prompt),
            "total_cost_cny": round(cost, 6),
            "avg_cost_cny": round(cost / len(llm), 6) if llm else 0.0,
            "spans_without_usage": no_usage,
        },
        "safety": {
            "tool_guard_blocked_rate": rate(
                sum(
                    1
                    for s in tool_guard
                    if s["attributes"].get("guard", {}).get("action") != "allow"
                ),
                len(tool_guard),
            ),
            "action_breakdown": _count_by(
                s["attributes"].get("guard", {}).get("action", "<none>") for s in guards
            ),
            "matched_count": sum(
                1 for s in guards if s["attributes"].get("guard", {}).get("matched") is True
            ),
            "detector_failures": sum(
                1
                for s in guards + tool_guard
                if s["attributes"].get("guard", {}).get("failed_detectors")
            ),
            "degraded_path_hits": sum(
                1 for s in guards + tool_guard if s["attributes"].get("guard", {}).get("fail_mode")
            ),
        },
    }


def _count_by(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_human(report: dict[str, Any], group: str | None, endpoint: str) -> str:
    lines: list[str] = []
    add = lines.append

    if report.get("traces") == 0:
        return f"没有可汇总的数据。{report.get('note', '')}\n  （Phoenix: {endpoint}）"

    def section(title: str, key: str, body: dict[str, Any]) -> None:
        if group and group != key:
            return
        add(f"\n{title}")
        add("-" * 46)
        for k, v in body.items():
            if isinstance(v, dict):
                add(f"  {k}:")
                for kk, vv in v.items():
                    add(f"      {kk:<28} {vv}")
            elif isinstance(v, float) and 0.0 <= v <= 1.0 and k.endswith("rate"):
                add(f"  {k:<34} {_pct(v)}")
            else:
                add(f"  {k:<34} {v}")

    add("=" * 46)
    add(f"  指标汇总 · 最近 {report['traces']} 次运行")
    add(f"  来源: {endpoint}")
    add("=" * 46)

    section("【性能】端到端延迟（agent.request）", "performance", report["performance"])
    section("【质量】工具调用", "quality", report["quality"])
    section("【成本】模型调用（人民币）", "cost", report["cost"])
    section("【安全】检测与拦截", "safety", report["safety"])

    add("")
    add("字段口径见 docs/metrics.md")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    # 先切编码：Windows 上下面每一条中文提示都可能撞上 cp1252/cp936。
    enable_utf8_console()
    parser = argparse.ArgumentParser(
        description="从 Phoenix 汇总可观测性指标（口径见 docs/metrics.md）"
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Phoenix 地址")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Phoenix 项目名")
    parser.add_argument("--last", type=int, default=10, help="统计最近多少次运行（trace）")
    parser.add_argument("--minutes", type=int, default=180, help="回看时间窗（分钟）")
    parser.add_argument(
        "--group",
        choices=["performance", "quality", "cost", "safety"],
        help="只看某一组",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SPAN_LIMIT,
        help=f"拉取 span 上限（自动按 Phoenix 单页 {PHOENIX_PAGE_MAX} 条分页）",
    )
    args = parser.parse_args(argv)

    since = datetime.now(UTC) - timedelta(minutes=max(1, args.minutes))
    try:
        spans = _fetch(args.endpoint, args.project, args.limit, since)
    except PhoenixError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    if not spans:
        print(
            f"最近 {args.minutes} 分钟内 Phoenix 没有 span。"
            "\n  检查：telemetry.enabled 是否为 true、服务近期是否有过请求、"
            "stderr 里是否有 'Failed to export spans'。",
            file=sys.stderr,
        )
        return 1

    report = compute(spans, args.last)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_human(report, args.group, args.endpoint))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
