"""对外端口（Protocol）定义。

这一层是"本地能跑单元测试"这个设计目标的技术前提：
所有会走网络、走 GPU、走外部进程的依赖，在本项目里都只以 Protocol 出现。
生产代码注入真实实现，测试注入 :mod:`tests.fakes` 里的确定性实现。

这样做换来的三件事：
1. 单元测试完全离线，不需要 VPN、API key 或 GPU；
2. 组件之间可以任意替换实现而不改调用方代码；
3. "观测边界"在类型上就是显式的 —— 拿不到的服务端指标不会被误当成可观测项。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次模型调用的 token 用量。

    这是 ``llm_call`` span 上最核心的成本口径字段，也是"遥测在调用侧闭环"
    的具体载体 —— MiniMax 是外部 SaaS，服务端指标不可得，用量只能从这里取。
    ``cached_prompt_tokens`` 是 prompt 缓存命中数（MiniMax 返回
    ``usage.prompt_tokens_details.cached_tokens``），差异化计费需要它：
    命中部分按 cache_read 优惠单价，未命中部分按标准输入单价。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("token 用量不能为负数")
        if self.cached_prompt_tokens < 0:
            raise ValueError("缓存命中 token 不能为负数")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求调用某个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具的对外声明（OpenAI 兼容的 tools 参数结构）。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    #: assistant 消息的工具调用声明（OpenAI/MiniMax 兼容协议要求：
    #: 后续 ``role=tool`` 结果消息必须能在此找到匹配的 tool_call_id，
    #: 否则 API 拒绝请求 —— 400 "tool result's tool id not found"）
    tool_calls: tuple[ToolCall, ...] = ()

    ROLE_SYSTEM = "system"
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_TOOL = "tool"

    def __post_init__(self) -> None:
        if self.role not in {self.ROLE_SYSTEM, self.ROLE_USER, self.ROLE_ASSISTANT, self.ROLE_TOOL}:
            raise ValueError(f"未知 role: {self.role!r}")
        if self.tool_calls and self.role != self.ROLE_ASSISTANT:
            raise ValueError("tool_calls 只允许出现在 assistant 消息上")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    messages: tuple[LLMMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    temperature: float = 1.0
    max_tokens: int | None = None
    stream: bool = False

    def __post_init__(self) -> None:
        # MiniMax 官方文档：temperature 取值范围 [0, 2]
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(f"temperature 必须在 [0, 2] 区间，当前 {self.temperature}")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    model: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = TokenUsage()
    finish_reason: str = "stop"
    response_id: str = ""


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------


class TrustLevel(Enum):
    """参考文档的信任等级（B8 信任边界）。"""

    #: 本行自建知识库。命中注入规则只打标记、不拦截 —— 内部文档常合法地
    #: **讨论**注入攻击（例如安全公告里引用攻击话术）。
    TRUSTED_INTERNAL = "trusted_internal"
    #: 用户上传、第三方 API 等外部来源。命中即拦截。
    UNTRUSTED_EXTERNAL = "untrusted_external"


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    """一条被检索出来的参考文档。

    **它不是可信输入。** 检索文档会被拼进 LLM 上下文，因此**携带指令的文档
    构成间接注入**（B8 关卡 0 实测：纯正则扫描对这类文档误报 50%、检出
    仅 25%）。所以 ``trust`` 不是装饰字段，而是安全决策的一等输入：

    - ``TRUSTED_INTERNAL`` —— 本行自建知识库。内容里出现注入话术通常是在
      **描述/防范**这类攻击（实测《防范提示词注入安全公告》会被 L1 正则
      直接 block），所以命中只打标记、不拦截。
    - ``UNTRUSTED_EXTERNAL`` —— 用户上传、第三方 API。命中即拦截。

    信任边界的**固有代价**必须说清楚：内部库同样可能被投毒。所以 ``trust``
    只降低误报，不提供"内部文档一定安全"的保证 —— 真正的兜底是检索内容
    的不可信标记与输出侧复检。
    """

    doc_id: str
    title: str
    content: str
    trust: TrustLevel = TrustLevel.TRUSTED_INTERNAL
    #: 来源标识，审计要能回答"这条内容从哪来"
    source: str = "kb"

    def __post_init__(self) -> None:
        if not self.doc_id:
            raise ValueError("doc_id 不能为空")


@runtime_checkable
class RetrieverPort(Protocol):
    """知识检索端口（B8）。

    实现方：生产用 :class:`retrieval.store.InMemoryKnowledgeBase`（内存
    关键词检索，无重依赖、可离线测）；测试注入 Fake。
    """

    name: str

    def search(self, query: str, *, top_k: int = 3) -> tuple[RetrievalDocument, ...]:
        """按相关度返回前 ``top_k`` 条。**永远不要抛异常**，检索失败应当
        表现为空结果 —— 抛错会被编排当成不可恢复故障，而"没检索到"是可
        降级的正常状态。"""
        ...


@runtime_checkable
class LLMPort(Protocol):
    """大模型后端端口。

    实现方在 B5 批次提供（``MinimaxLLM``），测试方提供 ``FakeLLM``。
    """

    name: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        """发起一次非流式补全。失败时抛 :class:`~agent_core.errors.LLMError` 子类。"""
        ...


@runtime_checkable
class EmbedderPort(Protocol):
    """嵌入向量端口。L3 语义相似度检测器依赖它。"""

    name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化，返回与输入等长的向量列表。"""
        ...


@runtime_checkable
class SpanPort(Protocol):
    """单个 span。

    刻意做窄：只暴露埋点真正需要的操作。

    **必须是上下文管理器**。理由不是方便，而是 §4.5 要求的 span 树
    （``input_guard`` 下挂 ``pii_check``、``generation_step_1`` 下挂
    ``tool_call_search``）靠的就是 ``with`` 的嵌套语义 —— 不引入"当前 span"
    这类状态，父子关系就无从表达。

    更关键的是异常路径：手工 ``start`` / ``end`` 时，一旦中间抛异常，
    ``end()`` 永远不会执行，span 悬着不关（内存泄漏，且这条 trace 残缺）。
    上下文管理器保证"一定关闭"，这是正确性问题，不是风格问题。
    """

    def set_attribute(self, key: str, value: Any) -> None: ...
    def record_exception(self, exc: BaseException) -> None: ...
    def end(self) -> None: ...
    def __enter__(self) -> SpanPort: ...
    def __exit__(self, *exc_info: object) -> None: ...


@runtime_checkable
class TelemetryPort(Protocol):
    """可观测性端口。

    嵌套由 ``with`` 的自然嵌套表达，实现方各自维护"当前 span"
    （OTel 用 contextvar，内存版用栈）。端口刻意不暴露 set_current_span ——
    那是实现细节，暴露了反而会诱导业务代码手动管理 span 生命周期。

    ``shutdown`` 不是可选的礼貌方法：批量导出的实现会把 span 攒在内存队列里
    由后台线程异步发送，进程退出时如果不显式 flush，队列里最后那几条
    （往往正是出事那几条）就跟着进程一起没了。
    """

    def start_span(self, name: str, **attributes: Any) -> SpanPort: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class Clock(Protocol):
    """时间来源。

    抽出来是为了可测试性：所有涉及超时/退避/保留期的断言都用注入的假时钟，
    禁止在测试里 ``sleep``，否则测试要么变慢要么变成 flaky。
    """

    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


__all__ = [
    "Clock",
    "EmbedderPort",
    "LLMMessage",
    "LLMPort",
    "LLMRequest",
    "LLMResponse",
    "RetrievalDocument",
    "RetrieverPort",
    "SpanPort",
    "TelemetryPort",
    "TokenUsage",
    "ToolCall",
    "ToolSpec",
    "TrustLevel",
]
