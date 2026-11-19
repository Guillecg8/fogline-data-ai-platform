"""End to end over a real stdio connection to a spawned server process (ADR 0005).

The server is spawned with `sys.executable -m commerce_gateway.server`, never a venv script, so
the tests run the same on Windows CI. Two clients are used: the SDK's `ClientSession`, as a real
agent client would, and a minimal raw JSON-RPC client for what the SDK client cannot send
(omitted arguments, non-object arguments) and for killing the process mid-session.
"""

import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import anyio
import pytest
from mcp import Client, ClientSession, StdioServerParameters, stdio_client

from commerce_gateway import server
from commerce_gateway.catalogue import load_catalogue
from commerce_gateway.tools import DESCRIPTIONS, INSTRUCTIONS, TOOL_NAMES, input_schema

MANDATE = """\
version: 1
session_label: "server-test-1"
budget_cents: 20000
approval_threshold_cents: 5000
allowed_categories: [beverages, cleaning, dairy, dry_goods, kitchen_supplies, produce]
price_lock_ttl_seconds: 1800
max_approval_requests: 2
tool_call_budget: 200
"""
MODULE = ["-m", "commerce_gateway.server"]
TIMEOUT_S = 60
SERVING = re.compile(r"commerce_gateway: serving session [0-9a-f]{32} \((opened|resumed)\)")
NO_ARGUMENT_TOOLS = ("get_policy", "view_cart", "request_checkout", "get_checkout_status")


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    mandate = tmp_path / "mandate.yaml"
    mandate.write_text(MANDATE, encoding="utf-8")
    return {"mandate": mandate, "db": tmp_path / "gateway.db", "stderr": tmp_path / "stderr.txt"}


def _env(paths: dict[str, Path]) -> dict[str, str]:
    return {server.MANDATE_ENV: str(paths["mandate"]), server.DB_ENV: str(paths["db"])}


def _scalar(db: Path, sql: str, params: tuple = ()) -> object:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()[0][0]
    finally:
        conn.close()


def _counters(db: Path) -> tuple[int, int]:
    """(tool_calls_used of the open session, audit rows)."""
    calls = _scalar(db, "SELECT tool_calls_used FROM sessions WHERE closed_at_ms IS NULL")
    return calls, _scalar(db, "SELECT count(*) FROM audit")


def _stderr_lines(paths: dict[str, Path]) -> list[str]:
    return paths["stderr"].read_text(encoding="utf-8").splitlines()


async def _session_calls(paths: dict[str, Path], calls: list[tuple[str, object]]) -> dict:
    """Run `calls` through the SDK client in one server process; return tools and results."""
    params = StdioServerParameters(command=sys.executable, args=MODULE, env=_env(paths))
    with paths["stderr"].open("a", encoding="utf-8") as errlog, anyio.fail_after(TIMEOUT_S):
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = (await session.list_tools()).tools
                results = [await session.call_tool(name, args) for name, args in calls]
    return {"init": init, "tools": tools, "results": results}


def run_session(paths: dict[str, Path], calls: list[tuple[str, object]]) -> dict:
    return anyio.run(_session_calls, paths, calls)


class RawClient:
    """Newline-delimited JSON-RPC over the server's stdin and stdout, nothing else."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self._errlog = paths["stderr"].open("a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, *MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._errlog,
            env={**os.environ, **_env(paths)},
            encoding="utf-8",
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self.stdout_lines: list[str] = []  # every line the server wrote, in order
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            self._lines.put(line)

    def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: object) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        while True:
            message = json.loads(self._lines.get(timeout=TIMEOUT_S))
            if message.get("id") == self._next_id:
                return message

    def initialize(self) -> None:
        params = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "raw-test-client", "version": "0"},
        }
        assert "result" in self.request("initialize", params)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, params: object) -> dict:
        return self.request("tools/call", params)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=TIMEOUT_S)
        self._reader.join(timeout=TIMEOUT_S)
        self._errlog.close()

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=TIMEOUT_S)
        self._errlog.close()


@pytest.fixture
def raw(paths):
    clients: list[RawClient] = []

    def start() -> RawClient:
        client = RawClient(paths)
        client.initialize()
        clients.append(client)
        return client

    yield start
    for client in clients:
        if client.proc.poll() is None:
            client.close()


def _structured(result) -> dict:
    """The structured result, checked against its text copy and its isError flag."""
    body = result.structured_content
    assert json.loads(result.content[0].text) == body
    assert result.is_error is (body["outcome"] == "error")
    return body


def test_list_tools_is_exactly_the_contract(paths):
    run = run_session(paths, [])
    assert [tool.name for tool in run["tools"]] == list(TOOL_NAMES)
    for tool in run["tools"]:
        assert tool.description == DESCRIPTIONS[tool.name]
        assert tool.input_schema == input_schema(tool.name)
        assert tool.output_schema is None
    assert run["init"].instructions == INSTRUCTIONS


MEGABYTE = "x" * 1_000_000
HOSTILE_CALLS = [
    ("add_to_cart", {"sku": "FP-0001", "quantity": "abc"}),
    ("get_product", {"sku": 123}),
    ("buy_everything_now", {}),
    ("search_products", {"query": MEGABYTE}),
]


def test_malformed_calls_reach_the_gateway_and_are_counted_and_audited(paths):
    run = run_session(paths, HOSTILE_CALLS)
    for result in run["results"]:
        body = _structured(result)
        assert (body["outcome"], body["error"], body["allowed"]) == (
            "error",
            "invalid_arguments",
            False,
        )
        assert "xxxxxxxxxx" not in result.content[0].text
    assert _counters(paths["db"]) == (len(HOSTILE_CALLS), 1 + len(HOSTILE_CALLS))
    conn = sqlite3.connect(paths["db"])
    rows = conn.execute(
        "SELECT tool, length(payload) FROM audit WHERE event = 'invalid_arguments' ORDER BY seq"
    ).fetchall()
    conn.close()
    assert [tool for tool, _ in rows] == ["add_to_cart", "get_product", None, "search_products"]
    assert all(size < 400 for _, size in rows)
    [line] = _stderr_lines(paths)
    assert SERVING.fullmatch(line)


@pytest.mark.parametrize("tool", NO_ARGUMENT_TOOLS)
def test_no_argument_tools_accept_omitted_null_and_empty_arguments(raw, paths, tool):
    client = raw()
    for params in (
        {"name": tool},
        {"name": tool, "arguments": None},
        {"name": tool, "arguments": {}},
    ):
        body = client.call(params)["result"]["structuredContent"]
        assert body["error"] in (None, "cart_empty")  # request_checkout on an empty cart
    extra = client.call({"name": tool, "arguments": {"note": "hi"}})["result"]
    assert extra["isError"] is True
    assert extra["structuredContent"]["error"] == "invalid_arguments"
    client.close()
    assert _counters(paths["db"]) == (4, 5)
    assert _scalar(paths["db"], "SELECT count(*) FROM audit WHERE event = 'invalid_arguments'") == 1


@pytest.mark.parametrize(
    "params",
    [
        {"name": "view_cart", "arguments": "abc"},
        {"name": "add_to_cart", "arguments": ["FP-0001", 1]},
        {"name": 123, "arguments": {}},
        {"arguments": {}},
    ],
    ids=["arguments-string", "arguments-array", "name-integer", "name-missing"],
)
def test_sdk_rejects_non_object_params_before_the_gateway(raw, paths, params):
    """The accepted gap of ADR 0005: -32602 from the SDK, not counted, not audited."""
    client = raw()
    before = _counters(paths["db"])
    response = client.call(params)
    assert response["error"]["code"] == -32602
    assert "result" not in response
    assert _counters(paths["db"]) == before
    client.close()


def test_killing_and_restarting_resumes_the_same_session_and_counters(raw, paths):
    first = raw()
    added = first.call({"name": "add_to_cart", "arguments": {"sku": "FP-0001", "quantity": 2}})
    assert added["result"]["structuredContent"]["outcome"] == "ok"
    first.call({"name": "view_cart", "arguments": {}})
    session_id = _scalar(paths["db"], "SELECT session_id FROM sessions")
    first.kill()
    run = run_session(paths, [("get_policy", {}), ("view_cart", {})])
    policy, cart = (_structured(result)["data"] for result in run["results"])
    assert policy["tool_calls_used"] == 3  # two before the kill, then this one
    assert [(line["sku"], line["quantity"]) for line in cart["cart"]["lines"]] == [("FP-0001", 2)]
    assert _scalar(paths["db"], "SELECT count(*) FROM sessions") == 1
    assert _scalar(paths["db"], "SELECT session_id FROM sessions") == session_id
    last_resume = _scalar(
        paths["db"], "SELECT payload FROM audit WHERE event = 'session_resumed' ORDER BY seq DESC"
    )
    assert json.loads(last_resume)["session_id"] == session_id
    assert [SERVING.fullmatch(line).group(1) for line in _stderr_lines(paths)] == [
        "opened",
        "resumed",
    ]


HAPPY_PATH = [
    ("get_policy", {}),
    ("search_products", {"query": "salt"}),
    ("get_product", {"sku": "FP-0001"}),
    ("add_to_cart", {"sku": "FP-0001", "quantity": 2}),
    ("view_cart", {}),
    ("request_checkout", {}),
    ("get_checkout_status", {}),
    ("search_products", {"query": "ginger syrup"}),
    ("get_product", {"sku": "FP-0028"}),
    ("add_to_cart", {"sku": "FP-0020", "quantity": 1}),
    ("request_checkout", {}),
    ("request_checkout", {}),
    ("get_checkout_status", {}),
]


def _strings_outside_untrusted(value: object) -> list[str]:
    if isinstance(value, dict):
        return [
            text
            for key, item in value.items()
            if key != "untrusted_text"
            for text in [key, *_strings_outside_untrusted(item)]
        ]
    if isinstance(value, list):
        return [text for item in value for text in _strings_outside_untrusted(item)]
    return [value] if isinstance(value, str) else []


def _catalogue_texts() -> set[str]:
    texts = set()
    for product in load_catalogue().products:
        texts |= {product.name, product.description, product.ingredients}
    return texts


def test_happy_path_completes_then_a_large_order_waits_for_approval(paths):
    bodies = [_structured(result) for result in run_session(paths, HAPPY_PATH)["results"]]
    outcomes = [body["outcome"] for body in bodies]
    assert outcomes == [*["ok"] * 5, "completed", *["ok"] * 4, *["pending_approval"] * 2, "ok"]
    completed, pending, again = bodies[5], bodies[10], bodies[11]
    assert completed["data"]["checkout"]["status"] == "completed"
    assert (pending["rule_id"], pending["data"]["checkout"]["total_cents"]) == (
        "approval_required",
        6900,
    )
    assert again["data"]["checkout"] == pending["data"]["checkout"]
    assert bodies[12]["data"]["checkout"]["status"] == "pending_approval"
    assert _scalar(paths["db"], "SELECT count(*) FROM checkouts") == 2


def test_product_text_appears_only_in_untrusted_text(paths):
    bodies = [_structured(result) for result in run_session(paths, HAPPY_PATH)["results"]]
    shown = [bodies[1]["data"]["results"][0], bodies[2]["data"]["product"]]
    assert all(
        set(product["untrusted_text"]) <= {"name", "description", "ingredients"}
        for product in shown
    )
    product_texts = {text for text in _catalogue_texts() if len(text) >= 6}
    outside = _strings_outside_untrusted(bodies)
    for text in outside:
        assert not any(product_text in text for product_text in product_texts), text
    inside = json.dumps([product["untrusted_text"] for product in shown])
    assert "Sea Salt Flakes" in inside


def _run_refused(env: dict[str, str]) -> subprocess.CompletedProcess:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("COMMERCE_")}
    return subprocess.run(
        [sys.executable, *MODULE],
        env={**clean, **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
    )


@pytest.mark.parametrize("missing", [server.MANDATE_ENV, server.DB_ENV])
def test_refuses_to_start_without_its_environment(paths, missing):
    env = {key: value for key, value in _env(paths).items() if key != missing}
    result = _run_refused(env)
    assert (result.returncode, result.stdout) == (server.EXIT_CONFIG, "")
    assert result.stderr == server.REFUSAL_ENV + "\n"
    assert not paths["db"].exists()


def test_refuses_an_invalid_mandate_without_quoting_it(paths):
    paths["mandate"].write_text(MANDATE + "SECRET_KEY: leak-me\n", encoding="utf-8")
    result = _run_refused(_env(paths))
    assert (result.returncode, result.stdout) == (server.EXIT_CONFIG, "")
    assert result.stderr == server.REFUSAL_MANDATE + "\n"


def test_refuses_a_file_that_is_not_a_gateway_database(paths):
    paths["db"].write_bytes(b"not a database, just some bytes" * 100)
    result = _run_refused(_env(paths))
    assert (result.returncode, result.stdout) == (server.EXIT_STORE, "")
    assert result.stderr == server.REFUSAL_DB + "\n"


def test_refuses_a_new_mandate_while_a_checkout_is_pending(paths):
    run_session(
        paths, [("add_to_cart", {"sku": "FP-0020", "quantity": 1}), ("request_checkout", {})]
    )
    paths["mandate"].write_text(MANDATE.replace("server-test-1", "server-test-2"), encoding="utf-8")
    result = _run_refused(_env(paths))
    assert (result.returncode, result.stdout) == (server.EXIT_SESSION, "")
    assert result.stderr == server.REFUSAL_PENDING + "\n"


def test_stdout_carries_only_json_rpc(raw, paths):
    client = raw()
    client.call({"name": "add_to_cart", "arguments": {"sku": "FP-0001", "quantity": 1}})
    client.call({"name": "search_products", "arguments": {"query": MEGABYTE}})
    client.call({"name": "view_cart", "arguments": "not an object"})
    client.close()
    assert len(client.stdout_lines) == 4  # initialize plus three answers
    for line in client.stdout_lines:
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
        assert ("result" in message) != ("error" in message)
    [serving] = _stderr_lines(paths)
    assert SERVING.fullmatch(serving)


SECRET = "secret-7f3a FP-0001 Ginger Syrup"


@pytest.mark.parametrize("mode", ["legacy", "auto"], ids=["handshake-era", "modern-era"])
def test_a_failure_after_the_gateway_returns_a_fixed_internal_error(
    make_env, monkeypatch, capsys, mode
):
    """Without the wrapper the handshake era would send str(exception) to the client."""
    env = make_env()

    def broken(result):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(server, "to_call_result", broken)

    async def call() -> object:
        async with Client(server.build_server(env.gateway), mode=mode) as client:
            return await client.call_tool("view_cart", {})

    result = anyio.run(call)
    assert capsys.readouterr().err == server.CONVERSION_FAILED + "\n"
    assert result.is_error is True
    assert result.structured_content == {
        "allowed": False,
        "outcome": "error",
        "rule_id": None,
        "error": "internal_error",
        "reason": "internal error; the call was counted and audited",
        "data": {},
    }
    assert "secret" not in result.model_dump_json()
    assert env.calls_used() == 1  # the gateway counted and audited it before the failure
