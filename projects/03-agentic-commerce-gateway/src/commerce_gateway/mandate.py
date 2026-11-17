"""The human-written mandate: strict YAML schema and a content hash (ADR 0003, ADR 0004).

YAML is parsed with a `SafeLoader` subclass that rejects duplicate and merge keys. Every value is
then checked by exact type, so YAML 1.1 surprises (`no` becoming `False`, `1800.0` becoming a
float, `2026-11-16` becoming a date) are rejected instead of being coerced.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from commerce_gateway.canonical import canonical_hash
from commerce_gateway.models import KNOWN_CATEGORIES, UNKNOWN_CATEGORY

MANDATE_VERSION = 1
SESSION_LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# key -> (minimum, maximum) for the integer fields
_INT_LIMITS = {
    "budget_cents": (1, 100_000_000),
    "approval_threshold_cents": (0, 100_000_000),
    "price_lock_ttl_seconds": (1, 86_400),
    "max_approval_requests": (0, 100),
    "tool_call_budget": (1, 10_000),
}
MANDATE_KEYS = frozenset({"version", "session_label", "allowed_categories", *_INT_LIMITS})


class MandateError(ValueError):
    """The mandate file or content is invalid."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys and merge keys instead of silently resolving them."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise MandateError("expected a mapping")
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise MandateError("merge keys are not allowed")
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise MandateError(f"duplicate key: {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class Mandate:
    """A validated mandate. Every field is checked by exact type here, however it was built."""

    session_label: str
    budget_cents: int
    approval_threshold_cents: int
    allowed_categories: tuple[str, ...]
    price_lock_ttl_seconds: int
    max_approval_requests: int
    tool_call_budget: int

    def __post_init__(self) -> None:
        label = self.session_label
        if type(label) is not str or not SESSION_LABEL_PATTERN.fullmatch(label):
            raise MandateError("session_label must be 1-64 characters of A-Z a-z 0-9 . _ -")
        for key, (low, high) in _INT_LIMITS.items():
            value = getattr(self, key)
            if type(value) is not int:
                raise MandateError(f"{key} must be an integer")
            if not low <= value <= high:
                raise MandateError(f"{key} must be between {low} and {high}")
        if self.approval_threshold_cents > self.budget_cents:
            raise MandateError("approval_threshold_cents must not exceed budget_cents")
        categories = self.allowed_categories
        if type(categories) is not tuple or not categories:
            raise MandateError("allowed_categories must be a non-empty list")
        for category in categories:
            if type(category) is not str:
                raise MandateError("every allowed category must be a string")
            if category == UNKNOWN_CATEGORY or category not in KNOWN_CATEGORIES:
                raise MandateError(f"not an allowable category: {category!r}")
        if len(set(categories)) != len(categories):
            raise MandateError("allowed_categories has duplicates")
        if list(categories) != sorted(categories):
            raise MandateError("allowed_categories must be sorted")

    @property
    def price_lock_ttl_ms(self) -> int:
        return self.price_lock_ttl_seconds * 1000

    def content(self) -> dict[str, object]:
        """The parsed, normalised content. Categories are sorted, so order in the file is moot."""
        return {
            "version": MANDATE_VERSION,
            "session_label": self.session_label,
            "budget_cents": self.budget_cents,
            "approval_threshold_cents": self.approval_threshold_cents,
            "allowed_categories": list(self.allowed_categories),
            "price_lock_ttl_seconds": self.price_lock_ttl_seconds,
            "max_approval_requests": self.max_approval_requests,
            "tool_call_budget": self.tool_call_budget,
        }

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.content())


def mandate_from_content(content: object) -> Mandate:
    """Check the structure of parsed content (YAML or a stored snapshot) and build a `Mandate`."""
    if not isinstance(content, dict):
        raise MandateError("the mandate must be a mapping")
    keys = set(content)
    if keys != MANDATE_KEYS:
        unknown, missing = sorted(map(str, keys - MANDATE_KEYS)), sorted(MANDATE_KEYS - keys)
        raise MandateError(f"unknown keys {unknown}, missing keys {missing}")
    if type(content["version"]) is not int or content["version"] != MANDATE_VERSION:
        raise MandateError(f"version must be {MANDATE_VERSION}")
    categories = content["allowed_categories"]
    if type(categories) is not list or not categories:
        raise MandateError("allowed_categories must be a non-empty list")
    if not all(type(category) is str for category in categories):
        raise MandateError("every allowed category must be a string")
    if len(set(categories)) != len(categories):
        raise MandateError("allowed_categories has duplicates")
    fields = {key: value for key, value in content.items() if key != "version"}
    return Mandate(**{**fields, "allowed_categories": tuple(sorted(categories))})


def parse_mandate(text: str) -> Mandate:
    try:
        content = yaml.load(text, Loader=_StrictLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise MandateError(f"invalid YAML: {exc}") from exc
    return mandate_from_content(content)


def load_mandate(path: Path) -> Mandate:
    return parse_mandate(path.read_text(encoding="utf-8"))
