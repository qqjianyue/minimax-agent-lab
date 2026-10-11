"""L2 PII 检测器（Presidio，双语）。

与 C4 正则 PII 的分工（pii.py 的 docstring 已约定）：C4 主打中国本地化实体
（身份证 / 手机号 / 银行卡，带校验位），Presidio 主打**通用 NER**（人名 /
组织 / 邮箱 / 地址……）。两者共用 label ``pii_leak``，policy 层无需区分，
审计通过 detector 名（``pii.presidio`` vs ``rules.l1``）区分谁判的。

**evidence 刻意不落明文**：Presidio 识别的是人名 / 组织这类**正则脱敏器
（redactor）兜不住**的实体 —— 如果把原文片段写进 evidence，C8 账本会变成
一个人名数据库。因此 evidence 用掩码形式（``PERSON:张***``），分数保留，
审计侧无需再依赖正则脱敏。

## 为什么需要双语 + 形态过滤（B4 之后实测得出的）

历史 bug：``AnalyzerEngine()`` 无参构造恒定加载 ``en_core_web_lg``，中文
文本丢进去连词都切不对。实测（12 条真实模型输出）：

* en 模型在中文上的误报率 **41.7%**，最长误报片段 **128 字**（整段输出被当成
  一个 LOCATION）—— OUTPUT guard 把正常的业务介绍整段 redact 掉。
* 同一批语料的正样本召回率只有 **33.3%**：既误报又漏报，等于没提供保护。

换成 ``zh_core_web_lg`` 并按语言路由后：

* 正样本召回率升到 **100%**（中文人名、地址全部命中）
* 但误报率升到 **66.7%** —— 形态变了，从"整段误判"变成"短片段误判"
* 误报内容经定位**全部不是中文实体**：模型输出里混进的英文（``sking``、
  ``needing``、``banks``）、emoji（🏦、⏱️）、中文分词碎片（``(大额``、``方App``）

所以这里加了一道**形态过滤**（:func:`passes_shape_filter`）。它**不判断
"这是不是真 PII"** —— 阈值那条路实测走不通（误报与真命中的分数**同为 0.85**，
无法区分）；它只判断"这**长得像不像**中文人名 / 地名"。真命中的形态高度统一
（``张伟`` / ``北京市朝阳区建国路88号``），误报则杂乱无章，一网打尽。

关卡 0 的 18 条语料上，lg 裸跑误报 66.7% → 过滤后 **0%**、召回保持 **100%**。

## ⚠️ 那个 0% 后来被真实流量证伪，又被重新做对（v0.1.15 → v0.1.17）

**v0.1.15 部署后走真实 ``/chat`` 复测，残留误报 4/12 = 33.3%**，与修复前的
英文基线持平。根因不是过滤器写错，是**判定的粒度错了**：过滤开关绑在
:func:`detect_language` 判出的**文档级**语言上，而模型输出常常英文表格骨架里
嵌中文单元格，``latin >= han`` 整段判 ``en``，形态过滤按设计整体关闭。

拿到**未脱敏**的原始输出后重新量化（20 条真实模型输出 + 9 个真实体），
比较了几种改法 —— **其中两种被实测否掉，值得记下来**：

| 方案 | 残留误报 | 真实体保留 |
|---|---|---|
| 文档级语言（原实现） | 3 | 9/9 |
| 纯片段级（只看片段含不含汉字） | **10** | 9/9 |
| 上下文窗口内出现业务词即丢弃 | 3 | **8/9** ❌ |
| **片段 OR 文档级 + 姓氏先验 + 空白收尾** | **0** | **9/9** |

⚠️ 那个 0 是**在这 20 条上测出来的**，不是设计上保证的。已知残留缺口：
英文文档里一个既不含业务词、又没留下切分痕迹（不以空白收尾）的中文长片段
仍然漏网 —— 语料里那条能挡住靠的是结尾的换行。不按长度砍是因为真实地址
可以很长。这条缺口由 ``tests/unit/test_detector_ml.py`` 的
``test_known_gap_long_location_in_english_doc_survives`` 钉住。

1. **"只按片段判语言"是错的**：它会在中文文档里把英文碎片放回去
   （``inquiry about`` / ``typical`` / 🏦），残留从 3 涨到 10。
   正确做法是**并集** —— 文档判中文 **或** 片段含汉字，才启用形态规则。
2. **上下文窗口匹配是有害的**：它能把真人名误杀 —— ``李娜`` 旁边 6 字内
   出现"网点"，于是被判成业务词丢弃。**PII 的漏报比误报严重得多**，
   所以最后**没有**用它。
3. **真正管用的是姓氏先验**：``卡需`` / ``普卡`` 之所以漏网，是因为
   ``term in fragment`` 要求业务词完全落在片段内，而 NER 把"银行卡需要"
   切成什么长度不受控。姓氏是**有界**的先验 —— 几百个姓覆盖近乎全部人口，
   不随业务上新而失效，58 条业务词里有 29 条在这批语料里根本没出现过。

## 教训

两次栽在同一个坑里：v0.1.13 是"绿色检查 ≠ 检查真的跑过"，这次是
"**自选语料上的好成绩 ≠ 线上表现**"。18 条语料上的 0% 是对自己选的测试集的
拟合 —— 业务词表又是看着这 18 条的失败样本写的，两者互相加固，强化的是错觉。
**度量必须走真实入口，且必须拿到未经后处理修饰的原始输出**（第一版语料存的是
已脱敏的响应，误报片段早被 ``[REDACTED:PERSON]`` 替换掉了，分析等于分析空气）。

重依赖策略（决策 D2 / D7）：presidio-analyzer + spacy + spacy-pkuseg 只在
**目标机 venv** 安装（``--extra ml`` + ``infra/spacy/setup.sh install``）。
本地未安装时构造即抛
:class:`~agent_core.errors.DependencyNotInstalledError` 并指引安装路径 ——
"跳过是可见的，不静默"。
"""

from __future__ import annotations

import time
from typing import Any

from agent_core.errors import DependencyNotInstalledError
from detector_ml.timeout import run_detector_with_timeout
from guard_contract.enums import GuardStage
from guard_contract.result import DetectorResult

#: 与 pyproject ml extra 的下限（presidio-analyzer>=2.2.358）对齐。
#: 升级 presidio 时同步 bump —— 审计要能回答"哪个版本判的"。
PRESIDIO_VERSION = "2.2.358"

#: 与 C4 正则 PII 共用：policy 层只认这一个 label。
LABEL_PII = "pii_leak"

#: spaCy 模型名。两者都必须在目标机 venv 里就位（infra/spacy/manifest.json）。
EN_MODEL = "en_core_web_lg"
ZH_MODEL = "zh_core_web_lg"

#: 支持的语言。语言路由的返回值只能取这里面的值。
LANG_EN = "en"
LANG_ZH = "zh"
SUPPORTED_LANGUAGES = (LANG_EN, LANG_ZH)

#: NER 类识别器的名字（小写比较）。只有它们的产出走形态过滤。
#:
#: 格式类（Email/Phone/Url/CreditCard…）靠正则与校验位，命中天然可信 ——
#: ``邮箱chenxiaodong@example.com`` 含拉丁字母却是真 PII，被"中文形态"规则
#: 误杀就本末倒置了。**认不出的识别器一律放行**：过滤器只删除能被证明来自
#: NER 模型的命中，绝不猜。
_NER_RECOGNIZER_NAMES = frozenset(
    {
        "spacyrecognizer",
        "stanazrecognizer",  # 防御性：拼错也不至于漏判成 NER
        "stanarecognizer",
        "transformersrecognizer",
        "glinerrecognizer",
        "huggingfacenerrecognizer",
    }
)

#: 金融业务词 —— 这些是产品名 / 术语，会被中文 NER 当成人名或地名。
#:
#: ⚠️ **需要持续维护**：新业务词上线时要补进来，否则会以误报形式冒出来。
#: 当前条目来自 B4 关卡 0 实测中观察到的误报（``如费率`` / ``代销`` / ``大额``），
#: 属于事后拟合，扩充语料后需重新校准。
_BUSINESS_TERMS = frozenset(
    {
        # 存款 / 理财
        "大额",
        "存单",
        "定期",
        "存款",
        "理财",
        "基金",
        "保险",
        "信托",
        "贵金属",
        "国债",
        "结构性",
        "净值",
        "年化",
        "收益",
        "起息",
        "期限",
        # 账户 / 支付
        "额度",
        "开户",
        "销户",
        "转账",
        "汇款",
        "代收",
        "代销",
        "存管",
        "信用卡",
        "借记卡",
        "储蓄卡",
        "年费",
        "账单",
        "分期",
        "透支",
        # 利率 / 费用
        "利率",
        "费率",
        "汇率",
        "基准",
        "浮动",
        "加点",
        "优惠",
        # 渠道 / 网点
        "网点",
        "营业部",
        "手机银行",
        "网银",
        "柜台",
        "渠道",
        "线上",
        "线下",
        # 风险 / 合规
        "风险",
        "合规",
        "监管",
        "备案",
        "尽调",
        "适当性",
        "风险等级",
        # 外汇 / 其他
        "外汇",
        "结售汇",
        "跨境",
        "离岸",
        "在岸",
    }
)

#: 常见单姓（百家姓前列，覆盖绝大多数汉族人）。
#:
#: **为什么需要它**：形态过滤原本靠业务词表兜底，但 NER 的切分并不稳定 ——
#: 同一个"银行卡需要"可能被切成"卡"/"卡需"/"银行卡需"，而 ``term in fragment``
#: 要求业务词**完全落在片段内**，于是词表再长也追不上这种切法（实测 58 条
#: 业务词里只有 39 条在本批真实语料里出现过）。
#:
#: 姓氏是另一类先验：它**有界**（几百个姓覆盖近乎全部人口）、**可枚举**、
#: 且不随业务变化而失效 —— ``卡需`` / ``普卡`` 的首字都不是姓，无论银行
#: 再上多少新产品都拦得住。
_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
)

#: 复姓。只查首字会误杀"欧阳""司马"这类，所以单姓之外再认一遍两字姓。
_COMPOUND_SURNAMES = frozenset(
    {
        "欧阳", "司马", "上官", "诸葛", "东方", "独孤", "南宫", "闻人",
        "夏侯", "皇甫", "尉迟", "公羊", "澹台", "公冶", "宗政", "濮阳",
        "淳于", "太叔", "申屠", "公孙", "仲孙", "轩辕", "令狐", "钟离",
        "宇文", "长孙", "慕容", "鲜于", "闾丘", "司徒", "司空", "第五",
    }
)


def _mask(fragment: str) -> str:
    """把实体原文压成 `首字符 + ***`，不落明文。"""
    if not fragment:
        return "***"
    return f"{fragment[0]}***"


def _is_han(ch: str) -> bool:
    """CJK 统一表意文字（含扩展 A 与兼容区）。"""
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF


def _is_emoji(ch: str) -> bool:
    """图标 / 符号 / 变体选择符。

    范围比"emoji 码表"宽是故意的：模型输出里的"图表"大多是符号类字符
    （▶ ● ⏱ ⏸ 〽 …），把范围卡死在 1F300+ 之类的码段上会漏掉
    ``⏱️``（U+23F1 + U+FE0F）这类实际高频的写法 —— 实测就漏过一次。
    """
    code = ord(ch)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2190 <= code <= 0x21FF
        or 0x2B00 <= code <= 0x2BFF
        or 0x2300 <= code <= 0x23FF  # ⏱⏲⏸⏹⏺ 等技术符号
        or 0x25A0 <= code <= 0x25FF  # ▶ ● ■ 等几何图形
        or code in (0x3030, 0x303D)  # 〰〽
        or 0xFE00 <= code <= 0xFE0F  # 变体选择符（⏱️ 的那个️）
    )


def detect_language(text: str) -> str:
    """判定文本主语言，返回 ``"zh"`` 或 ``"en"``。

    规则是**汉字 dominance**而非字符占比：汉字数 ≥ 2 且 ≥ 拉丁字母数。

    为什么不用占比：``"李娜的账户 sking 有问题吗"`` 里汉字 8 / 拉丁 5，
    占比 0.62 判 zh 没问题；但 ``"VIP 客户 张伟 来办理"`` 汉字 4 / 拉丁 3，
    占比 0.57 也很安全 —— 而纯占比阈值一旦调高，中文里夹一串英文产品名
    （"APP"、"ETF"、"5G"）就会被误判成 en，那正是误报最严重的那条路。

    ``han >= 2`` 这个下限是为了让 ``"张伟"`` 这种短人名也走中文模型。
    """
    han = sum(1 for ch in text if _is_han(ch))
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if han >= 2 and han >= latin:
        return LANG_ZH
    return LANG_EN


def is_cjk_fragment(fragment: str) -> bool:
    """片段**本身**是否含汉字。

    与 :func:`detect_language` 的区别是关键：那个判的是**整段**的主语言，
    决定用哪个 spaCy 模型；这个判的是**片段**用什么文字，决定形态规则要不要
    作用在它身上。两者粒度不同，不能互相替代。
    """
    return any(_is_han(ch) for ch in fragment)


def is_surname_initial(fragment: str) -> bool:
    """片段是否以常见汉姓开头（单姓或复姓）。

    ⚠️ **已知代价**：音译的外文人名（``约翰``/``迈克``）不在姓氏表里，
    会被当成误报丢弃。中文语境下的银行客服场景里这类写法罕见，
    选了这条取舍 —— 漏一个音译名，好过放行 ``卡需``/``普卡`` 这类
    每天都在发生的误报。
    """
    if not fragment:
        return False
    return fragment[0] in _SURNAMES or fragment[:2] in _COMPOUND_SURNAMES


def passes_shape_filter(fragment: str) -> bool:
    """该实体片段是否"像中文人名 / 地名"。

    故意**不判断真假** —— 分数区分不了（误报与真命中同为 0.85），
    这里只做形态筛查，五条规则任一不满足即丢弃：

    1. 至少 2 个汉字（单字片段基本是分词产物；中文人名极少单字）
    2. 不含 emoji / 图标 —— 模型爱在回答里放 🏦 ⏱️，中文 NER 不该对它们有反应
    3. 不含拉丁字母 —— 挡掉模型输出里混进的英文（sking / needing / banks）
       与中英混排碎片（方App / TF代销）。**数字不挡**：门牌号是正常地址的一部分
       （建国路88号），第一版把数字一并挡掉，把整条地址误杀了
    4. 不含金融业务词 —— 产品名与术语（如费率、代销）会被当成人名
    5. **不以空白 / 换行收尾** —— 真实人名地名不会以空格结尾，这是 NER
       切分毛躁的可靠信号（实测拦下 ``贷款金额和担保方式\\n``）。
       刻意**不加长度上限**：真实地址可以很长（``北京市朝阳区建国路88号``
       11 字），按长度砍会误杀整条地址
    """
    if sum(1 for ch in fragment if _is_han(ch)) < 2:
        return False
    if any(_is_emoji(ch) for ch in fragment):
        return False
    if any(ch.isascii() and ch.isalpha() for ch in fragment):
        return False
    if fragment[-1:].isspace():
        return False
    return not any(term in fragment for term in _BUSINESS_TERMS)


def looks_like_false_positive(fragment: str, *, entity_type: str, doc_language: str) -> bool:
    """这条 NER 命中是不是形态误报。

    三条规则任一成立即丢弃：

    1. **适用性**：文档判成中文，**或**片段本身含汉字。两条都满足才管。
       只认文档语言会让中英混排的输出整段逃逸（实测模型输出里 han/latin
       常在 0.37~0.61 之间，整段判 en 并不罕见）；只认片段又会在中文文档里
       放回英文碎片（实测 ``inquiry about`` / ``typical`` / 🏦 就是这么漏的）。
    2. **形态**：片段不像中文人名地名（:func:`passes_shape_filter` 的反面）
    3. **姓氏先验**：``PERSON`` 但首字不是常见汉姓

    第 3 条刻意**不用上下文窗口**：实测里它会把真人名误杀 ——
    ``李娜`` 旁边 6 字内出现"网点"，于是被当成业务词丢弃。
    **PII 的漏报比误报严重得多**，宁可不加这条。
    """
    # 纯图标片段不可能是人名或地名，**与语言无关**，所以放在适用性判断之前。
    # 模型很爱在回答里放 📈 🏦 ⏱️，实测这在英文文档里也会漏（'📈' 被判成 LOCATION）。
    if fragment and all(_is_emoji(ch) for ch in fragment):
        return True
    if not (doc_language == LANG_ZH or is_cjk_fragment(fragment)):
        return False
    if not passes_shape_filter(fragment):
        return True
    return entity_type == "PERSON" and not is_surname_initial(fragment)


def _is_ner_hit(hit: Any) -> bool:
    """该命中是否来自 NER 模型（决定要不要过形态过滤）。

    识别不出recognizer_name 时**返回 False（放行）**：过滤器只删除能被证明
    是 NER 产出的命中，不做猜测。单元测试用的 fake analyzer 没有这个元数据，
    放行也让契约测试与真实实现的差异保持可见。
    """
    metadata = getattr(hit, "recognition_metadata", None)
    if not isinstance(metadata, dict):
        return False
    name = str(metadata.get("recognizer_name") or "").lower()
    return name in _NER_RECOGNIZER_NAMES


def _build_nlp_engine() -> Any:
    """构造 en + zh 双语 spaCy 引擎。"""
    from presidio_analyzer.nlp_engine import NlpEngineProvider  # noqa: PLC0415

    return NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": LANG_EN, "model_name": EN_MODEL},
                {"lang_code": LANG_ZH, "model_name": ZH_MODEL},
            ],
            "ner_model_configuration": {
                # spaCy 吐的是 GPE / PER，Presidio 内部叫 LOCATION / PERSON。
                # 这张映射表漏了，模型认出来了 Presidio 也不认。
                "model_to_presidio_entity_mapping": {
                    "PERSON": "PERSON",
                    "PER": "PERSON",
                    "GPE": "LOCATION",
                    "LOC": "LOCATION",
                    "FAC": "LOCATION",
                    "ORG": "ORGANIZATION",
                    "DATE": "DATE_TIME",
                    "TIME": "DATE_TIME",
                },
                # 机构名 / 日期是中文场景的误报重灾区，统一压分。
                "low_confidence_score_multiplier": 0.4,
                "low_score_entity_names": ["ORGANIZATION", "DATE_TIME"],
            },
        }
    ).create_engine()


def _load_analyzer() -> Any:
    """惰性加载 Presidio 分析引擎（重依赖只在目标机存在）。

    只传 ``nlp_engine`` + ``supported_languages``，**registry 交给 Presidio
    自己建**。传自定义 registry 反而要求它的 ``supported_languages`` 与 analyzer
    严格一致，而内置识别器覆盖 20 国却没有中国，必然对不上直接 ValueError。
    """
    try:
        from presidio_analyzer import AnalyzerEngine  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 目标机依赖，本地不触发
        raise DependencyNotInstalledError(
            "Presidio 检测器需要 presidio-analyzer 与 spacy。"
            "目标机部署时由 --extra ml + infra/spacy/setup.sh install 提供；"
            "本地仅跑契约测试（Fake 注入）。"
        ) from exc

    try:
        engine = _build_nlp_engine()
        return AnalyzerEngine(nlp_engine=engine, supported_languages=list(SUPPORTED_LANGUAGES))
    except (Exception, SystemExit) as exc:  # pragma: no cover - 同上，本地不触发
        # 必须连 SystemExit 一起捕获：spacy 缺模型时内部会调
        # `spacy.cli.download`，下载失败抛的是 SystemExit（BaseException）
        # 而不是 Exception —— 只捕获 Exception 会让它在离线环境直接杀掉进程。
        # 模型未部署是"检测器不可用"的业务状态，不是进程级故障。
        raise DependencyNotInstalledError(
            "Presidio 初始化失败（spacy 模型未就绪？中文还需要 spacy-pkuseg，"
            f"先跑 infra/spacy/setup.sh install）：{type(exc).__name__}"
        ) from exc


class PresidioPiiDetector:
    """通用 NER PII 检测器（L2），支持中英双语。

    Args:
        language: 强制指定语言；``None``（默认）表示按文本自动判定。
        score_threshold: Presidio 命中分数阈值，默认 0.35（其推荐值）。
            **不指望它区分中文误报** —— 实测误报与真命中的分数同为 0.85。
        timeout_ms: 单次检测墙钟超时。None 表示不设（由 pipeline 层统一约束）。
        analyzer: 测试注入的分析引擎；不传则惰性加载真实 Presidio。
    """

    name = "pii.presidio"
    version = PRESIDIO_VERSION
    supported_stages = frozenset(
        {GuardStage.INPUT, GuardStage.OUTPUT, GuardStage.RETRIEVAL, GuardStage.TOOL}
    )

    def __init__(
        self,
        *,
        language: str | None = None,
        score_threshold: float = 0.35,
        timeout_ms: int | None = 2000,
        analyzer: Any | None = None,
    ) -> None:
        if language is not None and language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"language 只能是 {SUPPORTED_LANGUAGES} 或 None（自动判定），收到 {language!r}"
            )
        self._forced_language = language
        self._score_threshold = score_threshold
        self._timeout_ms = timeout_ms
        self._analyzer = analyzer if analyzer is not None else _load_analyzer()

    def detect(self, text: str, *, stage: GuardStage) -> tuple[DetectorResult, ...]:
        if not text:
            return ()

        language = self._forced_language or detect_language(text)

        def worker() -> tuple[DetectorResult, ...]:
            started = time.perf_counter()
            hits = self._analyzer.analyze(
                text=text,
                language=language,
                score_threshold=self._score_threshold,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            results: list[DetectorResult] = []
            for hit in hits:
                fragment = text[hit.start : hit.end]
                # 形态误报过滤只作用于 NER 产出：格式类（邮箱/手机号）靠正则与
                # 校验位，命中天然可信，被"中文形态"规则误杀就本末倒置了。
                if _is_ner_hit(hit) and looks_like_false_positive(
                    fragment,
                    entity_type=hit.entity_type,
                    doc_language=language,
                ):
                    continue
                results.append(
                    DetectorResult(
                        detector=self.name,
                        version=self.version,
                        stage=stage,
                        label=LABEL_PII,
                        score=min(hit.score, 1.0),
                        confidence=hit.score,
                        evidence=(f"{hit.entity_type}:{_mask(fragment)}",),
                        latency_ms=elapsed_ms,
                        # start/end 是**偏移量**不是原文：脱敏执行层靠它按决策
                        # 遮住实体（见 policy_engine.redactor 模块说明）。
                        # 只写偏移量也让审计账本保持"不落明文"的姿态。
                        metadata={
                            "entity": hit.entity_type,
                            "language": language,
                            "start": hit.start,
                            "end": hit.end,
                        },
                    )
                )
            return tuple(results)

        return run_detector_with_timeout(
            worker, timeout_ms=self._timeout_ms, detector_name=self.name
        )


__all__ = [
    "EN_MODEL",
    "LABEL_PII",
    "LANG_EN",
    "LANG_ZH",
    "PRESIDIO_VERSION",
    "PresidioPiiDetector",
    "SUPPORTED_LANGUAGES",
    "ZH_MODEL",
    "detect_language",
    "is_cjk_fragment",
    "is_surname_initial",
    "looks_like_false_positive",
    "passes_shape_filter",
]
