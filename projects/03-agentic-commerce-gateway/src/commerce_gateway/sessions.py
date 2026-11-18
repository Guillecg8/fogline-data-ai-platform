"""Sessions: one mandate governing one principal's spending (ADR 0004).

Server start resumes the open session when the mandate hash is unchanged, and supersedes it when
the hash differs, unless the open session still has an unexpired pending checkout.
"""

import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from commerce_gateway import audit, orders
from commerce_gateway.canonical import canonical_text
from commerce_gateway.clock import Clock
from commerce_gateway.mandate import Mandate, mandate_from_content
from commerce_gateway.models import require_int
from commerce_gateway.store import Store, StoreError, StoreIntegrityError

SESSION_OPENED = "session_opened"
SESSION_RESUMED = "session_resumed"
SESSION_SUPERSEDED = "session_superseded"


class SessionStartRefused(Exception):
    """A new mandate cannot supersede a session whose pending checkout has not expired."""


@dataclass(frozen=True)
class Session:
    session_id: str
    mandate_hash: str
    mandate: Mandate
    closed_at_ms: int | None
    last_seen_ms: int
    cart_revision: int
    tool_calls_used: int

    def __post_init__(self) -> None:
        if self.closed_at_ms is not None:
            require_int("closed_at_ms", self.closed_at_ms)
        require_int("last_seen_ms", self.last_seen_ms)
        require_int("cart_revision", self.cart_revision)
        require_int("tool_calls_used", self.tool_calls_used)

    @property
    def is_open(self) -> bool:
        return self.closed_at_ms is None


@dataclass(frozen=True)
class SessionStart:
    session_id: str
    event: str


def default_session_id() -> str:
    return secrets.token_hex(16)


def load_session(conn: sqlite3.Connection, session_id: str) -> Session:
    """Load a session and check that its mandate snapshot still hashes to the stored hash.

    Any failure of that check raises `StoreIntegrityError`: the gateway fails closed.
    """
    rows = conn.execute(
        "SELECT mandate_hash, mandate, closed_at_ms, last_seen_ms, cart_revision,"
        " tool_calls_used FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    if not rows:
        raise StoreError("unknown session")
    mandate_hash, snapshot, closed_at, last_seen, revision, calls = rows[0]
    try:
        mandate = mandate_from_content(json.loads(snapshot))
    except ValueError as exc:  # includes MandateError and JSONDecodeError
        raise StoreIntegrityError("the stored mandate is not a valid mandate") from exc
    if mandate.content_hash != mandate_hash:
        raise StoreIntegrityError("the stored mandate does not match its hash")
    return Session(session_id, mandate_hash, mandate, closed_at, last_seen, revision, calls)


def open_session_id(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute("SELECT session_id FROM sessions WHERE closed_at_ms IS NULL").fetchall()
    return rows[0][0] if rows else None


def effective_now(conn: sqlite3.Connection, session_id: str, clock: Clock) -> int:
    """max(clock now, last_seen_ms), written back: time never goes backwards in a session."""
    rows = conn.execute(
        "SELECT last_seen_ms FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchall()
    now_ms = max(clock.now_ms(), rows[0][0])
    conn.execute("UPDATE sessions SET last_seen_ms = ? WHERE session_id = ?", (now_ms, session_id))
    return now_ms


def count_call(conn: sqlite3.Connection, session_id: str) -> int:
    """Count one tool call. Returns the number of calls counted before this one."""
    rows = conn.execute(
        "UPDATE sessions SET tool_calls_used = tool_calls_used + 1 WHERE session_id = ?"
        " RETURNING tool_calls_used",
        (session_id,),
    ).fetchall()
    return rows[0][0] - 1


def _insert_session(
    conn: sqlite3.Connection, session_id: str, mandate: Mandate, now_ms: int
) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, mandate_hash, mandate, opened_at_ms, last_seen_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (session_id, mandate.content_hash, canonical_text(mandate.content()), now_ms, now_ms),
    )


def start_session(
    store: Store,
    mandate: Mandate,
    clock: Clock,
    new_session_id: Callable[[], str] = default_session_id,
) -> SessionStart:
    """Resume, open or supersede the session for `mandate`, auditing what happened."""
    with store.transaction() as conn:
        current = open_session_id(conn)
        if current is not None:
            session = load_session(conn, current)
            now_ms = effective_now(conn, current, clock)
            orders.expire_pending(conn, current, now_ms)
            if session.mandate_hash == mandate.content_hash:
                audit.append(
                    conn,
                    ts_ms=now_ms,
                    session_id=current,
                    actor="system",
                    event=SESSION_RESUMED,
                    payload={"session_id": current, "mandate_hash": session.mandate_hash},
                )
                return SessionStart(current, SESSION_RESUMED)
            if orders.pending_checkout(conn, current) is not None:
                raise SessionStartRefused(
                    "the open session has a pending checkout; decide it or wait for it to expire"
                )
            conn.execute(
                "UPDATE sessions SET closed_at_ms = ? WHERE session_id = ?", (now_ms, current)
            )
        new_id = new_session_id()
        opened_ms = clock.now_ms()
        _insert_session(conn, new_id, mandate, opened_ms)
        if current is not None:
            audit.append(
                conn,
                ts_ms=now_ms,
                session_id=current,
                actor="system",
                event=SESSION_SUPERSEDED,
                payload={
                    "old_session_id": current,
                    "old_mandate_hash": session.mandate_hash,
                    "new_session_id": new_id,
                    "new_mandate_hash": mandate.content_hash,
                },
            )
        audit.append(
            conn,
            ts_ms=opened_ms,
            session_id=new_id,
            actor="system",
            event=SESSION_OPENED,
            payload={"session_id": new_id, "mandate_hash": mandate.content_hash},
        )
        return SessionStart(new_id, SESSION_OPENED if current is None else SESSION_SUPERSEDED)
