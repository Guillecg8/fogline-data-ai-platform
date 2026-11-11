"""Start the MCP server as a subprocess and call its tools, as a real MCP client would.

uv run poe mcp-demo
"""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

CALLS = [
    ("search_documents", {"query": "fee for late check-out", "k": 2}),
    ("get_document", {"doc_id": "MEMO-2026-021"}),
    ("list_kpis", {}),
    (
        "query_kpi",
        {
            "metric": "revpar",
            "venue": "fogline-marina",
            "start_date": "2026-07-01",
            "end_date": "2026-10-31",
        },
    ),
    (
        "query_kpi",
        {
            "metric": "revpar; DROP TABLE hotel_daily",
            "venue": "x",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
    ),
]


async def run() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "ops_copilot.mcp_server"])
    async with Client(params) as client:
        tools = await client.list_tools()
        print("tools:", ", ".join(tool.name for tool in tools.tools))
        for name, arguments in CALLS:
            result = await client.call_tool(name, arguments)
            status = "ERROR" if result.is_error else "ok"
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            print(f"\n> {name}({json.dumps(arguments)}) -> {status}")
            print("  " + text[:400].replace("\n", " "))


if __name__ == "__main__":
    asyncio.run(run())
