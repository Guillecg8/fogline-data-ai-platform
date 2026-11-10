"""Search the corpus from the terminal.

uv run poe search "late check-out fee"
uv run poe search "free cancellation" --as-of 2025-06-01 --mode bm25
"""

import argparse
from datetime import date

import chromadb

from ops_copilot.net import use_system_certificates
from ops_copilot.paths import DATA_DIR
from ops_copilot.retrieval.embedding import local_embedder
from ops_copilot.retrieval.hybrid import HybridRetriever, load_chunks


def build_retriever() -> HybridRetriever:
    chunks_file = DATA_DIR / "gold" / "chunks.jsonl"
    if not chunks_file.exists():
        raise SystemExit("No chunks found. Run first: uv run poe ingest")
    chunks, fingerprint = load_chunks(chunks_file)
    client = chromadb.PersistentClient(path=str(DATA_DIR / "chroma"))
    return HybridRetriever(chunks, local_embedder(), client, fingerprint)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search Fogline's documents.")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--mode", choices=["hybrid", "bm25", "vector"], default="hybrid")
    args = parser.parse_args()

    use_system_certificates()
    hits = build_retriever().search(args.query, k=args.k, as_of=args.as_of, mode=args.mode)
    print(f"query={args.query!r} as_of={args.as_of} mode={args.mode}")
    for position, hit in enumerate(hits, start=1):
        ranks = f"bm25={hit.bm25_rank or '-'} vector={hit.vector_rank or '-'}"
        print(f"\n{position}. {hit.chunk.chunk_id}  [{hit.chunk.section}]  {ranks}")
        print(f"   {hit.chunk.text[:160]}")


if __name__ == "__main__":
    main()
