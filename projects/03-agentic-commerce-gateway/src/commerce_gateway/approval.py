"""The approver's side: list, approve and reject pending checkouts (ADR 0003, ADR 0004).

These functions back the approval CLI, which the agent cannot call. They act as the fixed actor
`approver`, use the mandate snapshot stored in the session, and apply the same lazy expiry as the
gateway. An approval is valid only for its quote hash and only before the quote expires.
"""

import re
from dataclasses import dataclass

from commerce_gateway import audit, orders
from commerce_gateway.audit import Head
from commerce_gateway.clock import Clock
from commerce_gateway.models import require_int
from commerce_gateway.policy import (
    ALLOW,
    DENY_MISMATCH,
    QUOTE_MISMATCH,
    Decision,
    FinaliseApproval,
    Verdict,
    evaluate,
)
from commerce_gateway.sessions import effective_now, load_session, open_session_id
from commerce_gateway.store import Store, StoreError, product_facts, product_texts
from commerce_gateway.untrusted import sanitize

APPROVER = "approver"
APPROVAL_EVENT = "approval_decision"
UNTRUSTED_NAME_MAX_CHARS = 60
_QUOTE_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class PendingLine:
    sku: str
    policy_category: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int
    untrusted_name: str

    def __post_init__(self) -> None:
        require_int("quantity", self.quantity, 1)
        require_int("unit_price_cents", self.unit_price_cents)
        require_int("line_total_cents", self.line_total_cents)


@dataclass(frozen=True)
class PendingCheckout:
    checkout_id: int
    quote_hash: str
    total_cents: int
    expires_at_ms: int
    lines: tuple[PendingLine, ...]

    def __post_init__(self) -> None:
        require_int("checkout_id", self.checkout_id, 1)
        require_int("total_cents", self.total_cents)
        require_int("expires_at_ms", self.expires_at_ms)


@dataclass(frozen=True)
class ApprovalOutcome:
    decision: Decision
    status: str | None
    head: Head


def list_pending(store: Store, clock: Clock) -> tuple[PendingCheckout, ...]:
    """The open session's pending checkout, if any. Applies lazy expiry, so it takes the lock."""
    with store.transaction() as conn:
        session_id = open_session_id(conn)
        if session_id is None:
            return ()
        load_session(conn, session_id)  # integrity check: fails closed on a tampered mandate
        now_ms = effective_now(conn, session_id, clock)
        orders.expire_pending(conn, session_id, now_ms)
        pending = orders.pending_checkout(conn, session_id)
        if pending is None or pending.quote is None:
            return ()
        skus = tuple(line.sku for line in pending.quote.lines)
        categories = {f.sku: f.policy_category for f in product_facts(conn, skus, now_ms)}
        texts = product_texts(conn)
        lines = tuple(
            PendingLine(
                sku=line.sku,
                policy_category=categories.get(line.sku, "unknown"),
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                line_total_cents=line.line_total_cents,
                untrusted_name=sanitize(texts[line.sku][0], UNTRUSTED_NAME_MAX_CHARS)[0],
            )
            for line in pending.quote.lines
        )
        return (
            PendingCheckout(
                pending.checkout_id,
                pending.quote_hash,
                pending.total_cents,
                pending.expires_at_ms,
                lines,
            ),
        )


def approve(store: Store, clock: Clock, checkout_id: int, quote_hash: str) -> ApprovalOutcome:
    return _decide(store, clock, checkout_id, quote_hash, approve=True)


def reject(store: Store, clock: Clock, checkout_id: int, quote_hash: str) -> ApprovalOutcome:
    return _decide(store, clock, checkout_id, quote_hash, approve=False)


def _decide(
    store: Store, clock: Clock, checkout_id: int, quote_hash: str, approve: bool
) -> ApprovalOutcome:
    if type(checkout_id) is not int:
        raise ValueError("checkout_id must be an integer")
    well_formed = type(quote_hash) is str and _QUOTE_HASH.fullmatch(quote_hash)
    clean_hash = quote_hash if well_formed else None
    with store.transaction() as conn:
        found = orders.get_checkout(conn, checkout_id)
        session_id = found.session_id if found else open_session_id(conn)
        if session_id is None:
            raise StoreError("no session to decide in")
        session = load_session(conn, session_id)
        now_ms = effective_now(conn, session_id, clock)
        orders.expire_pending(conn, session_id, now_ms)
        checkout = orders.get_checkout(conn, checkout_id)
        is_pending = (
            checkout is not None
            and checkout.session_id == session_id
            and checkout.status == orders.PENDING_APPROVAL
            and checkout.quote is not None
        )
        lines = checkout.quote.lines if is_pending else ()
        completed, pending_cents = orders.spend(conn, session_id)
        action = FinaliseApproval(
            quote_hash=clean_hash or "",
            pending_quote_hash=checkout.quote_hash if is_pending else None,
            quote_expires_at_ms=checkout.expires_at_ms if is_pending else 0,
            lines=lines,
            committed_other_cents=completed
            + pending_cents
            - (checkout.total_cents if is_pending else 0),
        )
        facts = product_facts(conn, tuple(line.sku for line in lines), now_ms)
        decision = evaluate(action, facts, session.mandate, now_ms)
        if not approve:
            decision = DENY_MISMATCH if decision.rule_id == QUOTE_MISMATCH else ALLOW
            if decision.verdict is Verdict.ALLOWED:
                orders.set_status(conn, checkout_id, orders.REJECTED, now_ms, APPROVER)
        elif decision.verdict is Verdict.ALLOWED:
            orders.set_status(conn, checkout_id, orders.COMPLETED, now_ms, APPROVER)
            orders.fulfil(conn, checkout.quote)
        elif decision.rule_id != QUOTE_MISMATCH:
            orders.set_status(
                conn, checkout_id, orders.REJECTED, now_ms, "gateway", decision.rule_id
            )
        final = orders.get_checkout(conn, checkout_id)
        status = final.status if final else None
        audit.append(
            conn,
            ts_ms=now_ms,
            session_id=session_id,
            actor=APPROVER,
            event=APPROVAL_EVENT,
            allowed=not decision.denied,
            rule_id=decision.rule_id,
            payload={
                "action": "approve" if approve else "reject",
                "checkout_id": checkout_id,
                "quote_hash": clean_hash,
                "status": status,
            },
        )
        return ApprovalOutcome(decision, status, audit.head(conn))
