import asyncio
import json

import pytest
from mcp import Client

from ops_copilot import mcp_server
from ops_copilot.ingestion import pipeline
from ops_copilot.paths import CORPUS_DIR
from ops_copilot.retrieval.embedding import hashing_embedder
from ops_copilot.retrieval.hybrid import HybridRetriever, load_chunks


@pytest.fixture(scope="module", autouse=True)
def offline_retriever(tmp_path_factory):
    """Give the server a retriever that needs no model download."""
    data_dir = tmp_path_factory.mktemp("data")
    pipeline.run(CORPUS_DIR, data_dir)
    chunks, _ = load_chunks(data_dir / "gold" / "chunks.jsonl")
    retriever = HybridRetriever(chunks, hashing_embedder())
    original = mcp_server.get_retriever
    mcp_server.get_retriever = lambda: retriever
    yield
    mcp_server.get_retriever = original


def call(name: str, arguments: dict):
    """Call a tool through a real MCP client connected to the server in-process."""

    async def run():
        async with Client(mcp_server.server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def text_of(result) -> str:
    return " ".join(block.text for block in result.content)


def test_server_lists_its_four_tools():
    async def run():
        async with Client(mcp_server.server) as client:
            return await client.list_tools()

    names = {tool.name for tool in asyncio.run(run()).tools}
    assert names == {"search_documents", "get_document", "list_kpis", "query_kpi"}


def test_search_returns_only_documents_in_force():
    arguments = {"query": "free cancellation hours before arrival", "as_of": "2026-11-10"}
    result = call("search_documents", arguments)
    assert not result.is_error
    hits = result.structured_content["result"]
    assert hits[0]["doc_id"] == "POL-CX-001"
    assert all(hit["version"] == 2 for hit in hits if hit["doc_id"] == "POL-CX-001")


def test_get_document_returns_the_version_for_the_date():
    current = call("get_document", {"doc_id": "pol-cx-001", "as_of": "2026-11-10"})
    past = call("get_document", {"doc_id": "POL-CX-001", "as_of": "2025-06-01"})
    assert current.structured_content["version"] == 2
    assert past.structured_content["version"] == 1


def test_unknown_document_is_an_explained_error():
    result = call("get_document", {"doc_id": "SOP-XX-999"})
    assert result.is_error
    assert "no version of" in text_of(result)


def test_query_kpi_returns_rows():
    arguments = {
        "metric": "occupancy",
        "venue": "fogline-mission",
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
    }
    result = call("query_kpi", arguments)
    assert not result.is_error
    assert len(result.structured_content["rows"]) == 3


def test_sql_sent_as_a_metric_is_rejected_with_a_readable_message():
    arguments = {
        "metric": "revpar; DROP TABLE hotel_daily",
        "venue": "fogline-marina",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    }
    result = call("query_kpi", arguments)
    assert result.is_error
    assert "unknown metric" in text_of(result)


def test_bad_date_is_an_explained_error():
    result = call("search_documents", {"query": "linen", "as_of": "yesterday"})
    assert result.is_error
    assert "must be a date like" in text_of(result)


def test_list_kpis_describes_every_metric():
    result = call("list_kpis", {})
    metrics = {
        item["metric"] for item in json.loads(json.dumps(result.structured_content))["result"]
    }
    assert metrics == {"occupancy", "adr", "revpar", "covers", "average_check"}
