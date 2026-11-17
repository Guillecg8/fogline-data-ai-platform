"""Two processes on one SQLite file cannot race on the cap or the approval limit (ADR 0004).

Each test uses separate connections to the same file, in one thread, with no sleeps:
- while one connection holds the write lock, the other cannot even start reading (`store_busy`);
- once the first commits, the second decides on the committed state.
Together these show that no decision can read state another decision is about to change.
Every connection is closed by the `open_store` fixture.
"""

import sqlite3

import pytest

from commerce_gateway import approval, orders
from commerce_gateway.gateway import Gateway
from commerce_gateway.models import LockedLine
from commerce_gateway.sessions import load_session
from commerce_gateway.store import StoreBusy

TTL = 30 * 60_000


def _quote(env, revision: int, sku: str, quantity: int) -> orders.Quote:
    facts = env.store.conn.execute(
        "SELECT unit_price_cents FROM product_prices WHERE sku = ? AND price_version = 1", (sku,)
    ).fetchall()
    line = LockedLine(sku, quantity, facts[0][0], 1, env.clock.now_ms() + TTL)
    return orders.Quote(env.session_id, revision, (line,))


def _place(store, env, quote):
    with store.transaction() as conn:
        mandate = load_session(conn, env.session_id).mandate
        return orders.place_quote(conn, mandate, quote, env.clock.now_ms())


def test_a_held_lock_blocks_the_other_process_before_it_reads(make_env, open_store):
    env = make_env()
    busy_store = open_store(busy_timeout_ms=0)
    other = Gateway(busy_store, env.session_id, env.clock)
    env.call("add_to_cart", sku="FP-0001", quantity=1)
    with env.store.transaction():
        assert other.call("request_checkout", {}).error == "store_busy"
        with pytest.raises(StoreBusy):
            approval.list_pending(busy_store, env.clock)
    assert env.calls_used() == 1
    assert other.call("request_checkout", {}).allowed


def test_two_servers_on_one_cart_pay_once(make_env, open_store):
    env = make_env()
    other = Gateway(open_store(), env.session_id, env.clock)
    env.call("add_to_cart", sku="FP-0001", quantity=2)
    first = env.call("request_checkout")
    second = other.call("request_checkout", {})
    assert first.data["checkout"]["status"] == "completed"
    assert second.error == "cart_empty"
    assert env.scalar("SELECT count(*) FROM checkouts") == 1
    assert env.scalar("SELECT stock FROM products WHERE sku = 'FP-0001'") == 118


def test_two_servers_on_one_cart_share_one_pending_checkout(make_env, open_store):
    env = make_env()
    other = Gateway(open_store(), env.session_id, env.clock)
    env.call("add_to_cart", sku="FP-0020", quantity=1)
    first = env.call("request_checkout")
    second = other.call("request_checkout", {})
    assert first.rule_id == "approval_required"
    assert second.data["checkout"] == first.data["checkout"]
    assert env.scalar("SELECT count(*) FROM checkouts") == 1


def test_two_connections_cannot_both_take_the_last_approval_slot(make_env, open_store):
    env = make_env(max_approval_requests=1)
    other = open_store()
    first, created = _place(env.store, env, _quote(env, 100, "FP-0020", 1))
    second, none = _place(other, env, _quote(env, 101, "FP-0021", 1))
    assert (first.rule_id, created.status) == ("approval_required", "pending_approval")
    assert (second.rule_id, none) == ("approval_limit", None)
    approval.reject(env.store, env.clock, created.checkout_id, created.quote_hash)
    third, none = _place(other, env, _quote(env, 102, "FP-0021", 1))
    assert (third.rule_id, none) == ("approval_limit", None)
    assert third.reason != second.reason  # the count limit, not the pending one


def test_schema_backstop_rejects_a_second_pending_checkout(make_env, open_store):
    env = make_env()
    other = open_store()
    _place(env.store, env, _quote(env, 100, "FP-0020", 1))
    quote = _quote(env, 101, "FP-0021", 1)
    with pytest.raises(sqlite3.IntegrityError), other.transaction() as conn:
        conn.execute(
            "INSERT INTO checkouts (session_id, cart_revision, quote_hash, quote, total_cents,"
            " expires_at_ms, status, needed_approval, created_at_ms)"
            " VALUES (?, 101, ?, '{}', 7400, ?, 'pending_approval', 1, 0)",
            (env.session_id, quote.quote_hash, quote.expires_at_ms),
        )


def test_two_connections_cannot_exceed_the_cap(make_env, open_store):
    env = make_env(budget_cents=10_000, approval_threshold_cents=8_000)
    other = open_store()
    first, done = _place(env.store, env, _quote(env, 100, "FP-0012", 2))  # 4800
    second, more = _place(other, env, _quote(env, 101, "FP-0012", 2))  # 4800, total 9600
    third, none = _place(env.store, env, _quote(env, 102, "FP-0001", 1))  # 650, total 10250
    assert [d.rule_id for d in (first, second, third)] == [None, None, "spend_cap"]
    assert (done.status, more.status, none) == ("completed", "completed", None)


def test_a_pending_reservation_counts_against_the_cap_in_another_process(make_env, open_store):
    env = make_env(budget_cents=10_000, approval_threshold_cents=5_000)
    other = open_store()
    reserved, pending = _place(env.store, env, _quote(env, 100, "FP-0020", 1))  # 6900 pending
    denied, none = _place(other, env, _quote(env, 101, "FP-0012", 2))  # 4800 -> 11700
    assert (reserved.rule_id, pending.status) == ("approval_required", "pending_approval")
    assert (denied.rule_id, none) == ("spend_cap", None)


def test_two_approvers_complete_an_order_once(make_env, open_store):
    env = make_env()
    env.call("add_to_cart", sku="FP-0020", quantity=1)
    env.call("request_checkout")
    [pending] = approval.list_pending(env.store, env.clock)
    first = approval.approve(open_store(), env.clock, pending.checkout_id, pending.quote_hash)
    second = approval.approve(open_store(), env.clock, pending.checkout_id, pending.quote_hash)
    assert (first.status, second.decision.rule_id) == ("completed", "quote_mismatch")
    assert env.scalar("SELECT stock FROM products WHERE sku = 'FP-0020'") == 4
