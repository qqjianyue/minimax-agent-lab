"""统一异常层级。

约定：
- 所有本项目异常都继承 :class:`AgentCoreError`，调用方可以只捕获这一个基类。
- 只有继承 :class:`TransientError` 的异常才允许被重试策略捕获重放。
  把"可重试"建模成类型而不是布尔值，是为了让 L5 重试逻辑无法误伤
  确定性失败（如响应格式错误）——重试一个必然再次失败的请求只是浪费额度。
"""

from __future__ import annotations


class AgentCoreError(Exception):
    """本项目所有异常的根。"""


class ConfigurationError(AgentCoreError):
    """配置缺失或非法。通常在进程启动阶段抛出，属于 fail-fast。"""


class VersionError(AgentCoreError):
    """版本信息无法解析或格式不符预期。"""


class DependencyNotInstalledError(AgentCoreError):
    """某个可选重依赖未安装（例如 presidio / sentence-transformers）。"""


class TransientError(AgentCoreError):
    """瞬时故障，允许重试。"""


class LLMError(AgentCoreError):
    """大模型调用相关错误的基类。"""


class LLMTimeoutError(LLMError, TransientError):
    """调用超时。可重试。"""


class LLMRateLimitError(LLMError, TransientError):
    """触发限流。可重试。"""


class LLMServerError(LLMError, TransientError):
    """上游返回 5xx。可重试。

    刻意同时继承 :class:`LLMError` 与 :class:`TransientError`：
    只继承后者会让它漏出 ``except LLMError`` 的捕获，导致 5xx 根本不进重试逻辑
    ——一个"看起来配了重试、实际 5xx 一次都不重"的隐蔽缺陷。
    """


class LLMResponseError(LLMError):
    """响应结构不合法。不可重试 —— 重放同一请求不会得到合法响应。"""


__all__ = [
    "AgentCoreError",
    "ConfigurationError",
    "DependencyNotInstalledError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
    "TransientError",
    "VersionError",
]
