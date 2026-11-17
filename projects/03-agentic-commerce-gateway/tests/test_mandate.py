from pathlib import Path

import pytest

from commerce_gateway.mandate import MandateError, load_mandate, parse_mandate

EXAMPLE = Path(__file__).parents[1] / "mandates" / "example.yaml"

VALID = """\
version: 1
session_label: "run-1"
budget_cents: 20000
approval_threshold_cents: 5000
allowed_categories: [dry_goods, dairy]
price_lock_ttl_seconds: 1800
max_approval_requests: 2
tool_call_budget: 50
"""


def _with(line_key: str, new_line: str | None) -> str:
    """VALID with the line for `line_key` replaced (or removed when `new_line` is None)."""
    lines = [(new_line if line.startswith(f"{line_key}:") else line) for line in VALID.splitlines()]
    return "\n".join(line for line in lines if line is not None) + "\n"


def test_example_mandate_loads():
    mandate = load_mandate(EXAMPLE)
    assert mandate.price_lock_ttl_ms == 1_800_000
    assert "alcohol" not in mandate.allowed_categories
    assert len(mandate.content_hash) == 64


def test_valid_mandate_parses_to_typed_values():
    mandate = parse_mandate(VALID)
    assert mandate.budget_cents == 20000
    assert mandate.allowed_categories == ("dairy", "dry_goods")


def test_hash_is_over_parsed_content_not_file_bytes():
    reformatted = (
        "# a comment\n"
        + _with("allowed_categories", "allowed_categories:\n  - dairy\n  - dry_goods")
    ).replace("budget_cents: 20000", "budget_cents:    20_000")
    assert parse_mandate(reformatted).content_hash == parse_mandate(VALID).content_hash


def test_hash_changes_with_session_label():
    other = _with("session_label", 'session_label: "run-2"')
    assert parse_mandate(other).content_hash != parse_mandate(VALID).content_hash


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (VALID + "budget_cents: 99999\n", "duplicate key"),
        (VALID + "extra: 1\n", "unknown keys"),
        (_with("tool_call_budget", None), "missing keys"),
        ("base: &b {version: 1}\n<<: *b\n", "merge keys"),
        ("- 1\n- 2\n", "mapping"),
        ("version: [1\n", "invalid YAML"),
        ("!!python/object:os.system {}\n", "invalid YAML"),
        (_with("version", "version: 2"), "version"),
    ],
)
def test_structure_is_strict(text, message):
    with pytest.raises(MandateError, match=message):
        parse_mandate(text)


@pytest.mark.parametrize(
    ("key", "line", "message"),
    [
        ("budget_cents", "budget_cents: 200.0", "integer"),
        ("budget_cents", "budget_cents: yes", "integer"),
        ("budget_cents", 'budget_cents: "20000"', "integer"),
        ("budget_cents", "budget_cents: 0", "between"),
        ("price_lock_ttl_seconds", "price_lock_ttl_seconds: 1800.5", "integer"),
        ("price_lock_ttl_seconds", "price_lock_ttl_seconds: 0", "between"),
        ("max_approval_requests", "max_approval_requests: -1", "between"),
        ("tool_call_budget", "tool_call_budget: on", "integer"),
        ("approval_threshold_cents", "approval_threshold_cents: 20001", "exceed"),
        ("allowed_categories", "allowed_categories: [no, dairy]", "string"),
        ("allowed_categories", "allowed_categories: [dairy, dairy]", "duplicates"),
        ("allowed_categories", "allowed_categories: [unknown]", "allowable"),
        ("allowed_categories", "allowed_categories: [groceries]", "allowable"),
        ("allowed_categories", "allowed_categories: []", "non-empty"),
        ("allowed_categories", "allowed_categories: dairy", "non-empty list"),
        ("session_label", "session_label: 2026-11-16", "session_label"),
        ("session_label", 'session_label: ""', "session_label"),
        ("session_label", 'session_label: "has space"', "session_label"),
        ("session_label", f'session_label: "{"a" * 65}"', "session_label"),
        ("session_label", "session_label: 12", "session_label"),
    ],
)
def test_values_are_validated_by_exact_type(key, line, message):
    with pytest.raises(MandateError, match=message):
        parse_mandate(_with(key, line))
