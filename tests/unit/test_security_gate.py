"""安全检测降级闸门的单元测试。

这个闸门本身是"让部署失败不再阻断"的东西 —— 一旦它坏了，表现形式就是
"该记录的证据没记录"或者"不该降级的也降级了"。两者都不会让测试变红，
所以必须自己钉死。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import security_gate


@pytest.fixture(autouse=True)
def _clean_gate_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """每个用例都从干净状态开始，且开关默认关闭。"""
    monkeypatch.delenv(security_gate.SKIP_ENV, raising=False)
    monkeypatch.setenv(security_gate.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AGENT_SECURITY_GATE_REPORT", str(tmp_path / "report.json"))
    security_gate._entries.clear()
    security_gate.begin_test("tests/functional/x.py::test_y")
    yield
    security_gate._entries.clear()
    security_gate.end_test()


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


class TestSwitch:
    def test_disabled_by_default(self) -> None:
        """默认关闭。忘记关掉它必须是显式动作，而不是默认行为。"""
        assert security_gate.is_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_enable(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(security_gate.SKIP_ENV, value)
        assert security_gate.is_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_other_values_keep_it_off(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(security_gate.SKIP_ENV, value)
        assert security_gate.is_enabled() is False


class TestRequestTracking:
    def test_requests_are_attributed_to_the_current_test(self) -> None:
        security_gate.note_request("req-aaa")
        security_gate.note_request("req-bbb")
        security_gate.note_request(None)  # 没有 request_id 的不记
        assert security_gate._current_requests == ["req-aaa", "req-bbb"]

    def test_new_test_resets_the_window(self) -> None:
        security_gate.note_request("req-aaa")
        security_gate.begin_test("tests/functional/x.py::test_z")
        assert security_gate._current_requests == []


class TestLedgerEvidence:
    def test_evidence_is_collected_by_request_id(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(
            ledger,
            [
                {
                    "request_id": "req-hit",
                    "stage": "output",
                    "action": "block",
                    "rule_name": "block_critical_injection",
                    "hit_labels": ["system_prompt_leak"],
                    "evidence": ["reveal the system prompt"],
                    "recorded_at": "2026-10-11T23:49:38Z",
                },
                {
                    "request_id": "req-other",
                    "stage": "input",
                    "action": "allow",
                    "rule_name": None,
                    "hit_labels": [],
                    "evidence": [],
                    "recorded_at": "2026-10-11T23:49:00Z",
                },
            ],
        )
        got = security_gate.collect_ledger_evidence(["req-hit"])
        assert got["ledger"] == str(ledger)
        assert len(got["records"]) == 1
        assert got["records"][0]["rule_name"] == "block_critical_injection"

    def test_missing_ledger_is_reported_not_hidden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """半截证据比没有证据更危险 —— 它看起来像是完整的。

        所以找不到账本时必须显式写明，绝不能安静地返回空列表。
        """
        monkeypatch.setenv(security_gate.LEDGER_ENV, str(tmp_path / "不存在.jsonl"))
        got = security_gate.collect_ledger_evidence(["req-aaa"])
        assert got["ledger"] is None
        assert got["note"] == "未找到审计账本，证据不完整"
        assert got["records"] == []

    def test_corrupt_ledger_line_does_not_abort_the_scan(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"request_id": "req-a", "action": "block"}\n{ 这不是 JSON\n'
            '{"request_id": "req-b", "action": "block"}\n',
            encoding="utf-8",
        )
        got = security_gate.collect_ledger_evidence(["req-a", "req-b"])
        assert [r["request_id"] for r in got["records"]] == ["req-a", "req-b"]


class TestFailureDowngrade:
    def _fake_report(self, text: str):
        class R:
            longrepr = text

        return R()

    def test_allow_records_are_excluded_from_the_summary(self, tmp_path: Path) -> None:
        """账本里 allow 的记录不是"命中了什么"，不该混进证据摘要。"""
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(
            ledger,
            [
                {"request_id": "req-a", "stage": "input", "action": "allow", "rule_name": None},
                {"request_id": "req-a", "stage": "output", "action": "block", "rule_name": "r"},
            ],
        )
        security_gate.note_request("req-a")
        entry = security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report("E assert 1 == 2")
        )
        assert len(entry["ledger_hits"]) == 1
        assert entry["ledger_hits"][0]["rule_name"] == "r"

    def test_skip_reason_carries_the_evidence(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(
            ledger,
            [
                {
                    "request_id": "req-a",
                    "stage": "output",
                    "action": "block",
                    "rule_name": "block_critical_injection",
                    "evidence": ["reveal the system prompt"],
                }
            ],
        )
        security_gate.note_request("req-a")
        entry = security_gate.record_failure(
            type("I", (), {"nodeid": "tests/functional/test_l3_functional.py::t"})(),
            self._fake_report("E assert 'allow' == 'block'"),
        )
        reason = security_gate.skip_reason(entry)
        assert "安全检测失败已降级为 skip" in reason
        assert "block_critical_injection" in reason
        assert "allow" in reason  # 失败原因本身要留在 skip reason 里

    def test_report_is_written_and_announced(self, tmp_path: Path) -> None:
        security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report("E boom")
        )
        summary = security_gate.flush()
        assert summary is not None
        assert "安全检测降级闸门" in summary
        payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert payload["setting"] == security_gate.SKIP_ENV
        assert payload["entries"][0]["nodeid"] == "n"

    def test_flush_is_a_noop_when_nothing_was_downgraded(self) -> None:
        """没有降级就不该产生报告 —— 空报告会让人以为"检查过了没问题"。"""
        assert security_gate.flush() is None

    def test_summary_says_it_is_not_a_pass(self, tmp_path: Path) -> None:
        """降级 ≠ 通过。这句话必须出现在部署日志里。"""
        security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report("E boom")
        )
        summary = security_gate.flush()
        assert summary is not None
        assert "不算通过" in summary
        assert "上线前重大问题.md" in summary
