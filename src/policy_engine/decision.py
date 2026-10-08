"""决策对象。

一次策略求值的完整产物。设计目标是：**只看这一条记录就能回答
"当时为什么这么判"** —— 命中了哪条规则、用了哪个 policy 版本、
哪些检测器失败了、被忽略的是谁、最终动作是什么。

这直接对应架构方案 §1.2 审计行要求的 who / when / detector_version /
verdict / evidence / action，以及 Q6 里"detector X timeout, fallback to
fail-closed 必须显式标记"的那条。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from guard_contract.enums import FailMode, GuardStage, PolicyAction
from guard_contract.port import DetectorFailure
from guard_contract.result import DetectorResult, decision_record
from policy_engine.redactor import Redactor


class Decision(BaseModel):
    """一次 guard 求值的结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 最终动作
    action: PolicyAction
    #: 人读理由，写进审计与日志
    reason: str
    #: 在哪个检测点求值
    stage: GuardStage
    #: 关联的请求标识（由调用方生成）
    request_id: str
    #: 参与求值的检测结果（降级模式下可能只含被信任的检测器）
    results: tuple[DetectorResult, ...] = ()
    #: 命中的规则名；``matched=False`` 时为 None
    rule_name: str | None = None
    #: 是否由某条规则命中。False 表示走了兜底（default_action）。
    matched: bool = False
    #: 本次使用的 policy 版本
    policy_version: str = ""
    use_case: str = "default"
    tenant: str = "default"
    #: 仅在有检测器失败时出现
    fail_mode: FailMode | None = None
    failed_detectors: tuple[DetectorFailure, ...] = ()
    #: 流水线尝试过的全部检测器名（含失败的）
    attempted_detectors: tuple[str, ...] = ()
    #: 实际参与求值的检测器名
    considered_detectors: tuple[str, ...] = ()
    #: 因降级被排除的检测器名
    ignored_detectors: tuple[str, ...] = ()

    @field_validator("request_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("request_id 不能为空")
        return value.strip()

    @property
    def requires_human(self) -> bool:
        """是否需要人工介入（挂起等待复核）。"""
        return self.action in (PolicyAction.ESCALATE, PolicyAction.REQUIRE_APPROVAL)

    @property
    def needs_regeneration(self) -> bool:
        """是否需要重新生成（REWRITE 会再走一轮 LLM 调用）。"""
        return self.action is PolicyAction.REWRITE

    @property
    def degraded(self) -> bool:
        """本次决策是否走了降级路径。"""
        return self.fail_mode is not None

    def to_audit_record(self, *, redactor: Redactor | None = None) -> dict[str, Any]:
        """转成可 JSON 序列化的审计记录。

        复用 :func:`~guard_contract.result.decision_record` 作为唯一构造入口，
        避免出现两套形状不同的审计格式。

        Args:
            redactor: 脱敏器。传入后 evidence 里的命中原文会被脱敏 ——
                审计记录会落盘，而 evidence 装的是原文片段，明文 PII 不该进账本。
                不传则原样输出（本地排查等明确需要原文的场景）。
        """
        record = decision_record(
            request_id=self.request_id,
            use_case=self.use_case,
            tenant=self.tenant,
            results=self.results,
            action=self.action,
            reason=self.reason,
            fail_mode=self.fail_mode.value if self.fail_mode is not None else None,
            # 契约层只认 Callable[[str], str]，不依赖这里的 Redactor 协议，
            # 避免 guard_contract 反向依赖 policy_engine。
            redactor=(lambda text: redactor.redact(text).text) if redactor else None,
        )
        record.update(
            {
                "stage": self.stage.value,
                "rule_name": self.rule_name,
                "matched": self.matched,
                "policy_version": self.policy_version,
                "failed_detectors": [f.to_audit_record() for f in self.failed_detectors],
                "attempted_detectors": list(self.attempted_detectors),
                "considered_detectors": list(self.considered_detectors),
                "ignored_detectors": list(self.ignored_detectors),
            }
        )
        return record


__all__ = ["Decision"]
