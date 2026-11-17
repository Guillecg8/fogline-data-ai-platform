"""Append-only, hash-chained audit trail (ADR 0004).

row_hash = sha256(canonical([1, seq, ts_ms, session_id, actor, event, tool, allowed, rule_id,
payload, prev_hash])). The first row's prev_hash is 64 zeros. Triggers block UPDATE and DELETE and
check seq and prev_hash on INSERT; `verify` recomputes every hash.
"""

import json
import sqlite3
from dataclasses import dataclass

from commerce_gateway.canonical import CanonicalError, canonical_hash, canonical_text
from commerce_gateway.models import require_int
from commerce_gateway.schema import GENESIS_HASH

HASH_FORMAT_VERSION = 1
ACTORS = ("agent", "approver", "system")

Head = tuple[int, str]


@dataclass(frozen=True)
class AuditRow:
    seq: int
    ts_ms: int
    session_id: str
    actor: str
    event: str
    tool: str | None
    allowed: bool | None
    rule_id: str | None
    payload: dict
    prev_hash: str
    row_hash: str

    def __post_init__(self) -> None:
        require_int("seq", self.seq, 1)
        require_int("ts_ms", self.ts_ms)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    rows: int
    head: Head
    first_bad_seq: int | None = None
    reason: str | None = None


def compute_row_hash(
    seq: int,
    ts_ms: int,
    session_id: str,
    actor: str,
    event: str,
    tool: str | None,
    allowed: bool | None,
    rule_id: str | None,
    payload: dict,
    prev_hash: str,
) -> str:
    record = [
        HASH_FORMAT_VERSION,
        seq,
        ts_ms,
        session_id,
        actor,
        event,
        tool,
        allowed,
        rule_id,
        payload,
        prev_hash,
    ]
    return canonical_hash(record)


def head(conn: sqlite3.Connection) -> Head:
    """(seq, row_hash) of the latest row, or (0, genesis) for an empty trail."""
    rows = conn.execute("SELECT seq, row_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchall()
    return (rows[0][0], rows[0][1]) if rows else (0, GENESIS_HASH)


def append(
    conn: sqlite3.Connection,
    *,
    ts_ms: int,
    session_id: str,
    actor: str,
    event: str,
    payload: dict,
    tool: str | None = None,
    allowed: bool | None = None,
    rule_id: str | None = None,
) -> int:
    """Append one row inside the caller's transaction. Returns its seq."""
    if not conn.in_transaction:
        raise RuntimeError("audit rows are appended inside a write transaction")
    if actor not in ACTORS:
        raise ValueError("unknown audit actor")
    last_seq, prev_hash = head(conn)
    seq = last_seq + 1
    row_hash = compute_row_hash(
        seq, ts_ms, session_id, actor, event, tool, allowed, rule_id, payload, prev_hash
    )
    conn.execute(
        "INSERT INTO audit (seq, ts_ms, session_id, actor, event, tool, allowed, rule_id,"
        " payload, prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            seq,
            ts_ms,
            session_id,
            actor,
            event,
            tool,
            None if allowed is None else int(allowed),
            rule_id,
            canonical_text(payload),
            prev_hash,
            row_hash,
        ),
    )
    return seq


_SELECT = (
    "SELECT seq, ts_ms, session_id, actor, event, tool, allowed, rule_id, payload,"
    " prev_hash, row_hash FROM audit"
)


def _row(raw: tuple) -> tuple[AuditRow, str]:
    seq, ts_ms, session_id, actor, event, tool, allowed, rule_id, payload, prev, row_hash = raw
    row = AuditRow(
        seq=seq,
        ts_ms=ts_ms,
        session_id=session_id,
        actor=actor,
        event=event,
        tool=tool,
        allowed=None if allowed is None else bool(allowed),
        rule_id=rule_id,
        payload=json.loads(payload),
        prev_hash=prev,
        row_hash=row_hash,
    )
    return row, payload


def read_rows(conn: sqlite3.Connection) -> list[AuditRow]:
    return [_row(raw)[0] for raw in conn.execute(_SELECT + " ORDER BY seq").fetchall()]


def _check_row(raw: tuple, expected_prev: str) -> tuple[AuditRow | None, str | None]:
    """Return (row, None) if the row is intact, else (None, reason)."""
    try:
        row, payload_text = _row(raw)
        if canonical_text(row.payload) != payload_text:
            return None, "payload is not canonical"
        recomputed = compute_row_hash(
            row.seq,
            row.ts_ms,
            row.session_id,
            row.actor,
            row.event,
            row.tool,
            row.allowed,
            row.rule_id,
            row.payload,
            row.prev_hash,
        )
    except (ValueError, CanonicalError):
        return None, "row cannot be decoded"
    if row.prev_hash != expected_prev:
        return None, "prev_hash does not match the previous row"
    if row.row_hash != recomputed:
        return None, "row_hash does not match the row content"
    return row, None


def verify(conn: sqlite3.Connection, anchor: Head | None = None) -> VerifyResult:
    """Recompute the chain. With `anchor`, also require row `anchor[0]` to have hash `anchor[1]`.

    Without an anchor, a truncated or fully rewritten chain still verifies (ADR 0004).
    """
    current: Head = (0, GENESIS_HASH)
    count = 0
    for raw in conn.execute(_SELECT + " ORDER BY seq").fetchall():
        count += 1
        if raw[0] != count:
            return VerifyResult(False, count, current, count, "sequence gap")
        row, reason = _check_row(raw, current[1])
        if row is None:
            return VerifyResult(False, count, current, raw[0], reason)
        if anchor is not None and row.seq == anchor[0] and row.row_hash != anchor[1]:
            return VerifyResult(False, count, current, row.seq, "row does not match the anchor")
        current = (row.seq, row.row_hash)
    if anchor is not None:
        if anchor[0] > current[0]:
            return VerifyResult(False, count, current, None, "chain is shorter than the anchor")
        if anchor[0] == 0 and anchor[1] != GENESIS_HASH:
            return VerifyResult(False, count, current, None, "anchor is not a valid head")
    return VerifyResult(True, count, current)
