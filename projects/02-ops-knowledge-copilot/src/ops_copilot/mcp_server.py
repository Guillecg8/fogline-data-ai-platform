"""MCP server: exposes Fogline's documents and KPIs as tools for any MCP client.

All tools are read-only. Run it over stdio with:

    uv run poe mcp
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from functools import cache
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ops_copilot import kpi
from ops_copilot.net import use_system_certificates
from ops_copilot.retrieval.hybrid import HybridRetriever, is_valid

MAX_RESULTS = 10

server = MCPServer(
    "fogline-ops-copilot",
    log_level="WARNING",
    instructions=(
        "Read-only access to Fogline Hospitality Group's operational documents (procedures, "
        "policies, supplier contracts, memos) and daily KPIs. Search results contain only "
        "documents in force on the requested date. Cite doc_id and version when you answer."
    ),
)


@cache
def get_retriever() -> HybridRetriever:
    """Built on first use, so the server starts fast and tests can replace it."""
    from ops_copilot.retrieval.cli import build_retriever

    return build_retriever()


@contextmanager
def _explained_errors() -> Iterator[None]:
    """Turn bad-input errors into messages the client can read and correct.

    Any other exception is a bug: the SDK logs it and hides its details from the client.
    """
    try:
        yield
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _parse_date(value: str | None, name: str) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-11-10, got {value!r}") from None


def _chunk_payload(chunk) -> dict[str, object]:
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "version": chunk.version,
        "doc_type": chunk.doc_type,
        "title": chunk.title,
        "section": chunk.section,
        "venue": chunk.venue,
        "effective_from": chunk.effective_from.isoformat(),
        "effective_to": chunk.effective_to.isoformat() if chunk.effective_to else None,
        "text": chunk.text,
    }


@server.tool()
def search_documents(query: str, as_of: str | None = None, k: int = 5) -> list[dict[str, object]]:
    """Search Fogline's documents and return the most relevant sections.

    Only documents in force on `as_of` (YYYY-MM-DD, default today) are returned, so superseded
    versions and expired contracts never appear. Use a past date to ask how things were then.
    """
    with _explained_errors():
        day = _parse_date(as_of, "as_of")
    hits = get_retriever().search(query, k=max(1, min(k, MAX_RESULTS)), as_of=day)
    return [_chunk_payload(hit.chunk) for hit in hits]


@server.tool()
def get_document(doc_id: str, as_of: str | None = None) -> dict[str, object]:
    """Return every section of one document, in the version in force on `as_of`.

    `doc_id` looks like SOP-FD-001, POL-CX-001, CTR-SUP-001 or MEMO-2026-014.
    """
    with _explained_errors():
        day = _parse_date(as_of, "as_of")
    chunks = [
        chunk
        for chunk in get_retriever().chunks
        if chunk.doc_id == doc_id.strip().upper() and is_valid(chunk, day)
    ]
    if not chunks:
        raise ToolError(f"no version of {doc_id!r} was in force on {day}")
    first = chunks[0]
    return {
        "doc_id": first.doc_id,
        "version": first.version,
        "title": first.title,
        "doc_type": first.doc_type,
        "venue": first.venue,
        "effective_from": first.effective_from.isoformat(),
        "effective_to": first.effective_to.isoformat() if first.effective_to else None,
        "sections": [{"section": chunk.section, "text": chunk.text} for chunk in chunks],
    }


@server.tool()
def list_kpis() -> list[dict[str, object]]:
    """List the KPIs that `query_kpi` can return, with their definition, unit and venues."""
    return kpi.list_kpis()


@server.tool()
def query_kpi(
    metric: str,
    venue: str,
    start_date: str,
    end_date: str,
    granularity: Literal["day", "week", "month", "total"] = "month",
) -> dict[str, object]:
    """Return one KPI for one venue over a date range (dates as YYYY-MM-DD, at most 366 days).

    Call `list_kpis` first to see the valid metrics and venues. This tool does not accept SQL.
    """
    with _explained_errors():
        return kpi.query_kpi(
            metric.strip().lower(),
            venue.strip().lower(),
            _parse_date(start_date, "start_date"),
            _parse_date(end_date, "end_date"),
            granularity,
        )


def main() -> None:
    use_system_certificates()
    server.run()


if __name__ == "__main__":
    main()
