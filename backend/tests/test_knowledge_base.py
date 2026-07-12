from qqquestion.knowledge_base import (
    InMemoryKnowledgeBase,
    KnowledgeBaseBuilder,
    split_chunks,
)
from qqquestion.models import Chunk


def test_split_chunks_size_and_overlap():
    text = "あ" * 1200
    chunks = split_chunks(text, size=500, overlap=100)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert chunks[0][-100:] == chunks[1][:100]  # オーバーラップ
    assert "".join(dict.fromkeys(chunks))  # 空チャンクなし


def test_split_chunks_empty():
    assert split_chunks("") == []
    assert split_chunks("   ") == []


def test_inmemory_query_scores_by_overlap(kb):
    results = kb.query("RNN 再帰結合")
    assert results
    assert "再帰結合" in results[0].text


def test_inmemory_query_no_match(kb):
    assert kb.query("量子コンピュータ") == []


class _FakeSearch:
    def __init__(self):
        self.queries = []

    def search(self, query, max_results=4):
        self.queries.append(query)
        return [Chunk(text="検索結果本文" * 50, url="https://example.com", title="t")]


def test_builder_caches_topics(tmp_path):
    kb = InMemoryKnowledgeBase()
    search = _FakeSearch()
    builder = KnowledgeBaseBuilder(kb, search, tmp_path / "cache.json")

    added_first = builder.build_for_topics(["RNN"])
    assert added_first > 0
    assert len(search.queries) == 1

    # 同一トピックは TTL 内なら再検索しない（キャッシュファイル経由でも）
    builder2 = KnowledgeBaseBuilder(kb, search, tmp_path / "cache.json")
    assert builder2.build_for_topics(["RNN"]) == 0
    assert len(search.queries) == 1


def test_builder_without_search_is_noop(tmp_path):
    kb = InMemoryKnowledgeBase()
    builder = KnowledgeBaseBuilder(kb, None, tmp_path / "cache.json")
    assert builder.build_for_topics(["RNN"]) == 0
    assert kb.count() == 0


class _FailingSearch:
    def search(self, query, max_results=4):
        raise RuntimeError("network down")


def test_builder_survives_search_failure(tmp_path):
    kb = InMemoryKnowledgeBase()
    builder = KnowledgeBaseBuilder(kb, _FailingSearch(), tmp_path / "cache.json")
    assert builder.build_for_topics(["RNN"]) == 0  # 例外を飲み込みコアループを止めない
