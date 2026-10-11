"""B8 C12 retrieval —— 参考文档检索组件。

检索本身不是安全边界，**但它是不可信内容进入上下文的入口**。本包只负责
"取回哪些文档"与"这些文档是什么信任等级"，判定由 C4/C5 检测器在
``GuardStage.RETRIEVAL`` 上完成（FT-09）。

分层依赖：本包只依赖 :mod:`agent_core` 的端口类型，不依赖任何检测器或
策略层 —— 依赖方向与 C1→C11 一致。
"""

from __future__ import annotations

from retrieval.seed import ALL_DOCS, EXTERNAL_DOCS, INTERNAL_DOCS, build_default_kb
from retrieval.store import InMemoryKnowledgeBase, split_by_trust, tokenize

__all__ = [
    "ALL_DOCS",
    "EXTERNAL_DOCS",
    "INTERNAL_DOCS",
    "InMemoryKnowledgeBase",
    "build_default_kb",
    "split_by_trust",
    "tokenize",
]