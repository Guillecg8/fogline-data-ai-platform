"""Oracle self-test (ADR 0006): a clean result only counts if the oracle can fail.

One clean database is built through the real server and CLI. For every check, a fresh copy is
corrupted by hand so that exactly that property breaks, and the oracle must report exactly that
`check_id`. The corruptions act as an operator with file access would: dropping triggers,
inserting well-formed rows, rewriting the whole hash chain.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import oracle
from harness import CATALOGUE, FAR_FUTURE_MS, Call, Cli, Expect, Run, Scenario

BASELINE = Scenario(
    "SELFTEST",
    "Clean baseline for the oracle self-test",
    (
        Call("search_products", {"query": "salt"}, Expect("ok")),
        Call("add_to_cart", {"sku": "FP-0001", "quantity": 2}, Expect("ok")),
        Call("request_checkout", {}, Expect("completed")),
        Call("add_to_cart", {"sku": "FP-0020", "quantity": 1}, Expect("ok")),
        Call("request_checkout", {}, Expect("pending_approval", "approval_required")),
        Cli(("approve", "{checkout_id}", "{quote_hash}"), 0, {"status": "completed"}),
        Call("add_to_cart", {"sku": "FP-0002", "quantity": 1}, Expect("ok")),
        Cli(("reject", "{checkout_id}", "0" * 64), 1, {"rule_id": "quote_mismatch"}),
    ),
    mandate={"max_approval_requests": 1},
)


@dataclass(frozen=True)
class Case:
    name: str
    check_id: str
    corrupt: Callable[[sqlite3.Connection, "Baseline"], None]
    anchored: bool = False
    history: Callable[[tuple[dict, ...]], tuple[dict, ...]] | None = None


@dataclass(frozen=True)
class Baseline:
    db: Path
    mandate_path: Path
    history: tuple[dict, ...]
    clean: dict[str, int]
    mismatches: tuple[str, ...]

    @property
    def session_id(self) -> str:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT session_id FROM sessions").fetchall()[0][0]
        finally:
            conn.close()


def _catalogue_price(sku: str) -> tuple[int, int]:
    """(price_version, unit_price_cents) in force at the floor: the latest version."""
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    product = next(p for p in document["products"] if p["sku"] == sku)
    latest = max(product["prices"], key=lambda price: price["price_version"])
    return latest["price_version"], latest["unit_price_cents"]


def _drop_triggers(conn: sqlite3.Connection, *names: str) -> None:
    for name in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _insert_completed(
    conn: sqlite3.Connection, session_id: str, sku: str, quantity: int, revision: int
) -> None:
    """A well-formed completed order: a valid quote, catalogue prices, stock taken."""
    version, unit = _catalogue_price(sku)
    lock = FAR_FUTURE_MS + 10_000_000
    quote = {
        "format": oracle.QUOTE_FORMAT_VERSION,
        "session_id": session_id,
        "cart_revision": revision,
        "lines": [[sku, quantity, unit, version, lock]],
        "total_cents": quantity * unit,
    }
    conn.execute(
        "INSERT INTO checkouts (session_id, cart_revision, quote_hash, quote, total_cents,"
        " expires_at_ms, status, needed_approval, created_at_ms, decided_at_ms, decided_by)"
        " VALUES (?, ?, ?, ?, ?, ?, 'completed', 0, ?, ?, 'gateway')",
        (
            session_id,
            revision,
            oracle.sha256_hex(quote),
            oracle.canonical(quote).decode("utf-8"),
            quantity * unit,
            lock,
            FAR_FUTURE_MS + 1,
            FAR_FUTURE_MS + 1,
        ),
    )
    conn.execute("UPDATE products SET stock = stock - ? WHERE sku = ?", (quantity, sku))


def _over_cap(conn: sqlite3.Connection, base: Baseline) -> None:
    for offset in range(3):  # 8200 already spent; three more orders of 4200 pass the 20000 cap
        _insert_completed(conn, base.session_id, "FP-0022", 1, 100 + offset)


def _forbidden_cart_line(conn: sqlite3.Connection, base: Baseline) -> None:
    version, unit = _catalogue_price("FP-0024")  # alcohol
    conn.execute(
        "INSERT INTO cart_lines VALUES (?, 'FP-0024', 1, ?, ?, ?)",
        (base.session_id, unit, version, FAR_FUTURE_MS + 1_800_000),
    )


def _remove_approval_rechain(conn: sqlite3.Connection, base: Baseline) -> None:
    """Delete the approver's approval row, then recompute the whole chain so it verifies."""
    _drop_triggers(conn, "audit_no_update", "audit_no_delete", "audit_chain_on_insert")
    columns = "seq, ts_ms, session_id, actor, event, tool, allowed, rule_id, payload"
    rows = conn.execute(f"SELECT {columns} FROM audit ORDER BY seq").fetchall()
    kept = [
        row
        for row in rows
        if not (row[3] == "approver" and json.loads(row[8]).get("action") == "approve")
    ]
    conn.execute("DELETE FROM audit")
    previous = oracle.GENESIS
    for seq, row in enumerate(kept, start=1):
        record = dict(zip(columns.split(", "), row, strict=True))
        record["seq"] = seq
        record["payload"] = json.loads(record["payload"])
        record["allowed"] = None if record["allowed"] is None else bool(record["allowed"])
        record["prev_hash"] = previous
        digest = oracle.row_hash(record)
        conn.execute(
            "INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, *row[1:], previous, digest),
        )
        previous = digest


def _clear_needed_approval(conn: sqlite3.Connection, base: Baseline) -> None:
    _drop_triggers(conn, "checkouts_needed_approval_fixed")
    conn.execute("UPDATE checkouts SET needed_approval = 0 WHERE total_cents > 5000")


def _complete_at_expiry(conn: sqlite3.Connection, base: Baseline) -> None:
    conn.execute("UPDATE checkouts SET decided_at_ms = expires_at_ms WHERE checkout_id = 1")


def _change_total(conn: sqlite3.Connection, base: Baseline) -> None:
    conn.execute("UPDATE checkouts SET total_cents = total_cents + 100 WHERE checkout_id = 1")


def _change_cart_price(conn: sqlite3.Connection, base: Baseline) -> None:
    conn.execute("UPDATE cart_lines SET unit_price_cents = unit_price_cents + 1")


def _version_not_in_force(conn: sqlite3.Connection, base: Baseline) -> None:
    """FP-0033 locked at its v2 price at a time when only v1 was in force."""
    conn.execute(
        "INSERT INTO cart_lines VALUES (?, 'FP-0033', 1, 1350, 2, ?)",
        (base.session_id, FAR_FUTURE_MS + 1_800_000),
    )


def _lose_stock(conn: sqlite3.Connection, base: Baseline) -> None:
    conn.execute("UPDATE products SET stock = stock - 1 WHERE sku = 'FP-0002'")


def other_session_id(session_id: str) -> str:
    """A session id guaranteed to differ from `session_id` (its first character changed).

    The server's ids are random; deriving the copy by forcing a fixed first character collided
    whenever the random id already started with it (1 time in 16), which made the self-test
    fail intermittently.
    """
    return ("1" if session_id[0] == "0" else "0") + session_id[1:]


def _duplicate_quote(conn: sqlite3.Connection, base: Baseline) -> None:
    """The same completed quote again, in a second (closed) session, with its stock taken."""
    original = base.session_id
    copy = other_session_id(original)
    conn.execute(
        "INSERT INTO sessions (session_id, mandate_hash, mandate, opened_at_ms, closed_at_ms,"
        " last_seen_ms) SELECT ?, mandate_hash, mandate, opened_at_ms, last_seen_ms,"
        " last_seen_ms FROM sessions WHERE session_id = ?",
        (copy, original),
    )
    conn.execute(
        "INSERT INTO checkouts (session_id, cart_revision, quote_hash, quote, total_cents,"
        " expires_at_ms, status, needed_approval, created_at_ms, decided_at_ms, decided_by)"
        " SELECT ?, cart_revision, quote_hash, quote, total_cents, expires_at_ms, status,"
        " needed_approval, created_at_ms, decided_at_ms, decided_by"
        " FROM checkouts WHERE checkout_id = 1",
        (copy,),
    )
    conn.execute("UPDATE products SET stock = stock - 2 WHERE sku = 'FP-0001'")


def _second_approval_request(conn: sqlite3.Connection, base: Baseline) -> None:
    _drop_triggers(conn, "checkouts_needed_approval_fixed")
    conn.execute("UPDATE checkouts SET needed_approval = 1 WHERE checkout_id = 1")


def _uncounted_call(conn: sqlite3.Connection, base: Baseline) -> None:
    conn.execute("UPDATE sessions SET tool_calls_used = tool_calls_used + 1")


def _edit_audit_payload(conn: sqlite3.Connection, base: Baseline) -> None:
    _drop_triggers(conn, "audit_no_update")
    conn.execute("UPDATE audit SET payload = '{\"edited\":true}' WHERE seq = 2")


def _truncate_last_row(conn: sqlite3.Connection, base: Baseline) -> None:
    _drop_triggers(conn, "audit_no_delete")
    conn.execute("DELETE FROM audit WHERE seq = (SELECT max(seq) FROM audit)")


def _no_change(conn: sqlite3.Connection, base: Baseline) -> None:
    """The database stays clean; the corruption is in the counter history."""


def _counter_drop(history: tuple[dict, ...]) -> tuple[dict, ...]:
    return (*history, {**history[-1], "tool_calls_used": history[-1]["tool_calls_used"] - 1})


def _mandate_edit(conn: sqlite3.Connection, base: Baseline) -> None:
    conn.execute("UPDATE sessions SET mandate = replace(mandate, ':20000,', ':20001,')")


CASES = (
    Case("edited mandate snapshot", "mandate_hash", _mandate_edit),
    Case("well-formed orders past the cap", "spend_cap", _over_cap),
    Case("forbidden category in the cart", "category", _forbidden_cart_line),
    Case("approval row removed, chain recomputed", "approval_present", _remove_approval_rechain),
    Case("approved order marked as needing none", "no_approval_needed", _clear_needed_approval),
    Case("completed at the quote expiry", "not_after_expiry", _complete_at_expiry),
    Case("stored total changed", "quote_integrity", _change_total),
    Case("cart line price changed", "locked_price", _change_cart_price),
    Case("price version not yet in force", "locked_price", _version_not_in_force),
    Case("stock lowered without an order", "stock", _lose_stock),
    Case("quote completed twice, second session", "one_order_per_quote", _duplicate_quote),
    Case("a second approval request", "approval_limit", _second_approval_request),
    Case("call counter raised", "tool_calls_counted", _uncounted_call),
    Case("audit payload edited", "audit_chain", _edit_audit_payload),
    Case("truncated below the anchor", "audit_chain", _truncate_last_row, anchored=True),
    Case("counter decreased in the history", "monotonic", _no_change, history=_counter_drop),
)


def build_baseline(workdir: Path) -> Baseline:
    workdir.mkdir(parents=True, exist_ok=True)
    run = Run(BASELINE, workdir)
    observed = run.run()
    return Baseline(
        run.db,
        run.mandate_path,
        tuple(run.history),
        observed.violations,
        tuple(observed.mismatches),
    )


def _copy(source: Path, target: Path) -> None:
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def run_case(base: Baseline, case: Case, workdir: Path) -> dict[str, int]:
    """Corrupt a fresh copy for `case` and return what the oracle reports."""
    db = workdir / f"{CASES.index(case):02d}.db"
    _copy(base.db, db)
    anchor = None
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        if case.anchored:
            anchor = tuple(
                conn.execute("SELECT seq, row_hash FROM audit ORDER BY seq DESC").fetchall()[0]
            )
        case.corrupt(conn, base)
    finally:
        conn.close()
    history = case.history(base.history) if case.history else base.history
    result = oracle.check(db, base.mandate_path, CATALOGUE, anchor=anchor, counter_history=history)
    return result.by_check()


def self_test(workdir: Path) -> dict[str, object]:
    """{"baseline_clean": bool, "cases": [{name, check_id, reported, detected}]}."""
    base = build_baseline(workdir / "baseline")
    cases = []
    for case in CASES:
        reported = run_case(base, case, workdir)
        cases.append(
            {
                "name": case.name,
                "check_id": case.check_id,
                "reported": reported,
                "detected": set(reported) == {case.check_id},
            }
        )
    return {"baseline_clean": not base.clean and not base.mismatches, "cases": cases}
