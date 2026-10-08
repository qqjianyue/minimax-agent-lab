"""关键词 / 模式类规则（L1 的主体）。

四个类别，各自对应 OWASP LLM Top 10 的一个风险面：

| 类别 | label | 覆盖 |
|---|---|---|
| 提示注入 | ``prompt_injection`` | LLM01 |
| 系统提示泄露 | ``system_prompt_leak`` | LLM06 |
| 危险工具调用 | ``dangerous_tool_call`` | LLM08 |
| 编码混淆 | ``encoding_obfuscation`` | LLM01 的绕过变体 |

**关于分数与置信度的取值逻辑**：

- 越"确定是恶意"的模式，confidence 越高（0.9+）。规则层的误报率低，
  策略可以据此下手重地 block。
- ``encoding_obfuscation`` 刻意给**低分**（0.45）：长 base64 串本身可能只是
  正常数据。给低分让它落到"交给 L4 判断"而不是"直接拦"——这正是分层架构里
  规则层不越权的体现。

**关于阶段**：危险工具调用只在 ``TOOL`` 阶段生效。用户在输入里说"删除所有
客户数据"是合法意图表达（可能就是业务人员），但在工具参数里出现就是另一回事；
按阶段区分可以避免把正常提问误拦。
"""

from __future__ import annotations

from detector_rules.base import build_rules
from guard_contract.enums import GuardStage

_ALL_STAGES = frozenset(GuardStage)
#: "内容"类检测点：用户输入、检索内容、模型输出。
#:
#: 注入与泄露类规则必须覆盖 OUTPUT —— 原因不是"模型也会说这些话"，而是
#: **模型输出里出现这类内容，意味着注入已经成功影响了模型**，或者模型正在
#: 复述被污染的上下文。终检层不查它，就等于把被污染的输出原样交给用户。
_CONTENT_STAGES = frozenset({GuardStage.INPUT, GuardStage.RETRIEVAL, GuardStage.OUTPUT})
_TOOL_STAGE = frozenset({GuardStage.TOOL})

# --- 提示注入 ---------------------------------------------------------------
# 经典的"忽略之前指令"族，误报率低，给高置信度。
INJECTION_RULES = build_rules(
    [
        (
            "inject_ignore_previous_en",
            r"ignore\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|preceding|earlier|foregoing)\s+(?:instructions?|prompts?|rules?|directions?|messages?)",
            "prompt_injection",
            0.90,
            0.95,
            _CONTENT_STAGES,
        ),
        (
            "inject_disregard_en",
            r"disregard\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier|foregoing|system)\s+(?:instructions?|prompts?|rules?|guidelines?|training)",
            "prompt_injection",
            0.90,
            0.95,
            _CONTENT_STAGES,
        ),
        (
            "inject_forget_en",
            r"forget\s+(?:everything|all)\s+(?:you\s+)?(?:were\s+)?(?:told|above|before|previously)",
            "prompt_injection",
            0.85,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "inject_new_instructions_marker",
            r"(?:new|updated|revised)\s+(?:system\s+)?instructions?\s*[:：]",
            "prompt_injection",
            0.70,
            0.80,
            _CONTENT_STAGES,
        ),
        (
            "inject_ignore_previous_zh",
            # 中文里修饰语会叠加（"之前**的**所有"），所以用 * 而不是 ?
            # 量词作用在字面量交替上，无嵌套回溯，不引入 ReDoS 风险。
            r"(?:忽略|无视|忘记|抛弃)(?:掉)?(?:的|所有|全部|一切|之前|以上|上面|前面|上述|先前|过)*"
            r"(?:指令|提示词|提示|规则|设定|限制|要求|人设)",
            "prompt_injection",
            0.88,
            0.93,
            _CONTENT_STAGES,
        ),
        (
            "inject_roleplay_bypass",
            r"(?:pretend|act\s+as|roleplay\s+as|imagine)\s+(?:that\s+)?(?:you\s+)?(?:have\s+no|are\s+no\s+longer|don't\s+have|without)\s+(?:rules|restrictions|guidelines|limits|filters|limits)",
            "prompt_injection",
            0.85,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "inject_zh_unrestricted",
            r"不受(?:任何)?(?:限制|约束|道德)(?:的)?\s*(?:AI|助手|模型|机器人)",
            "prompt_injection",
            0.85,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "inject_dan_jailbreak",
            r"you\s+are\s+now\s+(?:DAN|dan\b|in\s+developer\s+mode|in\s+god\s+mode)",
            "prompt_injection",
            0.88,
            0.93,
            _CONTENT_STAGES,
        ),
        (
            "inject_developer_mode",
            r"(?:developer|debug|god|admin)\s+mode\s+(?:is\s+)?(?:enabled|activated|on|unlocked)",
            "prompt_injection",
            0.85,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "inject_safety_bypass",
            r"(?:bypass|circumvent|disable|turn\s+off|switch\s+off)\s+(?:your\s+|all\s+|the\s+|any\s+)?(?:safety|security|content|ethical|moderation)\s*(?:filter|guideline|restriction|polic\w*|check)",
            "prompt_injection",
            0.85,
            0.90,
            _CONTENT_STAGES,
        ),
    ]
)

# --- 系统提示泄露 -----------------------------------------------------------
SYSTEM_LEAK_RULES = build_rules(
    [
        (
            "leak_reveal_prompt_en",
            r"(?:reveal|show|print|output|display|repeat|dump)\s+(?:me\s+)?(?:your|the)\s+(?:full\s+|exact\s+|original\s+|initial\s+)?(?:system\s+)?(?:prompt|instructions|message|rules|configuration)",
            "system_prompt_leak",
            0.92,
            0.92,
            _CONTENT_STAGES,
        ),
        (
            "leak_what_were_instructions",
            r"what\s+(?:were|was)\s+your\s+(?:exact\s+|original\s+|initial\s+)?(?:system\s+)?(?:prompt|instructions)",
            "system_prompt_leak",
            0.90,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "leak_repeat_above",
            r"repeat\s+(?:everything|the\s+text|all\s+text)\s+(?:above|before|preceding|prior)",
            "system_prompt_leak",
            0.88,
            0.88,
            _CONTENT_STAGES,
        ),
        (
            "leak_first_message",
            r"(?:first|initial|very\s+first)\s+message\s+you\s+(?:received|were\s+given|got)",
            "system_prompt_leak",
            0.90,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "leak_zh_system_prompt",
            r"(?:输出|显示|告诉我|重复|复述|打印)(?:你的)?(?:系统)?(?:提示词|提示|指令|初始设定|人设)",
            "system_prompt_leak",
            0.90,
            0.90,
            _CONTENT_STAGES,
        ),
        (
            "leak_zh_prompt_content",
            r"(?:系统提示|初始指令|人设)(?:词)?(?:的)?(?:内容|原文|是什么|具体)",
            "system_prompt_leak",
            0.88,
            0.88,
            _CONTENT_STAGES,
        ),
    ]
)

# --- 危险工具调用 -----------------------------------------------------------
# 只在 TOOL 阶段生效：输入里说"删除数据"可能是合法业务意图，
# 工具参数里出现才是风险。
DANGEROUS_TOOL_RULES = build_rules(
    [
        (
            "tool_drop_table",
            r"(?:drop|truncate|delete)\s+(?:table|database|schema|collection)\b",
            "dangerous_tool_call",
            0.92,
            0.92,
            _TOOL_STAGE,
        ),
        (
            "tool_delete_all_records",
            r"delete\s+(?:all|every)\s+(?:the\s+)?(?:records?|rows?|users?|customers?|accounts?|data|table)",
            "dangerous_tool_call",
            0.90,
            0.92,
            _TOOL_STAGE,
        ),
        (
            "tool_rm_rf",
            r"rm\s+-rf?\s+[~/\w]",
            "dangerous_tool_call",
            0.95,
            0.95,
            _TOOL_STAGE,
        ),
        (
            "tool_wipe_data",
            r"(?:wipe|purge|erase)\s+(?:all\s+)?(?:the\s+)?(?:data|records?|storage|logs?)",
            "dangerous_tool_call",
            0.88,
            0.90,
            _TOOL_STAGE,
        ),
        (
            "tool_transfer_money",
            r"(?:transfer|wire|send)\s+(?:the\s+)?(?:money|funds?|payment)\s+(?:to|out\s+to)\b",
            "dangerous_tool_call",
            0.95,
            0.95,
            _TOOL_STAGE,
        ),
        (
            "tool_zh_delete_all",
            r"(?:删除|清空|抹掉|清除)(?:掉)?(?:所有|全部|一切)?(?:的)?(?:客户|用户|账户|账号|数据|记录|表)",
            "dangerous_tool_call",
            0.90,
            0.90,
            _TOOL_STAGE,
        ),
        (
            "tool_zh_transfer",
            r"(?:转账|汇款|打款)(?:给|至|到)",
            "dangerous_tool_call",
            0.95,
            0.95,
            _TOOL_STAGE,
        ),
    ]
)

# --- 编码混淆 ---------------------------------------------------------------
# 刻意给低分：长 base64 串可能只是正常数据。低分的意义是把可疑内容
# 递交给 L4 判断，而不是让 L1 越权直接拦下。
OBFUSCATION_RULES = build_rules(
    [
        (
            "obfusc_base64_blob",
            r"\b[A-Za-z0-9+/]{40,}={0,2}\b",
            "encoding_obfuscation",
            0.45,
            0.70,
            _ALL_STAGES,
        ),
        (
            "obfusc_hex_blob",
            r"\b[0-9a-fA-F]{32,}\b",
            "encoding_obfuscation",
            0.40,
            0.60,
            _ALL_STAGES,
        ),
        (
            "obfusc_decode_instruction",
            r"(?:base64|base-64|rot13|rot-13|hex)\s*(?:decode|decoded|encoding|解码)",
            "encoding_obfuscation",
            0.70,
            0.85,
            _CONTENT_STAGES,
        ),
        (
            "obfusc_zh_decode_instruction",
            r"(?:解码|解出|还原)(?:下面|以下|这段|该)(?:的)?(?:内容|字符串|数据|base64)",
            "encoding_obfuscation",
            0.70,
            0.85,
            _CONTENT_STAGES,
        ),
    ]
)

#: L1 全部关键词类规则。PII 规则在 :mod:`detector_rules.pii`，单独成表。
KEYWORD_RULES = INJECTION_RULES + SYSTEM_LEAK_RULES + DANGEROUS_TOOL_RULES + OBFUSCATION_RULES

__all__ = [
    "DANGEROUS_TOOL_RULES",
    "INJECTION_RULES",
    "KEYWORD_RULES",
    "OBFUSCATION_RULES",
    "SYSTEM_LEAK_RULES",
]
