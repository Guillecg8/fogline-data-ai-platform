"""The MCP server: the agent's only interface to the store (ADR 0003, ADR 0005).

Run with `python -m commerce_gateway.server`. `COMMERCE_GATEWAY_MANDATE` and `COMMERCE_GATEWAY_DB`
are required. The server resumes or opens the session for the mandate, then serves the 8 tools
over stdio until the client closes stdin.

It uses the SDK's low-level `Server`, which does not validate arguments against the input schemas,
so every argument object reaches `Gateway.call` and is counted and audited there. stdout carries
only protocol traffic; stderr carries only the fixed lines in this module and in `gateway`.
"""

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from commerce_gateway import __version__
from commerce_gateway.clock import SystemClock
from commerce_gateway.gateway import ERROR_REASONS, INTERNAL_ERROR, Gateway, Outcome, ToolResult
from commerce_gateway.mandate import MandateError, load_mandate
from commerce_gateway.sessions import SessionStartRefused, start_session
from commerce_gateway.store import Store, StoreBusy, StoreError, StoreIntegrityError
from commerce_gateway.tools import DESCRIPTIONS, INSTRUCTIONS, TOOL_NAMES, input_schema

SERVER_NAME = "commerce-gateway"
MANDATE_ENV = "COMMERCE_GATEWAY_MANDATE"
DB_ENV = "COMMERCE_GATEWAY_DB"

EXIT_CONFIG = 2
EXIT_STORE = 3
EXIT_SESSION = 4

_REFUSED = "commerce_gateway: refused to start: "
REFUSAL_ENV = _REFUSED + f"{MANDATE_ENV} and {DB_ENV} must be set"
REFUSAL_MANDATE = _REFUSED + "the mandate file is missing or invalid"
REFUSAL_DB = _REFUSED + "the database cannot be opened"
REFUSAL_INTEGRITY = _REFUSED + "store integrity check failed"
REFUSAL_BUSY = _REFUSED + "store busy"
REFUSAL_PENDING = _REFUSED + "a pending checkout must be decided or expire first"
SERVING = "commerce_gateway: serving session {session_id} ({event})"
CONVERSION_FAILED = "commerce_gateway: result conversion failed; the call was recorded"
_EVENT_WORDS = {
    "session_opened": "opened",
    "session_resumed": "resumed",
    "session_superseded": "superseded",
}


class StartRefused(Exception):
    """The server cannot start. Carries the exit code and the fixed stderr line."""

    def __init__(self, code: int, line: str) -> None:
        super().__init__(line)
        self.code = code
        self.line = line


def silence_sdk_logging() -> None:
    """SDK log records can contain exception text, so none of them may reach stderr."""
    logger = logging.getLogger("mcp")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False


def tool_list() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=DESCRIPTIONS[name], input_schema=input_schema(name))
        for name in TOOL_NAMES
    ]


def _as_text(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def to_call_result(result: ToolResult) -> types.CallToolResult:
    """The same object as structured content and as JSON text; isError only for errors."""
    body = result.as_dict()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_as_text(body))],
        structured_content=body,
        is_error=result.outcome is Outcome.ERROR,
    )


# Built once from literals, so returning it cannot fail. The call it answers was already counted
# and audited by `Gateway.call`; what failed is turning its result into a protocol response.
_INTERNAL_ERROR_BODY = {
    "allowed": False,
    "outcome": "error",
    "rule_id": None,
    "error": INTERNAL_ERROR,
    "reason": ERROR_REASONS[INTERNAL_ERROR],
    "data": {},
}
INTERNAL_ERROR_RESULT = types.CallToolResult(
    content=[types.TextContent(type="text", text=_as_text(_INTERNAL_ERROR_BODY))],
    structured_content=_INTERNAL_ERROR_BODY,
    is_error=True,
)


def build_server(gateway: Gateway) -> Server:
    tools = types.ListToolsResult(tools=tool_list())

    async def list_tools(ctx: object, params: object) -> types.ListToolsResult:
        return tools

    async def call_tool(ctx: object, params: types.CallToolRequestParams) -> types.CallToolResult:
        # Raw name and arguments, unvalidated: the gateway validates, counts and audits them.
        # Nothing may raise out of here: the SDK would send the exception text to the client
        # (ADR 0005), and that text can contain arguments or product text.
        try:
            return to_call_result(gateway.call(params.name, params.arguments))
        except Exception:
            print(CONVERSION_FAILED, file=sys.stderr)  # fixed text, never the exception's
            return INTERNAL_ERROR_RESULT

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "")
    if not value:
        raise StartRefused(EXIT_CONFIG, REFUSAL_ENV)
    return Path(value)


def start() -> tuple[Store, Gateway, str]:
    """Load the mandate, open the store and resume or open the session, or refuse."""
    mandate_path, db_path = _required_path(MANDATE_ENV), _required_path(DB_ENV)
    try:
        mandate = load_mandate(mandate_path)
    except (OSError, UnicodeDecodeError, MandateError) as exc:
        raise StartRefused(EXIT_CONFIG, REFUSAL_MANDATE) from exc
    try:
        store = Store.open(db_path)
    except StoreIntegrityError as exc:
        raise StartRefused(EXIT_STORE, REFUSAL_INTEGRITY) from exc
    except StoreBusy as exc:
        raise StartRefused(EXIT_STORE, REFUSAL_BUSY) from exc
    except (StoreError, sqlite3.Error, OSError, ValueError) as exc:
        raise StartRefused(EXIT_STORE, REFUSAL_DB) from exc
    clock = SystemClock()
    try:
        started = start_session(store, mandate, clock)
    except BaseException as exc:
        store.close()
        if isinstance(exc, SessionStartRefused):
            raise StartRefused(EXIT_SESSION, REFUSAL_PENDING) from exc
        if isinstance(exc, StoreIntegrityError):
            raise StartRefused(EXIT_STORE, REFUSAL_INTEGRITY) from exc
        if isinstance(exc, StoreBusy):
            raise StartRefused(EXIT_STORE, REFUSAL_BUSY) from exc
        if isinstance(exc, StoreError | sqlite3.Error):
            raise StartRefused(EXIT_STORE, REFUSAL_DB) from exc
        raise
    event = _EVENT_WORDS[started.event]
    gateway = Gateway(store, started.session_id, clock)
    return store, gateway, SERVING.format(session_id=started.session_id, event=event)


async def _serve(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    silence_sdk_logging()
    try:
        store, gateway, line = start()
    except StartRefused as refused:
        print(refused.line, file=sys.stderr)
        return refused.code
    try:
        print(line, file=sys.stderr)
        anyio.run(_serve, build_server(gateway))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
