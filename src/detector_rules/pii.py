"""PII 规则（L1 的第二类）。

正则单独用不了，必须配校验器：

- 身份证只写 ``\\d{17}[\\dXx]`` 会把任何 18 位数字都判成身份证；
  加**校验位**后，随机数字的误报率降到可忽略。
- 银行卡同理，16-19 位数字必须过 **Luhn 校验**。

这是本项目里最直观的"build-vs-buy 论据"：正则层能做到 <1ms 且零成本，
但精确率必须靠校验位这类领域知识补足 —— 而 Presidio 做的正是把这套领域
知识规模化（支持更多国家/实体类型）。L1 与 Presidio 互补而非替代。

**与 L2 presidio 的分工**：这里处理中国本地化实体（身份证、手机号），
Presidio 侧主打通用 NER。两者标签相同（``pii_leak``），
policy 层面无需区分，审计里能看出是哪个检测器判的。
"""

from __future__ import annotations

import re

from detector_rules.base import RuleSpec
from guard_contract.enums import GuardStage

_ALL_STAGES = frozenset(GuardStage)

LABEL_PII = "pii_leak"

# --- 校验器 -----------------------------------------------------------------

#: 身份证前 17 位各位的加权因子（GB 11643）
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
#: 余数 → 校验码映射
_ID_CHECK_CHARS = "10X98765432"


def is_valid_cn_id(value: str) -> bool:
    """校验 18 位中国居民身份证号码（含校验位）。"""
    text = value.strip()
    if len(text) != 18 or not text[:17].isdigit():
        return False
    total = sum(int(ch) * w for ch, w in zip(text[:17], _ID_WEIGHTS, strict=True))
    return _ID_CHECK_CHARS[total % 11] == text[17].upper()


def is_luhn_valid(value: str) -> bool:
    """银行卡号 Luhn 校验。"""
    if not value.isdigit() or not 13 <= len(value) <= 19:
        return False
    total = 0
    for index, ch in enumerate(reversed(value)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _matches_valid_cn_id(match: re.Match[str]) -> bool:
    return is_valid_cn_id(match.group(0))


def _matches_luhn(match: re.Match[str]) -> bool:
    return is_luhn_valid(match.group(0))


# --- 规则 -------------------------------------------------------------------

CN_ID_RULE = RuleSpec(
    name="pii_cn_id_card",
    label=LABEL_PII,
    pattern=re.compile(r"(?<![0-9Xx])\d{17}[0-9Xx](?![0-9Xx])"),
    score=0.92,
    confidence=0.98,
    stages=_ALL_STAGES,
    kind="CN_ID",
    validator=_matches_valid_cn_id,
    description="18 位中国居民身份证号（已过校验位）",
)

CN_MOBILE_RULE = RuleSpec(
    name="pii_cn_mobile",
    label=LABEL_PII,
    # 前后加负向断言，避免从更长的数字串里截一段出来
    pattern=re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    score=0.90,
    confidence=0.97,
    stages=_ALL_STAGES,
    kind="CN_MOBILE",
    description="中国大陆手机号",
)

BANK_CARD_RULE = RuleSpec(
    name="pii_bank_card",
    label=LABEL_PII,
    pattern=re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    score=0.92,
    confidence=0.95,
    stages=_ALL_STAGES,
    kind="BANK_CARD",
    validator=_matches_luhn,
    description="银行卡号（已过 Luhn 校验）",
)

EMAIL_RULE = RuleSpec(
    name="pii_email",
    label=LABEL_PII,
    pattern=re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # 邮箱不一定是 PII：业务邮箱常在正常对话里出现。给低分让它走 redact，
    # 而不是与身份证同档直接触发高危动作。
    score=0.55,
    confidence=0.90,
    stages=_ALL_STAGES,
    kind="EMAIL",
    description="邮箱地址",
)

PII_RULES: tuple[RuleSpec, ...] = (CN_ID_RULE, CN_MOBILE_RULE, BANK_CARD_RULE, EMAIL_RULE)

__all__ = [
    "BANK_CARD_RULE",
    "CN_ID_RULE",
    "CN_MOBILE_RULE",
    "EMAIL_RULE",
    "LABEL_PII",
    "PII_RULES",
    "is_luhn_valid",
    "is_valid_cn_id",
]
