"""ML 检测器包（C5）。

批次范围（开发方案 §3.2）：

- L2 :class:`PresidioPiiDetector` —— 通用 NER PII（Presidio + spacy，D2）
- L3 :class:`EmbeddingSimilarityDetector` —— 语义注入检测（sentence-transformers，D1）
- L4 :class:`LLMJudgeDetector` —— LLM 边界判定（MiniMax，D3）
- :class:`DetectorRegistry` —— 分层触发注册表（后层只在前层未命中时触发）
- :mod:`detector_ml.timeout` —— 真实墙钟超时执行（B2 承诺随本批次落地）

重依赖只在目标机 venv 安装（``--extra ml``，决策 D7）；本地保持轻量，
用 Fake 注入 + skipif 测契约（见 tests/unit/test_detector_ml.py）。
"""

from detector_ml.embedders import (
    DEFAULT_MODEL_NAME,
    MODELS_HOME_ENV,
    SentenceEmbedder,
)
from detector_ml.embedding_similarity import (
    ATTACK_SAMPLES,
    DEFAULT_THRESHOLD,
    LABEL_INJECTION,
    SAMPLES_VERSION,
    EmbeddingSimilarityDetector,
)
from detector_ml.indirect_injection import (
    CONTENT_INJECTION_JUDGES,
    DEFAULT_MIN_CONFIDENCE,
    RETRIEVAL_JUDGE_PROMPT,
    IndirectInjectionDetector,
)
from detector_ml.indirect_injection import (
    PROMPT_VERSION as RETRIEVAL_PROMPT_VERSION,
)
from detector_ml.llm_judge import JUDGE_SYSTEM_PROMPT, JUDGE_VERSION, LLMJudgeDetector
from detector_ml.presidio_pii import (
    EN_MODEL,
    LABEL_PII,
    LANG_EN,
    LANG_ZH,
    PRESIDIO_VERSION,
    SUPPORTED_LANGUAGES,
    ZH_MODEL,
    PresidioPiiDetector,
    detect_language,
    is_cjk_fragment,
    is_surname_initial,
    looks_like_false_positive,
    passes_shape_filter,
)
from detector_ml.registry import DetectorRegistry
from detector_ml.timeout import run_detector_with_timeout

__all__ = [
    "ATTACK_SAMPLES",
    "CONTENT_INJECTION_JUDGES",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_THRESHOLD",
    "DEFAULT_MIN_CONFIDENCE",
    "EN_MODEL",
    "EmbeddingSimilarityDetector",
    "JUDGE_SYSTEM_PROMPT",
    "JUDGE_VERSION",
    "LABEL_INJECTION",
    "LABEL_PII",
    "LANG_EN",
    "LANG_ZH",
    "LLMJudgeDetector",
    "MODELS_HOME_ENV",
    "PRESIDIO_VERSION",
    "PresidioPiiDetector",
    "RETRIEVAL_JUDGE_PROMPT",
    "RETRIEVAL_PROMPT_VERSION",
    "SAMPLES_VERSION",
    "SUPPORTED_LANGUAGES",
    "SentenceEmbedder",
    "ZH_MODEL",
    "DetectorRegistry",
    "IndirectInjectionDetector",
    "detect_language",
    "is_cjk_fragment",
    "is_surname_initial",
    "looks_like_false_positive",
    "passes_shape_filter",
    "run_detector_with_timeout",
]
