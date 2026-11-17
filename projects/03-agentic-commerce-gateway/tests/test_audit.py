import hashlib
import sqlite3

import pytest

from commerce_gateway import audit
from commerce_gateway.schema import GENESIS_HASH


@pytest.fixture
def conn(open_store):
    store = open_store()
    with store.transaction() as tx:
        for index in range(5):
            audit.append(
                tx,
                ts_ms=1_000 + index,
                session_id="s1",
                actor="agent",
                event="tool_call",
                tool="view_cart",
                allowed=True,
                payload={"n": index, "skus": ("FP-0001",)},
            )
    return store.conn


def _drop_triggers(conn: sqlite3.Connection) -> None:
    for name in ("audit_no_update", "audit_no_delete", "audit_chain_on_insert"):
        conn.execute(f"DROP TRIGGER {name}")


def _rewrite_chain(conn: sqlite3.Connection) -> None:
    """What an operator with file access can do: recompute every hash after an edit."""
    prev = GENESIS_HASH
    for row in audit.read_rows(conn):
        fields = (row.seq, row.ts_ms, row.session_id, row.actor, row.event, row.tool)
        row_hash = audit.compute_row_hash(*fields, row.allowed, row.rule_id, row.payload, prev)
        conn.execute(
            "UPDATE audit SET prev_hash = ?, row_hash = ? WHERE seq = ?", (prev, row_hash, row.seq)
        )
        prev = row_hash


def test_row_hash_serialization_is_pinned():
    expected = hashlib.sha256(
        b'[1,1,1000,"s1","agent","tool_call","view_cart",true,null,{"a":[1,2]},"'
        + GENESIS_HASH.encode()
        + b'"]'
    ).hexdigest()
    got = audit.compute_row_hash(
        1, 1000, "s1", "agent", "tool_call", "view_cart", True, None, {"a": (1, 2)}, GENESIS_HASH
    )
    assert got == expected


def test_chain_verifies_and_links_to_genesis(conn):
    rows = audit.read_rows(conn)
    assert rows[0].prev_hash == GENESIS_HASH
    assert all(b.prev_hash == a.row_hash for a, b in zip(rows, rows[1:], strict=False))
    result = audit.verify(conn)
    assert result.ok and result.rows == 5
    assert result.head == audit.head(conn) == (5, rows[-1].row_hash)


def test_empty_trail_verifies(open_store):
    conn = open_store().conn
    assert audit.verify(conn) == audit.VerifyResult(True, 0, (0, GENESIS_HASH))
    assert audit.verify(conn, anchor=(0, GENESIS_HASH)).ok


def test_append_outside_a_transaction_is_refused(conn):
    with pytest.raises(RuntimeError):
        audit.append(conn, ts_ms=1, session_id="s1", actor="agent", event="x", payload={})


def test_append_refuses_floats_and_unknown_actors(open_store):
    store = open_store()
    with pytest.raises(ValueError), store.transaction() as tx:
        audit.append(tx, ts_ms=1, session_id="s1", actor="agent", event="x", payload={"c": 1.5})
    with pytest.raises(ValueError), store.transaction() as tx:
        audit.append(tx, ts_ms=1, session_id="s1", actor="root", event="x", payload={})


@pytest.mark.parametrize(
    "sql", ["UPDATE audit SET payload = '{}' WHERE seq = 2", "DELETE FROM audit WHERE seq = 5"]
)
def test_triggers_reject_update_and_delete(conn, sql):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    assert audit.verify(conn).ok


@pytest.mark.parametrize(("seq", "prev_hash"), [(7, None), (6, "f" * 64), (1, GENESIS_HASH)])
def test_insert_trigger_enforces_seq_and_prev_hash(conn, seq, prev_hash):
    prev_hash = prev_hash or audit.head(conn)[1]
    with pytest.raises(sqlite3.IntegrityError, match="audit chain violation"):
        conn.execute(
            "INSERT INTO audit (seq, ts_ms, session_id, actor, event, payload, prev_hash,"
            " row_hash) VALUES (?, 1, 's1', 'agent', 'x', '{}', ?, ?)",
            (seq, prev_hash, "e" * 64),
        )


@pytest.mark.parametrize(
    "sql",
    [
        'UPDATE audit SET payload = \'{"n":99,"skus":["FP-0001"]}\' WHERE seq = 3',
        "UPDATE audit SET ts_ms = 1 WHERE seq = 3",
        "UPDATE audit SET allowed = 0 WHERE seq = 3",
        "UPDATE audit SET rule_id = 'spend_cap' WHERE seq = 3",
        'UPDATE audit SET payload = \'{"n": 2, "skus": ["FP-0001"]}\' WHERE seq = 3',
        'UPDATE audit SET payload = \'{"n":2.0,"skus":["FP-0001"]}\' WHERE seq = 3',
    ],
)
def test_tampering_with_one_row_is_detected(conn, sql):
    _drop_triggers(conn)
    conn.execute(sql)
    result = audit.verify(conn)
    assert not result.ok and result.first_bad_seq == 3


def test_deleting_a_middle_row_is_detected(conn):
    _drop_triggers(conn)
    conn.execute("DELETE FROM audit WHERE seq = 2")
    result = audit.verify(conn)
    assert (result.ok, result.first_bad_seq, result.reason) == (False, 2, "sequence gap")


def test_truncation_needs_an_anchor_to_be_detected(conn):
    anchor = audit.head(conn)
    _drop_triggers(conn)
    conn.execute("DELETE FROM audit WHERE seq >= 4")
    assert audit.verify(conn).ok
    result = audit.verify(conn, anchor=anchor)
    assert not result.ok and result.reason == "chain is shorter than the anchor"


def test_whole_chain_rewrite_needs_an_anchor_to_be_detected(conn):
    anchor = audit.head(conn)
    _drop_triggers(conn)
    conn.execute("UPDATE audit SET allowed = 0 WHERE seq = 2")
    _rewrite_chain(conn)
    assert audit.verify(conn).ok
    result = audit.verify(conn, anchor=anchor)
    assert not result.ok and result.reason == "row does not match the anchor"


def test_an_older_anchor_still_verifies_a_longer_chain(conn):
    older = (3, audit.read_rows(conn)[2].row_hash)
    assert audit.verify(conn, anchor=older).ok


@pytest.mark.parametrize("recursive_triggers", ["OFF", "ON"])
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT OR REPLACE INTO audit (seq, ts_ms, session_id, actor, event, payload, prev_hash,"
        " row_hash) SELECT seq, 1, session_id, actor, event, '{}', prev_hash, row_hash"
        " FROM audit WHERE seq = 3",
        "REPLACE INTO audit (seq, ts_ms, session_id, actor, event, payload, prev_hash, row_hash)"
        " SELECT seq, 1, session_id, actor, event, '{}', prev_hash, row_hash FROM audit"
        " WHERE seq = 5",
        "UPDATE OR REPLACE audit SET ts_ms = 1 WHERE seq = 3",
        "UPDATE OR REPLACE audit SET seq = 2 WHERE seq = 3",
        "UPDATE OR IGNORE audit SET payload = '{}' WHERE seq = 3",
        "INSERT OR IGNORE INTO audit (seq, ts_ms, session_id, actor, event, payload, prev_hash,"
        " row_hash) VALUES (2, 1, 's1', 'agent', 'x', '{}', '"
        + "0" * 64
        + "', '"
        + "e" * 64
        + "')",
    ],
)
def test_conflict_clauses_cannot_bypass_the_triggers(conn, sql, recursive_triggers):
    conn.execute(f"PRAGMA recursive_triggers={recursive_triggers}")
    rows_before = audit.read_rows(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only|audit chain violation"):
        conn.execute(sql)
    assert audit.read_rows(conn) == rows_before
    assert audit.verify(conn).ok
