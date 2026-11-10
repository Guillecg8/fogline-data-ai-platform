from datetime import date

import chromadb
import pytest

from ops_copilot.ingestion import pipeline
from ops_copilot.paths import CORPUS_DIR
from ops_copilot.retrieval.embedding import hashing_embedder
from ops_copilot.retrieval.hybrid import (
    COLLECTION,
    HybridRetriever,
    is_valid,
    load_chunks,
    reciprocal_rank_fusion,
)

TODAY = date(2026, 11, 10)


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    pipeline.run(CORPUS_DIR, data_dir)
    return load_chunks(data_dir / "gold" / "chunks.jsonl")


@pytest.fixture(scope="module")
def retriever(gold) -> HybridRetriever:
    chunks, fingerprint = gold
    return HybridRetriever(chunks, hashing_embedder(), fingerprint=fingerprint)


def keys(hits) -> list[str]:
    return [f"{hit.chunk.doc_id}@v{hit.chunk.version}" for hit in hits]


def test_rrf_rewards_agreement_between_rankings():
    scores = reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
    assert max(scores, key=scores.get) == "b"


@pytest.mark.parametrize("mode", ["hybrid", "bm25", "vector"])
def test_finds_the_late_checkout_fee(retriever, mode):
    hits = retriever.search("What is the fee for late check-out?", as_of=TODAY, mode=mode)
    assert hits[0].chunk.doc_id == "SOP-FD-001"
    assert "$45" in hits[0].chunk.text


def test_every_hit_was_in_force_on_the_date(retriever):
    for query in ["free cancellation before arrival", "linen change", "produce deliveries"]:
        hits = retriever.search(query, k=10, as_of=TODAY)
        assert hits
        assert all(is_valid(hit.chunk, TODAY) for hit in hits)


def test_current_cancellation_policy_is_version_2(retriever):
    hits = retriever.search("free cancellation hours before arrival", as_of=TODAY)
    assert "POL-CX-001@v2" in keys(hits)
    assert "POL-CX-001@v1" not in keys(hits)


def test_asking_about_the_past_returns_the_old_version(retriever):
    hits = retriever.search("free cancellation hours before arrival", as_of=date(2025, 6, 1))
    assert "POL-CX-001@v1" in keys(hits)
    assert "POL-CX-001@v2" not in keys(hits)


def test_temporary_memo_only_applies_inside_its_dates(retriever):
    query = "check-in time at Fogline Marina during renovation"
    during = retriever.search(query, k=10, as_of=TODAY)
    after = retriever.search(query, k=10, as_of=date(2026, 12, 15))
    assert "MEMO-2026-014@v1" in keys(during)
    assert "MEMO-2026-014@v1" not in keys(after)


def test_expired_contract_is_not_returned(retriever):
    hits = retriever.search("produce supplier delivery days minimum order", k=10, as_of=TODAY)
    assert "CTR-SUP-005@v1" in keys(hits)
    assert "CTR-SUP-004@v1" not in keys(hits)


def test_failed_build_does_not_leave_an_empty_index(gold, tmp_path):
    chunks, fingerprint = gold
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))

    def broken_embedder(texts):
        raise ConnectionError("model download failed")

    with pytest.raises(ConnectionError):
        HybridRetriever(chunks, broken_embedder, client, fingerprint)
    assert COLLECTION not in {c.name for c in client.list_collections()}

    retriever = HybridRetriever(chunks, hashing_embedder(), client, fingerprint)
    assert retriever.search("late check-out fee", as_of=TODAY, mode="vector")


def test_incomplete_index_is_rebuilt(gold, tmp_path):
    chunks, fingerprint = gold
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    client.create_collection(COLLECTION, metadata={"chunks_sha256": fingerprint})

    retriever = HybridRetriever(chunks, hashing_embedder(), client, fingerprint)
    assert retriever.search("late check-out fee", as_of=TODAY, mode="vector")
