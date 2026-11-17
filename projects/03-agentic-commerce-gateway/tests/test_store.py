import sqlite3

import pytest

from commerce_gateway.catalogue import CATALOGUE_FILE, parse_catalogue
from commerce_gateway.store import Store, StoreBusy, StoreError, product_facts

PRICE_CHANGE_MS = 1_796_083_200_000


def test_new_store_has_schema_version_1_and_strict_tables(open_store):
    store = open_store()
    conn = store.conn
    assert conn.execute("PRAGMA user_version").fetchall()[0][0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchall()[0][0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchall()[0][0] == 1
    tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    assert {name for name, _ in tables} >= {"sessions", "cart_lines", "checkouts", "audit"}
    assert all(sql.rstrip().endswith("STRICT") for _, sql in tables)


def test_reopen_keeps_data(open_store):
    open_store().close()
    store = open_store()
    assert store.conn.execute("SELECT count(*) FROM products").fetchall()[0][0] == 32


def test_refuses_unknown_schema_version(open_store, db_path):
    open_store().close()
    with sqlite3.connect(db_path) as raw:
        raw.execute("PRAGMA user_version=99")
    raw.close()
    with pytest.raises(StoreError, match="schema version 99"):
        open_store()


def test_refuses_foreign_database(open_store, db_path):
    raw = sqlite3.connect(db_path)
    raw.execute("CREATE TABLE something (x INTEGER)")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError, match="not a commerce gateway database"):
        open_store()


def test_refuses_old_sqlite(open_store, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    with pytest.raises(StoreError, match="too old"):
        open_store()


def test_refuses_a_different_catalogue(open_store):
    open_store().close()
    other = parse_catalogue(CATALOGUE_FILE.read_bytes().replace(b"Flaky", b"Flakey"))
    with pytest.raises(StoreError, match="different catalogue"):
        open_store(catalogue=other)


def test_strict_tables_reject_fractional_cents(open_store):
    store = open_store()
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE product_prices SET unit_price_cents = 12.5 WHERE sku = 'FP-0001'"
        )


def test_one_open_session_per_database(open_store):
    conn = open_store().conn
    insert = (
        "INSERT INTO sessions (session_id, mandate_hash, mandate, opened_at_ms, last_seen_ms)"
        " VALUES (?, ?, '{}', 0, 0)"
    )
    conn.execute(insert, ("s1", "a" * 64))
    with pytest.raises(sqlite3.IntegrityError, match="one_open_session"):
        conn.execute(insert, ("s2", "b" * 64))


def test_one_pending_checkout_per_session_and_needed_approval_is_fixed(open_store):
    conn = open_store().conn
    conn.execute(
        "INSERT INTO sessions (session_id, mandate_hash, mandate, opened_at_ms, last_seen_ms)"
        " VALUES ('s1', ?, '{}', 0, 0)",
        ("a" * 64,),
    )
    insert = (
        "INSERT INTO checkouts (session_id, cart_revision, quote_hash, quote, total_cents,"
        " expires_at_ms, status, needed_approval, created_at_ms)"
        " VALUES ('s1', ?, ?, '{}', 100, 10, 'pending_approval', 1, 0)"
    )
    conn.execute(insert, (1, "c" * 64))
    with pytest.raises(sqlite3.IntegrityError, match="checkouts.session_id"):
        conn.execute(insert, (2, "d" * 64))
    with pytest.raises(sqlite3.IntegrityError, match="needed_approval is fixed"):
        conn.execute("UPDATE checkouts SET needed_approval = 0")


def test_product_facts_follow_the_price_history(open_store):
    conn = open_store().conn
    before = product_facts(conn, ("FP-0003", "FP-9999"), PRICE_CHANGE_MS - 1)
    after = product_facts(conn, ("FP-0003",), PRICE_CHANGE_MS)
    assert [(f.sku, f.price_version, f.unit_price_cents) for f in before] == [("FP-0003", 1, 720)]
    assert [(f.price_version, f.unit_price_cents) for f in after] == [(2, 820)]


def test_second_writer_gets_store_busy_while_first_holds_the_lock(open_store):
    first = open_store()
    second = open_store(busy_timeout_ms=0)
    with first.transaction():
        with pytest.raises(StoreBusy):
            with second.transaction():
                pass
    with second.transaction():
        pass


def test_transaction_rolls_back_on_error(open_store):
    store = open_store()
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("UPDATE products SET stock = 0")
            raise RuntimeError("boom")
    assert store.conn.execute("SELECT max(stock) FROM products").fetchall()[0][0] > 0


def test_store_closes_on_context_exit(db_path):
    with Store.open(db_path) as store:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")
