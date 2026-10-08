"""测试专用 Fakes —— 让 L0/L1 完全离线且确定。

这一层是"本地能跑单元测试"这个目标的实际承载物。三个设计约束：

1. **绝不发真实网络请求**。conftest 默认禁用 socket，任何越界尝试会立刻失败，
   而不是让测试结果依赖外网状态。
2. **绝不依赖真实时间**。用 :class:`FrozenClock` 代替 sleep / time.time()，
   超时、退避、保留期相关的断言才可能稳定。
3. **绝不用 mock 打桩计数代替真实行为**。:class:`FakeLLM` 用**预置响应队列**
   驱动，因此测试能覆盖"模型要求调工具 → 执行工具 → 带着结果再问模型"
   这种多轮状态转移，而不只是验证"HTTP 被调用了一次"。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core.errors import LLMError
from agent_core.ports import LLMRequest, LLMResponse, TokenUsage, ToolCall
from guard_contract.enums import GuardStage
from guard_contract.result import DetectorResult

# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


class FrozenClock:
    """可控时钟。满足 :class:`~agent_core.ports.Clock` 协议。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> FrozenClock:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds
        return self

    def advance_days(self, days: int) -> FrozenClock:
        return self.advance(days * 86400)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


@dataclass
class FakeLLM:
    """按队列返回预置响应的 LLM。

    Args:
        responses: 依次返回的响应。队列耗尽后返回最后一条（便于测试多轮循环）。
        raise_after: 前 N 次调用正常，之后抛 ``LLMError``（测重试/降级路径）。
    """

    name: str = "fake-llm"
    responses: list[LLMResponse] = field(default_factory=list)
    calls: list[LLMRequest] = field(default_factory=list)
    raise_after: int | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        index = len(self.calls)
        self.calls.append(request)

        if self.raise_after is not None and index >= self.raise_after:
            raise LLMError(f"fake failure at call #{index}")

        if not self.responses:
            # 没有预置响应时做确定性的回声，保证测试不会因为忘配而变脆
            last = request.messages[-1].content if request.messages else ""
            return LLMResponse(
                model=request.model,
                content=f"echo: {last}",
                usage=TokenUsage(prompt_tokens=len(last.split()), completion_tokens=3),
            )

        if index >= len(self.responses):
            return self.responses[-1]
        return self.responses[index]

    @property
    def call_count(self) -> int:
        return len(self.calls)


def make_response(
    content: str = "ok",
    *,
    tool_calls: Sequence[ToolCall] = (),
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    finish_reason: str = "stop",
) -> LLMResponse:
    """构造 :class:`LLMResponse` 的便捷工厂。"""
    return LLMResponse(
        model="fake-model",
        content=content,
        tool_calls=tuple(tool_calls),
        usage=TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        finish_reason=finish_reason,
    )


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


@dataclass
class FakeEmbedder:
    """确定性嵌入器。

    ``vectors`` 里显式给出的文本使用指定向量（用于构造"已知相似/已知不相似"
    的样本对）；未给出的按内容哈希生成固定维度向量，保证同输入永远同输出。
    """

    name: str = "fake-embedder"
    dimension: int = 8
    vectors: dict[str, list[float]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        return [self._one(t) for t in batch]

    def _one(self, text: str) -> list[float]:
        if text in self.vectors:
            return list(self.vectors[text])
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 for i in range(self.dimension)]
        norm = sum(v * v for v in raw) ** 0.5 or 1.0
        return [v / norm for v in raw]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度。L3 检测器的判定逻辑要用它，必须在测试里可直接调用。"""
    if len(a) != len(b):
        raise ValueError("维度不一致")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@dataclass
class RecordedSpan:
    """记录下来的 span。"""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    exceptions: list[str] = field(default_factory=list)
    ended: bool = False


class InMemorySpan:
    def __init__(self, recorded: RecordedSpan) -> None:
        self._recorded = recorded

    def set_attribute(self, key: str, value: Any) -> None:
        self._recorded.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self._recorded.exceptions.append(f"{type(exc).__name__}: {exc}")

    def end(self) -> None:
        self._recorded.ended = True

    def __enter__(self) -> InMemorySpan:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.end()


@dataclass
class InMemoryTelemetry:
    """内存版遥测端口。满足 :class:`~agent_core.ports.TelemetryPort`。"""

    spans: list[RecordedSpan] = field(default_factory=list)

    def start_span(self, name: str, **attributes: Any) -> InMemorySpan:
        recorded = RecordedSpan(name=name, attributes=dict(attributes))
        self.spans.append(recorded)
        return InMemorySpan(recorded)

    def span_names(self) -> list[str]:
        return [s.name for s in self.spans]

    def find(self, name: str) -> list[RecordedSpan]:
        return [s for s in self.spans if s.name == name]

    def assert_all_ended(self) -> None:
        unfinished = [s.name for s in self.spans if not s.ended]
        if unfinished:
            raise AssertionError(f"存在未结束的 span: {unfinished}")


# ---------------------------------------------------------------------------
# DetectorResult 工厂
# ---------------------------------------------------------------------------


def make_result(
    *,
    detector: str = "rules.l1",
    version: str = "1.0.0",
    stage: GuardStage = GuardStage.INPUT,
    label: str = "safe",
    score: float = 0.0,
    confidence: float = 0.9,
    evidence: Sequence[str] = (),
    latency_ms: float = 1.0,
    metadata: Mapping[str, Any] | None = None,
) -> DetectorResult:
    """构造 :class:`DetectorResult` 的便捷工厂。

    默认值是"安全且无害"的基线结果，测试里只写与基线不同的字段，
    避免每个用例重复六行样板。
    """
    return DetectorResult(
        detector=detector,
        version=version,
        stage=stage,
        label=label,
        score=score,
        confidence=confidence,
        evidence=tuple(evidence),
        latency_ms=latency_ms,
        metadata=dict(metadata or {}),
    )


# ---------------------------------------------------------------------------
# 检测器桩
# ---------------------------------------------------------------------------


@dataclass
class StubDetector:
    """按输入查表返回预置结果的检测器。

    Args:
        results: 命中时返回的结果。None 表示该检测器从不命中。
        raises: 命中时抛出的异常（用于测 TIMEOUT / ERROR 降级路径）。
        labels: 命中判定用的标签集合；为空则"总是命中"。
    """

    name: str = "stub"
    version: str = "1.0.0"
    supported_stages: frozenset[GuardStage] = frozenset(GuardStage)
    labels: frozenset[str] = frozenset()
    results: tuple[DetectorResult, ...] | None = None
    raises: BaseException | None = None
    #: 记录每次调用，便于断言"降级时被忽略的检测器确实没跑"
    calls: list[tuple[str, GuardStage]] = field(default_factory=list)

    def detect(self, text: str, *, stage: GuardStage) -> tuple[DetectorResult, ...]:
        self.calls.append((text, stage))
        if self.raises is not None:
            raise self.raises
        if self.results is None:
            return ()
        if self.labels and not any(r.label in self.labels for r in self.results):
            return ()
        return self.results

    @property
    def call_count(self) -> int:
        return len(self.calls)


__all__ = [
    "FakeEmbedder",
    "FakeLLM",
    "FrozenClock",
    "InMemorySpan",
    "InMemoryTelemetry",
    "RecordedSpan",
    "StubDetector",
    "cosine",
    "make_response",
    "make_result",
]
