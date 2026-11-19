"""Scenario harness (ADR 0006): runs scripted attackers against the real server and CLI.

It does not import `commerce_gateway`. It talks to the server only over stdio, with its own
newline-delimited JSON-RPC client, and to the approval CLI only by subprocess. Time moves only
through the session's `last_seen_ms` floor, set in the database; neither entry point can be told
the time. After a scenario the independent oracle judges the final database.
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import oracle

PROJECT = Path(__file__).resolve().parents[1]
CATALOGUE = PROJECT / "src" / "commerce_gateway" / "fixtures" / "catalogue_v1.json"
FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z: the floor always wins the max()
TIMEOUT_S = 60
PROTOCOL_VERSION = "2025-11-25"
MISSING = object()  # an argument object left out of the tools/call params entirely

POLICY_RULES = frozenset(
    {
        "category_not_allowed",
        "out_of_stock",
        "stale_price",
        "spend_cap",
        "approval_limit",
        "checkout_pending",
        "tool_call_budget",
        "quote_mismatch",
    }
)
AUDITED_ERRORS = ("invalid_arguments", "internal_error", "cart_empty", "session_closed")
UNAUDITED_ERRORS = ("store_busy", "store_integrity")
MIN_TEXT_CHARS = 6  # shorter catalogue strings ("salt") are too common to attribute


# --- Steps ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    outcome: str
    rule_id: str | None = None
    error: str | None = None
    data: dict | None = None  # dotted path -> expected value, for a few structured checks


@dataclass(frozen=True)
class Call:
    tool: object
    arguments: object
    expect: Expect
    attempt: bool = False
    server: str = "a"


@dataclass(frozen=True)
class RawCall:
    """A tools/call whose params the SDK must reject before the gateway (ADR 0005 gap)."""

    params: dict
    expect_code: int = -32602
    server: str = "a"


@dataclass(frozen=True)
class ListTools:
    names: tuple[str, ...]
    server: str = "a"


@dataclass(frozen=True)
class Concurrent:
    """Calls released together from different servers; expectations as a multiset."""

    calls: tuple[Call, ...]


@dataclass(frozen=True)
class Cli:
    args: tuple[str, ...]  # {quote_hash}, {checkout_id}: last checkout seen; {anchor}: seq:hash
    exit_code: int
    fields: dict = field(default_factory=dict)  # first word of an output line -> rest


@dataclass(frozen=True)
class Advance:
    """Raise the time floor by `ms`, as if that much time had passed."""

    ms: int


@dataclass(frozen=True)
class AdvanceToExpiry:
    """Raise the floor to the last checkout's quote expiry plus `offset_ms`."""

    offset_ms: int = 0


@dataclass(frozen=True)
class SetStock:
    """A sale outside the gateway: declared to the oracle as an external stock change."""

    sku: str
    stock: int


@dataclass(frozen=True)
class Start:
    server: str = "a"


@dataclass(frozen=True)
class Stop:
    server: str = "a"


@dataclass(frozen=True)
class Kill:
    server: str = "a"


@dataclass(frozen=True)
class RecordAnchor:
    pass


@dataclass(frozen=True)
class EditAuditPayload:
    seq: int


@dataclass(frozen=True)
class TruncateFromAnchor:
    """Delete the audit rows from the anchor on and lower the call counter to match."""


@dataclass(frozen=True)
class SameCheckout:
    """The last two checkouts seen by the agent are the same checkout."""


Step = (
    Call
    | RawCall
    | ListTools
    | Concurrent
    | Cli
    | Advance
    | AdvanceToExpiry
    | SetStock
    | Start
    | Stop
    | Kill
    | RecordAnchor
    | EditAuditPayload
    | TruncateFromAnchor
    | SameCheckout
)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    title: str
    steps: tuple[Step, ...]
    mandate: dict = field(default_factory=dict)  # overrides of BASE_MANDATE
    intended_lines: tuple[tuple[str, int], ...] | None = None
    expected_violations: tuple[str, ...] = ()
    expected_violations_anchored: tuple[str, ...] | None = None
    max_invalid_payload_bytes: int | None = None


BASE_MANDATE = {
    "version": 1,
    "session_label": "scenario",
    "budget_cents": 20_000,
    "approval_threshold_cents": 5_000,
    "allowed_categories": [
        "beverages",
        "cleaning",
        "dairy",
        "dry_goods",
        "kitchen_supplies",
        "produce",
    ],
    "price_lock_ttl_seconds": 1800,
    "max_approval_requests": 2,
    "tool_call_budget": 100,
}


# --- A minimal stdio JSON-RPC client -----------------------------------------------------------


class Server:
    def __init__(self, env: dict[str, str], errlog: Path) -> None:
        self._errlog = errlog.open("a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "commerce_gateway.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._errlog,
            env=env,
            encoding="utf-8",
        )
        self.stdout_lines: list[str] = []
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._next_id = 0
        self._lock = threading.Lock()
        self._initialize()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            self._lines.put(line)

    def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: object) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                message = json.loads(self._lines.get(timeout=TIMEOUT_S))
                if message.get("id") == request_id:
                    return message

    def _initialize(self) -> None:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "scenario-harness", "version": "1"},
        }
        if "result" not in self.request("initialize", params):
            raise RuntimeError("the server refused to initialise")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: object, arguments: object) -> dict:
        params: dict = {"name": tool}
        if arguments is not MISSING:
            params["arguments"] = arguments
        return self.request("tools/call", params)

    def stop(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=TIMEOUT_S)
        self._reader.join(timeout=TIMEOUT_S)
        self._errlog.close()

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=TIMEOUT_S)
        self._reader.join(timeout=TIMEOUT_S)
        self._errlog.close()


# --- The run ----------------------------------------------------------------------------------


@dataclass
class Observed:
    """What one scenario produced: the inputs of the report."""

    scenario_id: str
    steps: int = 0
    attempts_declared: int = 0
    attempts_measured: int = 0
    mismatches: list[str] = field(default_factory=list)
    client_errors: Counter = field(default_factory=Counter)
    transport_rejections: int = 0
    approval_waits: int = 0
    blocks: Counter = field(default_factory=Counter)
    audited_errors: Counter = field(default_factory=Counter)
    invalid_by_field: Counter = field(default_factory=Counter)
    approver_refusals: Counter = field(default_factory=Counter)
    permitted_deviations: int | None = None
    violations: dict[str, int] = field(default_factory=dict)
    violations_anchored: dict[str, int] | None = None


def _catalogue_texts() -> set[str]:
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    texts = set()
    for product in document["products"]:
        for key in ("name", "description", "ingredients"):
            texts.add(product[key])
    return {text for text in texts if len(text) >= MIN_TEXT_CHARS}


_PRODUCT_TEXTS = _catalogue_texts()


def _strings_outside_untrusted(value: object) -> list[str]:
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            if key != "untrusted_text":
                found += [key, *_strings_outside_untrusted(item)]
        return found
    if isinstance(value, list):
        return [text for item in value for text in _strings_outside_untrusted(item)]
    return [value] if isinstance(value, str) else []


def _dig(data: dict, path: str) -> object:
    value: object = data
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]  # type: ignore[index]
    return value


class Run:
    """One scenario against one temporary database."""

    def __init__(self, scenario: Scenario, workdir: Path) -> None:
        self.scenario = scenario
        self.workdir = workdir
        self.db = workdir / "gateway.db"
        self.mandate_path = workdir / "mandate.json"
        mandate = {**BASE_MANDATE, "session_label": scenario.scenario_id, **scenario.mandate}
        self.mandate_path.write_text(json.dumps(mandate, indent=2), encoding="utf-8")
        self.errlog = workdir / "server-stderr.txt"
        self.env = {
            **os.environ,
            "COMMERCE_GATEWAY_MANDATE": str(self.mandate_path),
            "COMMERCE_GATEWAY_DB": str(self.db),
        }
        self.servers: dict[str, Server] = {}
        self.observed = Observed(scenario.scenario_id)
        self.history: list[dict] = []
        self.external_stock: Counter = Counter()
        self.anchor: tuple[int, str] | None = None
        self.checkouts_seen: list[dict] = []
        self.all_stdout: list[str] = []

    # database helpers (the harness acts as the operator here)
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db, isolation_level=None, timeout=10)

    def _sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        conn = self._connect()
        try:
            return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def _raise_floor(self, ms: int) -> None:
        self._sql(
            "UPDATE sessions SET last_seen_ms = max(last_seen_ms, ?) WHERE closed_at_ms IS NULL",
            (ms,),
        )

    def _floor(self) -> int:
        return self._sql("SELECT last_seen_ms FROM sessions WHERE closed_at_ms IS NULL")[0][0]

    def _snapshot(self) -> None:
        if not self.db.exists():
            return
        rows = self._sql(
            "SELECT tool_calls_used, cart_revision, last_seen_ms FROM sessions"
            " WHERE closed_at_ms IS NULL"
        )
        if rows:
            self.history.append(dict(zip(oracle.COUNTER_KEYS, rows[0], strict=True)))

    def _mismatch(self, index: int, what: str) -> None:
        self.observed.mismatches.append(f"step {index}: {what}")

    # steps
    def _start(self, name: str) -> None:
        first = not self.db.exists()
        self.servers[name] = Server(self.env, self.errlog)
        if first:
            self._raise_floor(FAR_FUTURE_MS)  # before the first tool call

    def _close(self, name: str, kill: bool) -> None:
        server = self.servers.pop(name)
        if kill:
            server.kill()
        else:
            server.stop()
        self.all_stdout += server.stdout_lines

    def _inspect(self, index: int, tool: object, response: dict) -> dict | None:
        """Shape checks and metrics for one tool response; returns its structured content."""
        if "result" not in response:
            self._mismatch(index, f"{tool}: protocol error {response.get('error')}")
            return None
        body = response["result"]["structuredContent"]
        if json.loads(response["result"]["content"][0]["text"]) != body:
            self._mismatch(index, "text content differs from structured content")
        if response["result"].get("isError", False) is not (body["outcome"] == "error"):
            self._mismatch(index, "isError does not match the outcome")
        for text in _strings_outside_untrusted(body):
            if any(product_text in text for product_text in _PRODUCT_TEXTS):
                self._mismatch(index, "product text outside untrusted_text")
        if body["error"]:
            self.observed.client_errors[body["error"]] += 1
        if body["outcome"] == "pending_approval":
            self.observed.approval_waits += 1
        if isinstance(body["data"].get("checkout"), dict):
            self.checkouts_seen.append(body["data"]["checkout"])
        return body

    def _check_call(self, index: int, step: Call, response: dict) -> None:
        body = self._inspect(index, step.tool, response)
        if body is None:
            return
        expect = step.expect
        got = (body["outcome"], body["rule_id"], body["error"])
        if got != (expect.outcome, expect.rule_id, expect.error):
            wanted = (expect.outcome, expect.rule_id, expect.error)
            self._mismatch(index, f"{step.tool}: expected {wanted}, got {got}")
        for path, value in (expect.data or {}).items():
            try:
                actual = _dig(body["data"], path)
            except (KeyError, IndexError, TypeError):
                actual = "<missing>"
            if actual != value:
                self._mismatch(index, f"{step.tool}: data.{path} is {actual!r}, not {value!r}")

    def _call(self, index: int, step: Call) -> None:
        response = self.servers[step.server].call(step.tool, step.arguments)
        self._check_call(index, step, response)

    def _concurrent(self, index: int, step: Concurrent) -> None:
        seen_before = len(self.checkouts_seen)
        barrier = threading.Barrier(len(step.calls))
        responses: list[dict | None] = [None] * len(step.calls)

        def worker(position: int, call: Call) -> None:
            barrier.wait(timeout=TIMEOUT_S)
            responses[position] = self.servers[call.server].call(call.tool, call.arguments)

        threads = [
            threading.Thread(target=worker, args=(position, call))
            for position, call in enumerate(step.calls)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=TIMEOUT_S)
        expected = Counter((c.expect.outcome, c.expect.rule_id, c.expect.error) for c in step.calls)
        got: Counter = Counter()
        for call, response in zip(step.calls, responses, strict=True):
            body = self._inspect(index, call.tool, response or {}) or {}
            got[(body.get("outcome"), body.get("rule_id"), body.get("error"))] += 1
        if got != expected:
            self._mismatch(index, f"concurrent: expected {dict(expected)}, got {dict(got)}")
        ids = {c.get("checkout_id") for c in self.checkouts_seen[seen_before:]}
        if len(ids) > 1:
            self._mismatch(index, "concurrent calls saw different checkouts")

    def _raw(self, index: int, step: RawCall) -> None:
        response = self.servers[step.server].request("tools/call", step.params)
        code = response.get("error", {}).get("code")
        if code != step.expect_code:
            self._mismatch(index, f"raw call: expected JSON-RPC {step.expect_code}, got {code}")
        else:
            self.observed.transport_rejections += 1

    def _list_tools(self, index: int, step: ListTools) -> None:
        response = self.servers[step.server].request("tools/list", {})
        names = tuple(tool["name"] for tool in response["result"]["tools"])
        if names != step.names:
            self._mismatch(index, f"tools/list returned {names}")

    def _cli(self, index: int, step: Cli) -> None:
        last = self.checkouts_seen[-1] if self.checkouts_seen else {}
        anchor = f"{self.anchor[0]}:{self.anchor[1]}" if self.anchor else None
        values = {
            "quote_hash": last.get("quote_hash"),
            "checkout_id": last.get("checkout_id"),
            "anchor": anchor,
        }
        args = [arg.format(**values) for arg in step.args]
        result = subprocess.run(
            [sys.executable, "-m", "commerce_gateway.approval", *args],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=TIMEOUT_S,
        )
        if result.returncode != step.exit_code:
            self._mismatch(index, f"cli {step.args[0]}: exit {result.returncode}")
        lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
        for key, value in step.fields.items():
            if lines.get(key) != value:
                self._mismatch(index, f"cli {step.args[0]}: {key} is {lines.get(key)!r}")

    def _edit_payload(self, seq: int) -> None:
        conn = self._connect()
        try:
            conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
            conn.execute("UPDATE audit SET payload = '{\"edited\":true}' WHERE seq = ?", (seq,))
        finally:
            conn.close()

    def _truncate_from_anchor(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DROP TRIGGER IF EXISTS audit_no_delete")
            conn.execute("DELETE FROM audit WHERE seq >= ?", (self.anchor[0],))
            conn.execute(
                "UPDATE sessions SET tool_calls_used = (SELECT count(*) FROM audit"
                " WHERE audit.session_id = sessions.session_id AND actor = 'agent')"
            )
        finally:
            conn.close()

    def _record_anchor(self) -> None:
        self.anchor = tuple(self._sql("SELECT seq, row_hash FROM audit ORDER BY seq DESC")[0])

    def _run_step(self, index: int, step: Step) -> None:
        match step:
            case Call():
                self._call(index, step)
            case Concurrent():
                self._concurrent(index, step)
            case RawCall():
                self._raw(index, step)
            case ListTools():
                self._list_tools(index, step)
            case Cli():
                self._cli(index, step)
            case Advance():
                self._raise_floor(self._floor() + step.ms)
            case AdvanceToExpiry():
                self._raise_floor(self.checkouts_seen[-1]["expires_at_ms"] + step.offset_ms)
            case SetStock():
                before = self._sql("SELECT stock FROM products WHERE sku = ?", (step.sku,))[0][0]
                self._sql("UPDATE products SET stock = ? WHERE sku = ?", (step.stock, step.sku))
                self.external_stock[step.sku] += step.stock - before
            case Start():
                self._start(step.server)
            case Stop():
                self._close(step.server, kill=False)
            case Kill():
                self._close(step.server, kill=True)
            case RecordAnchor():
                self._record_anchor()
            case EditAuditPayload():
                self._edit_payload(step.seq)
            case TruncateFromAnchor():
                self._truncate_from_anchor()
            case SameCheckout():
                first, second = self.checkouts_seen[-2:]
                if first["checkout_id"] != second["checkout_id"]:
                    self._mismatch(index, "the two checkouts differ")

    def run(self) -> Observed:
        observed = self.observed
        self._start("a")
        try:
            for index, step in enumerate(self.scenario.steps, start=1):
                self._run_step(index, step)
                self._snapshot()
                observed.steps += 1
                calls = step.calls if isinstance(step, Concurrent) else (step,)
                observed.attempts_declared += sum(
                    1 for c in calls if isinstance(c, Call) and c.attempt
                )
        finally:
            for name in list(self.servers):
                self._close(name, kill=False)
        self._finish()
        return observed

    # after the script
    def _finish(self) -> None:
        observed = self.observed
        for line in self.all_stdout:
            message = json.loads(line)
            if message.get("jsonrpc") != "2.0":
                observed.mismatches.append("stdout carried a line that is not JSON-RPC")
        self._read_audit()
        self._judge()
        self._deviations()
        attempts_measured = sum(observed.blocks.values()) + sum(
            count
            for check_id, count in observed.violations.items()
            if check_id not in self.scenario.expected_violations
        )
        observed.attempts_measured = attempts_measured
        if attempts_measured != observed.attempts_declared:
            observed.mismatches.append(
                f"attempts: declared {observed.attempts_declared}, measured {attempts_measured}"
            )
        for error in AUDITED_ERRORS:
            if observed.client_errors[error] != observed.audited_errors[error]:
                observed.mismatches.append(f"{error}: client and audit counts differ")

    def _read_audit(self) -> None:
        observed = self.observed
        rows = self._sql("SELECT event, allowed, rule_id, payload FROM audit WHERE actor = 'agent'")
        sizes = []
        for event, allowed, rule_id, payload in rows:
            if allowed == 0 and rule_id in POLICY_RULES:
                observed.blocks[rule_id] += 1
            if event in AUDITED_ERRORS:
                observed.audited_errors[event] += 1
                sizes.append(len(payload))
                if event == "invalid_arguments":
                    observed.invalid_by_field[json.loads(payload).get("field")] += 1
            elif json.loads(payload).get("error") == "cart_empty":
                observed.audited_errors["cart_empty"] += 1
        refused = self._sql(
            "SELECT rule_id FROM audit WHERE actor = 'approver' AND allowed = 0"
            " AND event = 'approval_decision'"
        )
        for (rule_id,) in refused:
            observed.approver_refusals[rule_id] += 1
        limit = self.scenario.max_invalid_payload_bytes
        if limit is not None and any(size > limit for size in sizes):
            observed.mismatches.append("an invalid_arguments audit payload is too large")

    def _judge(self) -> None:
        observed = self.observed
        common = {
            "db_path": self.db,
            "mandate_path": self.mandate_path,
            "catalogue_path": CATALOGUE,
            "counter_history": tuple(self.history),
            "external_stock": dict(self.external_stock),
        }
        observed.violations = oracle.check(**common).by_check()
        if sorted(observed.violations) != sorted(self.scenario.expected_violations):
            observed.mismatches.append(f"oracle reported {observed.violations}")
        if self.scenario.expected_violations_anchored is not None:
            observed.violations_anchored = oracle.check(**common, anchor=self.anchor).by_check()
            if sorted(observed.violations_anchored) != sorted(
                self.scenario.expected_violations_anchored
            ):
                observed.mismatches.append(f"anchored oracle: {observed.violations_anchored}")

    def _deviations(self) -> None:
        intended = self.scenario.intended_lines
        if intended is None:
            return
        rows = self._sql(
            "SELECT quote FROM checkouts WHERE status IN ('completed', 'pending_approval')"
        )
        lines = [(line[0], line[1]) for (quote,) in rows for line in json.loads(quote)["lines"]]
        self.observed.permitted_deviations = sum(1 for line in lines if line not in intended)
