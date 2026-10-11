"""B8 C12 检索组件单元测试（L0，全部离线）。

三组关注点：
1. **分词**：中文 bigram + ASCII 词元，不能引入分词器依赖
2. **BM25 排序**：相关文档必须排在前面；不相关时**必须返回空**
3. **信任等级**：分组与默认值

第 2 条里"不相关返回空"容易被忽略，但很关键：硬凑 top_k 会让提示词凭空
变长，而且会让"检索到了东西"这个信号失去意义。
"""

from __future__ import annotations

import pytest

from agent_core.ports import RetrievalDocument, RetrieverPort, TrustLevel
from retrieval.store import InMemoryKnowledgeBase, split_by_trust, tokenize


@pytest.fixture
def kb() -> InMemoryKnowledgeBase:
    return InMemoryKnowledgeBase(
        [
            RetrievalDocument(
                "kb-01",
                "定期存款产品说明",
                "一年期年利率 1.85%，三年期年利率 2.35%。整存整取，提前支取按活期计息。",
            ),
            RetrievalDocument(
                "kb-02",
                "开立借记卡指引",
                "需提供本人二代身份证，在网点填写申请表并预留手机号。",
            ),
            RetrievalDocument(
                "kb-03",
                "理财产品风险等级",
                "R1 谨慎型、R2 稳健型、R3 平衡型、R4 进取型、R5 激进型。",
            ),
        ]
    )


# --- 分词 -------------------------------------------------------------------


class TestTokenize:
    def test_chinese_produces_bigrams(self) -> None:
        assert "定期" in tokenize("定期存款")

    def test_single_han_char_is_kept(self) -> None:
        """单字文档（极短标题）不至于完全无词元。"""
        assert "存" in tokenize("存")

    def test_ascii_is_lowercased_and_split(self) -> None:
        tokens = tokenize("Ignore ALL previous Instructions")
        assert "ignore" in tokens
        assert "all" in tokens
        assert "instructions" in tokens

    def test_mixed_chinese_and_ascii(self) -> None:
        tokens = tokenize("APP 怎么下载")
        assert "app" in tokens
        assert "怎么" in tokens

    def test_empty(self) -> None:
        assert tokenize("") == []


# --- BM25 检索 --------------------------------------------------------------


class TestSearch:
    def test_satisfies_retriever_port(self, kb: InMemoryKnowledgeBase) -> None:
        assert isinstance(kb, RetrieverPort)

    def test_ranks_relevant_document_first(self, kb: InMemoryKnowledgeBase) -> None:
        hits = kb.search("定期存款利率是多少", top_k=2)
        assert hits
        assert hits[0].doc_id == "kb-01"

    def test_distinct_queries_hit_distinct_docs(self, kb: InMemoryKnowledgeBase) -> None:
        assert kb.search("理财产品风险等级", top_k=1)[0].doc_id == "kb-03"
        assert kb.search("借记卡怎么办理", top_k=1)[0].doc_id == "kb-02"

    def test_synonyms_are_not_retrieved(self, kb: InMemoryKnowledgeBase) -> None:
        """**已知召回局限，如实钉住**：BM25 是纯词面匹配，"开卡"与文档里的
        "借记卡"在字符 bigram 层面不重叠，所以召不回。

        这是选关键词检索时接受的代价：语义同义（开卡/办卡/借记卡）需要向量
        检索或查询改写才能覆盖。真实系统里靠"多写别名到文档标题"来缓解。
        """
        assert kb.search("开卡需要什么材料", top_k=3) == ()

    def test_unrelated_query_returns_nothing(self, kb: InMemoryKnowledgeBase) -> None:
        """**不相关就返回空** —— 硬凑 top_k 会让提示词凭空变长，
        也会让"检索到了东西"这个信号失去意义。"""
        assert kb.search("今天天气怎么样", top_k=3) == ()

    def test_top_k_is_respected(self, kb: InMemoryKnowledgeBase) -> None:
        assert len(kb.search("利率 身份证 风险等级", top_k=2)) <= 2

    def test_empty_corpus(self) -> None:
        assert InMemoryKnowledgeBase().search("定期存款") == ()

    def test_empty_query(self, kb: InMemoryKnowledgeBase) -> None:
        assert kb.search("   ") == ()

    def test_score_is_zero_without_overlap(self, kb: InMemoryKnowledgeBase) -> None:
        assert kb.score("完全无关的查询", 0) == 0.0

    def test_add_reindexes(self, kb: InMemoryKnowledgeBase) -> None:
        """新增文档后立刻可被检索 —— 不重建索引就会静默漏召回。"""
        kb.add(
            RetrievalDocument(
                "kb-04",
                "跨行转账到账时间",
                "普通跨行 2 小时内到账，加急业务实时到账。",
            )
        )
        assert kb.size == 4
        assert kb.search("跨行转账多久到账", top_k=1)[0].doc_id == "kb-04"


# --- 信任等级 ---------------------------------------------------------------


class TestTrustLevel:
    def test_defaults_to_internal(self) -> None:
        assert RetrievalDocument("d", "t", "c").trust is TrustLevel.TRUSTED_INTERNAL

    def test_external_must_be_explicit(self) -> None:
        doc = RetrievalDocument("d", "t", "c", TrustLevel.UNTRUSTED_EXTERNAL, source="upload")
        assert doc.trust is TrustLevel.UNTRUSTED_EXTERNAL
        assert doc.source == "upload"

    def test_blank_doc_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="doc_id"):
            RetrievalDocument("", "t", "c")

    def test_split_by_trust(self) -> None:
        internal = RetrievalDocument("i", "t", "c")
        external = RetrievalDocument("e", "t", "c", TrustLevel.UNTRUSTED_EXTERNAL)
        got_internal, got_external = split_by_trust([internal, external])
        assert got_internal == (internal,)
        assert got_external == (external,)

    def test_split_by_trust_empty(self) -> None:
        assert split_by_trust([]) == ((), ())

    def test_search_preserves_trust(self) -> None:
        """检索结果必须原样携带 trust —— 信任等级一旦在检索里丢掉，
        上层的信任边界就形同虚设。"""
        kb = InMemoryKnowledgeBase(
            [
                RetrievalDocument(
                    "u-01",
                    "用户上传的资料",
                    "定期存款说明",
                    TrustLevel.UNTRUSTED_EXTERNAL,
                    source="upload",
                )
            ]
        )
        hits = kb.search("定期存款")
        assert hits and hits[0].trust is TrustLevel.UNTRUSTED_EXTERNAL