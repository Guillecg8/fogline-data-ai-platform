"""Hybrid search: BM25 and vector rankings merged with Reciprocal Rank Fusion."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import chromadb
from chromadb.api import ClientAPI
from rank_bm25 import BM25Okapi

from ops_copilot.ingestion.models import Chunk
from ops_copilot.retrieval.embedding import Embedder, tokenize

Mode = Literal["hybrid", "bm25", "vector"]
OPEN_ENDED = 99991231  # effective_to stored for documents with no end date
COLLECTION = "fogline_chunks"


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float
    bm25_rank: int | None
    vector_rank: int | None


def load_chunks(path: Path) -> tuple[list[Chunk], str]:
    """Read the gold chunks and return them with the SHA-256 of the file."""
    raw = path.read_bytes()
    chunks = [Chunk.model_validate_json(line) for line in raw.decode("utf-8").splitlines() if line]
    return chunks, hashlib.sha256(raw).hexdigest()


def is_valid(chunk: Chunk, as_of: date) -> bool:
    """A chunk can be used on a date only if its document was in force on that date."""
    return chunk.effective_from <= as_of and (
        chunk.effective_to is None or as_of <= chunk.effective_to
    )


def _as_int(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


class HybridRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        embedder: Embedder,
        client: ClientAPI | None = None,
        fingerprint: str = "",
    ) -> None:
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self._ids = list(self._chunks)
        self._embedder = embedder
        self._bm25 = BM25Okapi([tokenize(c.embedding_text) for c in chunks])
        if client is None:
            # In-memory clients share state inside one process: use a name nobody else has.
            client, name = chromadb.EphemeralClient(), f"{COLLECTION}_{uuid.uuid4().hex}"
        else:
            name = COLLECTION
        self._collection = self._open_collection(client, name, fingerprint)

    def _open_collection(self, client: ClientAPI, name: str, fingerprint: str):
        """Reuse the stored vectors only if they are complete and built from the same chunks."""
        existing = {c.name for c in client.list_collections()}
        if name in existing:
            collection = client.get_collection(name, embedding_function=None)
            metadata = collection.metadata or {}
            same_source = bool(fingerprint) and metadata.get("chunks_sha256") == fingerprint
            if same_source and collection.count() == len(self._ids):
                return collection
            client.delete_collection(name)

        # Embed first: if the model fails, no half-built collection is left behind.
        chunks = list(self._chunks.values())
        embeddings = self._embedder([c.embedding_text for c in chunks])
        collection = client.create_collection(
            name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "chunks_sha256": fingerprint},
        )
        collection.add(
            ids=self._ids,
            embeddings=embeddings,
            metadatas=[
                {
                    "effective_from": _as_int(c.effective_from),
                    "effective_to": _as_int(c.effective_to) if c.effective_to else OPEN_ENDED,
                }
                for c in chunks
            ],
        )
        return collection

    def _bm25_ranking(self, query: str, as_of: date, depth: int) -> list[str]:
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(zip(self._ids, scores, strict=True), key=lambda pair: -pair[1])
        valid = [i for i, score in ranked if score > 0 and is_valid(self._chunks[i], as_of)]
        return valid[:depth]

    def _vector_ranking(self, query: str, as_of: date, depth: int) -> list[str]:
        day = _as_int(as_of)
        result = self._collection.query(
            query_embeddings=self._embedder([query]),
            n_results=min(depth, len(self._ids)),
            where={"$and": [{"effective_from": {"$lte": day}}, {"effective_to": {"$gte": day}}]},
            include=[],
        )
        return result["ids"][0]

    def search(
        self,
        query: str,
        k: int = 5,
        as_of: date | None = None,
        mode: Mode = "hybrid",
        depth: int = 20,
    ) -> list[SearchHit]:
        as_of = as_of or date.today()
        bm25 = self._bm25_ranking(query, as_of, depth) if mode != "vector" else []
        vector = self._vector_ranking(query, as_of, depth) if mode != "bm25" else []
        fused = reciprocal_rank_fusion([bm25, vector])
        top = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))[:k]
        return [
            SearchHit(
                chunk=self._chunks[chunk_id],
                score=score,
                bm25_rank=bm25.index(chunk_id) + 1 if chunk_id in bm25 else None,
                vector_rank=vector.index(chunk_id) + 1 if chunk_id in vector else None,
            )
            for chunk_id, score in top
        ]
