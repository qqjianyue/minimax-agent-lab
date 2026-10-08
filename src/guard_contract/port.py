"""检测器端口与失败契约。

放在 ``guard_contract`` 而不是 ``agent_core``，是因为端口的签名里出现了
:class:`~guard_contract.enums.GuardStage`；而 ``agent_core`` 是本项目的最底层，
不能反向 import 上层的枚举。依赖方向必须是 core ← contract。

**为什么 :meth:`DetectorPort.detect` 返回元组而不是单个结果**：
一次检测完全可能同时命中多个标签（既像 prompt injection 又夹带 PII）。
契约强制一个结果只能有一个 label，那么"只返回最高分那个"就会丢掉其余证据，
审计记录会给出"只有注入攻击"的错误印象。返回元组让每个发现各自带证据，
由 policy engine 综合判断 —— 这也正是"多信号决策"的前提。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.errors import TransientError
from guard_contract.enums import GuardStage
from guard_contract.result import DetectorResult


class DetectorTimeoutError(TransientError):
    """检测器执行超时。

    继承 :class:`~agent_core.errors.TransientError` 是刻意的：它与网络超时
    属于同一类瞬时故障，在重试策略里可以统一处理，不必为检测器单开一套。

    B2 阶段这个异常只能由测试或桩触发；真正的按墙钟超时强制执行随 B5 的
    真实检测器（Presidio / sentence-transformers / LLM judge）落地。
    """


class DetectorFailureReason(StrEnum):
    """检测器未能给出结论的原因。"""

    #: 超过 detector_timeout_ms 被中断
    TIMEOUT = "timeout"
    #: 执行过程中抛异常
    ERROR = "error"
    #: 被配置显式关闭（如延迟敏感场景关掉 L4 judge）
    DISABLED = "disabled"


class DetectorFailure(BaseModel):
    """单个检测器的失败记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detector: str
    reason: DetectorFailureReason
    detail: str = Field(default="")

    @field_validator("detector")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("检测器名不能为空")
        return value.strip()

    def to_audit_record(self) -> dict[str, str]:
        return {
            "detector": self.detector,
            "reason": self.reason.value,
            "detail": self.detail,
        }

    @classmethod
    def from_audit_record(cls, record: dict[str, str]) -> DetectorFailure:
        return cls.model_validate(record)


@runtime_checkable
class DetectorPort(Protocol):
    """检测器端口。

    实现方：C4 规则检测器、B5 的 Presidio / 嵌入 / LLM judge。
    测试方：``tests.fakes`` 里的桩检测器。

    注意 ``name`` 与 ``DetectorResult.detector`` 必须一致 —— 审计记录要能
    从"哪条策略被触发"回溯到"哪个检测器的哪一版判的"。
    """

    name: str
    version: str
    supported_stages: frozenset[GuardStage]

    def detect(self, text: str, *, stage: GuardStage) -> tuple[DetectorResult, ...]:
        """检测文本，返回零到多个发现。无发现时返回空元组。"""
        ...


__all__ = [
    "DetectorFailure",
    "DetectorFailureReason",
    "DetectorPort",
    "DetectorTimeoutError",
]
