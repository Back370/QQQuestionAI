"""知識ベース: Web検索 → チャンク分割 → ベクトルDB格納（architecture.md §4.2）。

- 検索: Tavily（TAVILY_API_KEY があれば）→ ddgs へフォールバック → 無ければ空
- 格納: ChromaDB 永続化モード。import できない環境ではキーワード一致の
  InMemoryKnowledgeBase にフォールバック
- 同一トピックは TTL 30日でキャッシュし再検索しない

検索もDBも使えない環境では query() が空を返すだけで、
出題・判定のコアループは止まらない（引用なしの劣化モード）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from .models import Chunk

CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
CACHE_TTL_SECONDS = 30 * 24 * 3600


def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """500字・オーバーラップ100字の単純なチャンク分割。"""
    if not text:
        return []
    step = max(1, size - overlap)
    chunks = []
    for start in range(0, len(text), step):
        piece = text[start : start + size]
        if piece.strip():
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int = 4) -> list[Chunk]: ...


class TavilySearch:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def search(self, query: str, max_results: int = 4) -> list[Chunk]:
        import httpx

        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._api_key,
                "query": query,
                "max_results": max_results,
                "include_raw_content": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        chunks = []
        for item in response.json().get("results", []):
            text = item.get("raw_content") or item.get("content") or ""
            if text:
                chunks.append(
                    Chunk(text=text, url=item.get("url", ""), title=item.get("title", ""))
                )
        return chunks


class DuckDuckGoSearch:
    # ddgs はメタ検索ライブラリで、backend の既定値 "auto" は text カテゴリの全エンジン
    # （brave / google / startpage / yandex / yahoo / mojeek / wikipedia 等）に問い合わせる。
    # 送信先は README の「外部に送信されるデータ」で申告している以上、既定に任せると
    # 申告が実態と食い違う（利用者は DuckDuckGo だけのつもりで9社に送ることになる）。
    # 送信先を1つに固定するため backend を明示する。
    BACKEND = "duckduckgo"

    def search(self, query: str, max_results: int = 4) -> list[Chunk]:
        from ddgs import DDGS

        chunks = []
        with DDGS() as ddgs:
            for item in ddgs.text(query, max_results=max_results, backend=self.BACKEND):
                body = item.get("body") or ""
                if body:
                    chunks.append(
                        Chunk(text=body, url=item.get("href", ""), title=item.get("title", ""))
                    )
        return chunks


def create_search_provider() -> SearchProvider | None:
    import os

    if os.environ.get("QQQ_NO_SEARCH") == "1":
        return None
    if os.environ.get("TAVILY_API_KEY"):
        return TavilySearch(os.environ["TAVILY_API_KEY"])
    try:
        import ddgs  # noqa: F401

        return DuckDuckGoSearch()
    except ImportError:
        return None


class KnowledgeBase(Protocol):
    def add(self, topic: str, chunks: list[Chunk]) -> None: ...

    def query(self, text: str, k: int = 4) -> list[Chunk]: ...

    def count(self) -> int: ...


class InMemoryKnowledgeBase:
    """語の重なりでスコアリングする簡易ストア（テスト・フォールバック用）。"""

    def __init__(self):
        self._chunks: list[Chunk] = []

    def add(self, topic: str, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._chunks.append(chunk.model_copy(update={"topic": topic}))

    def query(self, text: str, k: int = 4) -> list[Chunk]:
        terms = set(text.lower().split()) | {text.lower()}
        scored = []
        for chunk in self._chunks:
            haystack = (chunk.text + " " + chunk.topic + " " + chunk.title).lower()
            score = sum(1 for term in terms if term and term in haystack)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: -pair[0])
        return [chunk for _, chunk in scored[:k]]

    def count(self) -> int:
        return len(self._chunks)


class ChromaKnowledgeBase:
    """ChromaDB 永続化モード。メタデータに URL・タイトル・取得日時を保持。"""

    def __init__(self, persist_dir: str):
        import chromadb

        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection("qqquestion_kb")

    def add(self, topic: str, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        now = time.time()
        base = self._collection.count()
        self._collection.add(
            ids=[f"{topic}-{now:.0f}-{base + i}" for i in range(len(chunks))],
            documents=[chunk.text for chunk in chunks],
            metadatas=[
                {"topic": topic, "url": chunk.url, "title": chunk.title, "fetched_at": now}
                for chunk in chunks
            ],
        )

    def query(self, text: str, k: int = 4) -> list[Chunk]:
        if self._collection.count() == 0:
            return []
        result = self._collection.query(query_texts=[text], n_results=k)
        chunks = []
        for doc, meta in zip(result["documents"][0], result["metadatas"][0]):
            chunks.append(
                Chunk(
                    text=doc,
                    url=meta.get("url", ""),
                    title=meta.get("title", ""),
                    topic=meta.get("topic", ""),
                )
            )
        return chunks

    def count(self) -> int:
        return self._collection.count()


def create_knowledge_base(data_dir: str) -> KnowledgeBase:
    try:
        return ChromaKnowledgeBase(str(Path(data_dir) / "chroma"))
    except Exception:
        return InMemoryKnowledgeBase()


class KnowledgeBaseBuilder:
    """トピックごとに Web検索→分割→格納。キャッシュで再検索を抑止する。"""

    def __init__(
        self,
        kb: KnowledgeBase,
        search: SearchProvider | None,
        cache_path: str | Path,
        ttl: float = CACHE_TTL_SECONDS,
    ):
        self._kb = kb
        self._search = search
        self._cache_path = Path(cache_path)
        self._ttl = ttl
        self._cache: dict[str, float] = self._load_cache()

    def _load_cache(self) -> dict[str, float]:
        if self._cache_path.exists():
            try:
                return json.loads(self._cache_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, ensure_ascii=False))

    def build_for_topics(self, topics: list[str]) -> int:
        """新規取得したチャンク数を返す。失敗したトピックはスキップ。"""
        if self._search is None:
            return 0
        added = 0
        now = time.time()
        for topic in topics:
            if now - self._cache.get(topic, 0) < self._ttl:
                continue
            try:
                results = self._search.search(f"{topic} とは 仕組み 解説")
            except Exception:
                continue
            chunks = []
            for result in results:
                for piece in split_chunks(result.text):
                    chunks.append(
                        Chunk(text=piece, url=result.url, title=result.title, topic=topic)
                    )
            self._kb.add(topic, chunks)
            added += len(chunks)
            self._cache[topic] = now
        self._save_cache()
        return added
