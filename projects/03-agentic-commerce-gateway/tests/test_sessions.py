import pytest

from commerce_gateway import approval, audit
from commerce_gateway.gateway import Gateway
from commerce_gateway.sessions import SessionStartRefused, start_session
from commerce_gateway.store import StoreIntegrityError

MINUTE = 60_000
TTL = 30 * MINUTE


def _events(conn) -> list[tuple[str, str, dict]]:
    return [(row.event, row.actor, row.payload) for row in audit.read_rows(conn)]


def _open_sessions(conn) -> list[str]:
    rows = conn.execute("SELECT session_id FROM sessions WHERE closed_at_ms IS NULL")
    return [row[0] for row in rows.fetchall()]


def _pending_order(env) -> None:
    """One chef knife (6900) is above the 5000 threshold: a pending checkout."""
    env.call("add_to_cart", sku="FP-0020", quantity=1)
    assert env.call("request_checkout").rule_id == "approval_required"


def test_first_start_opens_and_audits(make_env):
    env = make_env()
    [(event, actor, payload)] = _events(env.store.conn)
    assert (event, actor) == ("session_opened", "system")
    assert payload == {"session_id": env.session_id, "mandate_hash": env.mandate.content_hash}


def test_restart_with_same_mandate_resumes_with_counters(make_env, clock, session_ids):
    env = make_env()
    env.call("get_policy")
    env.call("add_to_cart", sku="FP-0001", quantity=1)
    start = start_session(env.store, env.mandate, clock, new_session_id=session_ids)
    assert (start.session_id, start.event) == (env.session_id, "session_resumed")
    assert env.calls_used() == 2
    assert _events(env.store.conn)[-1] == (
        "session_resumed",
        "system",
        {"session_id": env.session_id, "mandate_hash": env.mandate.content_hash},
    )
    resumed = Gateway(env.store, start.session_id, clock)
    assert resumed.call("view_cart", {}).data["cart"]["lines"][0]["sku"] == "FP-0001"


def test_new_mandate_supersedes_the_session(make_env, make_mandate, clock, session_ids):
    env = make_env()
    env.call("add_to_cart", sku="FP-0001", quantity=1)
    new_mandate = make_mandate(session_label="test-2")
    start = start_session(env.store, new_mandate, clock, new_session_id=session_ids)
    assert start.event == "session_superseded" and start.session_id != env.session_id
    assert _open_sessions(env.store.conn) == [start.session_id]
    superseded, opened = _events(env.store.conn)[-2:]
    assert superseded == (
        "session_superseded",
        "system",
        {
            "old_session_id": env.session_id,
            "old_mandate_hash": env.mandate.content_hash,
            "new_session_id": start.session_id,
            "new_mandate_hash": new_mandate.content_hash,
        },
    )
    assert opened[0] == "session_opened"
    fresh = Gateway(env.store, start.session_id, clock)
    assert fresh.call("view_cart", {}).data["cart"]["lines"] == []


def test_refuses_to_supersede_with_an_unexpired_pending_checkout(
    make_env, make_mandate, clock, session_ids
):
    env = make_env()
    _pending_order(env)
    before = env.audit_count()
    clock.advance(TTL - 1)
    with pytest.raises(SessionStartRefused):
        start_session(
            env.store, make_mandate(session_label="test-2"), clock, new_session_id=session_ids
        )
    assert _open_sessions(env.store.conn) == [env.session_id]
    assert env.audit_count() == before
    assert env.scalar("SELECT status FROM checkouts") == "pending_approval"


def test_supersedes_once_the_pending_checkout_has_expired(
    make_env, make_mandate, clock, session_ids
):
    env = make_env()
    _pending_order(env)
    clock.advance(TTL)
    start = start_session(
        env.store, make_mandate(session_label="test-2"), clock, new_session_id=session_ids
    )
    assert start.event == "session_superseded"
    assert env.scalar("SELECT status FROM checkouts") == "expired"
    events = [event for event, _, _ in _events(env.store.conn)[-3:]]
    assert events == ["checkout_expired", "session_superseded", "session_opened"]


def test_closed_session_answers_session_closed_and_still_counts(
    make_env, make_mandate, clock, session_ids
):
    env = make_env()
    new_mandate = make_mandate(session_label="test-2")
    start_session(env.store, new_mandate, clock, new_session_id=session_ids)
    result = env.call("view_cart")
    assert (result.allowed, result.error) == (False, "session_closed")
    assert env.calls_used() == 1
    last = audit.read_rows(env.store.conn)[-1]
    assert (last.event, last.session_id) == ("session_closed", env.session_id)
    assert last.tool == "view_cart"


def test_time_never_goes_backwards_within_a_session(make_env, clock):
    env = make_env()
    start = clock.now_ms()
    env.call("add_to_cart", sku="FP-0001", quantity=1)
    clock.advance(TTL)
    assert env.call("request_checkout").rule_id == "stale_price"
    clock.set(start)  # the system clock is moved back by 30 minutes
    assert env.call("request_checkout").rule_id == "stale_price"
    stamps = [row.ts_ms for row in audit.read_rows(env.store.conn)]
    assert stamps == sorted(stamps) and stamps[-1] == start + TTL
    assert env.scalar("SELECT last_seen_ms FROM sessions") == start + TTL


def test_approver_shares_the_session_time(make_env, clock, open_store):
    env = make_env()
    _pending_order(env)
    clock.advance(10 * MINUTE)
    env.call("view_cart")
    seen = clock.now_ms()
    clock.set(seen - 10 * MINUTE)  # the approver's clock reads earlier
    [pending] = approval.list_pending(open_store(), clock)
    outcome = approval.reject(open_store(), clock, pending.checkout_id, pending.quote_hash)
    assert outcome.status == "rejected"
    assert audit.read_rows(env.store.conn)[-1].ts_ms == seen


def _state(env) -> tuple:
    conn = env.store.conn
    return (
        conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall(),
        conn.execute("SELECT * FROM cart_lines ORDER BY sku").fetchall(),
        conn.execute("SELECT * FROM checkouts ORDER BY checkout_id").fetchall(),
        conn.execute("SELECT seq, row_hash FROM audit ORDER BY seq").fetchall(),
    )


def test_tampered_mandate_snapshot_fails_closed(make_env, make_mandate, clock, session_ids):
    env = make_env()
    env.call("add_to_cart", sku="FP-0020", quantity=1)
    env.call("request_checkout")
    env.store.conn.execute("UPDATE sessions SET mandate = replace(mandate, '20000', '99999')")
    before = _state(env)
    calls = [
        ("get_policy", {}),
        ("view_cart", {}),
        ("add_to_cart", {"sku": "FP-0001", "quantity": 1}),
        ("add_to_cart", {"sku": "FP-0001", "quantity": "x"}),  # invalid arguments path
        ("request_checkout", {}),
    ]
    for _ in range(2):
        for tool, arguments in calls:
            result = env.gateway.call(tool, arguments)
            assert (result.allowed, result.error) == (False, "store_integrity")
    assert _state(env) == before
    for mandate in (env.mandate, make_mandate(session_label="test-2")):
        with pytest.raises(StoreIntegrityError):
            start_session(env.store, mandate, clock, new_session_id=session_ids)
    with pytest.raises(StoreIntegrityError):
        approval.list_pending(env.store, clock)
    with pytest.raises(StoreIntegrityError):
        approval.approve(env.store, clock, 1, "a" * 64)
    assert _state(env) == before


def test_unparseable_mandate_snapshot_is_an_integrity_failure(make_env):
    env = make_env()
    env.store.conn.execute("UPDATE sessions SET mandate = '{not json'")
    assert env.call("view_cart").error == "store_integrity"
