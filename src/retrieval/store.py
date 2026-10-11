"""B8 C12 retrieval —— 内存知识库与关键词检索。

## 为什么是关键词检索而不是向量检索

B8 关卡 0 的实测结论是：**这里的主要矛盾不是召回质量，是信任边界**。
一份被投毒的文档能不能被召回出来，和我们用什么检索方式毫无关系；而向量
检索会带来两个实打实的成本 ——

1. 本地门禁（L0/L1）必须**完全离线**，而 embedding 模型是重依赖，只装在
   目标机（决策 D7）。用向量检索就意味着 FT-09 这类核心用例无法离线验证。
2. 检索结果的可解释性。审计要回答"为什么召回了这条"，BM25 的命中词
   可以直接写进证据，而向量相似度给不出这个。

真要上向量检索，实现方应当实现 :class:`~agent_core.ports.RetrieverPort`
并在容器里替换装配 —— 端口就是为此留的。**"为什么这里没用向量库"本身
是个可讲的取舍**：召回不是瓶颈，信任边界才是。

## 中文怎么切词

不引入分词器依赖（``jieba`` 之类），用**字符 bigram**：中文相邻两字构成
一个词元。对银行知识库这种短文本场景，"定期""期存""存款"都能命中，
召回足够；而且零依赖、可离线、完全确定。ASCII 走普通按词切分并小写化。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from agent_core.ports import RetrievalDocument, TrustLevel

#: ASCII 词元
_ASCII_TOKEN = re.compile(r"[A-Za-z0-9]+")

#: BM25 参数。k1 取经典默认值 1.5，b 取 0.75。
_BM25_K1 = 1.5
_BM25_B = 0.75


def tokenize(text: str) -> list[str]:
    """切成检索词元：ASCII 按词、中文按字符 bigram。

    单字中文不单独成词元 —— 单字召回噪声太大（"存"会命中所有含"存"的
    文档），bigram 在召回率和噪声之间更稳。
    """
    lowered = text.lower()
    tokens: list[str] = [_ASCII_TOKEN.match(m.group(0))[0] for m in _ASCII_TOKEN.finditer(lowered)]

    han_runs = re.findall(r"[㐀-䶿一-鿿]+", lowered)
    for run in han_runs:
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class InMemoryKnowledgeBase:
    """BM25 关键词检索（无重依赖、纯内存、可离线测）。

    Args:
        documents: 初始语料。``trust`` 决定命中后的处置方式，见
            :class:`~agent_core.ports.TrustLevel`。

    **语料规模与召回的诚实说明**：BM25 是 O(len(corpus)) 全量扫描，没有
    倒排索引，也没有增量更新。银行演示语料（几十到几百篇）完全够用；
    上到万级应当换成真正的检索引擎 —— 那时瓶颈才会从"信任边界"变成
    "召回延迟"。接口不变，换实现即可。
    """

    name = "in-memory-kb"

    def __init__(self, documents: Sequence[RetrievalDocument] = ()) -> None:
        self._docs: list[RetrievalDocument] = list(documents)
        self._tokens: list[Counter[str]] = []
        self._lengths: list[int] = []
        self._df: Counter[str] = Counter()
        self._index()

    # --- 构建 ---------------------------------------------------------------

    def _index(self) -> None:
        self._tokens = []
        self._lengths = []
        self._df = Counter()
        for doc in self._docs:
            toks = tokenize(f"{doc.title}\n{doc.content}")
            counts = Counter(toks)
            self._tokens.append(counts)
            self._lengths.append(max(1, len(toks)))
            self._df.update(counts.keys())

    def add(self, document: RetrievalDocument) -> None:
        """追加一篇文档并重建索引。"""
        self._docs.append(document)
        self._index()

    @property
    def documents(self) -> tuple[RetrievalDocument, ...]:
        return tuple(self._docs)

    @property
    def size(self) -> int:
        return len(self._docs)

    # --- 检索 ---------------------------------------------------------------

    def _idf(self, term: str) -> float:
        """带平滑的 IDF。``log(1 + (N - df + 0.5)/(df + 0.5))``。

        语料很小时 IDF 可能为负，加 1 保证权重非负 —— 否则常见的词
        （在所有文档里都出现）反而会拉低分数。
        """
        n = len(self._docs)
        df = self._df.get(term, 0)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str, index: int) -> float:
        """BM25 分数。语料为空时返回 0.0。"""
        if not self._docs:
            return 0.0
        counts = self._tokens[index]
        if not counts:
            return 0.0
        avg_len = sum(self._lengths) / len(self._lengths)
        query_terms = Counter(tokenize(query))
        total = 0.0
        for term, q_tf in query_terms.items():
            tf = counts.get(term, 0)
            if not tf:
                continue
            denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * self._lengths[index] / avg_len)
            total += self._idf(term) * (tf * (_BM25_K1 + 1)) / denom * min(q_tf, 3)
        return total

    def search(self, query: str, *, top_k: int = 3) -> tuple[RetrievalDocument, ...]:
        """返回分数最高的 ``top_k`` 篇。**分数为 0 的一律不返回** ——
        一条都不相关时给回一堆"最不相关"的文档，只会让提示词凭空变长。
        """
        if not self._docs or not query.strip():
            return ()
        scored = [(self.score(query, i), i) for i in range(len(self._docs))]
        scored = [(s, i) for s, i in scored if s > 0.0]
        scored.sort(key=lambda pair: (-pair[0], self._docs[pair[1]].doc_id))
        return tuple(self._docs[i] for _, i in scored[:top_k])


def split_by_trust(
    documents: Iterable[RetrievalDocument],
) -> tuple[tuple[RetrievalDocument, ...], tuple[RetrievalDocument, ...]]:
    """拆成 (内部可信, 外部不可信) 两组。"""
    internal: list[RetrievalDocument] = []
    external: list[RetrievalDocument] = []
    for doc in documents:
        if doc.trust is TrustLevel.TRUSTED_INTERNAL:
            internal.append(doc)
        else:
            external.append(doc)
    return tuple(internal), tuple(external)


__all__ = ["InMemoryKnowledgeBase", "split_by_trust", "tokenize"]