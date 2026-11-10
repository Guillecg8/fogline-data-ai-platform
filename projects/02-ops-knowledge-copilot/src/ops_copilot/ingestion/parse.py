"""Bronze -> silver: read a Markdown file and validate its front matter."""

import re
from pathlib import Path

import yaml

from ops_copilot.ingestion.models import Document, DocumentMeta

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class ParseError(ValueError):
    """The file cannot be turned into a valid Document."""


def parse_document(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise ParseError("missing YAML front matter")
    try:
        fields = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ParseError(f"invalid YAML front matter: {exc}") from exc
    if not isinstance(fields, dict):
        raise ParseError("front matter is not a key-value mapping")
    try:
        meta = DocumentMeta.model_validate(fields)
    except ValueError as exc:
        raise ParseError(str(exc)) from exc
    body = match.group(2).strip()
    if not body:
        raise ParseError("document has no body")
    return Document(meta=meta, body=body, source_file=path.name)


def validate_corpus(documents: list[Document]) -> list[str]:
    """Checks that involve more than one document. Returns a list of problems."""
    problems: list[str] = []
    keys = [d.meta.doc_key for d in documents]
    for key in sorted({k for k in keys if keys.count(k) > 1}):
        problems.append(f"{key}: duplicated document version")

    active: dict[str, list[int]] = {}
    for d in documents:
        if d.meta.status == "active":
            active.setdefault(d.meta.doc_id, []).append(d.meta.version)
    for doc_id, versions in sorted(active.items()):
        if len(versions) > 1:
            problems.append(f"{doc_id}: more than one active version {sorted(versions)}")

    known_keys = set(keys)
    known_ids = {d.meta.doc_id for d in documents}
    for d in documents:
        if d.meta.supersedes and d.meta.supersedes not in known_keys:
            problems.append(f"{d.meta.doc_key}: supersedes unknown {d.meta.supersedes}")
        if d.meta.amends and d.meta.amends not in known_ids:
            problems.append(f"{d.meta.doc_key}: amends unknown {d.meta.amends}")
    return problems
