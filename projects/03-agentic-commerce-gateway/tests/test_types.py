"""Money, time, quantity and counter fields reject float (even 2.0) and bool at construction."""

import dataclasses

import pytest

from commerce_gateway import approval, audit, orders, policy, sessions
from commerce_gateway.mandate import Mandate, MandateError, mandate_from_content
from commerce_gateway.models import LockedLine, PriceVersion, ProductFacts

MANDATE = mandate_from_content(
    {
        "version": 1,
        "session_label": "types",
        "budget_cents": 20_000,
        "approval_threshold_cents": 5_000,
        "allowed_categories": ["dry_goods"],
        "price_lock_ttl_seconds": 1800,
        "max_approval_requests": 2,
        "tool_call_budget": 10,
    }
)
LINE = LockedLine("FP-0001", 1, 650, 1, 1_000)
QUOTE = orders.Quote("s1", 1, (LINE,))
PENDING_LINE = approval.PendingLine("FP-0001", "dry_goods", 1, 650, 650, "name")

# (constructor, valid instance, field names to corrupt)
CASES = [
    (
        PriceVersion,
        PriceVersion(1, 650, 0),
        ("price_version", "unit_price_cents", "effective_from_ms"),
    ),
    (
        ProductFacts,
        ProductFacts("FP-0001", "dry_goods", 650, 1, 5),
        ("unit_price_cents", "price_version", "stock"),
    ),
    (
        LockedLine,
        LINE,
        ("quantity", "unit_price_cents", "price_version", "lock_expires_at_ms"),
    ),
    (orders.Quote, QUOTE, ("cart_revision",)),
    (
        orders.Checkout,
        orders.Checkout(1, "s1", 1, "a" * 64, QUOTE, 650, 1_000, "completed", False, None),
        ("checkout_id", "cart_revision", "total_cents", "expires_at_ms"),
    ),
    (
        sessions.Session,
        sessions.Session("s1", "a" * 64, MANDATE, None, 1_000, 0, 0),
        ("closed_at_ms", "last_seen_ms", "cart_revision", "tool_calls_used"),
    ),
    (policy.ToolCall, policy.ToolCall(0), ("calls_used",)),
    (policy.AddToCart, policy.AddToCart("FP-0001", 1, False), ("quantity_after",)),
    (
        policy.RequestCheckout,
        policy.RequestCheckout((LINE,), 0, 0, False),
        ("committed_cents", "approval_requests_used"),
    ),
    (
        policy.FinaliseApproval,
        policy.FinaliseApproval("a" * 64, "a" * 64, 1_000, (LINE,), 0),
        ("quote_expires_at_ms", "committed_other_cents"),
    ),
    (
        audit.AuditRow,
        audit.AuditRow(1, 1_000, "s1", "agent", "x", None, None, None, {}, "0" * 64, "0" * 64),
        ("seq", "ts_ms"),
    ),
    (approval.PendingLine, PENDING_LINE, ("quantity", "unit_price_cents", "line_total_cents")),
    (
        approval.PendingCheckout,
        approval.PendingCheckout(1, "a" * 64, 650, 1_000, 30, (PENDING_LINE,)),
        ("checkout_id", "total_cents", "expires_at_ms", "seconds_left"),
    ),
    (
        Mandate,
        MANDATE,
        (
            "budget_cents",
            "approval_threshold_cents",
            "price_lock_ttl_seconds",
            "max_approval_requests",
            "tool_call_budget",
        ),
    ),
]

PARAMS = [
    pytest.param(instance, name, bad, id=f"{kind.__name__}.{name}={bad!r}")
    for kind, instance, names in CASES
    for name in names
    for bad in (2.0, True)
]


@pytest.mark.parametrize(("instance", "field", "bad"), PARAMS)
def test_float_and_bool_are_rejected(instance, field, bad):
    with pytest.raises(ValueError):
        dataclasses.replace(instance, **{field: bad})


@pytest.mark.parametrize(("kind", "instance", "names"), CASES)
def test_valid_instances_rebuild(kind, instance, names):
    assert dataclasses.replace(instance) == instance


def test_mandate_built_directly_is_validated_like_one_parsed_from_yaml():
    with pytest.raises(MandateError, match="sorted"):
        dataclasses.replace(MANDATE, allowed_categories=("dry_goods", "dairy"))
    with pytest.raises(MandateError, match="allowable"):
        dataclasses.replace(MANDATE, allowed_categories=("unknown",))
    with pytest.raises(MandateError, match="session_label"):
        dataclasses.replace(MANDATE, session_label="bad label")
