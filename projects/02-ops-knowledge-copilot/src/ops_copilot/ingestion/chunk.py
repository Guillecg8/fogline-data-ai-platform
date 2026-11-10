"""Silver -> gold: split each document into section-based chunks."""

import re

from ops_copilot.ingestion.models import Chunk, Document

_H1 = re.compile(r"^# .*\n?", re.MULTILINE)
_H2 = re.compile(r"^## +(.+)$", re.MULTILINE)


def split_sections(body: str) -> list[tuple[str, str]]:
    """Return (section title, section text) pairs, one per `##` heading."""
    body = _H1.sub("", body, count=1).strip()
    parts = _H2.split(body)
    sections = [("Overview", parts[0].strip())] if parts[0].strip() else []
    for title, text in zip(parts[1::2], parts[2::2], strict=True):
        if text.strip():
            sections.append((title.strip(), text.strip()))
    return sections


def _pack_paragraphs(text: str, max_words: int) -> list[str]:
    """Group paragraphs into pieces of at most max_words. A paragraph is never cut."""
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for paragraph in re.split(r"\n\s*\n", text):
        words = len(paragraph.split())
        if current and count + words > max_words:
            pieces.append("\n\n".join(current))
            current, count = [], 0
        current.append(paragraph.strip())
        count += words
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def chunk_document(document: Document, max_words: int = 200) -> list[Chunk]:
    meta = document.meta
    chunks: list[Chunk] = []
    for section, text in split_sections(document.body):
        for piece in _pack_paragraphs(text, max_words):
            chunks.append(
                Chunk(
                    chunk_id=f"{meta.doc_key}#{len(chunks):02d}",
                    doc_id=meta.doc_id,
                    version=meta.version,
                    status=meta.status,
                    doc_type=meta.doc_type,
                    title=meta.title,
                    section=section,
                    department=meta.department,
                    venue=meta.venue,
                    effective_from=meta.effective_from,
                    effective_to=meta.effective_to,
                    source_file=document.source_file,
                    text=piece,
                    n_words=len(piece.split()),
                )
            )
    return chunks
