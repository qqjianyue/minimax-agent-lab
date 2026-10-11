"""误报率基线脚本的单测（L0 离线，不发网络请求）。

这个工具的全部价值在两件事上，所以单测也只钉这两件：

1. **fail-closed 必须和"判定结论"分开** —— 把"模型挂了被拦"记成安全系统
   的战果，是这个工具最容易犯也最贵的错；
2. **重复采样要出分布** —— 单次采样得到的数字不能被当成误报率。

其余（HTTP 交互、报告渲染）刻意不测：那是排障工具，真实服务才是它的测试。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fp_baseline.py"
_spec = importlib.util.spec_from_file_location("fp_baseline", _SCRIPT)
assert _spec and _spec.loader
fp_baseline: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules["fp_baseline"] = fp_baseline
_spec.loader.exec_module(fp_baseline)


def _body(
    *,
    action: str = "allow",
    input_guard: dict | None = None,
    output_guard: dict | None = None,
    retrieval: dict | None = None,
    llm_called: bool = True,
) -> dict:
    body: dict = {
        "action": action,
        "input_guard": input_guard,
        "output_guard": output_guard,
        "llm_called": llm_called,
    }
    if retrieval is not None:
        body["retrieval"] = retrieval
    return body


def _guard(action: str, rule: str | None, matched: bool = True) -> dict:
    return {"action": action, "rule_name": rule, "matched": matched}


class TestAttribution:
    def test_allow_is_allow(self) -> None:
        assert fp_baseline.attribute(_body(action="allow")) == ("allow", None, "")

    def test_input_rule_is_named(self) -> None:
        got = fp_baseline.attribute(
            _body(action="block", input_guard=_guard("block", "block_critical_injection"))
        )
        assert got == ("block", "input:block_critical_injection", "block_critical_injection")

    def test_output_rule_is_named(self) -> None:
        got = fp_baseline.attribute(
            _body(
                action="redact",
                input_guard=_guard("allow", None, False),
                output_guard=_guard("redact", "redact_pii"),
            )
        )
        assert got == ("redact", "output:redact_pii", "redact_pii")

    def test_fail_closed_is_not_counted_as_a_judgment(self) -> None:
        """**核心断言**：模型不可用被拦，与模型判有害，必须是两个归因。

        前者没有任何 ``rule_name``、``matched`` 为假、``llm_called`` 也是假 ——
        把它们混在一起统计，会把纯可用性问题记成安全系统的战果。
        """
        got = fp_baseline.attribute(
            _body(
                action="block",
                input_guard=_guard("block", None, matched=False),
                llm_called=False,
            )
        )
        assert got == ("block", "input:fail_closed", "fail_closed")

    def test_input_takes_precedence_over_output(self) -> None:
        """两侧都非放行时，输入侧先判 —— 排除入口问题优先于出口。"""
        got = fp_baseline.attribute(
            _body(
                action="block",
                input_guard=_guard("block", "block_critical_injection"),
                output_guard=_guard("block", "block_critical_injection"),
            )
        )
        assert got[1] == "input:block_critical_injection"

    def test_quarantine_is_attributed_to_retrieval(self) -> None:
        """隔离不是拦截；两侧都放行却仍非 allow 时才指向检索。"""
        got = fp_baseline.attribute(
            _body(
                action="block",
                input_guard=_guard("allow", None, False),
                output_guard=_guard("allow", None, False),
                retrieval={"quarantined": ["ext-01"]},
            )
        )
        assert got[1] == "retrieval:quarantine"

    def test_unattributable_is_explicit_not_guessed(self) -> None:
        """说不清来源时必须落到 unknown —— 宁可暴露缺口，不要归错因。"""
        got = fp_baseline.attribute(
            _body(action="escalate", input_guard=_guard("allow", None, False))
        )
        assert got[1] == "unknown"


def _samples(rows: list[tuple[int, str, str, str | None]]) -> list[dict]:
    return [
        {
            "id": case_id,
            "round": r,
            "text": case_id,
            "action": action,
            "attribution": attr,
            "rule_name": attr.split(":")[-1],
            "detail": "",
            "llm_called": action == "allow",
            "retrieval": None,
            "at": "2026-01-01T00:00:00+00:00",
        }
        for r, case_id, action, attr in rows
    ]


_HEALTH = {
    "version": {"version": "0.1.19"},
    "retrieval": {"enabled": True, "top_k": 2},
    "detectors": ["rules.l1"],
}


class TestReport:
    def test_spread_is_reported_not_just_the_mean(self) -> None:
        """两轮差异很大时，均值会骗人 —— 极差必须单独出现在报告里。"""
        samples = _samples(
            [(1, "a", "allow", ""), (1, "b", "block", "input:block_critical_injection"),
             (2, "a", "allow", ""), (2, "b", "allow", "")]
        )
        report = fp_baseline.build_report("http://x", 2, samples, _HEALTH)
        ar = report["allow_rate"]
        assert ar["mean"] == pytest.approx(0.75)
        assert ar["min"] == pytest.approx(0.5)
        assert ar["max"] == pytest.approx(1.0)
        assert ar["spread"] == pytest.approx(0.5)

    def test_flaky_and_always_bad_are_separated(self) -> None:
        """稳定被拦与偶发被拦指向完全不同的修法，必须分开列。"""
        samples = _samples(
            [
                (1, "flaky", "allow", ""),
                (1, "flaky", "block", "output:redact_pii"),
                (2, "flaky", "allow", ""),
                (1, "stable_bad", "block", "output:redact_pii"),
                (2, "stable_bad", "block", "output:redact_pii"),
            ]
        )
        report = fp_baseline.build_report("http://x", 2, samples, _HEALTH)
        assert report["flaky_cases"] == ["flaky"]
        assert report["always_bad_cases"] == ["stable_bad"]

    def test_fail_closed_totals_are_counted_separately(self) -> None:
        samples = _samples(
            [
                (1, "a", "block", "input:fail_closed"),
                (2, "a", "block", "input:fail_closed"),
                (1, "b", "block", "input:block_critical_injection"),
            ]
        )
        report = fp_baseline.build_report("http://x", 2, samples, _HEALTH)
        assert report["fail_closed_count"] == 2
        assert report["attribution"]["input:fail_closed"] == 2
        assert report["attribution"]["input:block_critical_injection"] == 1

    def test_quarantined_documents_are_counted_separately(self) -> None:
        """隔离不影响放行，所以它单独计数，不混进非放行统计。"""
        samples = _samples([(1, "a", "allow", "")])
        samples[0]["retrieval"] = {"quarantined": ["ext-01", "ext-04"]}
        report = fp_baseline.build_report("http://x", 1, samples, _HEALTH)
        assert report["quarantined_documents"] == 2
        assert report["attribution"] == {}

    def test_render_flags_high_variance(self) -> None:
        """极差 ≥ 10% 时必须在报告里明说「单次采样不足以代表误报率」。"""
        samples = _samples(
            [(1, "a", "allow", ""), (1, "b", "block", "output:redact_pii"),
             (2, "a", "allow", ""), (2, "b", "allow", "")]
        )
        report = fp_baseline.build_report("http://x", 2, samples, _HEALTH)
        text = fp_baseline.render(report)
        assert "极差" in text
        assert "单次采样不足以代表误报率" in text


class TestCorpus:
    def test_corpus_ids_are_unique(self) -> None:
        ids = [c["id"] for c in fp_baseline.CORPUS]
        assert len(ids) == len(set(ids))

    def test_corpus_texts_are_non_blank(self) -> None:
        assert all(c["text"].strip() for c in fp_baseline.CORPUS)

    def test_corpus_covers_the_known_fp_shapes(self) -> None:
        """语料必须覆盖已知的几个误报重灾区，否则量出来的数没有意义。"""
        joined = " ".join(c["text"] for c in fp_baseline.CORPUS)
        for shape in ("营业时间", "网点地址", "余额", "转账", "卡"):
            assert shape in joined, f"语料缺少「{shape}」这一类形态"