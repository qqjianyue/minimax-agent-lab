"""Policy schema —— 策略即配置（policy-as-code）。

设计要点：

* **策略是数据不是代码**。运营/安全团队调整阈值和动作不需要改 Python，
  这直接对应架构方案里"多租户雏形"和 Q3 里"tenant 可配置"的要求。
* **匹配条件是纯谓词**。:meth:`MatchCondition.matches` 不做任何 I/O，
  所以 B1 阶段就能把阈值边界（0.79 / 0.80 / 0.81）全部穷举测试掉。
* **优先级显式**。``priority`` 数值越小越先求值，避免依赖 YAML 书写顺序
  这种隐式约定 —— 顺序变了行为就变的策略配置，审计时是灾难。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from guard_contract.enums import DEFAULT_FAIL_MODE, FailMode, GuardStage, PolicyAction
from guard_contract.result import DetectorResult


class MatchCondition(BaseModel):
    """一条规则的匹配条件。所有已设置的条件之间是 AND 关系。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 风险分下限（含）。None 表示不限。
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    #: 置信度下限（含）。None 表示不限。
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    #: 只对这些阶段生效；None 表示所有阶段。
    stages: frozenset[GuardStage] | None = None
    #: 只对这些标签生效；None 表示所有标签。
    labels: frozenset[str] | None = None
    #: 只对这些检测器生效；None 表示所有检测器。
    detectors: frozenset[str] | None = None

    @model_validator(mode="after")
    def _at_least_one_constraint(self) -> MatchCondition:
        constraints = (
            self.min_score is not None,
            self.min_confidence is not None,
            self.stages is not None,
            self.labels is not None,
            self.detectors is not None,
        )
        if not any(constraints):
            raise ValueError(
                "MatchCondition 至少需要一个约束条件；全空的规则会匹配所有结果，"
                "通常是配置事故而非本意"
            )
        return self

    def matches(self, result: DetectorResult) -> bool:
        """纯谓词：判断某个检测结果是否命中本条件。"""
        # 这里刻意用一串卫语句而不是折成一个 return not (...)：
        # 条件会持续增加，卫语句形式让"新增一个约束"变成追加一行，
        # 不会出现长表达式里漏掉一个 and 的情况。
        if self.min_score is not None and result.score < self.min_score:
            return False
        if self.min_confidence is not None and result.confidence < self.min_confidence:
            return False
        if self.stages is not None and result.stage not in self.stages:
            return False
        if self.labels is not None and result.label not in self.labels:
            return False
        if self.detectors is not None and result.detector not in self.detectors:  # noqa: SIM103
            return False
        return True

    def describe(self) -> str:
        """人读条件描述，写进审计记录的 reason 字段。"""
        parts: list[str] = []
        if self.min_score is not None:
            parts.append(f"score>={self.min_score}")
        if self.min_confidence is not None:
            parts.append(f"confidence>={self.min_confidence}")
        if self.stages is not None:
            parts.append("stage∈{" + ",".join(sorted(s.value for s in self.stages)) + "}")
        if self.labels is not None:
            parts.append("label∈{" + ",".join(sorted(self.labels)) + "}")
        if self.detectors is not None:
            parts.append("detector∈{" + ",".join(sorted(self.detectors)) + "}")
        return " AND ".join(parts)


class PolicyRule(BaseModel):
    """单条策略规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    action: PolicyAction
    condition: MatchCondition
    #: 数值越小越先求值。
    priority: int = 100
    #: 写进审计记录的人类可读理由。
    reason: str = ""
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("规则名不能为空")
        return value.strip()

    def matches(self, result: DetectorResult) -> bool:
        return self.condition.matches(result)

    def describe(self) -> str:
        return (
            f"[{self.priority}] {self.name} -> {self.action.value} when {self.condition.describe()}"
        )


class PolicySet(BaseModel):
    """一组完整策略。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    use_case: str = "default"
    tenant: str = "default"
    fail_mode: FailMode = DEFAULT_FAIL_MODE
    rules: tuple[PolicyRule, ...]
    description: str = ""
    #: 所有规则都未命中时的兜底动作。
    #: 高安全场景应显式设为 BLOCK —— "没匹配到任何规则"与"确认安全"不是一回事。
    default_action: PolicyAction = PolicyAction.ALLOW
    #: DEGRADED 模式下仍被信任的检测器。
    #: 必须显式列出：降级时"用哪些检测器兜底"是风险决策，不该有默认值。
    degraded_detectors: frozenset[str] | None = None

    @field_validator("version")
    @classmethod
    def _version_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("策略版本不能为空")
        return value.strip()

    @model_validator(mode="after")
    def _check_rules(self) -> PolicySet:
        if not self.rules:
            raise ValueError("策略集不能为空：没有规则的策略集等于全部放行")
        names = [r.name for r in self.rules]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"规则名重复: {sorted(duplicates)}")
        priorities = [r.priority for r in self.rules]
        dup_priorities = {p for p in priorities if priorities.count(p) > 1}
        if dup_priorities:
            raise ValueError(
                f"规则优先级重复: {sorted(dup_priorities)}。优先级必须唯一，"
                "否则求值顺序会退化成依赖 YAML 书写顺序"
            )
        return self

    @model_validator(mode="after")
    def _reject_trivial_catch_all(self) -> PolicySet:
        """禁止写成「什么都匹配就放行」的规则。

        这类规则（condition 只有一个 ``min_score=0.0`` 的 ALLOW 规则）看起来
        合理，实际后果很隐蔽：它会匹配**任何**检测结果，于是 fail_mode 永远
        走不到 —— 即使所有检测器都挂了，只要还存在一条"结果"（哪怕是
        score=0.1 的安全结果），策略就会返回 allow，降级机制形同虚设。

        正确表达方式是 ``default_action: allow``，它只在"没有任何规则命中"
        时生效。兜底放行请写 default_action，不要写成规则。
        """
        for rule in self.rules:
            if rule.action is not PolicyAction.ALLOW:
                continue
            if rule.condition.min_score != 0.0:
                continue
            if rule.condition.min_confidence is not None:
                continue
            if rule.condition.stages is not None:
                continue
            if rule.condition.labels is not None:
                continue
            if rule.condition.detectors is not None:
                continue
            raise ValueError(
                f"规则 {rule.name} 匹配所有检测结果并放行，会让 fail_mode 永远失效。"
                "请改用 PolicySet.default_action: allow 表达兜底放行"
            )
        return self

    def sorted_rules(self) -> tuple[PolicyRule, ...]:
        """按优先级升序返回（优先级小者在前）。"""
        return tuple(sorted(self.rules, key=lambda r: r.priority))

    def enabled_rules(self) -> tuple[PolicyRule, ...]:
        return tuple(r for r in self.sorted_rules() if r.enabled)

    def trusts_in_degraded_mode(self, detector_name: str) -> bool:
        """DEGRADED 模式下是否仍信任某个检测器。

        未配置 ``degraded_detectors`` 时一律返回 False —— 此时 DEGRADED
        等价于"没有任何可用检测器"，应当按完全不可用处理（fail-closed），
        而不是悄悄退化成放行。
        """
        if self.degraded_detectors is None:
            return False
        return detector_name in self.degraded_detectors

    def describe(self) -> str:
        lines = [
            f"PolicySet {self.use_case}@{self.version} "
            f"(tenant={self.tenant}, fail_mode={self.fail_mode.value}, "
            f"default_action={self.default_action.value}, {len(self.rules)} rules)"
        ]
        lines.extend(f"  {rule.describe()}" for rule in self.sorted_rules())
        if self.degraded_detectors is not None:
            lines.append(f"  degraded_detectors: {sorted(self.degraded_detectors)}")
        return "\n".join(lines)
        return "\n".join(lines)


class GuardSettings(BaseModel):
    """guard-contract 包的运行期设置。

    刻意不塞进 :class:`~agent_core.config.Settings`：agent-core 是所有组件的
    下游依赖，让它 import guard_contract 的枚举会形成循环依赖。B3 批次由
    服务层把两者组合成完整配置。
    """

    model_config = ConfigDict(extra="forbid")

    #: 策略文件路径（相对或绝对）
    policy_file: str = "config/policy.default.yaml"
    #: 检测器整体开关，便于紧急时一键降级为"只跑 L1"
    detectors_enabled: frozenset[str] | None = None
    #: 单个检测器超时（毫秒），超时后按 fail_mode 处理
    detector_timeout_ms: float = Field(default=500.0, gt=0)
    #: L4 LLM-as-Judge 总开关。延迟敏感场景可关掉，只保留前三层。
    llm_judge_enabled: bool = True

    def allows_detector(self, name: str) -> bool:
        """检测器是否被启用。``detectors_enabled`` 为 None 表示全开。"""
        if self.detectors_enabled is None:
            return True
        return name in self.detectors_enabled

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_file": self.policy_file,
            "detectors_enabled": (
                sorted(self.detectors_enabled) if self.detectors_enabled is not None else None
            ),
            "detector_timeout_ms": self.detector_timeout_ms,
            "llm_judge_enabled": self.llm_judge_enabled,
        }


def policy_set_from_mapping(data: Mapping[str, Any]) -> PolicySet:
    """从已解析的 dict（通常是 YAML/JSON 解析结果）构造策略集。"""
    return PolicySet.model_validate(dict(data))


__all__ = [
    "GuardSettings",
    "MatchCondition",
    "PolicyRule",
    "PolicySet",
    "policy_set_from_mapping",
]
