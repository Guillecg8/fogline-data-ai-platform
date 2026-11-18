"""The approver's side: list, approve and reject pending checkouts (ADR 0003, ADR 0004).

These functions back the approval CLI (`python -m commerce_gateway.approval`, ADR 0005), which
the agent cannot call. They act as the fixed actor `approver`, use the mandate snapshot stored in
the session, and apply the same lazy expiry as the gateway. An approval is valid only for its
quote hash and only before the quote expires. The CLI uses the system clock, with no override.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from commerce_gateway import audit, orders
from commerce_gateway.audit import Head
from commerce_gateway.clock import Clock, SystemClock
from commerce_gateway.mandate import MandateError, parse_mandate
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
from commerce_gateway.store import (
    Store,
    StoreBusy,
    StoreError,
    StoreIntegrityError,
    product_facts,
    product_texts,
)
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
    untrusted_name_truncated: bool = False

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
        names = {sku: sanitize(texts[sku][0], UNTRUSTED_NAME_MAX_CHARS) for sku in skus}
        lines = tuple(
            PendingLine(
                sku=line.sku,
                policy_category=categories.get(line.sku, "unknown"),
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                line_total_cents=line.line_total_cents,
                untrusted_name=names[line.sku][0],
                untrusted_name_truncated=names[line.sku][1],
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


# --- Command-line interface -------------------------------------------------------------------

DB_ENV = "COMMERCE_GATEWAY_DB"
EXIT_OK = 0
EXIT_REFUSED = 1  # a decision was refused, verification failed or a mandate is invalid
EXIT_USAGE = 2
EXIT_STORE = 3

_PREFIX = "commerce_gateway approval: "
LINE_NO_DB = _PREFIX + f"{DB_ENV} must be set"
LINE_DB = _PREFIX + "the database cannot be opened"
LINE_INTEGRITY = _PREFIX + "store integrity check failed"
LINE_BUSY = _PREFIX + "store busy"
LINE_NO_SESSION = _PREFIX + "there is no session to decide in"
_ANCHOR = re.compile(r"([0-9]+):([0-9a-f]{64})")


class _Refused(Exception):
    def __init__(self, code: int, line: str) -> None:
        super().__init__(line)
        self.code = code
        self.line = line


def _untrusted(name: str, truncated: bool) -> str:
    """Product text for a human: marked, quoted and escaped, so it cannot pose as CLI output."""
    marker = "[untrusted, truncated]" if truncated else "[untrusted]"
    return f"{marker} {json.dumps(name, ensure_ascii=True)}"


def _open_store() -> Store:
    value = os.environ.get(DB_ENV, "")
    if not value:
        raise _Refused(EXIT_USAGE, LINE_NO_DB)
    path = Path(value)
    if not path.is_file():  # never create a database from the CLI
        raise _Refused(EXIT_STORE, LINE_DB)
    try:
        return Store.open(path)
    except StoreBusy as exc:
        raise _Refused(EXIT_STORE, LINE_BUSY) from exc
    except StoreIntegrityError as exc:
        raise _Refused(EXIT_STORE, LINE_INTEGRITY) from exc
    except (StoreError, sqlite3.Error, OSError, ValueError) as exc:
        raise _Refused(EXIT_STORE, LINE_DB) from exc


def _print_head(head: Head) -> None:
    print(f"audit head {head[0]} {head[1]}")


def _cmd_list(store: Store, args: argparse.Namespace) -> int:
    pending = list_pending(store, SystemClock())
    if not pending:
        print("no pending checkout")
    for checkout in pending:
        print(f"checkout {checkout.checkout_id} pending_approval")
        print(f"  quote_hash {checkout.quote_hash}")
        print(f"  total_cents {checkout.total_cents}")
        print(f"  expires_at_ms {checkout.expires_at_ms}")
        for line in checkout.lines:
            print(
                f"  line {line.sku} {line.policy_category} quantity {line.quantity}"
                f" unit_price_cents {line.unit_price_cents}"
                f" line_total_cents {line.line_total_cents}"
            )
            print(f"    name {_untrusted(line.untrusted_name, line.untrusted_name_truncated)}")
    return EXIT_OK


def _cmd_decide(store: Store, args: argparse.Namespace) -> int:
    decide = approve if args.command == "approve" else reject
    outcome = decide(store, SystemClock(), args.checkout_id, args.quote_hash)
    print(f"decision {'denied' if outcome.decision.denied else 'allowed'}")
    print(f"rule_id {outcome.decision.rule_id or 'none'}")
    print(f"status {outcome.status or 'none'}")
    _print_head(outcome.head)
    return EXIT_REFUSED if outcome.decision.denied else EXIT_OK


def _cmd_audit_head(store: Store, args: argparse.Namespace) -> int:
    _print_head(audit.head(store.conn))
    return EXIT_OK


def _cmd_verify(store: Store, args: argparse.Namespace) -> int:
    result = audit.verify(store.conn, args.anchor)
    if result.ok:
        print(f"ok rows {result.rows}")
    else:
        where = "" if result.first_bad_seq is None else f" at seq {result.first_bad_seq}"
        print(f"failed{where}: {result.reason}")
        print(f"rows {result.rows}")
    _print_head(result.head)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _cmd_check_mandate(args: argparse.Namespace) -> int:
    """Validate a mandate file. Errors are field paths and fixed reasons, never values."""
    try:
        mandate = parse_mandate(Path(args.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        print("(file): cannot be read as UTF-8 text")
        return EXIT_REFUSED
    except MandateError as exc:
        for problem in exc.problems:
            print(str(problem))
        return EXIT_REFUSED
    print(f"content_hash {mandate.content_hash}")
    print(f"session_label {mandate.session_label}")
    return EXIT_OK


def _anchor(text: str) -> Head:
    match = _ANCHOR.fullmatch(text)
    if match is None:
        raise argparse.ArgumentTypeError("expected <seq>:<64 lowercase hex characters>")
    return int(match.group(1)), match.group(2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m commerce_gateway.approval",
        description=f"Decide pending checkouts and check the audit trail. Uses {DB_ENV}.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show the pending checkout")
    for name in ("approve", "reject"):
        command = commands.add_parser(name, help=f"{name} a pending checkout by its quote hash")
        command.add_argument("checkout_id", type=int)
        command.add_argument("quote_hash")
    commands.add_parser("audit-head", help="print the latest audit seq and hash")
    verify = commands.add_parser("verify", help="verify the audit hash chain")
    verify.add_argument("--anchor", type=_anchor, default=None, help="<seq>:<row_hash>")
    check = commands.add_parser("check-mandate", help="validate a mandate file")
    check.add_argument("path")
    return parser


_STORE_COMMANDS = {
    "list": _cmd_list,
    "approve": _cmd_decide,
    "reject": _cmd_decide,
    "audit-head": _cmd_audit_head,
    "verify": _cmd_verify,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check-mandate":
        return _cmd_check_mandate(args)
    try:
        store = _open_store()
    except _Refused as refused:
        print(refused.line, file=sys.stderr)
        return refused.code
    try:
        return _STORE_COMMANDS[args.command](store, args)
    except StoreIntegrityError:
        print(LINE_INTEGRITY, file=sys.stderr)
        return EXIT_STORE
    except StoreBusy:
        print(LINE_BUSY, file=sys.stderr)
        return EXIT_STORE
    except StoreError:
        print(LINE_NO_SESSION, file=sys.stderr)
        return EXIT_STORE
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
