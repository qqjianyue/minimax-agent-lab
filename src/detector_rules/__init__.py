"""detector-rules（C4）：L1 规则检测器。

对应架构方案 §5.2 的第一层 —— 极速、可解释、零成本，是唯一在**每一次**
请求上都会跑的检测层（后两层只在前面未命中时触发）。

延迟最低不等于能力最强，本层的定位很清楚：**用最便宜的方式拦掉高置信度的
明显恶意，其余一律交给上层**。编码混淆只给 0.45 分就是这个定位的体现。
"""

from __future__ import annotations

from detector_rules.base import MAX_EVIDENCE, RuleHit, RuleSet, RuleSpec
from detector_rules.detector import (
    L1_RULES_VERSION,
    RulesL1Detector,
    build_l1_rule_set,
)
from detector_rules.patterns import (
    DANGEROUS_TOOL_RULES,
    INJECTION_RULES,
    KEYWORD_RULES,
    OBFUSCATION_RULES,
    SYSTEM_LEAK_RULES,
)
from detector_rules.pii import (
    PII_RULES,
    is_luhn_valid,
    is_valid_cn_id,
)

__all__ = [
    "DANGEROUS_TOOL_RULES",
    "INJECTION_RULES",
    "KEYWORD_RULES",
    "L1_RULES_VERSION",
    "MAX_EVIDENCE",
    "OBFUSCATION_RULES",
    "PII_RULES",
    "SYSTEM_LEAK_RULES",
    "RuleHit",
    "RuleSet",
    "RuleSpec",
    "RulesL1Detector",
    "build_l1_rule_set",
    "is_luhn_valid",
    "is_valid_cn_id",
]
