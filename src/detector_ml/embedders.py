"""嵌入端口的生产实现（sentence-transformers，D1）。

:class:`SentenceEmbedder` 是 :class:`~agent_core.ports.EmbedderPort` 的
生产实现，加载 D1 部署的 bge-base-zh-v1.5（768 维，中文）。

**加载路径策略**（与 infra/models 的 D1 约定对齐）：

1. ``local_dir`` 显式指定（测试 / 部署注入）；
2. 环境变量 ``MODELS_HOME``（infra/models/env.template 定义）——
   目标机 D1 下载目录下按 ``MODELS_HOME/BAAI/bge-base-zh-v1.5`` 布局；
3. 兜底：直接按 HF 仓库名加载（联网，生产不推荐）。

重依赖（torch / sentence-transformers）只在目标机 venv 存在（``--extra ml``，
决策 D7）。本地未安装时构造抛
:class:`~agent_core.errors.DependencyNotInstalledError`，指引 D1 安装路径
—— "跳过是可见的，不静默"（与 Presidio 检测器同一纪律）。
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_core.errors import DependencyNotInstalledError
from agent_core.ports import EmbedderPort

#: 与 infra/models/manifest.json 的 D1 声明一致
DEFAULT_MODEL_NAME = "BAAI/bge-base-zh-v1.5"
MODELS_HOME_ENV = "MODELS_HOME"


class SentenceEmbedder(EmbedderPort):
    """sentence-transformers 嵌入实现。

    Args:
        model_name: 模型名或本地目录。默认 bge-base-zh-v1.5（D1）。
        local_dir: 本地模型目录（优先于 model_name 的远程解析）。
    """

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL_NAME,
        local_dir: str | Path | None = None,
    ) -> None:
        # 依赖检查在构造时完成：容器装配时本地未装 sentence-transformers
        # 会立即抛 DependencyNotInstalledError，L3 检测器随之被跳过
        # （与 Presidio 同款"跳过是可见的"纪律）。
        try:
            import sentence_transformers  # noqa: F401, PLC0415
        except ImportError as exc:
            raise DependencyNotInstalledError(
                "嵌入检测需要 sentence-transformers（目标机 --extra ml + "
                "infra/models/setup.sh install 提供；本地仅跑契约测试）。"
            ) from exc
        self._model_name = model_name
        self._local_dir = _resolve_local_dir(local_dir)
        # 模型在**构造期**加载，不是惰性：L3 检测器的 detect 有 5s 墙钟
        # （run_detector_with_timeout），而 bge-base 首次加载要读 ~400MiB
        # 权重（CPU 上 2-4s）——惰性加载会让**第一次** detect 必超时。
        # 构造发生在容器装配时（build_container），不受检测墙钟约束；
        # 代价是启动慢几秒，换来每次 detect 都只做推理（~1s 内）。
        self._model = None  # _load 的哨兵：先占位再预热
        self._model = self._load()

    @property
    def name(self) -> str:
        return f"sentence:{self._model_name}"

    @property
    def dimension(self) -> int:
        return 768

    def _load(self) -> object:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415

            path = str(self._local_dir) if self._local_dir is not None else self._model_name
            self._model = SentenceTransformer(path)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in vectors]


def _resolve_local_dir(local_dir: str | Path | None) -> Path | None:
    """D1 集成：优先显式目录，其次 MODELS_HOME 下的标准布局。"""
    if local_dir is not None:
        return Path(local_dir)
    home = os.environ.get(MODELS_HOME_ENV)
    if home:
        candidate = Path(home) / DEFAULT_MODEL_NAME
        if candidate.is_dir():
            return candidate
    return None


__all__ = ["DEFAULT_MODEL_NAME", "MODELS_HOME_ENV", "SentenceEmbedder"]
