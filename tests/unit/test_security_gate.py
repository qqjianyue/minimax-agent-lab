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

    def test_response_hook_reads_unread_body(self) -> None:
        """钩子触发时 body **还没被读取** —— 必须先 read()。

        httpx 在 ``Client._send_handling_redirects`` 里先派发响应钩子，
        ``Client.send()`` 才调 ``response.read()``。不先读就 ``.json()``
        会抛 ``ResponseNotRead``。

        这条测试的存在理由：早先那版单测用的是**假响应对象**（``.json()``
        直接能调），压根没复现这个约束，于是本地绿、线上空 —— 钩子装上了、
        跑了、什么也没做，降级报告里 ``request_ids`` 永远是空的且不报错。
        **测试替身掩盖了真实约束**，这是比 bug 本身更值得记的一条。
        所以这一组用例全部改用真的 ``httpx.Response``，不再有假对象。
        """
        import httpx

        def body():
            yield b'{"request_id": "req-real", "action": "allow"}'

        request = httpx.Request("POST", "http://testserver/chat")
        # 流式响应：_content 尚未设置，与真实钩子触发时机一致
        response = httpx.Response(200, content=body(), request=request)
        assert not hasattr(response, "_content"), "这个前提不成立，测试就白测了"

        security_gate.record_response_request_id(response)
        assert security_gate._current_requests == ["req-real"]

    def test_response_hook_is_idempotent_with_read_body(self) -> None:
        """已经读过一遍的响应（``Client.send()`` 随后的那次 read）不能出问题。"""
        import httpx

        response = httpx.Response(
            200, json={"request_id": "req-read"}, request=httpx.Request("GET", "http://x/")
        )
        response.read()
        security_gate.record_response_request_id(response)
        assert security_gate._current_requests == ["req-read"]

    def test_response_hook_ignores_bodies_without_request_id(self) -> None:
        import httpx

        response = httpx.Response(
            200, json={"detail": "hello"}, request=httpx.Request("GET", "http://x/")
        )
        security_gate.record_response_request_id(response)
        assert security_gate._current_requests == []

    def test_response_hook_survives_non_json_body(self) -> None:
        """响应体不是 JSON 时静默跳过 —— 那与本钩子无关，不能因此炸掉测试。"""
        import httpx

        def body():
            yield b"<html>not json</html>"

        response = httpx.Response(
            200, content=body(), request=httpx.Request("GET", "http://x/")
        )
        security_gate.record_response_request_id(response)
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

    def test_explicit_ledger_path_never_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式指定的账本路径是**排他的**。

        这条在目标机上才暴露：目标机的 ``$HOME/.minimax-agent-home/shared/
        audit/ledger.jsonl`` 真的存在，所以指向不存在文件的 override 若继续
        向下探测，就会**静默换成另一个账本** —— 报告里的证据来自一个没人
        预期的文件，而它看起来是完整的。
        """
        real = tmp_path / "真账本.jsonl"
        _write_ledger(
            real, [{"request_id": "req-a", "stage": "output", "action": "block"}]
        )
        monkeypatch.setenv(security_gate.LEDGER_ENV, str(tmp_path / "不存在.jsonl"))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert security_gate.ledger_path() is None

        monkeypatch.setenv(security_gate.LEDGER_ENV, str(real))
        assert security_gate.ledger_path() == real

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


class TestDowngradeScope:
    """降级范围钉死。

    范围写错的后果是**"该拦的没拦"被悄悄放过**，而那种错误不会让任何测试变红
    —— 因为它恰恰是靠"测试没红"来生效的。所以这里逐条钉。
    """

    @pytest.mark.parametrize(
        "cls_name",
        [
            "TestFT01NormalQuery",
            "TestFT02DirectInjection",
            "TestFT06SystemPromptLeak",
            "TestFT10FailMode",
            "TestFT14OrchestratedInputBlock",
        ],
    )
    def test_security_classes_are_downgradable(self, cls_name: str) -> None:
        assert security_gate.in_security_scope(cls_name, "test_anything") is True

    @pytest.mark.parametrize(
        ("cls_name", "method"),
        [
            ("TestFT11CostObservability", "test_cost_is_not_zero"),
            ("TestFT12AuditIntegrity", "test_audit_has_full_provenance"),
            ("TestFT13OrchestratedChat", "test_max_steps_field_present"),
            ("TestThinEndToEnd", "test_chat_does_not_consume_model_when_blocked"),
            ("TestCredentialHygiene", "test_no_endpoint_leaks_the_api_key"),
            ("TestLedgerWrites", "test_chat_appends_input_and_output_records"),
            (None, "test_something"),
        ],
        ids=[
            "cost-observability",
            "audit-integrity",
            "orchestration-shape",
            "injection-still-blocked",
            "credential-hygiene",
            "ledger-writes",
            "no-class",
        ],
    )
    def test_non_security_tests_are_not_downgradable(
        self, cls_name: str | None, method: str
    ) -> None:
        """这些失败一律照常阻断部署。

        尤其 ``TestThinEndToEnd::test_chat_does_not_consume_model_when_blocked``：
        它与被降级的那条同属一次真实 ``/chat``，但它断言的是**注入必须在模型之前
        被拦** —— 确定性测试，挂了就是真回归。
        """
        assert security_gate.in_security_scope(cls_name, method) is False

    def test_p01_exposed_smoke_test_is_method_scoped(self) -> None:
        """P0-1 在 L2 冒烟里的暴露点：正常提问断言 allow。

        同类的另一条断言 block，必须保持不可降级 —— 这正是需要方法级粒度的原因。
        """
        assert (
            security_gate.in_security_scope(
                "TestThinEndToEnd", "test_chat_reaches_the_model"
            )
            is True
        )
        assert (
            security_gate.in_security_scope(
                "TestThinEndToEnd", "test_chat_does_not_consume_model_when_blocked"
            )
            is False
        )

    def test_scope_ignores_missing_method(self) -> None:
        assert security_gate.in_security_scope("TestThinEndToEnd", None) is False


class TestFailureSummaryQuality:
    """失败摘要必须是**能读懂的句子**，不是 pytest 的 diff 残片。

    v0.1.22 首次部署时报告里出现过 ``"reason": "+ block"`` —— 那是断言 diff
    的最后一行。一行没头没尾的 ``+ block`` 等于什么都没说，而这份报告的唯一
    用途就是让人事后读懂"当时到底怎么了"。
    """

    def _fake_report(self, text: str):
        class R:
            longrepr = text

        return R()

    def test_prefers_the_custom_assertion_message(self) -> None:
        repr_text = (
            "E       AssertionError: 正常业务查询被判 block。中文误报回归的话，"
            "多半是 Presidio 的双语装配或形态过滤被改坏了。\n"
            "E       assert 'block' == 'allow'\n"
            "E        +  where block = body['action']\n"
        )
        entry = security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report(repr_text)
        )
        assert entry["reason"].startswith("AssertionError:")
        assert "正常业务查询被判 block" in entry["reason"]
        assert "+  where" not in entry["reason"]

    def test_falls_back_to_the_assert_line(self) -> None:
        repr_text = "E       assert 'block' == 'allow'\nE        +  where block = body['action']\n"
        entry = security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report(repr_text)
        )
        assert entry["reason"] == "assert 'block' == 'allow'"

    def test_empty_report_is_explicit(self) -> None:
        entry = security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report("")
        )
        assert entry["reason"] == "(无失败详情)"

    def test_reason_is_truncated_to_a_single_line(self) -> None:
        long_msg = "E       AssertionError: " + "很长的说明" * 200
        entry = security_gate.record_failure(
            type("I", (), {"nodeid": "n"})(), self._fake_report(long_msg)
        )
        assert "\n" not in entry["reason"]
        assert len(entry["reason"]) <= 400
