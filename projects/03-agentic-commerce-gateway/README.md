# Agentic Commerce Gateway

Work in progress. A gateway that lets AI agents buy from the Fogline Pantry online store within a
spend mandate, with human approval above a threshold and a tamper-evident audit trail. Payment is
always simulated.

> The store, the venues and the fixture products are fictional.

The trust boundary (what the gateway enforces, and what it does not) is designed in
[ADR 0003](../../docs/decisions/0003-commerce-gateway-trust-boundary.md), written before any
gateway code. Sessions, the SQLite store and the hash-chained audit trail are designed in
[ADR 0004](../../docs/decisions/0004-state-store-and-audit.md).

Built so far: the fixture catalogue, the mandate loader, the pure policy function, the store,
sessions, cart, quotes, checkouts, the approver functions and the audit trail with `verify`. Not
built yet: the MCP server, the approval CLI and the agent runs, so there are no results to report.
`uv run pytest projects/03-agentic-commerce-gateway` runs the offline tests.
