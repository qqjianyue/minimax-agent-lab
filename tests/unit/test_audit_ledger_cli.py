"""C8 审计账本 CLI 单元测试。

重点覆盖三件事：

1. **检索语义与 :meth:`AuditLedger.query` 一致** —— CLI 不自己实现过滤逻辑，
   这里用"C8 写入 → CLI 读出"的链路验证，避免两边语义漂移。
2. **路径解析优先级**（显式参数 > 环境变量 > 默认值）。
3. **``prune`` 只删过期记录，且落盘后账本仍可读**（原子重写）。

CLI 的入参错误（非法 action / 无法解析的时间）必须**明确报错**而不是
静默返回空结果 —— 一个拼错参数就什么都查不到的 CLI 比直接报错的 CLI 糟糕得多。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import audit_ledger
from audit_ledger import AuditLedger, JsonlAuditSink, SystemClockAdapter
from audit_ledger.__main__ import main
from detector_rules.redactor import RegexRedactor
from guard_contract.enums import FailMode, GuardStage, PolicyAction
from guard_contract.port import DetectorFailure, DetectorFailureReason
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision
from tests.fakes import FrozenClock

#: 账本时间轴的起点：90 天前。
#:
#: 取 90 天而不是"今天"，是为了让 prune 的保留期边界落在**数据内部**：
#: offset=0 的记录已过期 90 天，offset=88/89 的记录只有 1~2 天，
#: 保留期 30 天时前者在保留区内、后者在保留区外。留这么宽的余量，
#: 测试不会因为"跑到午夜前后"而飘。
START = datetime.now(UTC) - timedelta(days=90)

#: (offset 天数, request_id, action, stage)
ENTRIES: list[tuple[int, str, PolicyAction, GuardStage]] = [
    (0, "req-old", PolicyAction.BLOCK, GuardStage.INPUT),
    (88, "req-a", PolicyAction.ALLOW, GuardStage.INPUT),
    (89, "req-a", PolicyAction.BLOCK, GuardStage.OUTPUT),
]


def make_decision(
    *,
    request_id: str,
    action: PolicyAction,
    stage: GuardStage,
) -> Decision:
    result = DetectorResult(
        detector="rules.l1",
        version="1.0.0",
        stage=stage,
        label="prompt_injection",
        score=0.95,
        confidence=0.9,
        evidence=("忽略以上所有指令",),
        metadata={"rule": "prompt_injection"},
    )
    return Decision(
        action=action,
        reason="命中注入",
        rule_name="block_injection",
        matched=True,
        request_id=request_id,
        tenant="default",
        use_case="bank-assistant-demo",
        stage=stage,
        policy_version="1.0.0",
        results=(result,),
        attempted_detectors=["rules.l1"],
    )


def write_ledger(root: Path, entries: list[tuple[int, str, PolicyAction, GuardStage]]) -> Path:
    """用**真实的 C8 写入路径**造一个账本文件。

    刻意不走"手写 JSON 行"的方式：那样测的是"CLI 能解析我自己编的格式"，
    而不是"CLI 能读 C8 真正写出来的账本"。schema 由 C8 保证，测试才立得住。
    """
    path = root / "audit" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    clock = FrozenClock(START)
    ledger = AuditLedger(
        JsonlAuditSink(path),
        clock=SystemClockAdapter(clock),
        redactor=RegexRedactor(),
        retention_days=365,
    )
    prev = 0
    for offset, request_id, action, stage in entries:
        delta = offset - prev
        if delta:
            clock.advance_days(delta)
        prev = offset
        ledger.record(
            make_decision(request_id=request_id, action=action, stage=stage),
        )
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """账本根目录（生产上就是 ``shared/data``）。"""
    base = tmp_path / "shared" / "data"
    base.mkdir(parents=True)
    return base


@pytest.fixture
def ledger_path(root: Path) -> Path:
    return write_ledger(root, ENTRIES)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_reports_totals_and_distributions(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["summary", "--root", str(root)]) == 0
        out = capsys.readouterr().out
        assert "总记录:   3" in out
        assert "block" in out
        assert "allow" in out
        assert "input" in out
        assert "output" in out

    def test_json_output_is_machine_readable(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["summary", "--root", str(root), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 3
        assert payload["by_action"] == {"block": 2, "allow": 1}
        assert payload["by_stage"] == {"input": 2, "output": 1}
        assert payload["ledger"] == str(ledger_path)

    def test_time_range_covers_first_and_last(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["summary", "--root", str(root), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["first_recorded_at"] == START.isoformat()
        assert payload["last_recorded_at"] > payload["first_recorded_at"]

    def test_missing_ledger_is_an_error_not_an_empty_summary(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["summary", "--root", str(root)]) == 1
        err = capsys.readouterr().err
        assert "账本不存在" in err

    def test_reading_never_creates_the_ledger(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """查一个不存在的账本，不能顺手造出一个空账本。

        否则"账本还没接上"和"账本是空的"就变成了同一件事 ——
        运维分不清到底是没接审计，还是审计没记录到东西。
        """
        assert main(["summary", "--root", str(root)]) == 1
        assert not (root / "audit" / "ledger.jsonl").exists()

    def test_empty_ledger_is_reported_as_empty(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空账本要说"空"，不能只显示一个 0 让分不清是没接上还是真没有。"""
        empty = root / "audit" / "ledger.jsonl"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_text("", encoding="utf-8")
        assert main(["summary", "--root", str(root)]) == 0
        out = capsys.readouterr().out
        assert "总记录:   0" in out
        assert "（空账本）" in out
        assert "（无）" in out


# ---------------------------------------------------------------------------
# 输出细节
# ---------------------------------------------------------------------------


class TestOutput:
    def test_metadata_is_shown_but_redaction_offsets_are_not(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """metadata 里的 start/end 是**偏移量**不是内容，展示出来没有意义，
        反而容易让人误以为账本里存了原文片段。"""
        path = root / "audit" / "ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        clock = FrozenClock(START)
        ledger = AuditLedger(
            JsonlAuditSink(path),
            clock=SystemClockAdapter(clock),
            redactor=RegexRedactor(),
            retention_days=365,
        )
        ledger.record(
            make_decision(
                request_id="req-meta",
                action=PolicyAction.REDACT,
                stage=GuardStage.OUTPUT,
            ),
            metadata={"entity": "CN_ID", "start": 3, "end": 19},
        )

        assert main(["query", "--root", str(root), "--request-id", "req-meta"]) == 0
        out = capsys.readouterr().out
        assert "entity" in out
        assert "start" not in out
        assert "end" not in out

    def test_noop_redactor_is_a_pass_through(self) -> None:
        """CLI 的占位脱敏器必须原样返回文本。

        它只在构造 AuditLedger 时被要求存在，从不参与真实脱敏 ——
        落盘记录在写入时就已经脱敏过了。万一它悄悄改写文本，
        prune 搬运记录时就会把已脱敏的证据改坏，所以这条要钉死。
        """
        from audit_ledger.__main__ import _NoopRedactor

        redactor = _NoopRedactor()
        assert redactor.name == "noop"
        result = redactor.redact("身份证 11010519491231002X")
        assert result.text == "身份证 11010519491231002X"
        assert not result.changed


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_filters_by_request_id(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--request-id", "req-a"]) == 0
        out = capsys.readouterr().out
        assert "req-a" in out
        assert "req-old" not in out
        assert "命中 2 条" in out

    def test_filters_by_action(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--action", "block"]) == 0
        assert "命中 2 条" in capsys.readouterr().out

    def test_filters_by_stage(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--stage", "output"]) == 0
        out = capsys.readouterr().out
        assert "命中 1 条" in out
        assert "req-a" in out

    def test_filters_by_since(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        since = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
        assert main(["query", "--root", str(root), "--since", since]) == 0
        out = capsys.readouterr().out
        assert "命中 2 条" in out
        assert "req-old" not in out

    def test_no_match_is_not_an_error(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--request-id", "req-nope"]) == 0
        assert "无匹配记录" in capsys.readouterr().out

    def test_invalid_action_is_reported(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """非法值必须报错，不能静默返回空结果。"""
        assert main(["query", "--root", str(root), "--action", "BLOCK_DROP"]) == 1
        err = capsys.readouterr().err
        assert "非法的 action" in err
        assert "block" in err  # 列出可选值

    def test_invalid_since_is_reported(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--since", "前天"]) == 1
        assert "参数错误" in capsys.readouterr().err

    def test_invalid_stage_is_reported(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--stage", "middle"]) == 1
        err = capsys.readouterr().err
        assert "非法的 stage" in err
        assert "input" in err  # 列出可选值

    def test_missing_ledger_is_an_error(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root)]) == 1
        assert "账本不存在" in capsys.readouterr().err

    def test_limit_truncates_display_but_not_the_match_count(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--limit", "1", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["matched"] == 3
        assert payload["shown"] == 1
        assert len(payload["records"]) == 1

    def test_json_output_carries_full_record(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["query", "--root", str(root), "--request-id", "req-a", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["matched"] == 2
        first = payload["records"][0]
        assert first["request_id"] == "req-a"
        assert first["action"] in {"allow", "block"}
        assert first["stage"] in {"input", "output"}
        assert first["policy_version"] == "1.0.0"


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


class TestPrune:
    def test_removes_only_expired_records(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["prune", "--root", str(root), "--days", "30"]) == 0
        out = capsys.readouterr().out
        assert "删除 1 条，保留 2 条" in out

        remaining = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [r["request_id"] for r in remaining] == ["req-a", "req-a"]

    def test_ledger_stays_readable_after_prune(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """原子重写：prune 之后账本必须仍是完整可解析的 JSONL。"""
        assert main(["prune", "--root", str(root), "--days", "30"]) == 0
        capsys.readouterr()
        assert main(["summary", "--root", str(root), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 2

    def test_no_temp_file_left_behind(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["prune", "--root", str(root), "--days", "30"]) == 0
        capsys.readouterr()
        leftovers = [p.name for p in (root / "audit").iterdir() if p.name != "ledger.jsonl"]
        assert leftovers == []

    def test_json_output_reports_counts(
        self, root: Path, ledger_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["prune", "--root", str(root), "--days", "30", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total_before"] == 3
        assert payload["removed"] == 1
        assert payload["kept"] == 2

    def test_missing_ledger_is_an_error(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["prune", "--root", str(root)]) == 1
        assert "账本不存在" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_explicit_root_wins_over_env(
        self,
        root: Path,
        ledger_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_AUDIT__ROOT", str(root / "nonexistent"))
        assert main(["summary", "--root", str(root)]) == 0
        assert "总记录:   3" in capsys.readouterr().out

    def test_env_root_is_used_when_no_flag(
        self,
        root: Path,
        ledger_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """手工执行时 systemd 注入的环境变量不生效，但显式 export 出来的要认。"""
        monkeypatch.setenv("MINIMAX_AGENT_AUDIT__ROOT", str(root))
        assert main(["summary"]) == 0
        assert "总记录:   3" in capsys.readouterr().out

    def test_cwd_is_the_last_resort(
        self,
        root: Path,
        ledger_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("MINIMAX_AGENT_AUDIT__ROOT", raising=False)
        monkeypatch.chdir(root)
        assert main(["summary"]) == 0
        assert "总记录:   3" in capsys.readouterr().out

    def test_explicit_path_is_relative_to_root(
        self,
        root: Path,
        ledger_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("MINIMAX_AGENT_AUDIT__ROOT", raising=False)
        assert main(["summary", "--root", str(root), "--path", "audit/ledger.jsonl"]) == 0
        assert "总记录:   3" in capsys.readouterr().out

    def test_absolute_path_ignores_root(
        self,
        root: Path,
        ledger_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """绝对路径直接用，不再挂到 root 底下 —— 免得拼出一个不存在的路径。"""
        monkeypatch.delenv("MINIMAX_AGENT_AUDIT__ROOT", raising=False)
        assert main(["summary", "--root", str(root), "--path", str(ledger_path)]) == 0
        assert "总记录:   3" in capsys.readouterr().out

    def test_env_path_is_used_when_no_flag(
        self,
        root: Path,
        ledger_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("MINIMAX_AGENT_AUDIT__ROOT", str(root))
        monkeypatch.setenv("MINIMAX_AGENT_AUDIT__PATH", "audit/ledger.jsonl")
        assert main(["summary"]) == 0
        assert "总记录:   3" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_module_is_runnable(
        self, root: Path, ledger_path: Path, tmp_path: Path
    ) -> None:
        """``python -m audit_ledger`` 必须真的能跑 —— 文档里就是这么写的。

        单独用子进程验证，是因为"函数能调通"不等于"模块入口能跑通"：
        ``__main__.py`` 的 import 路径、argparse 的 prog 名、退出码，
        都只有真正起一个进程才会暴露。
        """
        src_root = Path(audit_ledger.__file__).resolve().parents[1]
        # PYTHONIOENCODING=utf-8 + 显式 encoding="utf-8"：子进程输出的是中文，
        # 而 Windows 默认会用本地代码页（GBK）解码管道输出，直接解会抛
        # UnicodeDecodeError。显式约定成 UTF-8 让这个用例在任何机器上都一样。
        env = {**os.environ, "PYTHONPATH": str(src_root), "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(  # noqa: S603 - 参数是固定字面量，无注入面
            [sys.executable, "-m", "audit_ledger", "summary", "--root", str(root)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "总记录:   3" in result.stdout

# ---------------------------------------------------------------------------
# P1-1：CLI 必须能区分「判不了」与「判有害」
# ---------------------------------------------------------------------------


def make_failed_decision(*, request_id: str, matched: bool = False) -> Decision:
    """构造一条"检测器没能给出结论"的决策（fail-closed 路径）。"""
    result = DetectorResult(
        detector="rules.l1",
        version="1.0.0",
        stage=GuardStage.INPUT,
        label="safe",
        score=0.0,
        confidence=0.9,
        evidence=(),
        metadata={},
    )
    return Decision(
        action=PolicyAction.BLOCK,
        reason="检测器不可用且未能完成检查，按 fail-closed 阻断",
        rule_name=None,
        matched=matched,
        request_id=request_id,
        tenant="default",
        use_case="bank-assistant-demo",
        stage=GuardStage.INPUT,
        policy_version="1.0.0",
        results=(result,),
        attempted_detectors=["rules.l1", "llm.judge"],
        fail_mode=FailMode.CLOSED,
        failed_detectors=(
            DetectorFailure(
                detector="llm.judge",
                reason=DetectorFailureReason.ERROR,
                detail="judge 输出不是合法 JSON",
            ),
        ),
    )


def write_ledger_with_failures(root: Path) -> Path:
    """一份覆盖**全部四种判定语义**的账本。

    | 记录 | fail_mode | matched | 语义 |
    |---|---|---|---|
    | req-cannot-tell | closed | False | 判不了 |
    | req-normal | 无 | False | 正常放行 |
    | req-hit | 无 | True | 命中 |
    | req-hit-with-failure | closed | True | 命中（有检测器失败） |
    """
    path = root / "audit" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    clock = FrozenClock(START)
    ledger = AuditLedger(
        JsonlAuditSink(path),
        clock=SystemClockAdapter(clock),
        redactor=RegexRedactor(),
        retention_days=365,
    )
    clean = DetectorResult(
        detector="rules.l1",
        version="1.0.0",
        stage=GuardStage.INPUT,
        label="safe",
        score=0.0,
        confidence=0.9,
        evidence=(),
        metadata={},
    )

    def decision(
        request_id: str,
        *,
        action: PolicyAction,
        matched: bool,
        fail_mode: FailMode | None = None,
        failed: tuple[DetectorFailure, ...] = (),
    ) -> Decision:
        return Decision(
            action=action,
            reason="检测器不可用且未能完成检查，按 fail-closed 阻断"
            if fail_mode
            else "未命中任何策略规则，走 default_action",
            rule_name=None,
            matched=matched,
            request_id=request_id,
            tenant="default",
            use_case="bank-assistant-demo",
            stage=GuardStage.INPUT,
            policy_version="1.0.0",
            results=(clean,),
            attempted_detectors=["rules.l1", "llm.judge"],
            fail_mode=fail_mode,
            failed_detectors=failed,
        )

    failure = (
        DetectorFailure(
            detector="llm.judge",
            reason=DetectorFailureReason.ERROR,
            detail="judge 输出不是合法 JSON",
        ),
    )
    ledger.record(
        decision(
            "req-cannot-tell",
            action=PolicyAction.BLOCK,
            matched=False,
            fail_mode=FailMode.CLOSED,
            failed=failure,
        )
    )
    clock.advance_days(1)
    ledger.record(
        decision("req-normal", action=PolicyAction.ALLOW, matched=False)
    )
    clock.advance_days(1)
    ledger.record(
        decision("req-hit", action=PolicyAction.BLOCK, matched=True)
    )
    clock.advance_days(1)
    ledger.record(
        decision(
            "req-hit-with-failure",
            action=PolicyAction.BLOCK,
            matched=True,
            fail_mode=FailMode.CLOSED,
            failed=failure,
        )
    )
    return path


class TestVerdictProvenance:
    def test_query_shows_that_it_cannot_tell(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_ledger_with_failures(root)
        assert main(["query", "--root", str(root), "--request-id", "req-cannot-tell"]) == 0
        out = capsys.readouterr().out
        assert "判不了" in out
        assert "fail_mode=closed" in out
        assert "llm.judge(error)" in out

    def test_failed_only_filters_out_normal_records(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_ledger_with_failures(root)
        assert main(["query", "--root", str(root), "--failed-only"]) == 0
        out = capsys.readouterr().out
        assert "req-cannot-tell" in out
        assert "req-normal" not in out

    def test_summary_counts_undecidable_records(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """summary 要能回答「有多少次是系统判定自己判不了」。"""
        write_ledger_with_failures(root)
        assert main(["summary", "--root", str(root), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["by_kind"]["判不了"] == 1
        assert payload["by_failed_detector"] == {"llm.judge": 2}

    def test_summary_json_reports_all_four_kinds(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """四种语义各归其类 —— 读取时现算，不靠落盘的派生标记。"""
        write_ledger_with_failures(root)
        assert main(["summary", "--root", str(root), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 4
        assert payload["by_kind"] == {
            "命中": 1,
            "正常放行": 1,
            "判不了": 1,
            "命中（有检测器失败）": 1,
        }

    def test_legacy_records_report_no_failure(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """**存量记录读出来是"没有失败"，不是"判不了"。**

        旧记录里根本没有这两个字段 —— 反推就是编造，所以默认必须是"无失败"。
        """
        path = root / "audit" / "ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": "req-legacy",
                    "recorded_at": "2026-10-09T01:12:59.744806+00:00",
                    "tenant": "default",
                    "use_case": "bank-assistant-demo",
                    "stage": "input",
                    "action": "allow",
                    "rule_name": None,
                    "matched": False,
                    "policy_version": "1.0.0",
                    "detectors": ["rules.l1"],
                    "hit_labels": [],
                    "evidence": [],
                    "metadata": {},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        assert main(["summary", "--root", str(root), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["by_kind"] == {"正常放行": 1}
        assert "by_failed_detector" not in payload or not payload["by_failed_detector"]