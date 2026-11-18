"""Cart lines, quotes and checkouts. Every function runs inside the caller's write transaction.

Committed spend and the approval-request count are derived from the checkouts table, never kept
as counters (ADR 0004). Expiry is lazy: `expire_pending` is called at the start of every
transaction that touches a session, with the boundary `now >= expires_at_ms`.
"""

import json
import sqlite3
from dataclasses import dataclass

from commerce_gateway import audit
from commerce_gateway.canonical import canonical_hash, canonical_text
from commerce_gateway.clock import seconds_left
from commerce_gateway.mandate import Mandate
from commerce_gateway.models import LockedLine, require_int
from commerce_gateway.policy import ALLOW, NEEDS_APPROVAL, Decision, RequestCheckout, evaluate
from commerce_gateway.store import product_facts

COMPLETED = "completed"
PENDING_APPROVAL = "pending_approval"
REJECTED = "rejected"
EXPIRED = "expired"

QUOTE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Quote:
    """A frozen cart: what an approval refers to, identified by `quote_hash`."""

    session_id: str
    cart_revision: int
    lines: tuple[LockedLine, ...]

    def __post_init__(self) -> None:
        if type(self.session_id) is not str:
            raise ValueError("session_id must be a string")
        require_int("cart_revision", self.cart_revision)
        skus = [line.sku for line in self.lines]
        if not skus or skus != sorted(set(skus)):
            raise ValueError("a quote has at least one line, one per SKU, in SKU order")

    @property
    def total_cents(self) -> int:
        return sum(line.line_total_cents for line in self.lines)

    @property
    def expires_at_ms(self) -> int:
        """A quote expires when its earliest price lock expires."""
        return min(line.lock_expires_at_ms for line in self.lines)

    def content(self) -> dict[str, object]:
        return {
            "format": QUOTE_FORMAT_VERSION,
            "session_id": self.session_id,
            "cart_revision": self.cart_revision,
            "lines": [line.as_list() for line in self.lines],
            "total_cents": self.total_cents,
        }

    @property
    def quote_hash(self) -> str:
        return canonical_hash(self.content())


def quote_from_content(content: dict) -> Quote:
    if content.get("format") != QUOTE_FORMAT_VERSION:
        raise ValueError("unsupported quote format")
    quote = Quote(
        session_id=content["session_id"],
        cart_revision=content["cart_revision"],
        lines=tuple(LockedLine(*line) for line in content["lines"]),
    )
    if quote.total_cents != content["total_cents"]:
        raise ValueError("quote total does not match its lines")
    return quote


@dataclass(frozen=True)
class Checkout:
    checkout_id: int
    session_id: str
    cart_revision: int
    quote_hash: str
    quote: Quote | None  # None if the stored quote no longer matches quote_hash
    total_cents: int
    expires_at_ms: int
    status: str
    needed_approval: bool
    decision_rule_id: str | None

    def __post_init__(self) -> None:
        require_int("checkout_id", self.checkout_id, 1)
        require_int("cart_revision", self.cart_revision)
        require_int("total_cents", self.total_cents)
        require_int("expires_at_ms", self.expires_at_ms)

    def view(self, now_ms: int) -> dict[str, object]:
        """What the agent sees. `seconds_left` is from the transaction's effective time."""
        return {
            "checkout_id": self.checkout_id,
            "status": self.status,
            "quote_hash": self.quote_hash,
            "cart_revision": self.cart_revision,
            "total_cents": self.total_cents,
            "expires_at_ms": self.expires_at_ms,
            "seconds_left": seconds_left(self.expires_at_ms, now_ms),
            "rule_id": self.decision_rule_id,
        }


_CHECKOUT_COLUMNS = (
    "checkout_id, session_id, cart_revision, quote_hash, quote, total_cents, expires_at_ms, status,"
    " needed_approval, decision_rule_id"
)


def _checkout(row: tuple) -> Checkout:
    checkout_id, session_id, revision, quote_hash, quote_text, total = row[:6]
    expires, status, needed, rule = row[6:]
    try:
        quote = quote_from_content(json.loads(quote_text))
        if quote.quote_hash != quote_hash or quote.total_cents != total:
            quote = None
    except (ValueError, KeyError, TypeError):
        quote = None
    return Checkout(
        checkout_id,
        session_id,
        revision,
        quote_hash,
        quote,
        total,
        expires,
        status,
        bool(needed),
        rule,
    )


def _one_checkout(conn: sqlite3.Connection, where: str, params: tuple) -> Checkout | None:
    rows = conn.execute(f"SELECT {_CHECKOUT_COLUMNS} FROM checkouts WHERE {where}", params)
    found = rows.fetchall()
    return _checkout(found[0]) if found else None


def get_checkout(conn: sqlite3.Connection, checkout_id: int) -> Checkout | None:
    return _one_checkout(conn, "checkout_id = ?", (checkout_id,))


def find_checkout(conn: sqlite3.Connection, session_id: str, quote_hash: str) -> Checkout | None:
    return _one_checkout(conn, "session_id = ? AND quote_hash = ?", (session_id, quote_hash))


def pending_checkout(conn: sqlite3.Connection, session_id: str) -> Checkout | None:
    return _one_checkout(conn, "session_id = ? AND status = ?", (session_id, PENDING_APPROVAL))


def latest_checkout(conn: sqlite3.Connection, session_id: str) -> Checkout | None:
    return _one_checkout(
        conn,
        "checkout_id = (SELECT max(checkout_id) FROM checkouts WHERE session_id = ?)",
        (session_id,),
    )


def spend(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    """(completed cents, pending cents) for the session."""
    rows = conn.execute(
        "SELECT coalesce(sum(CASE WHEN status = ? THEN total_cents END), 0),"
        " coalesce(sum(CASE WHEN status = ? THEN total_cents END), 0)"
        " FROM checkouts WHERE session_id = ?",
        (COMPLETED, PENDING_APPROVAL, session_id),
    ).fetchall()
    return rows[0][0], rows[0][1]


def approval_requests_used(conn: sqlite3.Connection, session_id: str) -> int:
    rows = conn.execute(
        "SELECT count(*) FROM checkouts WHERE session_id = ? AND needed_approval = 1",
        (session_id,),
    ).fetchall()
    return rows[0][0]


def cart_revision(conn: sqlite3.Connection, session_id: str) -> int:
    rows = conn.execute(
        "SELECT cart_revision FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchall()
    return rows[0][0]


def bump_cart_revision(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET cart_revision = cart_revision + 1 WHERE session_id = ?",
        (session_id,),
    )


def cart_lines(conn: sqlite3.Connection, session_id: str) -> tuple[LockedLine, ...]:
    rows = conn.execute(
        "SELECT sku, quantity, unit_price_cents, price_version, lock_expires_at_ms"
        " FROM cart_lines WHERE session_id = ? ORDER BY sku",
        (session_id,),
    ).fetchall()
    return tuple(LockedLine(*row) for row in rows)


def cart_quantity(conn: sqlite3.Connection, session_id: str, sku: str) -> int:
    rows = conn.execute(
        "SELECT quantity FROM cart_lines WHERE session_id = ? AND sku = ?", (session_id, sku)
    ).fetchall()
    return rows[0][0] if rows else 0


def put_cart_line(conn: sqlite3.Connection, session_id: str, line: LockedLine) -> None:
    """Insert or replace the line for `line.sku`, then raise the cart revision."""
    conn.execute(
        "INSERT INTO cart_lines (session_id, sku, quantity, unit_price_cents, price_version,"
        " lock_expires_at_ms) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (session_id, sku) DO UPDATE SET quantity = excluded.quantity,"
        " unit_price_cents = excluded.unit_price_cents, price_version = excluded.price_version,"
        " lock_expires_at_ms = excluded.lock_expires_at_ms",
        (session_id, *line.as_list()),
    )
    bump_cart_revision(conn, session_id)


def reduce_cart_line(conn: sqlite3.Connection, session_id: str, sku: str, quantity: int) -> None:
    """Remove `quantity` units (the caller checked it is at most what is in the cart)."""
    if quantity < cart_quantity(conn, session_id, sku):
        conn.execute(
            "UPDATE cart_lines SET quantity = quantity - ? WHERE session_id = ? AND sku = ?",
            (quantity, session_id, sku),
        )
    else:
        conn.execute("DELETE FROM cart_lines WHERE session_id = ? AND sku = ?", (session_id, sku))
    bump_cart_revision(conn, session_id)


def set_status(
    conn: sqlite3.Connection,
    checkout_id: int,
    status: str,
    now_ms: int,
    decided_by: str,
    rule_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE checkouts SET status = ?, decided_at_ms = ?, decided_by = ?, decision_rule_id = ?"
        " WHERE checkout_id = ?",
        (status, now_ms, decided_by, rule_id, checkout_id),
    )


def expire_pending(conn: sqlite3.Connection, session_id: str, now_ms: int) -> tuple[int, ...]:
    """Lazy expiry: pending checkouts with `now >= expires_at_ms` become expired and audited."""
    rows = conn.execute(
        "SELECT checkout_id, quote_hash FROM checkouts"
        " WHERE session_id = ? AND status = ? AND expires_at_ms <= ? ORDER BY checkout_id",
        (session_id, PENDING_APPROVAL, now_ms),
    ).fetchall()
    for checkout_id, quote_hash in rows:
        set_status(conn, checkout_id, EXPIRED, now_ms, "system")
        audit.append(
            conn,
            ts_ms=now_ms,
            session_id=session_id,
            actor="system",
            event="checkout_expired",
            payload={"checkout_id": checkout_id, "quote_hash": quote_hash},
        )
    return tuple(checkout_id for checkout_id, _ in rows)


def fulfil(conn: sqlite3.Connection, quote: Quote) -> None:
    """Take the stock and, if the cart is still the quoted revision, empty it."""
    for line in quote.lines:
        conn.execute(
            "UPDATE products SET stock = stock - ? WHERE sku = ?", (line.quantity, line.sku)
        )
    if cart_revision(conn, quote.session_id) == quote.cart_revision:
        conn.execute("DELETE FROM cart_lines WHERE session_id = ?", (quote.session_id,))
        bump_cart_revision(conn, quote.session_id)


def place_quote(
    conn: sqlite3.Connection, mandate: Mandate, quote: Quote, now_ms: int
) -> tuple[Decision, Checkout | None]:
    """Decide a new quote. Allowed: completed. Above threshold: pending. Denied: no checkout."""
    completed, pending = spend(conn, quote.session_id)
    action = RequestCheckout(
        lines=quote.lines,
        committed_cents=completed + pending,
        approval_requests_used=approval_requests_used(conn, quote.session_id),
        pending_exists=pending_checkout(conn, quote.session_id) is not None,
    )
    facts = product_facts(conn, tuple(line.sku for line in quote.lines), now_ms)
    decision = evaluate(action, facts, mandate, now_ms)
    if decision == ALLOW:
        status, needed, decided_at, decided_by = COMPLETED, 0, now_ms, "gateway"
    elif decision == NEEDS_APPROVAL:
        status, needed, decided_at, decided_by = PENDING_APPROVAL, 1, None, None
    else:
        return decision, None
    cursor = conn.execute(
        "INSERT INTO checkouts (session_id, cart_revision, quote_hash, quote, total_cents,"
        " expires_at_ms, status, needed_approval, created_at_ms, decided_at_ms, decided_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            quote.session_id,
            quote.cart_revision,
            quote.quote_hash,
            canonical_text(quote.content()),
            quote.total_cents,
            quote.expires_at_ms,
            status,
            needed,
            now_ms,
            decided_at,
            decided_by,
        ),
    )
    if status == COMPLETED:
        fulfil(conn, quote)
    checkout = get_checkout(conn, cursor.lastrowid)
    return decision, checkout
