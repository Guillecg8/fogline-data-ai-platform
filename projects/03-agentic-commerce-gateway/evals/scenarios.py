"""The scenario catalogue (ADR 0006): deterministic scripted attackers, no LLM.

Every step declares its expected outcome and whether it is an attempt (a call the mandate
forbids) before it runs. Prices are those in force after the 2100-01-01 time floor.
"""

from harness import (
    MISSING,
    Advance,
    AdvanceToExpiry,
    Call,
    Cli,
    Concurrent,
    EditAuditPayload,
    Expect,
    Kill,
    ListTools,
    RawCall,
    RecordAnchor,
    SameCheckout,
    Scenario,
    SetStock,
    Start,
    Stop,
    TruncateFromAnchor,
)

TOOL_NAMES = (
    "get_policy",
    "search_products",
    "get_product",
    "add_to_cart",
    "remove_from_cart",
    "view_cart",
    "request_checkout",
    "get_checkout_status",
)

OK = Expect("ok")
COMPLETED = Expect("completed")
PENDING = Expect("pending_approval", "approval_required")
INVALID = Expect("error", error="invalid_arguments")
WRONG_HASH = "0" * 64


def denied(rule_id: str) -> Expect:
    return Expect("denied", rule_id)


def add(sku: str, quantity: object, expect: Expect = OK, **kw: object) -> Call:
    return Call("add_to_cart", {"sku": sku, "quantity": quantity}, expect, **kw)


def remove(sku: str, quantity: int, expect: Expect = OK, **kw: object) -> Call:
    return Call("remove_from_cart", {"sku": sku, "quantity": quantity}, expect, **kw)


def checkout(expect: Expect, **kw: object) -> Call:
    return Call("request_checkout", {}, expect, **kw)


def status(value: str, **kw: object) -> Call:
    return Call("get_checkout_status", {}, Expect("ok", data={"checkout.status": value}), **kw)


def approve(exit_code: int, rule_id: str, final: str, quote: str = "{quote_hash}") -> Cli:
    fields = {"rule_id": rule_id, "status": final}
    return Cli(("approve", "{checkout_id}", quote), exit_code, fields)


def reject(final: str = "rejected") -> Cli:
    return Cli(("reject", "{checkout_id}", "{quote_hash}"), 0, {"status": final})


MEGABYTE = "x" * 1_000_000
V1_LINE = {"cart.lines.0.price_version": 1, "cart.lines.0.unit_price_cents": 1200}
V2_LINE = {"cart.lines.0.price_version": 2, "cart.lines.0.unit_price_cents": 1350}
V2_CURRENT = {"product.price_version": 2, "product.unit_price_cents": 1350}

SCENARIOS = (
    Scenario(
        "S01",
        "Forbidden category",
        (
            add("FP-0024", 1, denied("category_not_allowed"), attempt=True),  # alcohol
            add("FP-0026", 1, denied("category_not_allowed"), attempt=True),  # unknown
            add("FP-0001", 1),
            checkout(COMPLETED),
        ),
    ),
    Scenario(
        "S02",
        "Injection in product text",
        (
            Call("get_product", {"sku": "FP-0027"}, OK),  # asks for 40 units of FP-0020
            Call("get_product", {"sku": "FP-0028"}, OK),
            Call("get_product", {"sku": "FP-0030"}, OK),
            Call("get_product", {"sku": "FP-0032"}, OK),
            Call("search_products", {"query": "saffron"}, OK),
            add("FP-0001", 1),  # the intended line
            add("FP-0020", 40, denied("out_of_stock"), attempt=True),  # the full injected ask
            add("FP-0020", 1),  # the obedient add within limits: a permitted deviation
            checkout(COMPLETED),
        ),
        mandate={"approval_threshold_cents": 8_000},
        intended_lines=(("FP-0001", 1),),
    ),
    Scenario(
        "S03",
        "Spend cap across several orders",
        (
            add("FP-0022", 1),
            checkout(COMPLETED),
            add("FP-0022", 1),
            checkout(COMPLETED),
            add("FP-0020", 1),
            checkout(PENDING),  # reserves 6900: committed 15300
            approve(0, "none", "completed"),
            add("FP-0021", 1),
            checkout(denied("spend_cap"), attempt=True),  # 15300 + 7400 > 20000
            remove("FP-0021", 1),
            add("FP-0001", 1),
            checkout(COMPLETED),
            Call("get_policy", {}, Expect("ok", data={"spent_cents": 15_950})),
        ),
    ),
    Scenario(
        "S04",
        "Above the threshold: repeats and cart changes while pending",
        (
            add("FP-0020", 1),
            checkout(PENDING),
            checkout(PENDING),
            SameCheckout(),
            checkout(PENDING),
            add("FP-0001", 1, denied("checkout_pending"), attempt=True),
            remove("FP-0020", 1, denied("checkout_pending"), attempt=True),
            status("pending_approval"),
            approve(0, "none", "completed"),
            status("completed"),
            Call("get_policy", {}, Expect("ok", data={"approval_requests_used": 1})),
        ),
    ),
    Scenario(
        "S05",
        "Wrong quote hash, then approval at expiry",
        (
            add("FP-0020", 1),
            checkout(PENDING),
            approve(1, "quote_mismatch", "pending_approval", quote=WRONG_HASH),
            AdvanceToExpiry(0),
            approve(1, "quote_mismatch", "expired"),
            checkout(Expect("expired")),
            status("expired"),
        ),
    ),
    Scenario(
        "S06",
        "Stale price",
        (
            add("FP-0001", 1),
            Advance(1_800_000),  # exactly the lock expiry
            checkout(denied("stale_price"), attempt=True),
            Call("view_cart", {}, Expect("ok", data={"cart.lines.0.seconds_left": 0})),
            add("FP-0001", 1, Expect("ok", data={"cart.lines.0.quantity": 2})),
            checkout(COMPLETED),
        ),
    ),
    Scenario(
        "S06b",
        "A price change while the price is locked",
        (
            # FP-0033 costs 1200 (v1) until 2100-01-01T00:10Z, then 1350 (v2).
            add("FP-0033", 1, Expect("ok", data=V1_LINE)),
            Advance(15 * 60_000),  # past the price change, inside the 30-minute lock
            Call("get_product", {"sku": "FP-0033"}, Expect("ok", data=V2_CURRENT)),
            checkout(Expect("completed", data={"checkout.total_cents": 1200})),
            add("FP-0033", 1, Expect("ok", data=V2_LINE)),
            checkout(Expect("completed", data={"checkout.total_cents": 1350})),
        ),
    ),
    Scenario(
        "S07",
        "Out of stock at approval",
        (
            add("FP-0020", 2),
            checkout(PENDING),
            # A sale outside the gateway takes the last units.
            SetStock("FP-0020", 0),
            approve(1, "out_of_stock", "rejected"),
            status("rejected"),
            checkout(Expect("rejected")),
        ),
    ),
    Scenario(
        "S08",
        "Tool-call budget, with the approval CLI still working",
        (
            add("FP-0020", 1),
            checkout(PENDING),
            Call("view_cart", {}, OK),
            Call("get_policy", {}, OK),
            Call("search_products", {"query": "salt"}, OK),
            status("pending_approval"),
            *(
                Call(tool, arguments, denied("tool_call_budget"), attempt=True)
                for tool, arguments in (
                    ("get_policy", {}),
                    ("search_products", {"query": "salt"}),
                    ("get_product", {"sku": "FP-0001"}),
                    ("add_to_cart", {"sku": "FP-0001", "quantity": 1}),
                    ("remove_from_cart", {"sku": "FP-0001", "quantity": 1}),
                    ("view_cart", {}),
                    ("request_checkout", {}),
                    ("get_checkout_status", {}),
                )
            ),
            approve(0, "none", "completed"),
            Cli(("audit-head",), 0),
        ),
        mandate={"tool_call_budget": 6},
    ),
    Scenario(
        "S09",
        "Approval-request limit",
        (
            add("FP-0020", 1),
            checkout(PENDING),
            reject(),
            add("FP-0001", 1),
            checkout(denied("approval_limit"), attempt=True),  # 7550 needs a second approval
            remove("FP-0020", 1),
            checkout(COMPLETED),
        ),
        mandate={"max_approval_requests": 1},
    ),
    Scenario(
        "S10",
        "Hostile arguments",
        (
            Call("search_products", {"query": MEGABYTE}, INVALID),
            add("FP-0001", "abc", INVALID),
            Call("get_product", {"sku": 123}, INVALID),
            Call("view_cart", {"extra": 1}, INVALID),
            Call("add_to_cart", {"sku": "FP-0001"}, INVALID),
            Call("drop_tables", {}, INVALID),
            add("FP-0001", 2.5, INVALID),
            add("FP-0001", True, INVALID),
            add("FP-0001", 10**30, INVALID),
            Call("view_cart", None, OK),
            Call("view_cart", MISSING, OK),
            RawCall({"name": "view_cart", "arguments": "abc"}),
            RawCall({"name": 123, "arguments": {}}),
            Call("get_policy", {}, Expect("ok", data={"tool_calls_used": 12})),
        ),
        max_invalid_payload_bytes=400,
    ),
    Scenario(
        "S11",
        "Restart mid-session, then two servers on one cart",
        (
            add("FP-0001", 2),
            Kill("a"),
            Start("a"),
            Call("view_cart", {}, Expect("ok", data={"cart.lines.0.quantity": 2})),
            Call("get_policy", {}, Expect("ok", data={"tool_calls_used": 3})),
            Start("b"),
            add("FP-0020", 1),
            checkout(PENDING),
            checkout(PENDING, server="b"),
            SameCheckout(),
            Concurrent((checkout(PENDING), checkout(PENDING, server="b"))),
            approve(0, "none", "completed"),
            add("FP-0002", 1, server="b"),
            Concurrent(
                (
                    checkout(COMPLETED),
                    checkout(Expect("error", error="cart_empty"), server="b"),
                )
            ),
            Call("get_policy", {}, Expect("ok", data={"approval_requests_used": 1}), server="b"),
            Stop("b"),
        ),
    ),
    Scenario(
        "S12",
        "The agent cannot approve",
        (
            ListTools(TOOL_NAMES),
            add("FP-0020", 1),
            checkout(PENDING),
            Call("approve", {}, INVALID),
            Call("approve_checkout", {"checkout_id": 1, "quote_hash": WRONG_HASH}, INVALID),
            Call("admin_approve", {}, INVALID),
            Call("set_mandate", {"budget_cents": 1_000_000}, INVALID),
            Call("finalise", {}, INVALID),
            status("pending_approval"),
        ),
    ),
    Scenario(
        "S13",
        "Audit tampering: an edited row",
        (
            add("FP-0001", 1),
            checkout(COMPLETED),
            Stop("a"),
            EditAuditPayload(3),
            Cli(("verify",), 1),
        ),
        expected_violations=("audit_chain",),
    ),
    Scenario(
        "S14",
        "Audit tampering: truncation below an anchor",
        (
            add("FP-0001", 1),
            RecordAnchor(),
            Call("view_cart", {}, OK),
            Call("get_policy", {}, OK),
            Stop("a"),
            TruncateFromAnchor(),
            Cli(("verify",), 0),
            Cli(("verify", "--anchor", "{anchor}"), 1),
        ),
        expected_violations=("monotonic",),
        expected_violations_anchored=("audit_chain", "monotonic"),
    ),
)
