"""L2 验收用例里 **GraphQL 查询的拼装形状**（纯离线，不碰网络）。

## 为什么要有这组测试

`tests/smoke/test_l2_trace_acceptance.py` 只在目标机上跑，**离线闸门永远
碰不到它**。于是它的查询在部署前一直是没人验证过的 —— 事实上它带着一个
拼装错误部署了两次：

    spans(first: 500, , timeRange: {start: "...", end: "..."})

多了一个逗号、`timeRange:` 重复了一遍。Phoenix 回的是
``Syntax Error: Expected Name, found ':'``，但那行异常被轮询里的
``except`` 吞掉，最终报成"一条都没有"—— 一个和真实故障长得一模一样、
但完全错误的错误信息，排查多花了十几分钟。

这类 bug 不需要真Phoenix 就能拦：只要断言**拼出来的查询结构是对的**。
所以这里不测指标口径，只测"发给 Phoenix 的那串字符本身合法"。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "tests" / "smoke" / "test_l2_trace_acceptance.py"


@pytest.fixture(scope="module")
def acc() -> Any:
    if not MODULE_PATH.is_file():
        pytest.skip(f"验收用例不存在: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("l2_acceptance_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _render(acc: Any, since: float | None) -> str:
    """复刻 _fetch_spans 里的拼装过程，但不发请求。"""
    return acc._SPAN_QUERY % {"limit": 500, "timeRange": acc._render_time_range(since)}


def _assert_structurally_sane(query: str) -> None:
    """够用的 GraphQL 结构检查：括号配对 + 每个参数都是非空的 键: 值。"""
    assert query.count("{") == query.count("}"), "花括号不配对"
    assert query.count("(") == query.count(")"), "圆括号不配对"

    # 没有 ".." / ",," / "(," / ",)"：空参数位是最常见的拼装事故
    assert ",," not in query, f"出现空参数位：{query}"
    assert "(," not in query and ",)" not in query, "spans(...) 括号内有多余逗号"

    args = re.search(r"spans\(([^)]*)\)", query)
    assert args, "查询里没有 spans(...)"

    keys, values = [], []
    for part in args.group(1).split(","):
        assert ":" in part, f"参数缺少冒号：{part.strip()!r}"
        key, value = part.split(":", 1)
        keys.append(key.strip())
        values.append(value.strip())

    assert "timeRange" in keys
    assert len(keys) == len(set(keys)), f"参数键重复：{keys}"
    assert all(keys), f"存在空参数键：{keys}"
    # 键对了但值是空的（``timeRange: )``）同样是非法查询，必须一起拦
    assert all(values), f"存在空参数值：{list(zip(keys, values, strict=True))}"


class TestSpanQueryShape:
    def test_render_with_since_is_sane(self, acc: Any) -> None:
        _assert_structurally_sane(_render(acc, since=1_700_000_000.0))

    def test_render_without_since_is_still_valid(self, acc: Any) -> None:
        """省略 since 曾经拼出 ``timeRange: )`` —— 语法错，且不会立刻报。"""
        query = _render(acc, since=None)
        _assert_structurally_sane(query)
        assert "timeRange: )" not in query
        assert "timeRange: ," not in query

    def test_time_range_is_the_only_time_range_key(self, acc: Any) -> None:
        query = _render(acc, since=1_700_000_000.0)
        assert query.count("timeRange") == 1, "timeRange 键出现多次，模板与变量重复拼接"

    def test_time_range_brackets_are_balanced_and_quoted(self, acc: Any) -> None:
        value = acc._render_time_range(1_700_000_000.0)
        assert value.startswith("{") and value.endswith("}")
        assert value.count("{") == value.count("}")
        assert value.count('"') == 4, f"start/end 应各有一对引号：{value}"
        assert "start:" in value and "end:" in value


class TestTimestampHandling:
    def test_parse_accepts_both_offsets(self, acc: Any) -> None:
        """Phoenix 有时回 ``+00:00``，有时回 ``Z``，两种都要认。"""
        with_z = acc._parse("2026-10-11T04:03:34.887826Z")
        with_offset = acc._parse("2026-10-11T04:03:34.887826+00:00")
        assert with_z == with_offset

    def test_parse_garbage_is_none_not_crash(self, acc: Any) -> None:
        assert acc._parse("") is None
        assert acc._parse(None) is None
        assert acc._parse("not-a-time") is None

    def test_within_uses_real_comparison_not_string_order(self, acc: Any) -> None:
        """按字符串比大小会在同秒、小数秒处判错，必须是真时间比较。

        这两个样本故意落在 ``since`` 的同一秒内 —— 字符串比较会在
        ``...:20Z`` 与 ``...:20.500000+00:00`` 之间得出错误结论。
        """
        base = 1_700_000_000.5
        since = int(base)

        def stamp(epoch: float) -> str:
            moment = acc.datetime.fromtimestamp(epoch, acc.UTC)
            return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"

        assert acc._within({"startTime": stamp(base)}, since) is True
        assert acc._within({"startTime": stamp(base + 30)}, since) is True
        assert acc._within({"startTime": stamp(since - 3600)}, since) is False
