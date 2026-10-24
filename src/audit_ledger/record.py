"""审计记录的数据结构。

## 字段对应架构方案 §1.2 的审计行

| 要求 | 字段 |
|---|---|
| who | ``tenant`` / ``use_case`` |
| when | ``recorded_at`` |
| detector_version | 每条 ``DetectorResult.version`` |
| verdict | ``action`` |
| evidence | ``DetectorResult.evidence``（**落盘前已脱敏**） |
| action | ``action`` + ``rule_name`` |
| 能否归因 | ``fail_mode`` / ``failed_detectors`` / ``reason``（"判不了"与"判有害"要能分开） |

## 为什么 evidence 在**构造时**就要求已脱敏

FT-12 要求审计记录"只追加不修改"，落盘后不能再回头改。这意味着**写入前**
必须是对的 —— 一旦写进 JSONL，明文 PII 就再也删不干净了（至少不能保证）。

所以 :meth:`AuditRecord.from_decision` 强制要求传入 ``redactor``，没有它
就**拒绝构造**，而不是"先写进去回头再说"。这与 ``to_audit_record`` 的
"默认不脱敏"是刻意的分工：那里服务于临时排查（不落盘），这里服务于落盘。

``metadata`` 同样经过脱敏：它是调用方自由携带的字典，未来某个调用点
很可能顺手把用户输入塞进来 —— 靠"调用方自觉"保护账本，与 append-only
的哲学相悖。字符串值统一过一遍 redactor；非字符串原样保留。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from guard_contract.enums import GuardStage, PolicyAction
from policy_engine.decision import Decision
from policy_engine.redactor import Redactor


def _redact_metadata(metadata: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """对 metadata 的字符串值做 PII 脱敏，其余类型原样保留。"""
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, str):
            result = redactor.redact(value)
            out[key] = result.text if result.changed else value
        else:
            out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一条不可变的审计记录。

    ``frozen`` + ``slots``：构造后不可改。这是 append-only 在**对象层**的
    保证 —— 即便调用方想改，也改不动。
    """

    request_id: str
    recorded_at: str
    tenant: str
    use_case: str
    stage: GuardStage
    action: PolicyAction
    rule_name: str | None
    matched: bool
    policy_version: str
    detectors: tuple[str, ...]
    hit_labels: tuple[str, ...]
    evidence: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    #: 本次是否走了「检测器没给出结论」的路径。``None`` = 全部检测器都正常返回。
    #:
    #: 这个字段回答的是 P1-1 那个问题 —— **「判有害」与「判不了」在账本里
    #: 曾经完全同形**（都是 ``rule_name=null`` / ``matched=false`` /
    #: ``evidence=[]``）。调用方在事发当场能从 API 响应体里看到
    #: ``fail_mode`` 与 ``failed_detectors``，而账本里什么都没有 ——
    #: 账本不该比 API 响应更贫瘠。
    fail_mode: str | None = None

    #: 失败的检测器，**只记 ``{detector, reason}``，刻意不记 detail**。
    #:
    #: ``detail`` 是异常消息，可能复述用户输入 —— judge 的解析失败消息就带着
    #: 模型输出片段（``llm_judge.py``）。账本是 append-only，写进去就删不掉，
    #: 所以这里只留"谁挂了、为什么挂"这个结构化事实。
    #: 想知道具体挂在哪一步，看服务日志里那条归因 warning（带 ``mode``）。
    failed_detectors: tuple[dict[str, str], ...] = ()

    #: 人读理由：policy 规则的 ``reason`` 或策略引擎的常量说明。
    #:
    #: 刻意**不脱敏**：它来自 policy YAML 或引擎常量，是配置文本而非用户内容
    #: （fail-closed 路径上就是那句"检测器不可用且未能完成检查，按 fail-closed
    #: 阻断"）。脱敏反而可能把合法的规则说明改坏。
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # StrEnum 与 tuple 都要能直接 json.dumps
        data["stage"] = self.stage.value
        data["action"] = self.action.value
        data["detectors"] = list(self.detectors)
        data["hit_labels"] = list(self.hit_labels)
        data["evidence"] = list(self.evidence)
        return data

    @classmethod
    def from_decision(
        cls,
        decision: Decision,
        *,
        recorded_at: datetime,
        redactor: Redactor,
        metadata: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """从策略判定构造审计记录。

        Args:
            redactor: **必填**。落盘的记录不允许带明文 PII，
                因此没有脱敏器就不构造 —— 见模块说明。
            recorded_at: 由调用方传入（通常是注入的 Clock），
                不在这里取当前时间 —— 测试要能控制时间。
        """
        if redactor is None:  # pragma: no cover - 类型已保证，防御性
            raise ValueError("落盘的审计记录必须提供脱敏器")

        evidence: list[str] = []
        for result in decision.results:
            for item in result.evidence:
                evidence.append(redactor.redact(item).text)

        return cls(
            request_id=decision.request_id,
            recorded_at=recorded_at.isoformat(),
            tenant=decision.tenant,
            use_case=decision.use_case,
            stage=decision.stage,
            action=decision.action,
            rule_name=decision.rule_name,
            matched=decision.matched,
            policy_version=decision.policy_version,
            detectors=tuple(decision.attempted_detectors),
            hit_labels=tuple(sorted({r.label for r in decision.results})),
            evidence=tuple(evidence),
            metadata=_redact_metadata(dict(metadata or {}), redactor),
            # 失败路径的两个事实。字段名与 API 响应的 failed_detectors 一致 ——
            # 账本与响应体说的是同一件事，只是账本说的是**事后**。
            fail_mode=decision.fail_mode.value if decision.fail_mode is not None else None,
            failed_detectors=tuple(
                {"detector": failure.detector, "reason": failure.reason.value}
                for failure in decision.failed_detectors
            ),
            reason=decision.reason,
        )


__all__ = ["AuditRecord"]
