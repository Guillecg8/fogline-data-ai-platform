"""Tool-call execution: one transaction per call, every call counted and audited (ADR 0004).

`Gateway.call(tool, arguments)` is what the MCP layer will call with raw arguments. The flow:
validate arguments, then in one `BEGIN IMMEDIATE` transaction: effective time, count the call,
lazy expiry, tool-call budget, the tool itself, the audit row. If the tool raises, that work is
rolled back and a second minimal transaction counts the call and audits `internal_error`.

The handler runs inside a SAVEPOINT: if it rejects its arguments after writing, those writes are
rolled back before the rejection is audited.

Three outcomes leave no trace, by design (ADR 0004): `store_busy` (the lock was not obtained),
`store_integrity` (the stored mandate no longer matches its hash; the call fails closed with no
state change) and a double fault (recording a failed call itself failed; `internal_error` is
returned and one fixed line goes to stderr).
"""

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from commerce_gateway import audit, orders
from commerce_gateway.clock import Clock, seconds_left
from commerce_gateway.models import LockedLine, ProductFacts
from commerce_gateway.policy import (
    AddToCart,
    Decision,
    RemoveFromCart,
    ToolCall,
    Verdict,
    evaluate,
)
from commerce_gateway.sessions import Session, count_call, effective_now, load_session
from commerce_gateway.store import (
    Store,
    StoreBusy,
    StoreIntegrityError,
    product_facts,
    product_texts,
)
from commerce_gateway.tools import (
    QUERY_MAX_CHARS,
    SEARCH_MAX_RESULTS,
    TOOL_NAMES,
    InvalidArguments,
    state_rejection,
    validate_arguments,
)
from commerce_gateway.untrusted import query_audit_fields, sanitize

# Store and protocol errors. These are not policy rule_ids; policy never returns them.
INVALID_ARGUMENTS = "invalid_arguments"
INTERNAL_ERROR = "internal_error"
SESSION_CLOSED = "session_closed"
STORE_BUSY = "store_busy"
STORE_INTEGRITY = "store_integrity"
CART_EMPTY = "cart_empty"

ERROR_REASONS = {
    INTERNAL_ERROR: "internal error; the call was counted and audited",
    SESSION_CLOSED: "this session was closed by a newer mandate",
    STORE_BUSY: "store busy; try again",
    STORE_INTEGRITY: "store integrity check failed; the gateway refuses to act",
    CART_EMPTY: "the cart is empty",
}

# Fixed text for outcomes that need an action from the agent. Any other allowed call has no reason.
STATE_REASONS = {
    "pending_approval": "a human must decide; use get_checkout_status; do not retry",
    "rejected": "this checkout was rejected; change the cart to try again",
    "expired": "the quote expired; add the lines again to re-lock their price",
}

TOOL_CALL_EVENT = "tool_call"
DOUBLE_FAULT_MESSAGE = "commerce_gateway: double fault; a failed tool call could not be recorded"
HANDLER_SAVEPOINT = "tool_handler"

NAME_MAX_CHARS = 120
DESCRIPTION_MAX_CHARS = 500
INGREDIENTS_MAX_CHARS = 500


class Outcome(StrEnum):
    """What one tool call did. `ok` covers read-only tools and cart changes.

    `request_checkout` answers with the checkout's status (completed, pending_approval, rejected,
    expired), or denied when a rule refused it and no checkout exists.
    """

    OK = "ok"
    COMPLETED = "completed"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True)
class ToolResult:
    allowed: bool
    outcome: Outcome
    rule_id: str | None = None
    error: str | None = None
    reason: str | None = None
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome is Outcome.ERROR:
            consistent = not self.allowed and self.error is not None and self.rule_id is None
        elif self.outcome is Outcome.DENIED:
            consistent = not self.allowed and self.error is None and self.rule_id is not None
        elif self.outcome is Outcome.PENDING_APPROVAL:
            consistent = self.allowed and self.error is None and self.rule_id == "approval_required"
        else:
            consistent = self.allowed and self.error is None and self.rule_id is None
        if self.outcome in (Outcome.ERROR, Outcome.DENIED):
            consistent = consistent and self.reason is not None
        else:
            consistent = consistent and self.reason == STATE_REASONS.get(str(self.outcome))
        if not consistent:
            raise ValueError(f"inconsistent tool result for outcome {self.outcome}")

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "outcome": str(self.outcome),
            "rule_id": self.rule_id,
            "error": self.error,
            "reason": self.reason,
            "data": self.data,
        }


def _ok(data: dict) -> ToolResult:
    return ToolResult(True, Outcome.OK, data=data)


def _allowed(outcome: Outcome, data: dict) -> ToolResult:
    """An allowed result: a reason only for states that need an agent action."""
    rule_id = "approval_required" if outcome is Outcome.PENDING_APPROVAL else None
    return ToolResult(True, outcome, rule_id, None, STATE_REASONS.get(str(outcome)), data)


def _from_decision(
    decision: Decision, data: dict | None = None, success: Outcome = Outcome.OK
) -> ToolResult:
    """`allowed` is false only for a denial; a needs-approval decision is allowed with its rule.

    A denial carries the policy's fixed reason and no data.
    """
    if decision.verdict is Verdict.DENIED:
        return ToolResult(False, Outcome.DENIED, decision.rule_id, None, decision.reason, {})
    if decision.verdict is Verdict.NEEDS_APPROVAL:
        return _allowed(Outcome.PENDING_APPROVAL, data or {})
    return _allowed(success, data or {})


def _error(code: str, reason: str | None = None) -> ToolResult:
    return ToolResult(False, Outcome.ERROR, None, code, reason or ERROR_REASONS[code])


@dataclass(frozen=True)
class _Outcome:
    result: ToolResult
    audit: dict


Handler = Callable[[sqlite3.Connection, Session, int, dict], _Outcome]


def _product_view(fact: ProductFacts, texts: tuple[str, str, str], detail: bool) -> dict:
    name, description, ingredients = texts
    untrusted = {
        "name": sanitize(name, NAME_MAX_CHARS)[0],
        "description": sanitize(description, DESCRIPTION_MAX_CHARS)[0],
    }
    if detail:
        untrusted["ingredients"] = sanitize(ingredients, INGREDIENTS_MAX_CHARS)[0]
    return {
        "sku": fact.sku,
        "policy_category": fact.policy_category,
        "unit_price_cents": fact.unit_price_cents,
        "price_version": fact.price_version,
        "stock": fact.stock,
        "untrusted_text": untrusted,
    }


def _cart_view(conn: sqlite3.Connection, session_id: str, now_ms: int) -> dict:
    lines = orders.cart_lines(conn, session_id)
    return {
        "cart_revision": orders.cart_revision(conn, session_id),
        "frozen": orders.pending_checkout(conn, session_id) is not None,
        "lines": [
            {
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price_cents": line.unit_price_cents,
                "price_version": line.price_version,
                "lock_expires_at_ms": line.lock_expires_at_ms,
                "seconds_left": seconds_left(line.lock_expires_at_ms, now_ms),
                "line_total_cents": line.line_total_cents,
                "stale": now_ms >= line.lock_expires_at_ms,
            }
            for line in lines
        ],
        "total_cents": sum(line.line_total_cents for line in lines),
    }


def _get_policy(conn: sqlite3.Connection, session: Session, now_ms: int, args: dict) -> _Outcome:
    completed, pending = orders.spend(conn, session.session_id)
    fresh = load_session(conn, session.session_id)
    data = {
        "mandate": session.mandate.content(),
        "mandate_hash": session.mandate_hash,
        "spent_cents": completed,
        "reserved_cents": pending,
        "remaining_cents": session.mandate.budget_cents - completed - pending,
        "approval_requests_used": orders.approval_requests_used(conn, session.session_id),
        "tool_calls_used": fresh.tool_calls_used,
    }
    return _Outcome(_ok(data), {})


def _search_products(
    conn: sqlite3.Connection, session: Session, now_ms: int, args: dict
) -> _Outcome:
    query = args["query"]
    tokens = sanitize(query, QUERY_MAX_CHARS)[0].casefold().split()
    texts = product_texts(conn)
    matches = [
        sku
        for sku, (name, description, _) in texts.items()
        if all(token in f"{name} {description}".casefold() for token in tokens)
    ][:SEARCH_MAX_RESULTS]
    facts = product_facts(conn, tuple(matches), now_ms)
    results = [_product_view(fact, texts[fact.sku], detail=False) for fact in facts]
    record = {**query_audit_fields(query, QUERY_MAX_CHARS), "result_skus": matches}
    return _Outcome(_ok({"results": results}), record)


def _one_fact(conn: sqlite3.Connection, args: dict, now_ms: int) -> ProductFacts:
    facts = product_facts(conn, (args["sku"],), now_ms)
    if not facts:
        raise state_rejection("unknown sku", "sku", "known_sku", args)
    return facts[0]


def _get_product(conn: sqlite3.Connection, session: Session, now_ms: int, args: dict) -> _Outcome:
    fact = _one_fact(conn, args, now_ms)
    view = _product_view(fact, product_texts(conn)[fact.sku], detail=True)
    return _Outcome(_ok({"product": view}), {"sku": fact.sku})


def _add_to_cart(conn: sqlite3.Connection, session: Session, now_ms: int, args: dict) -> _Outcome:
    sku, quantity = args["sku"], args["quantity"]
    fact = _one_fact(conn, args, now_ms)
    sid = session.session_id
    quantity_after = orders.cart_quantity(conn, sid, sku) + quantity
    frozen = orders.pending_checkout(conn, sid) is not None
    decision = evaluate(AddToCart(sku, quantity_after, frozen), (fact,), session.mandate, now_ms)
    record = {"sku": sku, "quantity": quantity, "quantity_after": quantity_after}
    if decision.denied:
        return _Outcome(_from_decision(decision), record)
    line = LockedLine(
        sku=sku,
        quantity=quantity_after,
        unit_price_cents=fact.unit_price_cents,
        price_version=fact.price_version,
        lock_expires_at_ms=now_ms + session.mandate.price_lock_ttl_ms,
    )
    orders.put_cart_line(conn, sid, line)
    record |= {"price_version": line.price_version, "lock_expires_at_ms": line.lock_expires_at_ms}
    return _Outcome(_from_decision(decision, {"cart": _cart_view(conn, sid, now_ms)}), record)


def _remove_from_cart(
    conn: sqlite3.Connection, session: Session, now_ms: int, args: dict
) -> _Outcome:
    sku, quantity = args["sku"], args["quantity"]
    sid = session.session_id
    frozen = orders.pending_checkout(conn, sid) is not None
    decision = evaluate(RemoveFromCart(frozen), (), session.mandate, now_ms)
    record = {"sku": sku, "quantity": quantity}
    if decision.denied:
        return _Outcome(_from_decision(decision), record)
    in_cart = orders.cart_quantity(conn, sid, sku)
    if in_cart == 0:
        raise state_rejection("sku is not in the cart", "sku", "sku_in_cart", args)
    if quantity > in_cart:
        reason = "quantity exceeds the quantity in the cart"
        raise state_rejection(reason, "quantity", "quantity_in_cart", args)
    orders.reduce_cart_line(conn, sid, sku, quantity)
    return _Outcome(_from_decision(decision, {"cart": _cart_view(conn, sid, now_ms)}), record)


def _view_cart(conn: sqlite3.Connection, session: Session, now_ms: int, args: dict) -> _Outcome:
    cart = _cart_view(conn, session.session_id, now_ms)
    return _Outcome(_ok({"cart": cart}), {})


def _existing_checkout(existing: orders.Checkout, now_ms: int) -> ToolResult:
    """The idempotent answer to an unchanged cart: the checkout it already produced.

    The outcome is the checkout's current status. The call itself is not a denial, so it is
    allowed; a rule that rejected the checkout at finalisation is in `data.checkout.rule_id`.
    """
    return _allowed(Outcome(existing.status), {"checkout": existing.view(now_ms)})


def _request_checkout(
    conn: sqlite3.Connection, session: Session, now_ms: int, args: dict
) -> _Outcome:
    sid = session.session_id
    lines = orders.cart_lines(conn, sid)
    if not lines:
        return _Outcome(_error(CART_EMPTY), {"error": CART_EMPTY})
    quote = orders.Quote(sid, orders.cart_revision(conn, sid), lines)
    existing = orders.find_checkout(conn, sid, quote.quote_hash)
    if existing is not None:
        record = {
            "checkout_id": existing.checkout_id,
            "quote_hash": existing.quote_hash,
            "status": existing.status,
            "idempotent": True,
        }
        return _Outcome(_existing_checkout(existing, now_ms), record)
    decision, checkout = orders.place_quote(conn, session.mandate, quote, now_ms)
    record = {
        "checkout_id": checkout.checkout_id if checkout else None,
        "quote_hash": quote.quote_hash,
        "status": checkout.status if checkout else None,
        "total_cents": quote.total_cents,
        "idempotent": False,
    }
    data = {"checkout": checkout.view(now_ms)} if checkout else {}
    return _Outcome(_from_decision(decision, data, success=Outcome.COMPLETED), record)


def _get_checkout_status(
    conn: sqlite3.Connection, session: Session, now_ms: int, args: dict
) -> _Outcome:
    sid = session.session_id
    latest = orders.latest_checkout(conn, sid)
    if latest is None:
        return _Outcome(_ok({"checkout": None}), {"checkout_id": None})
    # Any cart change raises the revision, so an equal revision means the same lines.
    matches = latest.cart_revision == orders.cart_revision(conn, sid)
    view = {**latest.view(now_ms), "matches_current_cart": matches}
    record = {"checkout_id": latest.checkout_id, "matches_current_cart": matches}
    return _Outcome(_ok({"checkout": view}), record)


HANDLERS: dict[str, Handler] = {
    "get_policy": _get_policy,
    "search_products": _search_products,
    "get_product": _get_product,
    "add_to_cart": _add_to_cart,
    "remove_from_cart": _remove_from_cart,
    "view_cart": _view_cart,
    "request_checkout": _request_checkout,
    "get_checkout_status": _get_checkout_status,
}


class Gateway:
    """The agent's side of the store, bound to one session."""

    def __init__(self, store: Store, session_id: str, clock: Clock) -> None:
        self._store = store
        self._session_id = session_id
        self._clock = clock

    def call(self, tool: object, arguments: object) -> ToolResult:
        """Run one tool call. Never raises: every failure becomes a `ToolResult` error."""
        try:
            return self._call(tool, arguments)
        except Exception:
            # Double fault: recording the failure failed too. Nothing can be recorded, so say so
            # on stderr with a fixed line (never the exception text) and report internal_error.
            print(DOUBLE_FAULT_MESSAGE, file=sys.stderr)
            return _error(INTERNAL_ERROR)

    def _call(self, tool: object, arguments: object) -> ToolResult:
        tool_name = tool if type(tool) is str and tool in TOOL_NAMES else None
        try:
            args = validate_arguments(tool, arguments)
        except InvalidArguments as exc:
            return self._record_failure(tool_name, INVALID_ARGUMENTS, exc.reason, exc.audit)
        except Exception:
            # A bug in validation, not a malformed call: count and audit it once as internal_error.
            return self._record_failure(
                tool_name, INTERNAL_ERROR, ERROR_REASONS[INTERNAL_ERROR], {}
            )
        try:
            with self._store.transaction() as conn:
                return self._run(conn, tool_name, args)
        except StoreBusy:
            return _error(STORE_BUSY)
        except StoreIntegrityError:
            return _error(STORE_INTEGRITY)
        except Exception:
            # The exception text is never stored or returned: it may contain arguments.
            return self._record_failure(
                tool_name, INTERNAL_ERROR, ERROR_REASONS[INTERNAL_ERROR], {}
            )

    def _append(self, conn: sqlite3.Connection, now_ms: int, **fields: object) -> None:
        audit.append(conn, ts_ms=now_ms, session_id=self._session_id, actor="agent", **fields)

    def _run(self, conn: sqlite3.Connection, tool: str, args: dict) -> ToolResult:
        session = load_session(conn, self._session_id)
        now_ms = effective_now(conn, self._session_id, self._clock)
        calls_before = count_call(conn, self._session_id)
        if not session.is_open:
            result = _error(SESSION_CLOSED)
            self._append(conn, now_ms, event=SESSION_CLOSED, tool=tool, allowed=False, payload={})
            return result
        orders.expire_pending(conn, self._session_id, now_ms)
        budget = evaluate(ToolCall(calls_before), (), session.mandate, now_ms)
        if budget.denied:
            self._append(
                conn,
                now_ms,
                event=TOOL_CALL_EVENT,
                tool=tool,
                allowed=False,
                rule_id=budget.rule_id,
                payload={},
            )
            return _from_decision(budget)
        conn.execute(f"SAVEPOINT {HANDLER_SAVEPOINT}")
        try:
            outcome = HANDLERS[tool](conn, session, now_ms, args)
        except InvalidArguments as exc:
            # Undo anything the handler wrote before it rejected its arguments.
            conn.execute(f"ROLLBACK TO {HANDLER_SAVEPOINT}")
            conn.execute(f"RELEASE {HANDLER_SAVEPOINT}")
            payload = {"reason": exc.reason, **exc.audit}
            self._append(
                conn, now_ms, event=INVALID_ARGUMENTS, tool=tool, allowed=False, payload=payload
            )
            return _error(INVALID_ARGUMENTS, exc.reason)
        conn.execute(f"RELEASE {HANDLER_SAVEPOINT}")
        self._append(
            conn,
            now_ms,
            event=TOOL_CALL_EVENT,
            tool=tool,
            allowed=outcome.result.allowed,
            rule_id=outcome.result.rule_id,
            payload=outcome.audit,
        )
        return outcome.result

    def _record_failure(self, tool: str | None, code: str, reason: str, extra: dict) -> ToolResult:
        """Count and audit a call that did no work: malformed, or failed with an exception."""
        try:
            with self._store.transaction() as conn:
                session = load_session(conn, self._session_id)
                now_ms = effective_now(conn, self._session_id, self._clock)
                count_call(conn, self._session_id)
                if session.is_open:
                    orders.expire_pending(conn, self._session_id, now_ms)
                payload = {"reason": reason, **extra}
                self._append(conn, now_ms, event=code, tool=tool, allowed=False, payload=payload)
        except StoreBusy:
            return _error(STORE_BUSY)
        except StoreIntegrityError:
            return _error(STORE_INTEGRITY)
        return _error(code, reason)
