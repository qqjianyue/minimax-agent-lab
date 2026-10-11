"""PII 规则（L1 的第二类）。

正则单独用不了，必须配校验器：

- 身份证只写 ``\\d{17}[\\dXx]`` 会把任何 18 位数字都判成身份证；
  加**校验位**后，随机数字的误报率降到可忽略。
- 银行卡同理，16-19 位数字必须过 **Luhn 校验**。
- 手机号没有校验位，只能靠"位数 + 号段"的形态判定兜底。

这是本项目里最直观的"build-vs-buy 论据"：正则层能做到 <1ms 且零成本，
但精确率必须靠校验位这类领域知识补足 —— 而 Presidio 做的正是把这套领域
知识规模化（支持更多国家/实体类型）。L1 与 Presidio 互补而非替代。

**与 L2 presidio 的分工**：这里处理中国本地化实体（身份证、手机号），
Presidio 侧主打通用 NER。两者标签相同（``pii_leak``），
policy 层面无需区分，审计里能看出是哪个检测器判的。

## 分段写法为什么在这里解决，而不是在入口做文本规范化

``138-0013-8000`` / ``138 0013 8000`` 是同一个号码的两种写法，正则按
"11 位连续数字"去匹配必然漏。修法有两条路：

1. **先规范化整段输入文本再匹配**，命中后把偏移量映射回原文；
2. **让正则本身容忍分隔符**，校验器里再剥。

选 2。理由不是"更省事"，而是 1 有两个实打实的代价：

- **偏移映射表是有状态的重写逻辑**，作用在不可信输入上，是新的性能与
  攻击面。而 L1 规则层压根不产出 span（``metadata`` 里只有规则名），
  span 只由 Presidio 那侧提供 —— 也就是说映射表**在 L1 这条路上一个
  消费者都没有**，纯粹是白付成本。
- **规范化后的字符串会替换原文进入审计 evidence**。审计要回答的是
  "用户到底发了什么"，把 ``138-0013-8000`` 记成 ``13800138000``
  等于改写了现场。

选 2 还有个附带好处：``detector_rules.redactor.RegexRedactor`` 是直接
在**原文**上跑 ``pattern.finditer`` 拿 span 的。正则一旦容忍分隔符，
命中区间天然就是原文区间（含分隔符），脱敏器一行都不用改，
``138-0013-8000`` 会被整体替换成 ``[REDACTED:CN_MOBILE]`` 而不是原地不动。
"""

from __future__ import annotations

import re

from detector_rules.base import RuleSpec
from guard_contract.enums import GuardStage

_ALL_STAGES = frozenset(GuardStage)

LABEL_PII = "pii_leak"

#: 号码类实体里允许出现的分段分隔符：连字符、空白（含全角空格）、下划线。
#:
#: 刻意**不含** ``.`` 与 ``,`` —— 它们是金额与千分位分隔符，
#: ``订单金额1380013.8000元`` 剥掉点号后就是一个标准手机号。
#: 放进来等于把刚治理掉的误报重新请回来。
_SEPARATORS = re.compile(r"[-\s_]")


def strip_separators(value: str) -> str:
    """去掉号码里的分段分隔符。

    这是"规范化"在本模块的唯一形态：作用在**已经匹配出来的片段**上，
    而不是作用在整段输入文本上。片段的边界仍由正则决定，因此命中区间
    始终指向原文（见模块 docstring）。
    """
    return _SEPARATORS.sub("", value)


# --- 校验器 -----------------------------------------------------------------

#: 身份证前 17 位各位的加权因子（GB 11643）
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
#: 余数 → 校验码映射
_ID_CHECK_CHARS = "10X98765432"


def is_valid_cn_id(value: str) -> bool:
    """校验 18 位中国居民身份证号码（含校验位）。

    分段写法（``110105 19491231 002X``）先剥分隔符再校验。
    """
    text = strip_separators(value.strip())
    if len(text) != 18 or not text[:17].isdigit():
        return False
    total = sum(int(ch) * w for ch, w in zip(text[:17], _ID_WEIGHTS, strict=True))
    return _ID_CHECK_CHARS[total % 11] == text[17].upper()


def is_luhn_valid(value: str) -> bool:
    """银行卡号 Luhn 校验。分段写法先剥分隔符再校验。"""
    digits = strip_separators(value)
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, ch in enumerate(reversed(digits)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def is_valid_cn_mobile(value: str) -> bool:
    """校验中国大陆手机号：恰好 11 位、``1`` 开头、次位 3-9。

    手机号**没有校验位**，这是它与身份证/银行卡的本质差别 ——
    放宽正则之后，正则本身已经不再能保证"这就是个手机号"，位数与
    号段判定就是唯一的把关。分段写法先剥分隔符再判定。
    """
    digits = strip_separators(value)
    if len(digits) != 11 or not digits.isdigit():
        return False
    return digits[0] == "1" and digits[1] in "3456789"


def _matches_valid_cn_id(match: re.Match[str]) -> bool:
    return is_valid_cn_id(match.group(0))


def _matches_luhn(match: re.Match[str]) -> bool:
    return is_luhn_valid(match.group(0))


def _matches_valid_cn_mobile(match: re.Match[str]) -> bool:
    return is_valid_cn_mobile(match.group(0))


# --- 规则 -------------------------------------------------------------------

CN_ID_RULE = RuleSpec(
    name="pii_cn_id_card",
    label=LABEL_PII,
    # 6-8-4 是身份证的标准分段（``110105 19491231 002X``）。
    # 分隔符可选，因此连续写法和分段写法用同一条正则覆盖。
    pattern=re.compile(r"(?<![0-9Xx])\d{6}[-\s_]?\d{8}[-\s_]?\d{3}[0-9Xx](?![0-9Xx])"),
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
    # 3-4-4 分段，容忍连字符/空白/下划线。**分段位置固定**：这是国内最
    # 通行的写法，同时把"任意位置插分隔符"的误报面挡在外面 —— 比如
    # ``1380 0138000`` 这种非分段写法不会被当成手机号。
    # ``(?:[-\s_]?\d{4}){2}`` 是**定长**嵌套，每轮至少吃掉一个数字字符，
    # 不构成 ReDoS。
    pattern=re.compile(r"(?<!\d)1[3-9]\d(?:[-\s_]?\d{4}){2}(?!\d)"),
    score=0.90,
    confidence=0.97,
    stages=_ALL_STAGES,
    kind="CN_MOBILE",
    # 手机号无校验位，正则放宽后必须由形态判定兜底
    validator=_matches_valid_cn_mobile,
    description="中国大陆手机号",
)

BANK_CARD_RULE = RuleSpec(
    name="pii_bank_card",
    label=LABEL_PII,
    # 两个分支刻意分工：
    #   - ``\d{16,19}``          连续写法的原样保留，零行为变更；
    #   - 分段分支（4-4-4-4 + 可选 4-4-4-4-3）里分隔符**必填**。
    # 分隔符必填这一条是防误报的关键：若允许段间缺省，
    # ``13800138000 13800138000`` 会被跨过中间的空格凑成 16 位并
    # 以约 1/10 的概率通过 Luhn —— 那是一个纯靠运气产生的误报。
    # 收紧后只有"确实被分隔符分组"的长号码才走这条分支。
    pattern=re.compile(
        r"(?<!\d)(?:\d{16,19}|\d{4}(?:[-\s_]\d{4}){3}(?:[-\s_]\d{1,3})?)(?!\d)"
    ),
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
    "is_valid_cn_mobile",
    "strip_separators",
]
