"""默认策略基线。

这份 YAML 同时是三样东西：
1. 可运行的默认策略（B2 之后 agent 开箱即用的兜底配置）；
2. 单元测试的 fixture（保证每个批次测的都是同一份真实策略，而不是各自造数据）；
3. 仓库里 policy 配置的**模板**，新增 use case 时复制它改阈值即可。

阈值取值参考架构方案 §3.1 / §5.1：
- 规则层高置信命中直接 block（规则层误报率低，可以下手重）；
- 嵌入相似度落在中间地带走 require_approval（不确定就交给人）；
- PII 走 redact（数据要留、但必须脱敏）；
- LLM judge 判定有害才 block（成本高，只在必要时触发）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from guard_contract.policy_schema import PolicySet, policy_set_from_mapping

DEFAULT_POLICY_YAML = """\
# 默认策略 —— 修改前请确认 use_case 与 tenant 是否匹配
version: "1.0.0"
use_case: bank-assistant-demo
tenant: default
# 银行场景默认 fail-closed：检测器不可用时阻断而非静默放行
fail_mode: closed
# 所有规则都未命中时的兜底放行。高安全场景应改成 block。
default_action: allow
# DEGRADED 模式下只信任 L1 规则层。必须显式列出 —— 降级时"还信谁"是风险决策。
degraded_detectors: ["rules.l1"]
description: 基线策略 —— 覆盖 prompt injection / PII / system prompt 泄露 / 危险工具调用

rules:
  # --- 最高优先级：高置信度恶意信号直接阻断 ---
  - name: block_critical_injection
    action: block
    priority: 10
    condition:
      labels: ["prompt_injection", "system_prompt_leak", "harmful_content"]
      min_score: 0.90
      min_confidence: 0.80
    reason: 高置信度恶意内容，阻断并返回预设响应

  # --- 危险工具调用：不允许自动执行 ---
  - name: escalate_dangerous_tool
    action: require_approval
    priority: 20
    condition:
      stages: ["tool"]
      min_score: 0.70
    reason: 工具调用风险偏高，转人工确认

  # --- PII：脱敏后放行，不阻断 ---
  - name: redact_pii
    action: redact
    priority: 30
    condition:
      labels: ["pii_leak"]
      min_score: 0.50
    reason: 命中 PII，替换为占位符后放行

  # --- 中间地带：不确定则升级 ---
  # 注意这里**刻意没有** "什么都匹配就 allow" 的兜底规则。
  # 兜底放行由上面的 default_action 表达 —— 写成 min_score=0.0 的规则会让
  # 任何检测结果都命中规则，从而让 fail_mode 永远走不到（见 PolicySet
  # 的 _reject_trivial_catch_all 校验）。
  - name: escalate_borderline
    action: escalate
    priority: 40
    condition:
      min_score: 0.60
      min_confidence: 0.50
    reason: 置信度不足，放行但标记人工复核
"""


def load_policy_set(source: str | Mapping[str, Any]) -> PolicySet:
    """从 YAML 字符串或已解析的 mapping 加载策略集。"""
    if isinstance(source, Mapping):
        return policy_set_from_mapping(source)
    data = yaml.safe_load(source)
    if not isinstance(data, dict):
        raise ValueError(f"策略文件内容不是映射结构: {type(data).__name__}")
    return policy_set_from_mapping(data)


def load_default_policy() -> PolicySet:
    """加载内置基线策略。"""
    return load_policy_set(DEFAULT_POLICY_YAML)


__all__ = ["DEFAULT_POLICY_YAML", "load_default_policy", "load_policy_set"]
