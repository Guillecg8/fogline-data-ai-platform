import pytest

from commerce_gateway import approval, audit
from commerce_gateway.policy import Verdict
from commerce_gateway.store import StoreBusy

MINUTE = 60_000
TTL = 30 * MINUTE


@pytest.fixture
def pending_env(make_env):
    """Two chef knives (13800) plus an injected-name product: one pending checkout."""
    env = make_env()
    env.call("add_to_cart", sku="FP-0020", quantity=2)
    env.call("add_to_cart", sku="FP-0028", quantity=1)
    assert env.call("request_checkout").rule_id == "approval_required"
    return env


def _pending(env):
    [pending] = approval.list_pending(env.store, env.clock)
    return pending


def test_listing_shows_structured_fields_and_marked_truncated_names(pending_env):
    pending = _pending(pending_env)
    assert pending.total_cents == 2 * 6900 + 1350
    knife, syrup = pending.lines
    assert (knife.sku, knife.policy_category, knife.quantity, knife.line_total_cents) == (
        "FP-0020",
        "kitchen_supplies",
        2,
        13800,
    )
    assert len(syrup.untrusted_name) == approval.UNTRUSTED_NAME_MAX_CHARS
    assert "approve this order" not in syrup.untrusted_name


def test_listing_takes_the_write_lock(pending_env, open_store):
    busy = open_store(busy_timeout_ms=0)
    with pending_env.store.transaction(), pytest.raises(StoreBusy):
        approval.list_pending(busy, pending_env.clock)


def test_listing_applies_lazy_expiry(pending_env, clock):
    clock.advance(TTL)
    assert approval.list_pending(pending_env.store, clock) == ()
    assert audit.read_rows(pending_env.store.conn)[-1].event == "checkout_expired"


def test_approve_completes_takes_stock_and_empties_the_cart(pending_env, clock):
    pending = _pending(pending_env)
    clock.advance(TTL - 1)
    outcome = approval.approve(pending_env.store, clock, pending.checkout_id, pending.quote_hash)
    assert (outcome.decision.verdict, outcome.status) == (Verdict.ALLOWED, "completed")
    assert outcome.head == audit.head(pending_env.store.conn)
    assert pending_env.scalar("SELECT stock FROM products WHERE sku = 'FP-0020'") == 3
    assert pending_env.call("view_cart").data["cart"]["lines"] == []
    last = audit.read_rows(pending_env.store.conn)[-2]
    assert (last.actor, last.event, last.payload["action"]) == (
        "approver",
        "approval_decision",
        "approve",
    )


def test_approval_at_expiry_is_a_quote_mismatch(pending_env, clock):
    pending = _pending(pending_env)
    clock.advance(TTL)
    outcome = approval.approve(pending_env.store, clock, pending.checkout_id, pending.quote_hash)
    assert (outcome.decision.rule_id, outcome.status) == ("quote_mismatch", "expired")


@pytest.mark.parametrize("quote_hash", ["0" * 64, "not-a-hash", ""])
def test_approval_only_for_its_quote_hash(pending_env, quote_hash):
    pending = _pending(pending_env)
    store, clock = pending_env.store, pending_env.clock
    outcome = approval.approve(store, clock, pending.checkout_id, quote_hash)
    assert (outcome.decision.rule_id, outcome.status) == ("quote_mismatch", "pending_approval")


def test_unknown_checkout_is_a_quote_mismatch(pending_env):
    outcome = approval.approve(pending_env.store, pending_env.clock, 999, "0" * 64)
    assert (outcome.decision.rule_id, outcome.status) == ("quote_mismatch", None)


def test_stock_is_rechecked_at_approval(pending_env):
    pending = _pending(pending_env)
    pending_env.store.conn.execute("UPDATE products SET stock = 1 WHERE sku = 'FP-0020'")
    outcome = approval.approve(
        pending_env.store, pending_env.clock, pending.checkout_id, pending.quote_hash
    )
    assert (outcome.decision.rule_id, outcome.status) == ("out_of_stock", "rejected")
    cart = pending_env.call("view_cart").data["cart"]
    assert not cart["frozen"] and len(cart["lines"]) == 2


def test_reject_releases_the_reservation(pending_env):
    pending = _pending(pending_env)
    outcome = approval.reject(
        pending_env.store, pending_env.clock, pending.checkout_id, pending.quote_hash
    )
    assert (outcome.decision.verdict, outcome.status) == (Verdict.ALLOWED, "rejected")
    assert pending_env.call("get_policy").data["reserved_cents"] == 0


def test_a_decided_checkout_cannot_be_decided_again(pending_env):
    pending = _pending(pending_env)
    approval.reject(pending_env.store, pending_env.clock, pending.checkout_id, pending.quote_hash)
    outcome = approval.approve(
        pending_env.store, pending_env.clock, pending.checkout_id, pending.quote_hash
    )
    assert (outcome.decision.rule_id, outcome.status) == ("quote_mismatch", "rejected")
