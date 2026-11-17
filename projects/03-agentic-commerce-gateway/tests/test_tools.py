import hashlib
import re
from pathlib import Path

import pytest

from commerce_gateway.gateway import HANDLERS
from commerce_gateway.tools import (
    MAX_QUANTITY_PER_CALL,
    QUERY_MAX_CHARS,
    SEARCH_MAX_RESULTS,
    TOOL_NAMES,
    TOOL_PARAMETERS,
    InvalidArguments,
    validate_arguments,
)

ADR_0003 = (
    Path(__file__).parents[3] / "docs" / "decisions" / "0003-commerce-gateway-trust-boundary.md"
)


def test_tool_names_are_exactly_the_eight_in_adr_0003():
    text = ADR_0003.read_text(encoding="utf-8")
    block = text.split("with exactly these 8 tools:", 1)[1].split("\n\n", 2)[1]
    assert tuple(re.findall(r"`([a-z_]+)`", block)) == TOOL_NAMES
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 8


def test_no_tool_approves_rejects_or_edits_the_mandate():
    forbidden = ("approve", "reject", "mandate", "audit", "create_cart")
    assert not [name for name in TOOL_NAMES if any(word in name for word in forbidden)]


def test_every_layer_uses_the_same_constant():
    assert tuple(TOOL_PARAMETERS) == TOOL_NAMES
    assert tuple(HANDLERS) == TOOL_NAMES


def test_state_changing_tools_take_no_free_text():
    for tool in ("add_to_cart", "remove_from_cart", "request_checkout"):
        assert "query" not in TOOL_PARAMETERS[tool]


@pytest.mark.parametrize(
    ("tool", "arguments", "reason"),
    [
        ("approve_order", {}, "unknown tool"),
        (["view_cart"], {}, "unknown tool"),
        ("view_cart", [], "must be an object"),
        ("view_cart", {"x": 1}, "unexpected or missing"),
        ("add_to_cart", {"sku": "FP-0001"}, "unexpected or missing"),
        ("add_to_cart", {"sku": "fp-0001", "quantity": 1}, "sku"),
        ("add_to_cart", {"sku": 1, "quantity": 1}, "sku"),
        ("add_to_cart", {"sku": "FP-0001", "quantity": "2"}, "quantity"),
        ("add_to_cart", {"sku": "FP-0001", "quantity": 2.0}, "quantity"),
        ("add_to_cart", {"sku": "FP-0001", "quantity": True}, "quantity"),
        ("add_to_cart", {"sku": "FP-0001", "quantity": 0}, "quantity"),
        ("add_to_cart", {"sku": "FP-0001", "quantity": 100}, "quantity"),
        ("search_products", {"query": 5}, "string"),
        ("search_products", {"query": "   "}, "empty"),
        ("search_products", {"query": "\ud800"}, "not valid text"),
    ],
)
def test_malformed_arguments_are_rejected(tool, arguments, reason):
    with pytest.raises(InvalidArguments, match=reason):
        validate_arguments(tool, arguments)


def test_valid_arguments_pass_through():
    assert validate_arguments("view_cart", None) == {}
    assert validate_arguments("add_to_cart", {"sku": "FP-0001", "quantity": 99}) == {
        "sku": "FP-0001",
        "quantity": 99,
    }
    assert validate_arguments("search_products", {"query": "x" * QUERY_MAX_CHARS})


def test_too_long_query_is_rejected_with_hash_and_cut_text():
    query = "salt " * 100
    with pytest.raises(InvalidArguments, match="too long") as caught:
        validate_arguments("search_products", {"query": query})
    assert caught.value.audit == {
        "field": "query",
        "expected": "text_within_limit",
        "received": "string",
        "length": 500,
        "sha256": hashlib.sha256(query.encode()).hexdigest(),
    }


def test_limits_are_named_constants():
    assert (MAX_QUANTITY_PER_CALL, SEARCH_MAX_RESULTS, QUERY_MAX_CHARS) == (
        99,
        20,
        200,
    )
