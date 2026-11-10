from pathlib import Path

import pytest

from ops_copilot.ingestion import pipeline
from ops_copilot.ingestion.chunk import chunk_document, split_sections
from ops_copilot.ingestion.parse import ParseError, parse_document
from ops_copilot.paths import CORPUS_DIR

VALID = """---
doc_id: SOP-XX-001
title: "Test Procedure"
doc_type: sop
version: {version}
status: {status}
effective_from: 2026-01-01
effective_to: {effective_to}
department: operations
venue: all-venues
synthetic: true
---

# Test Procedure

## Purpose
Explain the test.

## Steps
First paragraph of the steps.

Second paragraph of the steps.
"""


def write_doc(folder: Path, name: str, version=1, status="active", effective_to="null") -> Path:
    path = folder / name
    text = VALID.format(version=version, status=status, effective_to=effective_to)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_reads_front_matter_and_body(tmp_path):
    document = parse_document(write_doc(tmp_path, "a.md"))
    assert document.meta.doc_key == "SOP-XX-001@v1"
    assert document.body.startswith("# Test Procedure")


def test_parse_accepts_windows_line_endings(tmp_path):
    path = tmp_path / "crlf.md"
    path.write_bytes(VALID.format(version=1, status="active", effective_to="null").encode())
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert parse_document(path).meta.doc_id == "SOP-XX-001"


def test_parse_rejects_missing_front_matter(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("# No metadata here\n", encoding="utf-8")
    with pytest.raises(ParseError, match="front matter"):
        parse_document(path)


def test_superseded_document_needs_an_end_date(tmp_path):
    with pytest.raises(ParseError, match="effective_to"):
        parse_document(write_doc(tmp_path, "a.md", status="superseded"))


def test_sections_follow_h2_headings():
    sections = split_sections("# Title\n\n## One\nalpha\n\n## Two\nbeta")
    assert sections == [("One", "alpha"), ("Two", "beta")]


def test_chunks_carry_metadata_and_stable_ids(tmp_path):
    document = parse_document(write_doc(tmp_path, "a.md"))
    chunks = chunk_document(document)
    assert [c.chunk_id for c in chunks] == ["SOP-XX-001@v1#00", "SOP-XX-001@v1#01"]
    assert chunks[1].section == "Steps"
    assert chunks[1].embedding_text.startswith("Test Procedure > Steps\n")


def test_long_sections_split_on_paragraphs(tmp_path):
    document = parse_document(write_doc(tmp_path, "a.md"))
    chunks = chunk_document(document, max_words=6)
    assert [c.text for c in chunks if c.section == "Steps"] == [
        "First paragraph of the steps.",
        "Second paragraph of the steps.",
    ]


def test_pipeline_quarantines_bad_files_and_keeps_going(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_doc(corpus, "good.md")
    (corpus / "bad.md").write_text("no front matter", encoding="utf-8")
    manifest = pipeline.run(corpus, tmp_path / "data")
    assert manifest["documents"] == 1
    assert [q["source_file"] for q in manifest["quarantined"]] == ["bad.md"]


def test_pipeline_stops_on_two_active_versions(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_doc(corpus, "v1.md", version=1)
    write_doc(corpus, "v2.md", version=2)
    with pytest.raises(pipeline.CorpusError, match="more than one active version"):
        pipeline.run(corpus, tmp_path / "data")


def test_pipeline_output_is_deterministic(tmp_path):
    first = pipeline.run(CORPUS_DIR, tmp_path / "one")
    second = pipeline.run(CORPUS_DIR, tmp_path / "two")
    assert first["chunks_sha256"] == second["chunks_sha256"]


def test_fogline_corpus_is_clean(tmp_path):
    manifest = pipeline.run(CORPUS_DIR, tmp_path / "data")
    assert manifest["documents"] == 32
    assert manifest["quarantined"] == []
