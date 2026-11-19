# Agentic Commerce Gateway

Work in progress. A gateway that lets AI agents buy from the Fogline Pantry online store within a
spend mandate, with human approval above a threshold and a tamper-evident audit trail. Payment is
always simulated.

> The store, the venues and the fixture products are fictional.

The trust boundary (what the gateway enforces, and what it does not) is designed in
[ADR 0003](../../docs/decisions/0003-commerce-gateway-trust-boundary.md), written before any
gateway code. Sessions, the SQLite store and the hash-chained audit trail are designed in
[ADR 0004](../../docs/decisions/0004-state-store-and-audit.md). The MCP tool contract (descriptions,
schemas, result shape, errors) and the two entry points are designed in
[ADR 0005](../../docs/decisions/0005-mcp-tool-contract.md).

Built so far: the fixture catalogue, the mandate loader, the pure policy function, the store,
sessions, cart, quotes, checkouts, the audit trail, the MCP server, the approval CLI, and the
adversarial scenarios with an independent oracle
([ADR 0006](../../docs/decisions/0006-evals-oracle-and-metrics.md)). Not built yet: the LLM agent
runs.

**Scripted attack scenarios** (no LLM): `uv run poe cg-scenarios` runs 15 deterministic attackers
over real stdio against the server and the approval CLI, then judges each final database with an
oracle that shares no code with the gateway. Results, from that command
([evals/results/scenarios.md](evals/results/scenarios.md)): **0 forbidden actions completed**;
every scripted forbidden call was blocked, listed per rule in the report; 1 permitted deviation
(an injected instruction the mandate allows). Each of the oracle's 14 checks has been seen to
fail on a hand-corrupted database. This shows that the enumerated attacks are blocked, not that
the gateway is safe against attacks nobody wrote down. One limit is worth stating plainly: the
truncation scenario (S14) detects a truncated audit trail without an anchor only through the
harness's out-of-band record of the counters; the gateway alone does not detect a truncation
without an anchor, so the audit head must be exported and kept elsewhere.
`uv run pytest projects/03-agentic-commerce-gateway` runs the offline tests, including end-to-end
tests over a real stdio connection to a spawned server.

```bash
# Both variables are required; there is no default database path.
export COMMERCE_GATEWAY_MANDATE=projects/03-agentic-commerce-gateway/mandates/example.yaml
export COMMERCE_GATEWAY_DB=data/commerce_gateway.db

uv run python -m commerce_gateway.server            # MCP server over stdio (for an MCP client)

uv run python -m commerce_gateway.approval check-mandate "$COMMERCE_GATEWAY_MANDATE"
uv run python -m commerce_gateway.approval list     # pending checkout, structured fields
uv run python -m commerce_gateway.approval approve <checkout_id> <quote_hash>
uv run python -m commerce_gateway.approval reject <checkout_id> <quote_hash>
uv run python -m commerce_gateway.approval audit-head
uv run python -m commerce_gateway.approval verify --anchor <seq>:<row_hash>
```
