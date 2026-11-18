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


ROOT = "(root)"  # path of a problem with the document itself, or with a key that is not known


@dataclass(frozen=True)
class Problem:
    """One reason a mandate is invalid: a field path and a fixed reason, never the value."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


class MandateError(ValueError):
    """The mandate file or content is invalid. Its message never quotes a rejected value."""

    def __init__(self, *problems: Problem) -> None:
        super().__init__("; ".join(map(str, problems)))
        self.problems = problems


def _key_path(key: object) -> str:
    """A known key is its own path; an unknown key is file content, so it is not echoed."""
    return key if key in MANDATE_KEYS else ROOT


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys and merge keys instead of silently resolving them."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise MandateError(Problem(ROOT, "the mandate must be a mapping"))
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise MandateError(Problem(ROOT, "merge keys are not allowed"))
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise MandateError(Problem(_key_path(key), "duplicate key"))
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _value_problems(values: dict[str, object], sequence_type: type) -> list[Problem]:
    """Check the value of every known key present in `values`. Missing keys are not checked."""
    problems: list[Problem] = []
    if "session_label" in values:
        label = values["session_label"]
        if type(label) is not str or not SESSION_LABEL_PATTERN.fullmatch(label):
            reason = "must be 1-64 characters of A-Z a-z 0-9 . _ -"
            problems.append(Problem("session_label", reason))
    valid_ints: dict[str, int] = {}
    for key, (low, high) in _INT_LIMITS.items():
        if key not in values:
            continue
        value = values[key]
        if type(value) is not int:
            problems.append(Problem(key, "must be an integer"))
        elif not low <= value <= high:
            problems.append(Problem(key, f"must be between {low} and {high}"))
        else:
            valid_ints[key] = value
    threshold = valid_ints.get("approval_threshold_cents")
    budget = valid_ints.get("budget_cents")
    if threshold is not None and budget is not None and threshold > budget:
        problems.append(Problem("approval_threshold_cents", "must not exceed budget_cents"))
    if "allowed_categories" in values:
        problems += _category_problems(values["allowed_categories"], sequence_type)
    return problems


def _category_problems(categories: object, sequence_type: type) -> list[Problem]:
    if type(categories) is not sequence_type or not categories:
        return [Problem("allowed_categories", "must be a non-empty list")]
    problems: list[Problem] = []
    for index, category in enumerate(categories):
        path = f"allowed_categories[{index}]"
        if type(category) is not str:
            problems.append(Problem(path, "must be a string"))
        elif category == UNKNOWN_CATEGORY or category not in KNOWN_CATEGORIES:
            problems.append(Problem(path, "not an allowable category"))
    if problems:
        return problems
    if len(set(categories)) != len(categories):
        problems.append(Problem("allowed_categories", "has duplicates"))
    elif sequence_type is tuple and list(categories) != sorted(categories):
        problems.append(Problem("allowed_categories", "must be sorted"))
    return problems


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
        values = {key: getattr(self, key) for key in MANDATE_KEYS if key != "version"}
        problems = _value_problems(values, tuple)
        if problems:
            raise MandateError(*problems)

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
    """Check parsed content (YAML or a stored snapshot) and build a `Mandate`.

    Every problem found is reported at once, each as a field path and a fixed reason.
    """
    if not isinstance(content, dict):
        raise MandateError(Problem(ROOT, "the mandate must be a mapping"))
    problems = [Problem(ROOT, "unknown key") for key in content if key not in MANDATE_KEYS]
    problems += [Problem(key, "missing key") for key in sorted(MANDATE_KEYS - set(content))]
    if "version" in content and (
        type(content["version"]) is not int or content["version"] != MANDATE_VERSION
    ):
        problems.append(Problem("version", f"must be {MANDATE_VERSION}"))
    problems += _value_problems(content, list)
    if problems:
        raise MandateError(*problems)
    fields = {key: value for key, value in content.items() if key != "version"}
    return Mandate(**{**fields, "allowed_categories": tuple(sorted(fields["allowed_categories"]))})


def parse_mandate(text: str) -> Mandate:
    try:
        content = yaml.load(text, Loader=_StrictLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        # The parser's message quotes the document, so only the line number is kept.
        mark = getattr(exc, "problem_mark", None)
        path = f"line {mark.line + 1}" if mark is not None else ROOT
        raise MandateError(Problem(path, "invalid YAML")) from None
    return mandate_from_content(content)


def load_mandate(path: Path) -> Mandate:
    return parse_mandate(path.read_text(encoding="utf-8"))
