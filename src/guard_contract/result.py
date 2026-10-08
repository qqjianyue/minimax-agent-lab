"""Detector Contract —— 所有检测器的统一返回结构。

这是整个平台里最需要被钉死的类型：无论底层是 5ms 的正则还是 500ms 的
LLM-as-Judge，消费方拿到的都是同一个 ``DetectorResult``。这样 policy engine
不需要知道任何具体检测器的存在，新增检测器也不用改上层代码 —— 这是
"guardrail platform"而非"每个应用自建检测"的结构性保证。

字段命名上 ``version`` 指的是**检测器自身的版本**，不是应用版本。
审计记录里靠它回答"上周还在用 detector v1.2，这周升到 v1.3 后误报降了多少"。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from guard_contract.enums import GuardStage, PolicyAction

#: 审计记录用的可选脱敏函数：入参原文，返回脱敏后的文本。
#:
#: 刻意用 ``Callable[[str], str]`` 而不是具体的脱敏器类型 —— 契约层不该知道
#: "哪些字符算身份证"，那是规则层（detector_rules）的知识。写成具体类型就会
#: 让 guard_contract 反向依赖 policy_engine / detector_rules，依赖方向倒挂。
EvidenceRedactor = Callable[[str], str]


class DetectorResult(BaseModel):
    """单个检测器对单段输入/输出的判定。"""

    # frozen 保证审计记录一旦生成就不会被下游意外改写（append-only 的前提）
    # extra="forbid" 保证契约演进时新增字段必须显式声明，不会静默吞掉拼写错误
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 检测器标识，如 "rules.l1"、"presidio.l2"、"judge.l4"
    detector: str
    #: 检测器版本（语义化版本），如 "1.2.3"
    version: str
    #: 在哪个检测点产生
    stage: GuardStage
    #: 分类标签，如 "safe" / "prompt_injection" / "pii_leak" / "system_prompt_leak"
    label: str
    #: 风险分数 [0, 1]，越高越危险
    score: float = Field(ge=0.0, le=1.0)
    #: 检测器对自身判断的置信度 [0, 1]
    confidence: float = Field(ge=0.0, le=1.0)
    #: 证据：命中的规则名、匹配到的文本片段等
    evidence: tuple[str, ...] = ()
    #: 检测耗时（毫秒）
    latency_ms: float = Field(default=0.0, ge=0.0)
    #: 扩展字段，放检测器特有信息（如命中的 PII 类型分布）
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("detector", "version", "label")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("该字段不能为空")
        return value.strip()

    @property
    def is_flagged(self) -> bool:
        """是否给出了正向风险信号。

        纯粹按 ``label`` 判定：``safe`` 是约定的"无风险"标签。
        是否足以触发某个动作由 policy 决定，不由检测器决定 ——
        检测器不碰阈值，这是它和 policy engine 的职责边界。
        """
        return self.label.lower() != "safe"

    def to_audit_record(self, *, redactor: EvidenceRedactor | None = None) -> dict[str, Any]:
        """转成可 JSON 序列化的审计记录。

        ``evidence`` 与 ``metadata`` 在这里被规范化：tuple 变 list、
        mapping 变 dict，保证 ``json.dumps`` 不会因类型而失败。

        Args:
            redactor: 可选的文本脱敏函数。**审计记录会落盘**，而 evidence
                里装的是命中的原文片段 —— 用户发来身份证号，evidence 里就会
                出现完整身份证号。审计账本一旦存下明文 PII，它就成了新的
                PII 数据库，而且比原始请求更难追溯与删除。所以凡是可能落盘的
                审计记录都必须传脱敏器。

                刻意**默认不脱敏**：调用方不传就是明确选择原样输出
                （例如本地 CLI 排查）。要安全就该显式传，而不是靠默认值兜底。
        """
        evidence = [redactor(item) if redactor else item for item in self.evidence]
        return {
            "detector": self.detector,
            "version": self.version,
            "stage": self.stage.value,
            "label": self.label,
            "score": self.score,
            "confidence": self.confidence,
            "evidence": evidence,
            "latency_ms": self.latency_ms,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_audit_record(cls, record: Mapping[str, Any]) -> DetectorResult:
        """从审计记录还原。字段与 :meth:`to_audit_record` 对称。"""
        return cls.model_validate(dict(record))

    def summary(self) -> str:
        """单行摘要，用于 CLI 输出和失败日志。"""
        action_hint = ""
        if self.is_flagged:
            action_hint = f" [flagged: {self.label}]"
        return (
            f"{self.detector}@{self.version} {self.stage.value} "
            f"{self.label} score={self.score:.3f} conf={self.confidence:.3f} "
            f"{self.latency_ms:.1f}ms{action_hint}"
        )


def decision_record(
    *,
    request_id: str,
    use_case: str,
    tenant: str,
    results: tuple[DetectorResult, ...],
    action: PolicyAction,
    reason: str,
    fail_mode: str | None = None,
    redactor: EvidenceRedactor | None = None,
) -> dict[str, Any]:
    """组装一条完整的策略决策记录。

    字段对应架构方案 §1.2「审计」行：who（tenant/use_case）、when（由调用方补时间戳）、
    detector_version、verdict（action）、evidence、action。

    ``fail_mode`` 仅在降级路径上出现，用于回答 Q6 那类问题 ——
    "这次是检测器超时后 fail-closed 的，还是正常判定 block 的"。

    ``redactor`` 会透传给每条 :class:`DetectorResult`，对 evidence 做脱敏。
    见 :meth:`DetectorResult.to_audit_record` 的说明：**落盘的审计记录必须脱敏**。
    """
    return {
        "request_id": request_id,
        "use_case": use_case,
        "tenant": tenant,
        "detector_results": [r.to_audit_record(redactor=redactor) for r in results],
        "action": action.value,
        "reason": reason,
        "fail_mode": fail_mode,
    }


__all__ = ["DetectorResult", "EvidenceRedactor", "decision_record"]
