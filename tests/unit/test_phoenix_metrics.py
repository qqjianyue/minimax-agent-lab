"""阶段二 · 指标口径的单元测试。

`scripts/phoenix_metrics.py` 是 `docs/metrics.md` 的**可执行版本** ——
两份定义必须永远一致，否则文档就变成了考古材料。这里锁住计算口径。

## 为什么值得单独测

指标算错不会让任何东西崩，它只会安静地给出一个**看起来合理的错数字**。
面试时说出"p95 是 820ms"而实际口径算的是 1200ms，那是当场被问穿的结局。
这类 bug 靠人眼看汇总表永远发现不了。

脚本刻意只用标准库（排障工具不该因为缺依赖而在目标机上跑不起来），
所以这里用 importlib 按路径加载，而不是 `import phoenix_metrics`。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "phoenix_metrics.py"


@pytest.fixture(scope="module")
def pm() -> Any:
    if not SCRIPT.is_file():
        pytest.skip(f"脚本不存在: {SCRIPT}")
    spec = importlib.util.spec_from_file_location("phoenix_metrics_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def span(
    name: str, trace: str, at: str, latency: float = 10.0, attrs: dict | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "trace": {"id": trace},
        "startTime": at,
        "latencyMs": latency,
        "attributes": attrs or {},
    }


def guard_attrs(action: str, matched: bool = False, extra: dict | None = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {"guard": {"action": action, "matched": matched, "latency_ms": 5.0}}
    if extra:
        attrs["guard"].update(extra)
    return attrs


def llm_attrs(prompt: int, completion: int, cached: int, cost: float) -> dict[str, Any]:
    return {
        "llm": {
            "token_count": {
                "prompt": prompt,
                "completion": completion,
                "total": prompt + completion,
                "prompt_details": {"cache_read": cached},
            },
            "cost": {"total": cost},
            "token_usage_present": True,
        }
    }


def sample(pm: Any) -> list[dict[str, Any]]:
    """两次运行：一次全绿，一次工具失败 + 检测器降级。"""
    return [
        span("agent.request", "T1", "2026-10-11T10:00:00Z", 1200.0),
        span("input_guard", "T1", "2026-10-11T10:00:00Z", 5.0, guard_attrs("allow")),
        span("llm_call", "T1", "2026-10-11T10:00:01Z", 900.0, llm_attrs(100, 50, 60, 0.0009)),
        span("tool_guard", "T1", "2026-10-11T10:00:02Z", 3.0, guard_attrs("allow")),
        span(
            "tool_call",
            "T1",
            "2026-10-11T10:00:02Z",
            7.0,
            {"tool": {"name": "get_product_rate", "status": "ok", "duration_ms": 7.0}},
        ),
        span(
            "output_guard",
            "T1",
            "2026-10-11T10:00:03Z",
            4.0,
            guard_attrs("redact", True),
        ),
        span("agent.request", "T2", "2026-10-11T10:01:00Z", 800.0),
        span("llm_call", "T2", "2026-10-11T10:01:01Z", 700.0, llm_attrs(80, 20, 0, 0.0004)),
        span(
            "tool_call",
            "T2",
            "2026-10-11T10:01:02Z",
            2.0,
            {"tool": {"name": "ghost", "status": "not_found"}},
        ),
        span(
            "output_guard",
            "T2",
            "2026-10-11T10:01:03Z",
            4.0,
            guard_attrs(
                "block",
                True,
                {"fail_mode": "closed", "failed_detectors": "llm.judge"},
            ),
        ),
    ]


class TestTraceGrouping:
    def test_groups_by_trace_id_not_time_window(self, pm: Any) -> None:
        """按 trace 归属，不按时间窗切。

        并发请求下按时间切会把两次请求的 span 混成一组 —— 延迟和失败率
        都会错，而且**不会报任何错**。
        """
        report = pm.compute(sample(pm), 10)
        assert report["traces"] == 2

    def test_last_n_selects_newest(self, pm: Any) -> None:
        report = pm.compute(sample(pm), 1)
        assert report["traces"] == 1
        # 最近那次是 T2（800ms、工具 not_found）
        assert report["performance"]["avg_latency_ms"] == 800.0
        assert report["quality"]["tool_status_breakdown"] == {"not_found": 1}

    def test_spans_without_root_are_not_counted_as_runs(self, pm: Any) -> None:
        """没有 agent.request 的孤儿 span 不该被当成一次运行。"""
        orphan = [span("llm_call", "TX", "2026-10-11T10:00:00Z", 10.0, llm_attrs(1, 1, 0, 0.1))]
        assert pm.compute(orphan, 10)["traces"] == 0

    def test_empty_input_does_not_explode(self, pm: Any) -> None:
        assert pm.compute([], 10).get("traces") == 0


class TestPerformanceMetrics:
    def test_percentiles_use_nearest_rank(self, pm: Any) -> None:
        """分位数取真实观测值，不做插值。

        插值出来的 p99 是一个没人能复现的数字；最近秩至少能指到某条
        真实发生过的观测。
        """
        report = pm.compute(sample(pm), 10)
        perf = report["performance"]
        assert perf["avg_latency_ms"] == 1000.0
        assert perf["p50_ms"] == 800.0
        assert perf["p95_ms"] == 1200.0
        assert perf["max_ms"] == 1200.0

    def test_percentile_on_single_sample(self, pm: Any) -> None:
        assert pm._percentile([42.0], 99) == 42.0

    def test_percentile_empty_is_zero(self, pm: Any) -> None:
        assert pm._percentile([], 95) == 0.0

    def test_stage_latency_breakdown(self, pm: Any) -> None:
        report = pm.compute(sample(pm), 10)
        assert report["performance"]["guard_latency_avg_ms"] == 5.0
        assert report["performance"]["tool_duration_avg_ms"] == 3.5


class TestQualityMetrics:
    def test_tool_success_rate_uses_status_field(self, pm: Any) -> None:
        """工具失败率必须按 tool.status 算，不能按"有没有 span"算。"""
        report = pm.compute(sample(pm), 10)
        assert report["quality"]["tool_calls"] == 2
        assert report["quality"]["tool_success_rate"] == 0.5

    def test_status_breakdown_covers_every_tool_span(self, pm: Any) -> None:
        report = pm.compute(sample(pm), 10)
        breakdown = report["quality"]["tool_status_breakdown"]
        assert sum(breakdown.values()) == report["quality"]["tool_calls"]

    def test_rate_with_zero_denominator(self, pm: Any) -> None:
        assert pm.rate(0, 0) == 0.0


class TestCostMetrics:
    def test_token_totals(self, pm: Any) -> None:
        cost = pm.compute(sample(pm), 10)["cost"]
        assert cost["prompt_tokens"] == 180
        assert cost["completion_tokens"] == 70
        assert cost["llm_calls"] == 2

    def test_cache_hit_rate(self, pm: Any) -> None:
        """缓存命中率是本项目最值得盯的成本指标 —— 命中与否差一个数量级。"""
        cost = pm.compute(sample(pm), 10)["cost"]
        assert cost["cache_read_tokens"] == 60
        assert abs(cost["cache_hit_rate"] - 60 / 180) < 1e-4

    def test_cost_sum_and_average(self, pm: Any) -> None:
        cost = pm.compute(sample(pm), 10)["cost"]
        assert abs(cost["total_cost_cny"] - 0.0013) < 1e-9
        assert abs(cost["avg_cost_cny"] - 0.00065) < 1e-9

    def test_spans_without_usage_are_counted_separately(self, pm: Any) -> None:
        """缺 usage 的 span 成本按 0 记，会拉低总量 —— 必须能单独看见。"""
        spans = [
            span("agent.request", "T1", "2026-10-11T10:00:00Z", 100.0),
            span(
                "llm_call",
                "T1",
                "2026-10-11T10:00:01Z",
                50.0,
                {"llm": {"token_usage_present": False}},
            ),
        ]
        cost = pm.compute(spans, 10)["cost"]
        assert cost["spans_without_usage"] == 1
        assert cost["total_cost_cny"] == 0.0


class TestSafetyMetrics:
    def test_detector_failures_and_degraded_path_are_visible(self, pm: Any) -> None:
        """降级机制是否在工作，只有这两个数能说明。

        检测器挂了而它们一直是 0，说明不是没挂，是根本没跑到。
        """
        safety = pm.compute(sample(pm), 10)["safety"]
        assert safety["detector_failures"] == 1
        assert safety["degraded_path_hits"] == 1

    def test_action_breakdown(self, pm: Any) -> None:
        safety = pm.compute(sample(pm), 10)["safety"]
        assert safety["action_breakdown"] == {"allow": 1, "block": 1, "redact": 1}

    def test_tool_guard_blocked_rate(self, pm: Any) -> None:
        spans = [
            span("agent.request", "T1", "2026-10-11T10:00:00Z", 100.0),
            span("tool_guard", "T1", "2026-10-11T10:00:01Z", 3.0, guard_attrs("allow")),
            span(
                "tool_guard",
                "T1",
                "2026-10-11T10:00:02Z",
                3.0,
                guard_attrs("require_approval", True),
            ),
        ]
        safety = pm.compute(spans, 10)["safety"]
        assert safety["tool_guard_blocked_rate"] == 0.5

    def test_matched_flag_is_not_a_substitute_for_action(self, pm: Any) -> None:
        """guard.matched 在 default_action 时也是 true —— 判断拦截必须看 action。"""
        spans = [
            span("agent.request", "T1", "2026-10-11T10:00:00Z", 100.0),
            span("input_guard", "T1", "2026-10-11T10:00:01Z", 3.0, guard_attrs("allow", True)),
        ]
        safety = pm.compute(spans, 10)["safety"]
        # 命中了规则但动作是 allow —— 统计必须如实反映 action
        assert safety["matched_count"] == 1
        assert safety["action_breakdown"] == {"allow": 1}


class TestRendering:
    def test_human_output_mentions_every_group(self, pm: Any) -> None:
        text = pm.render_human(pm.compute(sample(pm), 10), None, "http://x")
        for marker in ("【性能】", "【质量】", "【成本】", "【安全】"):
            assert marker in text

    def test_group_filter_narrows_output(self, pm: Any) -> None:
        text = pm.render_human(pm.compute(sample(pm), 10), "cost", "http://x")
        assert "【成本】" in text
        assert "【性能】" not in text

    def test_empty_report_is_explained_not_crashed(self, pm: Any) -> None:
        text = pm.render_human(pm.compute([], 10), None, "http://x")
        assert "没有可汇总的数据" in text

    def test_json_output_is_serialisable(self, pm: Any) -> None:
        import json

        report = pm.compute(sample(pm), 10)
        assert json.loads(json.dumps(report, ensure_ascii=False)) == report


class TestPagination:
    """分页契约 —— 这组用例是踩过坑之后补的。

    第一版把 ``first`` 直接设成默认的 1500，在目标机上**每次运行都报**
    ``first must be less than or equal to 1000``。它没被上面任何一条口径
    用例抓到，因为它们根本不联网。

    所以这里锁的是"发给 Phoenix 的查询长什么样"，不是算出来的数字。
    """

    @staticmethod
    def _page(count: int, has_next: bool, cursor: str = "C1") -> dict[str, Any]:
        edges = [
            {
                "cursor": f"c{i}",
                "node": {
                    "name": "agent.request",
                    "startTime": "2026-10-11T01:00:00+00:00",
                    "latencyMs": 10.0,
                    "attributes": "{}",
                    "trace": {"id": f"t{i}"},
                },
            }
            for i in range(count)
        ]
        return {
            "data": {
                "projects": {
                    "edges": [
                        {
                            "node": {
                                "name": "default",
                                "spans": {
                                    "pageInfo": {
                                        "hasNextPage": has_next,
                                        "endCursor": cursor,
                                    },
                                    "edges": edges,
                                },
                            }
                        }
                    ]
                }
            }
        }

    def _run(self, pm: Any, pages: list[dict[str, Any]], limit: int):
        queries: list[str] = []

        def fake_post(endpoint: str, query: str) -> dict[str, Any]:
            queries.append(query)
            return pages[len(queries) - 1]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(pm, "_post", fake_post)
        try:
            result = pm._fetch("http://x", "default", limit, pm.datetime.now(pm.UTC))
        finally:
            monkey.undo()
        return result, queries

    def test_page_size_never_exceeds_phoenix_cap(self, pm: Any) -> None:
        """默认 limit(4000) 远超单页上限，必须自动拆页而不是直接报错。"""
        assert pm.DEFAULT_SPAN_LIMIT > pm.PHOENIX_PAGE_MAX

        pages = [self._page(pm.PHOENIX_PAGE_MAX, True, "C1") for _ in range(4)]
        spans, queries = self._run(pm, pages, pm.DEFAULT_SPAN_LIMIT)

        assert len(queries) >= 4, "没有翻页，limit 会被静默截断"
        for query in queries:
            assert f"first: {pm.PHOENIX_PAGE_MAX}," in query
        assert len(spans) == 4 * pm.PHOENIX_PAGE_MAX

    def test_cursor_is_sent_on_later_pages(self, pm: Any) -> None:
        pages = [self._page(2, True, "C1"), self._page(2, False, "C2")]
        _, queries = self._run(pm, pages, pm.DEFAULT_SPAN_LIMIT)

        assert "after:" not in queries[0], "首页不该带游标"
        assert 'after: "C1"' in queries[1], "第二页必须带上页的 endCursor"

    def test_stops_when_no_next_page(self, pm: Any) -> None:
        pages = [self._page(3, False)]
        spans, queries = self._run(pm, pages, pm.DEFAULT_SPAN_LIMIT)

        assert len(queries) == 1
        assert len(spans) == 3

    def test_stops_when_page_is_empty(self, pm: Any) -> None:
        """空页但 hasNextPage 仍为 true 时不能死循环。"""
        pages = [self._page(0, True, "C1")]
        spans, queries = self._run(pm, pages, pm.DEFAULT_SPAN_LIMIT)

        assert len(queries) == 1
        assert spans == []

    def test_limit_below_cap_does_not_paginate(self, pm: Any) -> None:
        pages = [self._page(5, False)]
        _, queries = self._run(pm, pages, 10)
        assert len(queries) == 1
        assert "first: 10," in queries[0]
