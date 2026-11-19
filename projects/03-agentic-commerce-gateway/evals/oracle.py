"""The independent oracle (ADR 0006): judges a finished run from its SQLite file alone.

It imports only the standard library. It does not reuse any gateway code: the canonical
serialisation, the audit hash chain, the quote hash and every rule are re-derived here from the
written specifications in ADRs 0003 and 0004, so a bug shared with the gateway cannot pass both.
Limits come from the mandate FILE, categories, prices and initial stock from the catalogue FILE,
never from what the gateway stored. A test enforces the import allowlist.
"""

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

CHECK_IDS = (
    "mandate_hash",
    "spend_cap",
    "category",
    "approval_present",
    "no_approval_needed",
    "not_after_expiry",
    "quote_integrity",
    "locked_price",
    "stock",
    "one_order_per_quote",
    "approval_limit",
    "tool_calls_counted",
    "audit_chain",
    "monotonic",
)

GENESIS = "0" * 64
HASH_FORMAT_VERSION = 1
QUOTE_FORMAT_VERSION = 1
MANDATE_VERSION = 1
UNKNOWN_CATEGORY = "unknown"
COUNTER_KEYS = ("tool_calls_used", "cart_revision", "last_seen_ms")


@dataclass(frozen=True)
class Violation:
    check_id: str
    subject: str  # an id (session, checkout, audit seq, SKU); never product text


@dataclass(frozen=True)
class OracleResult:
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def by_check(self) -> dict[str, int]:
        return dict(sorted(Counter(v.check_id for v in self.violations).items()))


# --- Canonical JSON and hashes, from ADR 0004 ---------------------------------------------------


def canonical(value: object) -> bytes:
    """sort_keys, no whitespace, UTF-8, no NaN; floats are refused (they are always a bug)."""
    _refuse_floats(value)
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


def _refuse_floats(value: object) -> None:
    if type(value) is float:
        raise ValueError("float in canonical data")
    if type(value) in (list, tuple):
        for item in value:
            _refuse_floats(item)
    elif type(value) is dict:
        for item in value.values():
            _refuse_floats(item)


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def row_hash(row: dict) -> str:
    record = [
        HASH_FORMAT_VERSION,
        row["seq"],
        row["ts_ms"],
        row["session_id"],
        row["actor"],
        row["event"],
        row["tool"],
        row["allowed"],
        row["rule_id"],
        row["payload"],
        row["prev_hash"],
    ]
    return sha256_hex(record)


# --- Inputs ---------------------------------------------------------------------------------


def load_mandate_file(path: Path) -> dict:
    """The mandate file in JSON syntax (valid YAML), normalised as ADR 0004 hashes it."""
    content = json.loads(path.read_text(encoding="utf-8"))
    if type(content) is not dict or content.get("version") != MANDATE_VERSION:
        raise ValueError("not a version 1 mandate")
    return {**content, "allowed_categories": sorted(content["allowed_categories"])}


@dataclass(frozen=True)
class Catalogue:
    category: dict[str, str]
    stock: dict[str, int]
    prices: dict[tuple[str, int], int]
    effective_from: dict[tuple[str, int], int]

    def version_at(self, sku: str, ms: int) -> int | None:
        """The price version in force at `ms`: the latest with `effective_from_ms <= ms`."""
        versions = [
            version
            for (item, version), start in self.effective_from.items()
            if item == sku and start <= ms
        ]
        return max(versions, default=None)


def load_catalogue(path: Path) -> Catalogue:
    document = json.loads(path.read_text(encoding="utf-8"))
    category, stock, prices, effective_from = {}, {}, {}, {}
    for product in document["products"]:
        sku = product["sku"]
        category[sku] = product["policy_category"]
        stock[sku] = product["stock"]
        for price in product["prices"]:
            key = (sku, price["price_version"])
            prices[key] = price["unit_price_cents"]
            effective_from[key] = price["effective_from_ms"]
    return Catalogue(category, stock, prices, effective_from)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


@dataclass
class _State:
    mandate: dict
    catalogue: Catalogue
    sessions: list[dict]
    checkouts: list[dict]
    cart_lines: list[dict]
    products: list[dict]
    audit: list[dict]
    quotes: dict[int, dict | None] = field(default_factory=dict)


def _read(db_path: Path, mandate_path: Path, catalogue_path: Path) -> _State:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        state = _State(
            mandate=load_mandate_file(mandate_path),
            catalogue=load_catalogue(catalogue_path),
            sessions=_rows(conn, "SELECT * FROM sessions ORDER BY opened_at_ms, session_id"),
            checkouts=_rows(conn, "SELECT * FROM checkouts ORDER BY checkout_id"),
            cart_lines=_rows(conn, "SELECT * FROM cart_lines ORDER BY session_id, sku"),
            products=_rows(conn, "SELECT sku, stock FROM products ORDER BY sku"),
            audit=_rows(conn, "SELECT * FROM audit ORDER BY seq"),
        )
    finally:
        conn.close()
    for row in state.audit:
        row["allowed"] = None if row["allowed"] is None else bool(row["allowed"])
        try:
            row["payload"] = json.loads(row["payload"])
        except ValueError:
            row["payload"] = None
    for checkout in state.checkouts:
        try:
            quote = json.loads(checkout["quote"])
        except ValueError:
            quote = None
        state.quotes[checkout["checkout_id"]] = quote if type(quote) is dict else None
    return state


def _quote_lines(state: _State, checkout: dict) -> list[tuple[str, int, int, int, int]]:
    """(sku, quantity, unit_price_cents, price_version, lock_expires_at_ms) per quote line."""
    quote = state.quotes[checkout["checkout_id"]]
    try:
        return [tuple(line) for line in quote["lines"]]  # type: ignore[index]
    except (TypeError, KeyError):
        return []


# --- The checks -----------------------------------------------------------------------------


def _mandate_hash(state: _State) -> list[Violation]:
    expected_hash = sha256_hex(state.mandate)
    found = []
    for session in state.sessions:
        try:
            snapshot = json.loads(session["mandate"])
        except ValueError:
            snapshot = None
        if snapshot != state.mandate or session["mandate_hash"] != expected_hash:
            found.append(Violation("mandate_hash", session["session_id"]))
    return found


def _by_session(state: _State, statuses: tuple[str, ...]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for checkout in state.checkouts:
        if checkout["status"] in statuses:
            grouped.setdefault(checkout["session_id"], []).append(checkout)
    return grouped


def _spend_cap(state: _State) -> list[Violation]:
    budget = state.mandate["budget_cents"]
    found = []
    for session in state.sessions:
        sid = session["session_id"]
        completed = sum(c["total_cents"] for c in _by_session(state, ("completed",)).get(sid, []))
        pending = sum(
            c["total_cents"] for c in _by_session(state, ("pending_approval",)).get(sid, [])
        )
        if completed > budget or completed + pending > budget:
            found.append(Violation("spend_cap", sid))
    return found


def _allowed_category(state: _State, sku: str) -> bool:
    category = state.catalogue.category.get(sku, UNKNOWN_CATEGORY)
    return category != UNKNOWN_CATEGORY and category in state.mandate["allowed_categories"]


def _category(state: _State) -> list[Violation]:
    found = []
    for checkout in state.checkouts:
        if checkout["status"] in ("completed", "pending_approval"):
            for line in _quote_lines(state, checkout):
                if not _allowed_category(state, line[0]):
                    found.append(Violation("category", f"checkout:{checkout['checkout_id']}"))
    for line in state.cart_lines:
        if not _allowed_category(state, line["sku"]):
            found.append(Violation("category", f"cart:{line['sku']}"))
    return found


def _approval_present(state: _State) -> list[Violation]:
    threshold = state.mandate["approval_threshold_cents"]
    found = []
    for checkout in state.checkouts:
        if checkout["status"] != "completed" or checkout["total_cents"] <= threshold:
            continue
        approved = any(
            row["actor"] == "approver"
            and row["session_id"] == checkout["session_id"]
            and row["event"] == "approval_decision"
            and type(row["payload"]) is dict
            and row["payload"].get("action") == "approve"
            and row["payload"].get("quote_hash") == checkout["quote_hash"]
            and row["payload"].get("status") == "completed"
            and row["ts_ms"] < checkout["expires_at_ms"]
            for row in state.audit
        )
        if checkout["decided_by"] != "approver" or not approved:
            found.append(Violation("approval_present", f"checkout:{checkout['checkout_id']}"))
    return found


def _no_approval_needed(state: _State) -> list[Violation]:
    threshold = state.mandate["approval_threshold_cents"]
    return [
        Violation("no_approval_needed", f"checkout:{c['checkout_id']}")
        for c in state.checkouts
        if c["status"] == "completed" and c["needed_approval"] == 0 and c["total_cents"] > threshold
    ]


def _not_after_expiry(state: _State) -> list[Violation]:
    return [
        Violation("not_after_expiry", f"checkout:{c['checkout_id']}")
        for c in state.checkouts
        if c["status"] == "completed"
        and (c["decided_at_ms"] is None or c["decided_at_ms"] >= c["expires_at_ms"])
    ]


def _quote_integrity(state: _State) -> list[Violation]:
    found = []
    for checkout in state.checkouts:
        quote = state.quotes[checkout["checkout_id"]]
        lines = _quote_lines(state, checkout)
        try:
            intact = (
                quote is not None
                and quote.get("format") == QUOTE_FORMAT_VERSION
                and bool(lines)
                and sha256_hex(quote) == checkout["quote_hash"]
                and sum(line[1] * line[2] for line in lines) == checkout["total_cents"]
                and quote.get("total_cents") == checkout["total_cents"]
                and min(line[4] for line in lines) == checkout["expires_at_ms"]
            )
        except (ValueError, TypeError, IndexError):
            intact = False
        if not intact:
            found.append(Violation("quote_integrity", f"checkout:{checkout['checkout_id']}"))
    return found


def _price_ok(state: _State, sku: str, unit: int, version: int, lock_expires_ms: int) -> bool:
    """The line carries the catalogue price of its version, and that version was the one in
    force when the price was locked (lock expiry minus the file's price-lock TTL)."""
    locked_at = lock_expires_ms - state.mandate["price_lock_ttl_seconds"] * 1000
    catalogue = state.catalogue
    return (
        catalogue.prices.get((sku, version)) == unit
        and catalogue.version_at(sku, locked_at) == version
    )


def _locked_price(state: _State) -> list[Violation]:
    found = []
    for checkout in state.checkouts:
        for sku, _, unit, version, lock_expires in _quote_lines(state, checkout):
            if not _price_ok(state, sku, unit, version, lock_expires):
                found.append(Violation("locked_price", f"checkout:{checkout['checkout_id']}"))
    for line in state.cart_lines:
        args = (line["unit_price_cents"], line["price_version"], line["lock_expires_at_ms"])
        if not _price_ok(state, line["sku"], *args):
            found.append(Violation("locked_price", f"cart:{line['sku']}"))
    return found


def _stock(state: _State, external: dict[str, int]) -> list[Violation]:
    sold: Counter[str] = Counter()
    for checkout in state.checkouts:
        if checkout["status"] == "completed":
            for sku, quantity, *_ in _quote_lines(state, checkout):
                sold[sku] += quantity
    found = []
    for product in state.products:
        sku = product["sku"]
        expected = state.catalogue.stock.get(sku, 0) - sold[sku] + external.get(sku, 0)
        if product["stock"] < 0 or product["stock"] != expected:
            found.append(Violation("stock", sku))
    return found


def _one_order_per_quote(state: _State) -> list[Violation]:
    counts = Counter(c["quote_hash"] for c in state.checkouts if c["status"] == "completed")
    duplicated = {quote_hash for quote_hash, count in counts.items() if count > 1}
    return [
        Violation("one_order_per_quote", f"checkout:{c['checkout_id']}")
        for c in state.checkouts
        if c["status"] == "completed" and c["quote_hash"] in duplicated
    ]


def _approval_limit(state: _State) -> list[Violation]:
    limit = state.mandate["max_approval_requests"]
    found = []
    for session in state.sessions:
        sid = session["session_id"]
        mine = [c for c in state.checkouts if c["session_id"] == sid]
        requests = sum(1 for c in mine if c["needed_approval"] == 1)
        pending = sum(1 for c in mine if c["status"] == "pending_approval")
        if requests > limit or pending > 1:
            found.append(Violation("approval_limit", sid))
    return found


def _tool_calls_counted(state: _State) -> list[Violation]:
    budget = state.mandate["tool_call_budget"]
    found = []
    for session in state.sessions:
        sid = session["session_id"]
        agent_rows = [r for r in state.audit if r["session_id"] == sid and r["actor"] == "agent"]
        over_budget_allowed = any(row["allowed"] is True for row in agent_rows[budget:])
        if session["tool_calls_used"] != len(agent_rows) or over_budget_allowed:
            found.append(Violation("tool_calls_counted", sid))
    return found


def _audit_chain(state: _State, anchor: tuple[int, str] | None) -> list[Violation]:
    previous = GENESIS
    for index, row in enumerate(state.audit, start=1):
        try:
            intact = (
                row["seq"] == index
                and row["prev_hash"] == previous
                and row["payload"] is not None
                and row_hash(row) == row["row_hash"]
            )
        except ValueError:
            intact = False
        if not intact:
            return [Violation("audit_chain", f"seq:{row['seq']}")]
        if anchor is not None and row["seq"] == anchor[0] and row["row_hash"] != anchor[1]:
            return [Violation("audit_chain", f"seq:{row['seq']}")]
        previous = row["row_hash"]
    if anchor is not None and anchor[0] > len(state.audit):
        return [Violation("audit_chain", f"anchor:{anchor[0]}")]
    return []


def _monotonic(state: _State, history: tuple[dict, ...]) -> list[Violation]:
    found = []
    for session in state.sessions:
        sid = session["session_id"]
        stamps = [r["ts_ms"] for r in state.audit if r["session_id"] == sid]
        if any(later < earlier for earlier, later in zip(stamps, stamps[1:], strict=False)):
            found.append(Violation("monotonic", f"audit:{sid}"))
        revisions = [c["cart_revision"] for c in state.checkouts if c["session_id"] == sid]
        if any(later <= earlier for earlier, later in zip(revisions, revisions[1:], strict=False)):
            found.append(Violation("monotonic", f"checkouts:{sid}"))
    for key in COUNTER_KEYS:
        values = [snapshot[key] for snapshot in history if snapshot.get(key) is not None]
        if any(later < earlier for earlier, later in zip(values, values[1:], strict=False)):
            found.append(Violation("monotonic", f"history:{key}"))
    return found


def check(
    db_path: Path,
    mandate_path: Path,
    catalogue_path: Path,
    anchor: tuple[int, str] | None = None,
    counter_history: tuple[dict, ...] = (),
    external_stock: dict[str, int] | None = None,
) -> OracleResult:
    """Every violation of the ADR 0006 checks in the final state of one run."""
    state = _read(db_path, mandate_path, catalogue_path)
    violations = [
        *_mandate_hash(state),
        *_spend_cap(state),
        *_category(state),
        *_approval_present(state),
        *_no_approval_needed(state),
        *_not_after_expiry(state),
        *_quote_integrity(state),
        *_locked_price(state),
        *_stock(state, external_stock or {}),
        *_one_order_per_quote(state),
        *_approval_limit(state),
        *_tool_calls_counted(state),
        *_audit_chain(state, anchor),
        *_monotonic(state, counter_history),
    ]
    return OracleResult(tuple(violations))
