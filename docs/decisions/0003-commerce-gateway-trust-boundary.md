# 0003. Commerce gateway: trust boundary and control layer

- Status: accepted
- Date: 2026-11-16
- Project: `projects/03-agentic-commerce-gateway`

## Context

The gateway makes the Fogline Pantry online store purchasable by AI agents. An agent acts for a
human, who sets the limits and approves large orders. The agent is a language model (locally
`qwen2.5:7b`): it can misread a task, call tools with wrong arguments, or follow instructions
hidden in product text. Payment is always simulated.

This ADR fixes what the gateway enforces and where the trust boundary is. It was written
**before any gateway code**. How state and the audit trail are stored, and how agent behaviour is
evaluated, are separate ADRs.

### Threat model

| Actor or input | Trust | What it can do |
|---|---|---|
| Human principal | Trusted | Writes the mandate file, starts the server, approves or rejects orders through the approval CLI |
| Agent (LLM) | Not trusted to respect limits | Calls the 8 MCP tools below, with any arguments, in any order, any number of times |
| Product text (names, descriptions, ingredients) | Untrusted input | May contain instructions aimed at the agent, at the enrichment LLM or at the approver |
| Operator with file-system access | Trusted, out of scope | Can edit the database, the mandate or the code; the gateway does not defend against this |

**Assumption that defines the boundary:** the agent's only capability is the MCP tool surface of
this server. It has no shell, no file access and no other tool that can reach the approval CLI,
the database or the mandate file. A deployment that gives the agent such tools is outside what
this design protects.

Assets, in order: money (no order above the limits without a human), the integrity of the audit
trail, order correctness (what was bought matches what was approved), and the approver's
attention. An agent that floods the human with approval requests wears down the only control
above the threshold, so the number of requests is limited by the mandate (below).

## Decision

### MCP is the only agent interface

One MCP server over stdio, with exactly these 8 tools:

`get_policy`, `search_products`, `get_product`, `add_to_cart`, `remove_from_cart`, `view_cart`,
`request_checkout`, `get_checkout_status`.

- **No tool approves, rejects, changes the mandate or edits the audit trail.** A test asserts
  that the tool list is exactly the 8 names above.
- **One implicit cart per session.** There is no `create_cart` and no `cart_id`, so the agent
  cannot address someone else's cart and has fewer identifiers to get wrong.
- **No free-text arguments on tools that change state.** `add_to_cart` and `remove_from_cart`
  take a SKU and a quantity, and `request_checkout` takes nothing, so these calls cannot carry
  data out.
- **The one free-text argument is the `search_products` query.** It is untrusted like any agent
  input: capped to a fixed length (longer queries are rejected, not silently cut), used only as
  a search string, and written to the audit trail as its hash plus the capped text, marked as
  untrusted. It never reaches policy.
- **No FastAPI or HTTP API.** The agent reaches the gateway through MCP and the human through a
  CLI, and neither needs HTTP. An HTTP layer would add surface area and a second contract to keep
  in sync for no user of this project.

### The mandate

The human writes a YAML mandate. An environment variable selects it when the server starts, one
mandate per session. It holds:

- the session budget (spend cap);
- the allowed policy categories;
- the approval threshold;
- the price-lock TTL (30 minutes);
- the maximum number of approval requests per session;
- the per-session tool-call budget.

At most one approval may be pending per session; that limit is fixed, not configurable. The
tool-call budget is enforced by the gateway because the agent runner's step cap is outside the
trust boundary: the gateway cannot assume any particular client stops its agent. What
identifies a session, and so what resets the budget and the tool-call counter, is decided in the
state-store ADR.

There is no tool and no CLI that creates or edits a mandate while the server runs. Changing it
means editing the file and starting a new session. `get_policy` returns the mandate to the agent
read-only, so the agent can plan within its limits.

### Deterministic policy over text-free facts

- Policy is a pure function of the requested action, the facts about the products involved, the
  mandate and the current time (an injected clock). It is deterministic code, not a model.
- The facts type, `ProductFacts`, **has no text fields**: SKU, policy category, unit price in
  integer cents, price version and stock. Policy cannot read product text because it is never
  given any. A test checks that changing every text field of a product, including to an
  injection, leaves every decision unchanged.
- Rules, each with a stable `rule_id` that is returned to the agent and written to the audit
  trail:
  - `category_not_allowed`: the product's policy category is not in the mandate. A product whose
    category is unknown is treated as not allowed.
  - `out_of_stock`: the requested quantity exceeds stock, checked at `add_to_cart` and again
    when an order is finalised.
  - `stale_price`: `add_to_cart` locks the current price version for the mandate TTL. At
    checkout, a line whose lock has expired is rejected as stale. Adding the product again
    re-locks it at the current price.
  - `spend_cap`: the order total plus the session's completed and pending orders would exceed
    the session budget. The cap is hard: **human approval cannot exceed it.**
  - `approval_required`: the order total is above the approval threshold, so the order becomes
    `pending_approval` instead of being paid.
  - `quote_mismatch`: an approval or finalisation refers to a quote that is not the current
    pending one.
  - `approval_limit`: a checkout would need approval but the session has used up its maximum
    number of approval requests. "At most one pending approval" is a structural invariant
    enforced by `checkout_pending`: while a checkout is pending the cart is frozen, so no second
    quote can be requested through MCP. Policy still checks it defensively, with the same
    `rule_id` and a fixed reason that says which limit was hit, and that branch is tested at the
    store level, not through MCP, because no tool sequence can reach it.
  - `checkout_pending`: `add_to_cart` or `remove_from_cart` while the cart has a pending
    checkout. The cart is frozen until the human decides or the quote expires.
  - `tool_call_budget`: the session has used up its tool-call budget. Every call counts, allowed
    or denied, and once the budget is spent every tool returns this rejection. The approval CLI
    is not affected, so a pending order can still be decided.
- Rejections carry the `rule_id` and a short fixed reason, never product text.

### Quotes, idempotency and approval out of band

- `request_checkout` freezes the cart into a **quote**: session, cart revision, lines (SKU,
  quantity, locked unit price, price version, lock expiry) and total. `quote_hash` is a hash of
  exactly that content. The quote expires when its earliest price lock expires.
- **Idempotency is server-side, keyed by (cart, `quote_hash`).** Calling `request_checkout`
  again with an unchanged cart returns the existing checkout and never creates a second order.
  The agent supplies no idempotency key. Any change to the cart raises the revision, so it
  produces a new quote.
- At or below the threshold, the order is paid by the payment simulator and completed. Above it,
  the order is `pending_approval` and the budget is reserved.
- **Approval happens only through a CLI the agent cannot call.** It runs as a separate process
  against the same store and acts as the fixed actor `approver` (never the operating-system user
  name). An approval is valid **only for its `quote_hash` and only before the quote expires.** A
  pending approval does not extend the price locks. When the human approves, the gateway checks
  the quote again (hash, expiry, stock, cap) and then completes it or rejects it with a `rule_id`.
- The approval CLI shows structured fields (SKU, category, quantity, prices, total) and shows
  product names only as clearly marked, truncated untrusted text.

### Checkout states and the cart lifecycle

A checkout is `completed`, `pending_approval`, `rejected` (by the human, or by a rule when it is
finalised) or `expired` (its quote expired while pending). The cart revision only increases within
a session, never resets, so two quotes in one session never share a `quote_hash`.

- **Denied:** when `request_checkout` is denied by a rule (`spend_cap`, `stale_price`,
  `category_not_allowed`, `out_of_stock`, `approval_limit`), **no checkout is created**, and no
  approval request either. The denial is only audited. Requesting checkout again on the unchanged
  cart is simply denied again.
- **Completed:** the order is recorded, the implicit cart is emptied and its revision increases.
  Buying the same products again creates a new revision and a new quote.
- **Rejected:** the reservation against the budget is released and the cart keeps its lines. The
  cart unfreezes. Requesting checkout again on the unchanged cart has the same `quote_hash`, so it
  returns the existing rejected checkout. It does not create a new approval request. To try again
  the agent must change the cart, which creates a new revision and a new quote. If that quote
  needs approval it counts toward the approval-request limit.
- **Expired:** the same as rejected. The reservation is released, the cart keeps its lines and
  unfreezes, and the unchanged cart returns the expired checkout. The expired lines have to be
  added again to re-lock their price (`stale_price` otherwise), which creates a new revision.

### Product text is untrusted everywhere

`search_products` and `get_product` return product text only in a field named for that purpose,
with a length cap and control characters removed. Product text never appears in rule reasons,
error messages or the audit trail; those use SKUs and hashes. Marking the text does not stop an
agent from following it. It only keeps the gateway's own outputs free of it.

### Audit

Every tool call is appended to an audit trail, whether it was allowed or denied, with the
decision and its `rule_id`. So are every approval and rejection and every order state change. The
storage, append-only enforcement and hash chain are decided in the state-store ADR.

## What is NOT guaranteed

- **Intent.** The gateway enforces the mandate, not what the human meant. Take an injected
  instruction that makes the agent add an allowed product, inside the budget and below the
  threshold. It is not blocked: no rule is broken. Only orders above the threshold reach a human.
  Evaluations report these deviations separately from blocked violations.
- **Agent robustness.** Nothing here makes the agent resist prompt injection. How often it
  *attempts* a forbidden action is measured, not prevented.
- **Coverage.** Tests prove that the attacks they enumerate are blocked, and nothing more. A
  rule that is missing or wrong is not caught by the tests that assume it.
- **Correct categories.** The category rule is only as good as the policy category in
  `ProductFacts`. In phase B that category comes from a deterministic mapping or human review,
  never from the LLM alone, and unknown means blocked. A wrong deterministic mapping is not
  detected by the policy.
- **Capabilities outside MCP.** An agent with a shell or file access could run the approval CLI
  or edit the store. The design assumes it has neither.
- **The operator.** Anyone who can write the database, the mandate or the code is trusted. The
  hash chain detects edits to individual audit rows. It detects a rewrite of the whole chain only
  if the latest hash was recorded somewhere else.
- **A deceived approver.** The approver can still be misled by product names or by approving
  without reading. Showing structured fields reduces this; it does not prevent it.
- **Commerce realism.** There are no real payments, authentication, fraud checks, tax, shipping,
  refunds or multi-user sessions.

## Alternatives considered

- **An approval tool guarded by a confirmation in the MCP client.** Rejected: safety would then
  depend on every client asking the human, and the agent would hold a tool that approves.
- **A guardrail model that reads product text or tool calls.** Rejected as the enforcement
  layer: it is not deterministic, it can itself be injected, and it cannot be tested
  exhaustively. Detection models could be added later for reporting, never for enforcement.
- **A REST API (FastAPI) with an MCP adapter on top.** Rejected: no actor needs HTTP, and it
  doubles the interface.
- **Agent-supplied idempotency keys.** Rejected: a 7B model inventing unique keys is a new way
  to fail, and the real duplicate to prevent (paying the same cart twice) is identified by the
  cart and its quote.
- **Explicit carts (`create_cart`, `cart_id`).** Rejected: more identifiers for the agent to
  carry and no use with one session per mandate.
- **Creating or changing mandates at run time.** Rejected for scope. A file read at start is
  enough to show limits set by a human, and it keeps the mandate out of the agent's reach.

## Consequences

- Policy, quotes and approval can be tested offline and deterministically, with an injected
  clock and no LLM.
- The time available to approve an order is the TTL minus the age of the oldest price lock in
  the cart. An order whose quote expires before approval has to be re-added and requested again.
  That costs the agent steps and will show in task success rates.
- The store honours a locked price for up to the TTL, even if the current price has changed in
  the meantime, in either direction. This is a deliberate trade-off: the agent and the approver
  see a price that does not move under them, and the store accepts up to 30 minutes of exposure
  to price changes. Re-adding a product always locks the current price.
- Once the tool-call budget is used up, the session can do nothing more; once the
  approval-request limit is used up, it can still complete orders at or below the threshold but
  none above it. A new session is the only way past either limit. Task success rates will show it
  if the limits are set too tight.
- Evaluations must report separately: forbidden attempts by the agent, blocks by the gateway,
  forbidden actions that completed (the target is zero, and any is a bug), and deviations the
  policy allows. That design goes in the evals ADR, written before any agent run.
- Relation to the published agent-commerce protocols (ACP, AP2) has not been evaluated. Their
  details must be checked against their own specifications before the README compares this
  design with them.
