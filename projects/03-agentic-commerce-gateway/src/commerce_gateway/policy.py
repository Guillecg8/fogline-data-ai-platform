"""Deterministic policy: a pure function of action, product facts, mandate and time (ADR 0003).

Boundaries, each tested:
- spend cap: a total equal to the cap is allowed; one cent more is denied;
- approval threshold: a total equal to the threshold is paid without approval;
- price locks and quotes: expired when `now >= expires_at` (at equality it is already expired);
- unknown category, or a SKU with no facts: treated as not allowed;
- tool-call budget N: calls 1..N are allowed, call N+1 and every later call are denied.

A decision is tri-state: `allowed`, `denied` (with the rule that denied it) or
`needs_approval` (rule `approval_required`; the store turns it into a `pending_approval`
checkout). Every reason is fixed text; no reason ever contains product text or an argument.
"""

from dataclasses import dataclass
from enum import StrEnum

from commerce_gateway.mandate import Mandate
from commerce_gateway.models import LockedLine, ProductFacts, require_int

CATEGORY_NOT_ALLOWED = "category_not_allowed"
OUT_OF_STOCK = "out_of_stock"
STALE_PRICE = "stale_price"
SPEND_CAP = "spend_cap"
APPROVAL_REQUIRED = "approval_required"
QUOTE_MISMATCH = "quote_mismatch"
APPROVAL_LIMIT = "approval_limit"
CHECKOUT_PENDING = "checkout_pending"
TOOL_CALL_BUDGET = "tool_call_budget"

RULE_IDS = (
    CATEGORY_NOT_ALLOWED,
    OUT_OF_STOCK,
    STALE_PRICE,
    SPEND_CAP,
    APPROVAL_REQUIRED,
    QUOTE_MISMATCH,
    APPROVAL_LIMIT,
    CHECKOUT_PENDING,
    TOOL_CALL_BUDGET,
)


class Verdict(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule_id: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.verdict is Verdict.ALLOWED:
            consistent = self.rule_id is None
        elif self.verdict is Verdict.NEEDS_APPROVAL:
            consistent = self.rule_id == APPROVAL_REQUIRED
        else:
            consistent = self.rule_id in RULE_IDS and self.rule_id != APPROVAL_REQUIRED
        if not consistent:
            raise ValueError(f"rule_id {self.rule_id!r} does not fit verdict {self.verdict}")

    @property
    def denied(self) -> bool:
        return self.verdict is Verdict.DENIED


ALLOW = Decision(Verdict.ALLOWED, None, "allowed")


def _deny(rule_id: str, reason: str) -> Decision:
    return Decision(Verdict.DENIED, rule_id, reason)


DENY_CATEGORY = _deny(CATEGORY_NOT_ALLOWED, "product category is not allowed by the mandate")
DENY_STOCK = _deny(OUT_OF_STOCK, "requested quantity exceeds stock")
DENY_STALE = _deny(STALE_PRICE, "price lock expired; add the product again to re-lock it")
DENY_CAP = _deny(SPEND_CAP, "order would exceed the session spend cap")
NEEDS_APPROVAL = Decision(
    Verdict.NEEDS_APPROVAL, APPROVAL_REQUIRED, "order total is above the approval threshold"
)
DENY_MISMATCH = _deny(QUOTE_MISMATCH, "quote is not the current pending quote")
DENY_APPROVAL_COUNT = _deny(APPROVAL_LIMIT, "approval request limit for this session is used up")
DENY_APPROVAL_PENDING = _deny(APPROVAL_LIMIT, "another approval is already pending")
DENY_PENDING = _deny(CHECKOUT_PENDING, "cart is frozen while a checkout awaits approval")
DENY_BUDGET = _deny(TOOL_CALL_BUDGET, "tool-call budget for this session is used up")


@dataclass(frozen=True)
class ToolCall:
    """Any tool call. `calls_used` is the number of calls counted before this one."""

    calls_used: int

    def __post_init__(self) -> None:
        require_int("calls_used", self.calls_used)


@dataclass(frozen=True)
class AddToCart:
    sku: str
    quantity_after: int
    cart_frozen: bool

    def __post_init__(self) -> None:
        require_int("quantity_after", self.quantity_after, 1)


@dataclass(frozen=True)
class RemoveFromCart:
    cart_frozen: bool


@dataclass(frozen=True)
class RequestCheckout:
    lines: tuple[LockedLine, ...]
    committed_cents: int
    approval_requests_used: int
    pending_exists: bool

    def __post_init__(self) -> None:
        require_int("committed_cents", self.committed_cents)
        require_int("approval_requests_used", self.approval_requests_used)


@dataclass(frozen=True)
class FinaliseApproval:
    """The approver approves `quote_hash`; `pending_quote_hash` is the session's pending quote."""

    quote_hash: str
    pending_quote_hash: str | None
    quote_expires_at_ms: int
    lines: tuple[LockedLine, ...]
    committed_other_cents: int

    def __post_init__(self) -> None:
        require_int("quote_expires_at_ms", self.quote_expires_at_ms)
        require_int("committed_other_cents", self.committed_other_cents)


Action = ToolCall | AddToCart | RemoveFromCart | RequestCheckout | FinaliseApproval


def _lines_decision(
    lines: tuple[LockedLine, ...], facts: dict[str, ProductFacts], mandate: Mandate, now_ms: int
) -> Decision:
    """Category, then stock, then stale price, checked over every line in SKU order."""
    for line in lines:
        fact = facts.get(line.sku)
        if fact is None or fact.policy_category not in mandate.allowed_categories:
            return DENY_CATEGORY
    for line in lines:
        if line.quantity > facts[line.sku].stock:
            return DENY_STOCK
    for line in lines:
        if now_ms >= line.lock_expires_at_ms:
            return DENY_STALE
    return ALLOW


def _total(lines: tuple[LockedLine, ...]) -> int:
    return sum(line.line_total_cents for line in lines)


def evaluate(
    action: Action, facts: tuple[ProductFacts, ...], mandate: Mandate, now_ms: int
) -> Decision:
    """Decide `action`. Pure: no I/O, no clock, no product text."""
    by_sku = {fact.sku: fact for fact in facts}

    if isinstance(action, ToolCall):
        return DENY_BUDGET if action.calls_used >= mandate.tool_call_budget else ALLOW

    if isinstance(action, AddToCart):
        if action.cart_frozen:
            return DENY_PENDING
        fact = by_sku.get(action.sku)
        if fact is None or fact.policy_category not in mandate.allowed_categories:
            return DENY_CATEGORY
        if action.quantity_after > fact.stock:
            return DENY_STOCK
        return ALLOW

    if isinstance(action, RemoveFromCart):
        return DENY_PENDING if action.cart_frozen else ALLOW

    if isinstance(action, RequestCheckout):
        lines_decision = _lines_decision(action.lines, by_sku, mandate, now_ms)
        if lines_decision.denied:
            return lines_decision
        total = _total(action.lines)
        if action.committed_cents + total > mandate.budget_cents:
            return DENY_CAP
        if total <= mandate.approval_threshold_cents:
            return ALLOW
        if action.pending_exists:
            return DENY_APPROVAL_PENDING
        if action.approval_requests_used >= mandate.max_approval_requests:
            return DENY_APPROVAL_COUNT
        return NEEDS_APPROVAL

    if isinstance(action, FinaliseApproval):
        if (
            action.pending_quote_hash is None
            or action.pending_quote_hash != action.quote_hash
            or now_ms >= action.quote_expires_at_ms
        ):
            return DENY_MISMATCH
        lines_decision = _lines_decision(action.lines, by_sku, mandate, now_ms)
        if lines_decision.denied:
            return lines_decision
        if action.committed_other_cents + _total(action.lines) > mandate.budget_cents:
            return DENY_CAP
        return ALLOW

    raise TypeError(f"unknown action {type(action).__name__}")
