"""Run the ingestion: corpus/*.md -> data/silver/documents.jsonl -> data/gold/chunks.jsonl.

Files that fail validation are quarantined with the reason instead of stopping the run.
Problems across documents (duplicates, two active versions) stop the run.
"""

import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from ops_copilot.ingestion.chunk import chunk_document
from ops_copilot.ingestion.models import Document
from ops_copilot.ingestion.parse import ParseError, parse_document, validate_corpus
from ops_copilot.paths import CORPUS_DIR, DATA_DIR


class CorpusError(RuntimeError):
    """The corpus breaks a rule that involves more than one document."""


def _write_jsonl(path: Path, rows: list[BaseModel]) -> str:
    """Write one JSON object per line and return the SHA-256 of the file content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(row.model_dump_json() + "\n" for row in rows)
    path.write_text(content, encoding="utf-8", newline="\n")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def run(corpus_dir: Path = CORPUS_DIR, data_dir: Path = DATA_DIR) -> dict:
    documents: list[Document] = []
    quarantined: list[dict[str, str]] = []
    for path in sorted(corpus_dir.glob("*.md")):
        try:
            documents.append(parse_document(path))
        except ParseError as exc:
            quarantined.append({"source_file": path.name, "reason": str(exc)})

    problems = validate_corpus(documents)
    if problems:
        raise CorpusError("; ".join(problems))

    chunks = [chunk for document in documents for chunk in chunk_document(document)]
    manifest = {
        "documents": len(documents),
        "active_documents": sum(d.meta.status == "active" for d in documents),
        "chunks": len(chunks),
        "quarantined": quarantined,
        "documents_sha256": _write_jsonl(data_dir / "silver" / "documents.jsonl", documents),
        "chunks_sha256": _write_jsonl(data_dir / "gold" / "chunks.jsonl", chunks),
    }
    (data_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def main() -> int:
    manifest = run()
    print(
        f"documents={manifest['documents']} active={manifest['active_documents']} "
        f"chunks={manifest['chunks']} quarantined={len(manifest['quarantined'])}"
    )
    for item in manifest["quarantined"]:
        print(f"  quarantined {item['source_file']}: {item['reason']}")
    return 1 if manifest["quarantined"] else 0


if __name__ == "__main__":
    sys.exit(main())
