"""L2 PII 检测器（Presidio）。

与 C4 正则 PII 的分工（pii.py 的 docstring 已约定）：C4 主打中国本地化实体
（身份证 / 手机号 / 银行卡，带校验位），Presidio 主打**通用 NER**（人名 /
组织 / 邮箱 / 地址……）。两者共用 label ``pii_leak``，policy 层无需区分，
审计通过 detector 名（``pii.presidio`` vs ``rules.l1``）区分谁判的。

**evidence 刻意不落明文**：Presidio 识别的是人名 / 组织这类**正则脱敏器
（redactor）兜不住**的实体 —— 如果把原文片段写进 evidence，C8 账本会变成
一个人名数据库。因此 evidence 用掩码形式（``PERSON:张***``），分数保留，
审计侧无需再依赖正则脱敏。

重依赖策略（决策 D2 / D7）：presidio-analyzer + spacy 只在**目标机 venv**
安装（``--extra ml`` + ``infra/spacy/setup.sh install``）。本地未安装时
构造即抛 :class:`~agent_core.errors.DependencyNotInstalledError` 并指引
安装路径 —— "跳过是可见的，不静默"。
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


def _mask(fragment: str) -> str:
    """把实体原文压成 `首字符 + ***`，不落明文。"""
    if not fragment:
        return "***"
    return f"{fragment[0]}***"


def _load_analyzer(language: str) -> Any:
    """惰性加载 Presidio 分析引擎（重依赖只在目标机存在）。"""
    try:
        from presidio_analyzer import AnalyzerEngine  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 目标机依赖，本地不触发
        raise DependencyNotInstalledError(
            "Presidio 检测器需要 presidio-analyzer 与 spacy。"
            "目标机部署时由 --extra ml + infra/spacy/setup.sh install 提供；"
            "本地仅跑契约测试（Fake 注入）。"
        ) from exc

    try:
        # AnalyzerEngine 默认用 spacy en_core_web_lg；模型缺失时构造会失败
        return AnalyzerEngine()
    except (Exception, SystemExit) as exc:  # pragma: no cover - 同上，本地不触发
        # 必须连 SystemExit 一起捕获：presidio 发现模型缺失时会在内部调
        # `spacy.cli.download`（spacy 3.8 走 uv pip install），下载失败时
        # 它抛的是 SystemExit（BaseException）而不是 Exception ——
        # 只捕获 Exception 会让它在离线环境（如 L0 测试）直接杀掉进程。
        # 模型未部署是"检测器不可用"的业务状态，不是进程级故障。
        raise DependencyNotInstalledError(
            "Presidio 初始化失败（spacy 模型未就绪？"
            f"先跑 infra/spacy/setup.sh install）：{type(exc).__name__}"
        ) from exc


class PresidioPiiDetector:
    """通用 NER PII 检测器（L2）。

    Args:
        language: Presidio 识别语言（默认 ``en``，覆盖英文实体）。
        score_threshold: Presidio 的命中分数阈值，默认 0.35（其推荐值）。
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
        language: str = "en",
        score_threshold: float = 0.35,
        timeout_ms: int | None = 2000,
        analyzer: Any | None = None,
    ) -> None:
        self._language = language
        self._score_threshold = score_threshold
        self._timeout_ms = timeout_ms
        self._analyzer = analyzer if analyzer is not None else _load_analyzer(language)

    def detect(self, text: str, *, stage: GuardStage) -> tuple[DetectorResult, ...]:
        if not text:
            return ()

        def worker() -> tuple[DetectorResult, ...]:
            started = time.perf_counter()
            hits = self._analyzer.analyze(
                text=text,
                language=self._language,
                score_threshold=self._score_threshold,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return tuple(
                DetectorResult(
                    detector=self.name,
                    version=self.version,
                    stage=stage,
                    label=LABEL_PII,
                    score=min(hit.score, 1.0),
                    confidence=hit.score,
                    evidence=(f"{hit.entity_type}:{_mask(text[hit.start : hit.end])}",),
                    latency_ms=elapsed_ms,
                    metadata={"entity": hit.entity_type},
                )
                for hit in hits
            )

        return run_detector_with_timeout(
            worker, timeout_ms=self._timeout_ms, detector_name=self.name
        )


__all__ = ["LABEL_PII", "PRESIDIO_VERSION", "PresidioPiiDetector"]
