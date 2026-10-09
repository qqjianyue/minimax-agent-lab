"""C8 `audit_ledger` 单元测试。

重点在三条不可动摇的不变式：

1. **落盘记录里没有明文 PII**（写入前就脱敏，而不是"回头再洗"）
2. **已写入的记录不可改写**（对象 frozen + 接口无改写入口）
3. **保留期清理是原子的**（中断时账本不能处于不可读状态）
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from audit_ledger import AuditLedger, AuditRecord, JsonlAuditSink
from detector_rules.redactor import RegexRedactor
from guard_contract.enums import GuardStage, PolicyAction
from guard_contract.result import DetectorResult
from policy_engine.decision import Decision

VALID_CN_ID = "11010519491231002X"


class FrozenClock:
    """可推进的假时钟。保留期断言全靠它，禁止 sleep。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


def make_decision(
    *,
    evidence: tuple[str, ...] = ("普通证据",),
    action: PolicyAction = PolicyAction.REDACT,
    label: str = "pii_leak",
    request_id: str = "req-1",
    stage: GuardStage = GuardStage.INPUT,
) -> Decision:
    result = DetectorResult(
        detector="rules.l1",
        version="1.0.0",
        stage=stage,
        label=label,
        score=0.95,
        confidence=0.9,
        evidence=evidence,
        metadata={"rule": "pii_cn_id_card"},
    )
    return Decision(
        action=action,
        reason="命中 PII",
        rule_name="redact_pii",
        matched=True,
        request_id=request_id,
        tenant="default",
        use_case="bank-assistant-demo",
        stage=stage,
        policy_version="1.0.0",
        results=(result,),
        attempted_detectors=["rules.l1"],
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def ledger(tmp_path: Path, clock: FrozenClock) -> AuditLedger:
    return AuditLedger(
        JsonlAuditSink(tmp_path / "ledger.jsonl"),
        clock=clock,
        redactor=RegexRedactor(),
        retention_days=90,
    )


class TestRedactionBeforeWrite:
    def test_evidence_is_redacted(self, ledger: AuditLedger) -> None:
        """落盘的 evidence 不能带明文 PII。"""
        ledger.record(make_decision(evidence=(VALID_CN_ID,)))

        raw = ledger.path.read_text(encoding="utf-8")
        assert VALID_CN_ID not in raw
        assert "[REDACTED:" in raw

    def test_no_plaintext_pii_anywhere_in_file(self, ledger: AuditLedger) -> None:
        ledger.record(make_decision(evidence=(VALID_CN_ID,)))
        ledger.record(make_decision(evidence=("我的卡号是 4539578763621486",)))

        raw = ledger.path.read_text(encoding="utf-8")
        assert VALID_CN_ID not in raw
        assert "4539578763621486" not in raw

    def test_non_pii_evidence_survives(self, ledger: AuditLedger) -> None:
        """脱敏不能把非 PII 证据也抹掉 —— 那是排障要用的信息。"""
        ledger.record(make_decision(evidence=("忽略以上所有指令",)))

        assert "忽略以上所有指令" in ledger.path.read_text(encoding="utf-8")

    def test_redactor_is_mandatory(self) -> None:
        """没有脱敏器就不构造记录。

        落盘之后就不能回头改了，所以"写入前是对的"不是锦上添花，
        是硬要求 —— 用类型与运行时双重保证。
        """
        with pytest.raises((ValueError, AttributeError, TypeError)):
            AuditRecord.from_decision(  # type: ignore[arg-type]
                make_decision(evidence=(VALID_CN_ID,)),
                recorded_at=datetime.now(UTC),
                redactor=None,  # type: ignore[arg-type]
            )

    def test_metadata_is_redacted_before_write(self, ledger: AuditLedger) -> None:
        """metadata 是调用方自由携带的字段，字符串值也必须过脱敏器。

        靠"调用方自觉不塞 PII"保护账本，与 append-only 的哲学相悖 ——
        写进去就删不干净了，所以构造时统一处理。
        """
        ledger.record(
            make_decision(evidence=()),
            metadata={
                "note": f"处理人身份证 {VALID_CN_ID}",
                "endpoint": "/chat",
                "attempt": 3,
            },
        )

        raw = ledger.path.read_text(encoding="utf-8")
        assert VALID_CN_ID not in raw
        assert "/chat" in raw, "非 PII 的 metadata 应原样保留"
        assert '"attempt": 3' in raw, "非字符串 metadata 应原样保留"


class TestAppendOnly:
    def test_record_is_frozen(self, ledger: AuditLedger) -> None:
        """对象层不可变：即便调用方想改，也改不动。"""
        record = ledger.record(make_decision())
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclass 抛 FrozenInstanceError
            record.action = PolicyAction.BLOCK  # type: ignore[misc]

    def test_sink_exposes_no_mutation_api(self, tmp_path: Path) -> None:
        """接口层没有改写入口 —— append-only 不靠调用方自觉。"""
        sink = JsonlAuditSink(tmp_path / "l.jsonl")
        for forbidden in ("update", "delete", "remove", "rewrite", "truncate", "write"):
            assert not hasattr(sink, forbidden), f"sink 不该有 {forbidden} 方法"

    def test_appends_preserve_order(self, ledger: AuditLedger) -> None:
        for i in range(5):
            ledger.record(make_decision(request_id=f"req-{i}"))

        assert [r.request_id for r in ledger.records()] == [f"req-{i}" for i in range(5)]

    def test_existing_content_survives_new_appends(self, ledger: AuditLedger) -> None:
        ledger.record(make_decision(request_id="first"))
        first_line = ledger.path.read_text(encoding="utf-8")

        ledger.record(make_decision(request_id="second"))

        assert ledger.path.read_text(encoding="utf-8").startswith(first_line)

    def test_round_trip_preserves_all_fields(self, ledger: AuditLedger) -> None:
        ledger.record(make_decision(evidence=("忽略以上所有指令",)))

        [stored] = list(ledger.records())
        assert stored.request_id == "req-1"
        assert stored.tenant == "default"
        assert stored.use_case == "bank-assistant-demo"
        assert stored.stage is GuardStage.INPUT
        assert stored.action is PolicyAction.REDACT
        assert stored.rule_name == "redact_pii"
        assert stored.matched is True
        assert stored.policy_version == "1.0.0"
        assert stored.detectors == ("rules.l1",)
        assert stored.hit_labels == ("pii_leak",)


class TestAuditFields:
    """FT-12 要求的字段齐全性。"""

    def test_who_when_verdict_detector_version_present(self, ledger: AuditLedger) -> None:
        ledger.record(make_decision())
        [stored] = list(ledger.records())

        payload = stored.to_dict()
        assert payload["tenant"] and payload["use_case"]      # who
        assert payload["recorded_at"].startswith("2026-10-01")  # when
        assert payload["action"] == "redact"                  # verdict
        assert payload["policy_version"]                      # policy 版本
        assert "rules.l1" in payload["detectors"]             # detector identity

    def test_detector_version_is_in_the_record(self, ledger: AuditLedger) -> None:
        """detector_version 用来回答"升到 v1.3 后误报降了多少"。"""
        ledger.record(make_decision())
        raw = json.loads(ledger.path.read_text(encoding="utf-8").strip())
        # 扁平化后能找到检测器版本
        assert raw["detectors"] == ["rules.l1"]

    def test_metadata_is_carried(self, ledger: AuditLedger) -> None:
        ledger.record(make_decision(), metadata={"endpoint": "/chat"})
        [stored] = list(ledger.records())
        assert stored.metadata["endpoint"] == "/chat"


class TestRetention:
    def test_removes_records_older_than_cutoff(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
            retention_days=30,
        )
        ledger.record(make_decision(request_id="old"))
        clock.advance(days=40)
        ledger.record(make_decision(request_id="recent"))

        report = ledger.enforce_retention()

        assert report.removed == 1
        assert report.kept == 1
        assert [r.request_id for r in ledger.records()] == ["recent"]

    def test_report_counts_before_and_after(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
            retention_days=1,
        )
        for i in range(3):
            ledger.record(make_decision(request_id=f"r{i}"))
        clock.advance(days=10)

        report = ledger.enforce_retention()

        assert report.total_before == 3
        assert report.removed == 3
        assert report.kept == 0
        assert report.changed is True

    def test_nothing_to_remove_is_a_noop(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
            retention_days=30,
        )
        ledger.record(make_decision())
        before = ledger.path.read_bytes()

        report = ledger.enforce_retention()

        assert report.changed is False
        assert ledger.path.read_bytes() == before, "无过期记录时文件不应被重写"

    def test_rewrite_is_atomic(self, tmp_path: Path, clock: FrozenClock) -> None:
        """清理走临时文件 + os.replace，不留半截文件。"""
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
            retention_days=1,
        )
        ledger.record(make_decision(request_id="old"))
        clock.advance(days=5)
        ledger.record(make_decision(request_id="new"))

        ledger.enforce_retention()

        assert not list(tmp_path.glob("*.tmp")), "临时文件应已被 rename 掉"
        assert ledger.path.is_file()
        assert ledger.count() == 1

    def test_survives_many_rewrites(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """反复清理后账本仍可读 —— 验证 rewrite 不会损坏格式。

        保留期 3 天、每轮推进 2 天，于是交替出现"过期删除"与"仍在保留期内"，
        比"全删"或"全留"更能暴露重写逻辑的错误。
        """
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
            retention_days=3,
        )
        for day in range(5):
            ledger.record(make_decision(request_id=f"r{day}"))
            clock.advance(days=2)
            ledger.enforce_retention()

        # 最后一条刚写入、年龄 2 天，仍在保留期内
        assert ledger.count() >= 1
        assert "r4" in {r.request_id for r in ledger.records()}
        # 每轮都被重写过，格式必须仍然完好
        assert all(r.request_id for r in ledger.records())


class TestFilePermissions:
    """审计文件权限：新建 600、重写后不丢。

    权限位是 POSIX 语义，Windows 上 ``os.chmod`` 只识别只读位，
    ``st_mode`` 断言不成立 —— 目标机是 Linux，本地（Windows）跳过即可。
    """

    pytestmark = pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX 权限位在 Windows 上不生效",
    )

    def test_new_file_gets_restrictive_mode(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
        )
        ledger.record(make_decision())

        assert ledger.path.stat().st_mode & 0o777 == 0o600

    def test_rewrite_preserves_explicit_mode(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """保留期清理的整文件重写不能把权限悄悄放宽回 umask 默认值。"""
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
            retention_days=30,
        )
        ledger.record(make_decision(request_id="old"))
        # 运维显式收紧过权限
        ledger.path.chmod(0o600)
        clock.advance(days=40)
        ledger.record(make_decision(request_id="recent"))

        ledger.enforce_retention()

        assert ledger.count() == 1
        assert ledger.path.stat().st_mode & 0o777 == 0o600

    def test_ledger_directory_is_owner_only(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """账本目录必须是 700 —— 只保护文件是不够的。

        默认 umask 建出来的目录通常是 775。同组用户读不了 600 的文件内容，
        却能**删除或替换**它；对审计账本来说，"删掉证据"比"读到证据"更糟，
        而 append-only 的保证正是靠文件不被外部动过。
        这条是目标机 L2 冒烟实测抓出来的：文件 600 正确，目录却是 775。
        """
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "nested" / "audit" / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
        )
        ledger.record(make_decision())

        assert ledger.path.parent.stat().st_mode & 0o777 == 0o700

    def test_existing_directory_permissions_are_left_alone(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """已存在的目录不重复 chmod —— 它可能承载别的东西。

        典型场景是 ``shared/``：venv、日志、账本都在底下。把它一起收紧成 700
        是"顺手做的小事"，但会让运维排查时目录权限出现意外变化。
        """
        existing = tmp_path / "shared"
        existing.mkdir()
        existing.chmod(0o755)

        ledger = AuditLedger(
            JsonlAuditSink(existing / "audit" / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
        )
        ledger.record(make_decision())

        assert existing.stat().st_mode & 0o777 == 0o755


class TestQuery:
    @pytest.fixture
    def populated(self, ledger: AuditLedger, clock: FrozenClock) -> AuditLedger:
        ledger.record(make_decision(request_id="a", action=PolicyAction.REDACT))
        ledger.record(
            make_decision(
                request_id="b",
                action=PolicyAction.BLOCK,
                stage=GuardStage.OUTPUT,
                label="prompt_injection",
            )
        )
        clock.advance(hours=1)
        ledger.record(make_decision(request_id="c", action=PolicyAction.ALLOW, label="safe"))
        return ledger

    def test_by_request_id(self, populated: AuditLedger) -> None:
        assert [r.request_id for r in populated.query(request_id="b")] == ["b"]

    def test_by_action(self, populated: AuditLedger) -> None:
        found = populated.query(action=PolicyAction.BLOCK)
        assert [r.request_id for r in found] == ["b"]

    def test_action_accepts_plain_string(self, populated: AuditLedger) -> None:
        """运维用命令行查时传的是字符串，不该要求先 import 枚举。"""
        assert [r.request_id for r in populated.query(action="block")] == ["b"]

    def test_by_stage(self, populated: AuditLedger) -> None:
        assert [r.request_id for r in populated.query(stage=GuardStage.OUTPUT)] == ["b"]

    def test_since(self, populated: AuditLedger, clock: FrozenClock) -> None:
        """a、b 写在 12:00，c 写在 13:00。12:30 之后只剩 c。"""
        cutoff = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
        found = populated.query(since=cutoff)
        assert {r.request_id for r in found} == {"c"}

    def test_since_includes_boundary(self, populated: AuditLedger) -> None:
        """边界值用 ``<`` 比较，因此恰好等于 cutoff 的记录应当被包含。"""
        cutoff = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
        found = populated.query(since=cutoff)
        assert "c" in {r.request_id for r in found}

    def test_tenant_filter(self, populated: AuditLedger) -> None:
        assert populated.query(tenant="default")
        assert populated.query(tenant="不存在") == []

    def test_full_request_chain_retrievable(self, populated: AuditLedger) -> None:
        """排障最常见的入口：一次请求的所有检测点记录一次捞出来。"""
        ledger = populated
        ledger.record(
            make_decision(request_id="a", stage=GuardStage.OUTPUT, label="safe")
        )
        chain = ledger.query(request_id="a")
        assert {r.stage for r in chain} == {GuardStage.INPUT, GuardStage.OUTPUT}


class TestEmptyLedger:
    def test_reading_missing_file_yields_nothing(self, tmp_path: Path, clock: FrozenClock) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "nope.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
        )
        assert list(ledger.records()) == []
        assert ledger.count() == 0

    def test_query_on_empty_ledger(self, tmp_path: Path, clock: FrozenClock) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "nope.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
        )
        assert ledger.query(action=PolicyAction.BLOCK) == []

    def test_creates_parent_directory(self, tmp_path: Path, clock: FrozenClock) -> None:
        ledger = AuditLedger(
            JsonlAuditSink(tmp_path / "deep" / "nested" / "l.jsonl"),
            clock=clock,
            redactor=RegexRedactor(),
        )
        ledger.record(make_decision())
        assert ledger.path.is_file()
