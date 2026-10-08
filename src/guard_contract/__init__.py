"""guard-contract：安全检测的公共契约层。

本包只定义**结构**，不定义**行为**：

- :class:`~guard_contract.result.DetectorResult` —— 检测器返回什么
- :class:`~guard_contract.enums.PolicyAction` 等枚举 —— 可能出现哪些取值
- :class:`~guard_contract.policy_schema.PolicySet` —— 策略怎么配置

判定逻辑（把多个检测结果求值成动作）在 C3 policy-engine；具体检测实现
在 C4/C5。这样切分的收益是：本包零外部依赖，单元测试可以在本地毫秒级跑完，
而策略正确性可以脱离任何真实模型来验证。
"""

from __future__ import annotations

from guard_contract.default_policy import (
    DEFAULT_POLICY_YAML,
    load_default_policy,
    load_policy_set,
)
from guard_contract.enums import (
    DEFAULT_FAIL_MODE,
    FailMode,
    GuardStage,
    PolicyAction,
    Severity,
)
from guard_contract.policy_schema import (
    GuardSettings,
    MatchCondition,
    PolicyRule,
    PolicySet,
    policy_set_from_mapping,
)
from guard_contract.port import (
    DetectorFailure,
    DetectorFailureReason,
    DetectorPort,
    DetectorTimeoutError,
)
from guard_contract.result import DetectorResult, decision_record

__all__ = [
    "DEFAULT_FAIL_MODE",
    "DEFAULT_POLICY_YAML",
    "DetectorFailure",
    "DetectorFailureReason",
    "DetectorPort",
    "DetectorResult",
    "DetectorTimeoutError",
    "FailMode",
    "GuardSettings",
    "GuardStage",
    "MatchCondition",
    "PolicyAction",
    "PolicyRule",
    "PolicySet",
    "Severity",
    "decision_record",
    "load_default_policy",
    "load_policy_set",
    "policy_set_from_mapping",
]
