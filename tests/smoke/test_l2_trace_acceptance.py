"""L2 冒烟 · 阶段二验收：可观测性端到端可验证。

## 这组用例为什么必须存在

阶段二的验收标准第一条是：

    "任意一次 Agent 运行后，能在 observability 工具中看到完整 trace"

它长期是**靠人眼看 Phoenix** 保证的。人眼的问题不是不可靠，而是**不在
CI 里** —— 谁把埋点改坏了，本地单测和 L0/L1 全绿，因为它们用的是内存
telemetry；等发现时，trace 已经断了一段时间。

这组用例把验收标准本身变成会红的断言。

## 与已有测试的分工

* L0/L1 用 :class:`~tests.fakes.InMemoryTelemetry` 验证 span 结构 —— 快，
  但**验证不到"真的导出到 Phoenix"**
* 本文件验证导出链路本身：请求 → OTel → OTLP → Phoenix → 查得到

## 工具 span 的断言为什么是条件的

真实模型**不一定**会发起工具调用（是否调用由模型行为决定，不可控）。
所以核心 span（根 / 输入检测 / 模型调用 / 输出检测）**必须**齐全，
而 ``tool_call`` 只在响应里确实 ``tools_called`` 时才断言 ——
写死"必须有工具 span"会得到一条靠运气的测试，红了也不知道是回归还是模型没听话。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from tests.target_support import base_url, phoenix_endpoint

#: 一次请求**必定**产生的 span。少任何一个都说明埋点断了。
REQUIRED_SPANS = {"agent.request", "input_guard", "llm_call", "output_guard"}

#: BatchSpanProcessor 是异步批量发送的，请求返回后 span 可能还没到 Phoenix。
#: 轮询而不是死等固定秒数：固定等待要么 flaky（等待不够就红），
#: 要么拖慢每一轮（等待过长）。
EXPORT_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 1.0

_SPAN_QUERY = """
{
  projects(first: 20) {
    edges {
      node {
        name
        spans(first: %(limit)d, timeRange: %(timeRange)s) {
          edges {
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


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _graphql(query: str, timeout: float = 20.0) -> dict[str, Any]:
    payload = json.dumps({"query": query}).encode("utf-8")
    response = httpx.post(
        f"{phoenix_endpoint()}/graphql",
        content=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise AssertionError(f"Phoenix GraphQL 错误：{data['errors'][0].get('message')}")
    return data


def _render_time_range(since: float | None) -> str:
    """拼出 ``timeRange`` 的取值部分（**不含** ``timeRange:`` 这个键本身）。

    单独抽出来是为了能被离线单测直接断言 —— 模板里已经有 ``timeRange:``，
    这里再带一次就会拼出重复键和空参数位。
    """
    # 省略 since 时退化成近 24 小时，而不是空串：空串会拼出 ``timeRange: )``。
    floor = since if since is not None else time.time() - 24 * 3600
    # 起点留 60s 余量：测试机与 Phoenix 主机可能存在时钟偏差，
    # 卡太紧会在 span 已到达时被边界切掉。
    return f'{{start: "{_stamp(floor - 60)}", end: "{_stamp(time.time() + 60)}"}}'


def _fetch_spans(
    project: str = "default",
    limit: int = 500,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """取 span。``since`` 给的是窗口起点，**下推到 Phoenix 的 timeRange**。

    必须下推，不能取回来再在本地按时间筛：``spans(first: N)`` 默认返回的是
    项目里**最早**的 N 条。项目跑久了（实测 ~1300 次运行），无 timeRange
    的查询拿到的是几小时前的历史数据，随后任何"最近一次请求"的判定都会
    得到"没有新数据"，而**不会报任何错**。

    这个坑本项目在指标脚本和这份用例上各踩过一次 —— 所以拼装细节另有
    离线单测（``tests/unit/test_l2_acceptance_query.py``）盯着。
    """
    data = _graphql(_SPAN_QUERY % {"limit": limit, "timeRange": _render_time_range(since)})
    for edge in data["data"]["projects"]["edges"]:
        node = edge["node"]
        if node["name"] != project:
            continue
        out = []
        for item in node["spans"]["edges"]:
            span = dict(item["node"])
            raw = span.get("attributes")
            try:
                span["attributes"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                span["attributes"] = {}
            out.append(span)
        return out
    return []


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within(span: dict[str, Any], since: float) -> bool:
    """span 是否落在本次运行之后（含少量时钟偏差余量）。"""
    started = _parse(span.get("startTime"))
    return started is not None and started >= datetime.fromtimestamp(since - 5, UTC)


def _latest_run_trace(spans: list[dict[str, Any]], since: float) -> set[str] | None:
    """返回本次请求所在 trace 的 span 名集合；还没到就返回 None。"""
    floor = datetime.fromtimestamp(since - 5, UTC)
    fresh = [s for s in spans if (_parse(s.get("startTime")) or floor) >= floor]
    if not fresh:
        return None

    # 本次请求的 trace = 最近这批里时间最新的那个（按 trace 归属，不猜时间窗）
    latest = max(fresh, key=lambda s: _parse(s.get("startTime")) or floor)
    tid = (latest.get("trace") or {}).get("id")
    if not tid:
        return None
    return {s["name"] for s in spans if (s.get("trace") or {}).get("id") == tid}


def _wait_for_trace(since: float, timeout: float = EXPORT_TIMEOUT_S) -> set[str]:
    deadline = time.time() + timeout
    last_seen: set[str] | None = None
    last_error: str = ""
    while time.time() < deadline:
        try:
            names = _latest_run_trace(_fetch_spans(since=since), since)
        except httpx.HTTPError as exc:
            # 收集端刚起来时可能短暂连不上，重试是合理的
            last_error = f"连不上 Phoenix：{exc}"
            names = None
        if names:
            last_seen = names
            if names >= REQUIRED_SPANS:
                return names
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"{timeout:.0f}s 内 Phoenix 没有出现本次请求的完整 trace。\n"
        f"  期望至少含: {sorted(REQUIRED_SPANS)}\n"
        f"  实际看到  : {sorted(last_seen) if last_seen else '（一条都没有）'}\n"
        + (f"  最后一次查询报错: {last_error}\n" if last_error else "")
        + "  排查方向：telemetry.enabled 是否为 true；stderr 里是否有 "
        "'Failed to export spans'；phoenix_endpoint 是否指向真实收集端。\n"
        "  注意：查询本身出错（GraphQL 语法/字段不存在）会**立即**抛出而不是"
        "重试到超时 —— 那种情况重试 30 次也只是把真因藏起来。"
    )


@pytest.fixture(scope="module")
def traced_run(http: httpx.Client) -> tuple[dict[str, Any], set[str], float]:
    """跑一次真实请求，并拿到它在 Phoenix 里的 span 名集合与起始时刻。

    这里**不把失败降级成 skip**。trace 不可见就是验收不通过，必须红：
    降级成 skip 的结果是"遥测悄悄坏了、部署照样记成功"，而这正是
    这组用例要消灭的那件事。

    返回 ``since`` 是为了让后续断言只针对**这一次**运行，
    而不是"项目里最近一条 llm_call"——后者在并发或旧数据堆积时会指错对象。
    """
    since = time.time()
    response = http.post(
        f"{base_url()}/chat",
        json={"message": "用一句话介绍你们的定期存款产品"},
        timeout=120.0,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["llm_called"] is True, "本次运行没有真正调用模型，不构成有效的验收样本"

    names = _wait_for_trace(since)
    return body, names, since


class TestTraceIsVisibleInPhoenix:
    """验收标准 1：任意一次运行都能在观测工具里看到完整 trace。"""

    def test_core_spans_are_all_present(self, traced_run) -> None:
        _, names, _ = traced_run
        missing = REQUIRED_SPANS - names
        assert not missing, (
            f"trace 缺少 span: {sorted(missing)}。"
            "埋点被改坏时本地测试不会发现（它们用内存 telemetry），"
            "所以这条断言是唯一的守门人。"
        )

    def test_guard_spans_record_the_verdict(self, traced_run) -> None:
        """检测点 span 必须带判定结果，光有时长没有意义。"""
        body, names, _ = traced_run
        assert "input_guard" in names and "output_guard" in names

        # 请求 id 必须能从 trace 里找回来 —— 它是审计与 trace 的关联键
        assert body["request_id"].startswith("req-")

    def test_tool_spans_present_when_tools_were_called(self, traced_run) -> None:
        """模型确实调了工具时，tool_guard 与 tool_call 都必须有。

        条件断言：模型是否发起工具调用由它自己决定，写死会让这条测试
        变成靠运气 —— 红了分不清是回归还是模型没听话。
        """
        body, names, _ = traced_run
        if not body.get("tools_called"):
            pytest.skip("本次运行模型未发起工具调用，工具 span 不适用")
        assert "tool_guard" in names, "调了工具却没有 TOOL 阶段判定 span"
        assert "tool_call" in names, (
            "调了工具却没有工具执行 span —— 无法区分'被策略拦下'与'执行失败'，工具失败率也无从算起"
        )


class TestCostIsRecorded:
    """验收标准：模型调用要有 latency / token，且成本口径不能恒为 0。"""

    def test_llm_span_carries_tokens_and_latency(self, traced_run) -> None:
        body, names, _ = traced_run
        assert "llm_call" in names
        # Phoenix 的 latencyMs 是 span 原生字段，查询里已经取回来了；
        # body.usage 是同一份用量经响应回传，两边对不上说明埋点漏了一处
        assert body["usage"]["total_tokens"] > 0

    def test_cost_is_not_zero(self, traced_run) -> None:
        """成本恒为 0 是 B4 真实踩过的坑（属性名没对齐 OpenInference）。

        那次 trace 正常落库、Phoenix 界面正常显示，只是钱算不出来 ——
        不加这条断言，那种回归会静默通过很久。
        """
        _, _, since = traced_run
        spans = [
            s for s in _fetch_spans(since=since) if s["name"] == "llm_call" and _within(s, since)
        ]
        assert spans, (
            "本次运行的窗口内查不到 llm_call span —— 与 trace 断言矛盾，"
            "说明查询窗口或 timeRange 有问题"
        )
        latest = max(spans, key=lambda s: _parse(s.get("startTime")) or since)
        llm = latest["attributes"].get("llm", {})

        assert llm.get("token_count", {}).get("total", 0) > 0, (
            "llm_call span 上没有 token 用量 —— Phoenix 算不出成本"
        )
        assert llm.get("cost", {}).get("total", 0) > 0, (
            "llm.cost.total 恒为 0：单价未配置，或属性名没对齐 OpenInference "
            "约定导致 Phoenix 关联不到成本"
        )
        assert llm.get("model_name"), "缺 llm.model_name，Phoenix 匹配不到定价目录"


def test_phoenix_endpoint_is_configured() -> None:
    """收集端必须真实可达。

    遥测关闭时服务**照常健康** —— 接口正常、返回正确，只是没有 trace。
    所以这里把"收集端活着"单列为显式断言：它失败时，
    上面那些 trace 断言的失败原因才是清楚的（收集端不通 vs 埋点断了）。
    """
    try:
        _graphql("{ projects(first:1){ edges { node { name } } } }", timeout=10.0)
    except (httpx.HTTPError, AssertionError) as exc:
        pytest.fail(
            f"Phoenix 不可达（{phoenix_endpoint()}）：{exc}\n"
            "  遥测开启时收集端不可达，服务不会报错但 trace 会静默丢失 —— "
            "这正是要在部署阶段暴露的问题。"
        )
