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
#: 法人主体的注册标识。**刻意不叫 PII**：统一社会信用代码标识的是公司而不是
#: 自然人，在国家企业信用信息公示系统里是公开信息。把它当个人隐私脱敏，会把
#: 「查 91350100M000100Y43 的开户信息」这类正常业务请求直接打残。
LABEL_ORG_IDENTIFIER = "org_identifier"

#: 上下文锚点与号码之间的最大距离（字符数）。12 字足够覆盖「联系电话：」
#: 「座机」「固话」等常见写法，又不至于把半句话之外的数字拉进来。
_CONTEXT_WINDOW = 12

#: 座机的上下文锚点关键词
_LANDLINE_CONTEXT = re.compile(r"座机|固定电话|固话|联系电话|电话|Tel", re.IGNORECASE)

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


# --- 上下文锚点 -------------------------------------------------------------
#
# 弱形态实体（形态本身就与业务数字重合）唯一的精确率杠杆就是上下文。
# 裸座机是典型：``[2-9]\d{6,7}`` 会把「本次交易金额为8765432元」当成座机，
# 而金额是银行对话里最高频的数字形态。所以裸座机必须带锚点才成立。
#
# 锚点只看**号码之前**：中文里「座机58880000」「联系电话：58880000」
# 「固话 83000000」都是关键词在前，写在号码之后的（「58880000 是座机」）
# 会被漏掉。这是有意的取舍 —— 放宽到前后都看会把整句里的数字都拉进来。


def has_context_before(
    match: re.Match[str],
    pattern: re.Pattern[str],
    window: int = _CONTEXT_WINDOW,
) -> bool:
    """匹配片段前 ``window`` 个字符内是否出现过 ``pattern``。

    走 :class:`RuleSpec` 既有的 ``validator`` 扩展点，不给契约加字段：
    ``re.Match`` 自带 ``.string`` 与 ``.start()``，校验器拿得到上下文，
    而 :func:`~detector_rules.redactor.RegexRedactor` 走的也是同一条
    ``validator`` 调用路径，两边行为天然一致。
    """
    head = match.string[max(0, match.start() - window) : match.start()]
    return pattern.search(head) is not None


#: 带区号座机的尾部形态。裸座机规则用它避免对同一个号码重复出证：
#: ``021-58880000`` 里的 ``58880000`` 本身也满足裸号形态且前后有"座机"，
#: 会让一个实体在审计里留下两条证据。两份证据脱敏结果相同（占位符同为
#: CN_LANDLINE，重叠取最长），但审计不该有这种噪声。
_LANDLINE_TAIL = re.compile(r"0\d{2,3}[-\s]$")


def _matches_landline_bare(match: re.Match[str]) -> bool:
    if not has_context_before(match, _LANDLINE_CONTEXT):
        return False
    head = match.string[max(0, match.start() - 5) : match.start()]
    return _LANDLINE_TAIL.search(head) is None


# --- 统一社会信用代码（GB 32100-2015）----------------------------------------

#: USCC 字符集：数字 + 大写字母，**排除 I O S V Z**（与 1 0 5 V Z 形近）
USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
#: 前 17 位的加权因子
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
#: 首位登记管理部门代码：1 机构编制 / 5 民政 / 9 工商 / N 农业 / Y 其他
_USCC_REGISTRY_CODES = frozenset("159NY")


def is_valid_uscc(value: str) -> bool:
    """校验统一社会信用代码（GB 32100-2015）。

    三道关：字符集、结构（首位登记管理部门代码 + 6 位数字行政区划码）、
    mod-31 校验位。

    结构约束刻意放在**校验器里**而不是只写在正则里 —— 少了它，随机 token
    的通过率是 3.204%；带上它是 0.040%（蒙特卡洛各 10 万次，80 倍收敛）。
    只做校验位的话，本函数被单独调用会给出与规则实际行为不符的答案：
    规则走的是"正则 ∧ 校验器"，函数单独走只有校验器。两者口径必须一致，
    否则它就是个会说谎的工具。
    """
    text = value.upper()
    if len(text) != 18 or any(c not in USCC_CHARSET for c in text):
        return False
    # 第 3-8 位是登记管理机关行政区划码，按 GB/T 2260 必须是 6 位纯数字
    if text[0] not in _USCC_REGISTRY_CODES or not text[2:8].isdigit():
        return False
    total = sum(USCC_CHARSET.index(c) * w for c, w in zip(text[:17], _USCC_WEIGHTS, strict=True))
    return USCC_CHARSET[(31 - total % 31) % 31] == text[17]


def _matches_valid_uscc(match: re.Match[str]) -> bool:
    return is_valid_uscc(match.group(0))


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

PASSPORT_RULE = RuleSpec(
    name="pii_cn_passport",
    label=LABEL_PII,
    # 2012 年起新版中国护照 = 1 个法定前缀字母 + 8 位数字。前缀字母取自
    # 外交部签发的证件种类集合，比"任意字母 + 8 位数字"紧得多 ——
    # 后者在 SKU / 工单号里非常常见。re.I 的代价可以忽略：前缀本来只有
    # 7 个字母，大小写变体再多也还是那 7 类。
    pattern=re.compile(r"(?<![0-9A-Za-z])[EGDSPHMP]\d{8}(?![0-9A-Za-z])", re.IGNORECASE),
    score=0.90,
    # 没有校验位，置信度比身份证低一档
    confidence=0.95,
    stages=_ALL_STAGES,
    kind="CN_PASSPORT",
    description="中国护照号（法定前缀 + 8 位）",
)

LANDLINE_AREA_RULE = RuleSpec(
    name="pii_cn_landline_area",
    label=LABEL_PII,
    # 区号必须以 0 开头 —— 这一条同时把 400 / 950 / 955 这类**客服热线**
    # 排除在外。它们是企业对外公开号码，不是个人信息，脱敏它们只会
    # 把「客服电话多少」的答案打残。
    pattern=re.compile(r"(?<!\d)0\d{2,3}[-\s]\d{7,8}(?!\d)"),
    score=0.55,
    confidence=0.95,
    stages=_ALL_STAGES,
    kind="CN_LANDLINE",
    description="带区号的固定电话（不含客服热线）",
)

LANDLINE_BARE_RULE = RuleSpec(
    name="pii_cn_landline_bare",
    label=LABEL_PII,
    pattern=re.compile(r"(?<!\d)[2-9]\d{6,7}(?!\d)"),
    score=0.55,
    confidence=0.80,
    stages=_ALL_STAGES,
    kind="CN_LANDLINE",
    # 裸号形态与业务数字完全重合，**必须**带上下文锚点才成立
    validator=_matches_landline_bare,
    description="无区号的固定电话（需前文有电话类关键词）",
)

PII_RULES: tuple[RuleSpec, ...] = (
    CN_ID_RULE,
    CN_MOBILE_RULE,
    BANK_CARD_RULE,
    EMAIL_RULE,
    PASSPORT_RULE,
    LANDLINE_AREA_RULE,
    LANDLINE_BARE_RULE,
)

USCC_RULE = RuleSpec(
    name="org_uscc",
    label=LABEL_ORG_IDENTIFIER,
    # 结构约束来自 GB 32100，不是拍脑袋：
    #   - 首位是登记管理部门代码，只可能 ∈ {1,5,9,N,Y}（编制/民政/工商/农业/其他）
    #   - 第 3-8 位是登记管理机关行政区划码，按 GB/T 2260 必须是 6 位纯数字
    # 这两条把随机 token 的碰撞率从 3.204% 压到 0.040%（蒙特卡洛 10 万次），
    # 比只靠校验位强 80 倍 —— 这正是本模块开头说的"精确率必须靠领域知识补足"。
    # 字符集用 re.I：小写代码的召回代价远大于 0.001pp 的精确率收益。
    pattern=re.compile(
        r"(?<![0-9A-Za-z])[159NY][0-9A-HJ-NP-RT-UW-Y]\d{6}[0-9A-HJ-NP-RT-UW-Y]{10}(?![0-9A-Za-z])",
        re.IGNORECASE,
    ),
    # 0.45 刻意低于 escalate_borderline 的 0.60 门槛：这条规则的价值是
    # "看见并记进审计"，不是"拦下来"。score 的语义是有多危险，法人注册
    # 标识在银行对话里确实不高危 —— 与 encoding_obfuscation 同理给 0.45。
    score=0.45,
    confidence=0.98,
    stages=_ALL_STAGES,
    kind="USCC",
    validator=_matches_valid_uscc,
    description="统一社会信用代码（GB 32100，仅审计不脱敏）",
)

#: 只识别、不脱敏的规则。**刻意不进** :data:`PII_RULES` ——
#: RegexRedactor 默认只吃 PII_RULES，分开之后脱敏器根本看不见它，
#: "不脱敏"就成了结构保证，而不是靠策略里恰好没有匹配规则。
IDENTIFIER_RULES: tuple[RuleSpec, ...] = (USCC_RULE,)

__all__ = [
    "BANK_CARD_RULE",
    "CN_ID_RULE",
    "CN_MOBILE_RULE",
    "EMAIL_RULE",
    "IDENTIFIER_RULES",
    "LABEL_ORG_IDENTIFIER",
    "LABEL_PII",
    "LANDLINE_AREA_RULE",
    "LANDLINE_BARE_RULE",
    "PII_RULES",
    "PASSPORT_RULE",
    "USCC_CHARSET",
    "USCC_RULE",
    "has_context_before",
    "is_luhn_valid",
    "is_valid_cn_id",
    "is_valid_cn_mobile",
    "is_valid_uscc",
    "strip_separators",
]
