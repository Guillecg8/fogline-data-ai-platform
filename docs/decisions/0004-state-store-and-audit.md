# 0004. Commerce gateway: state store, sessions and audit trail

- Status: accepted
- Date: 2026-11-16
- Project: `projects/03-agentic-commerce-gateway`

## Context

[ADR 0003](0003-commerce-gateway-trust-boundary.md) fixed what the gateway enforces and deferred
three things to this ADR: what a session is (and so what resets the budget and the tool-call
counter), how state is stored and shared between the MCP server and the approval CLI, and how the
audit trail is made append-only and tamper-evident. This ADR is written before any store code.

Two processes act on the same state: the MCP server (actor `agent`) and the approval CLI (actor
`approver`). Every policy decision that matters is a read-then-write: read the session's
committed spend, decide, reserve. If two processes can interleave those steps, the cap and the
approval-request limit are not enforced, however correct the policy function is.

Constraints from the repo: cost zero, no server to run, portable to Windows CI, tests offline and
deterministic with an injected clock.

## Decision

### Storage: one SQLite file, stdlib `sqlite3`

- One SQLite database file per deployment, path from `COMMERCE_GATEWAY_DB` (default under
  `data/`, which is git-ignored). Tests and each eval task use their own temporary file, so tasks
  never share a session.
- Every table is `STRICT`. That rejects lossy values in an `INTEGER` column (`12.5`, text), but
  it is not a float guard: SQLite stores `2.0` as the integer `2`. The real guards are upstream:
  every dataclass that carries money, time or a quantity rejects `float` (including `2.0`) and
  `bool` in `__post_init__`, and the canonical encoder raises on any float. `STRICT` needs SQLite
  3.37; the store refuses to open on an older library. Money and time columns also carry `CHECK`
  constraints (cents `>= 0`, quantities `> 0`).
- No ORM. The schema is a handful of tables and the transaction boundaries are the point of the
  design, so they stay visible in plain SQL.

### Session identity

**The proposal evaluated:** a session row is created at every server start, bound to the mandate
content hash and a generated id; a restart creates a new session; counters live in the session row.

**Its flaw:** it makes the spend cap a limit per *process lifetime*, not per mandate. MCP clients
start stdio servers themselves and restart them after a crash or when the client restarts. Any
restart, deliberate or not, would give the agent a fresh budget, a fresh tool-call budget and a
fresh approval-request limit. An agent that can make the server crash (a malformed argument that
hits a bug) would reset its own limits. It also orphans a pending checkout: its reservation lives
in the old session, so the new session's cap does not see it and total spend can exceed what the
human intended.

**Decision:**

- A **session** is the period during which one mandate governs one principal's spending on this
  store. Its row holds a generated id (random 128-bit hex, from an injected id factory so tests
  are deterministic), the mandate content hash, a snapshot of the mandate in canonical JSON,
  `opened_at_ms`, `closed_at_ms`, `last_seen_ms`, the cart revision and `tool_calls_used`.
- At most one session is open per database, enforced by a partial unique index on sessions
  where `closed_at_ms IS NULL`.
- **Server start resumes the open session if its mandate hash equals the hash of the mandate being
  loaded.** A restart, a crash or a second server process with the same mandate continues the same
  session and the same counters.
- **A different mandate hash closes the open session and opens a new one.** That is the only way
  to reset the limits, and it requires editing the mandate file, which only the human can do.
  To start a fresh session with identical limits, the human changes the mandate's
  `session_label` field (a required free-form label that is part of the hash, validated in A3).
- **The server refuses to start** (exits with an error, no session change) if the open session
  has a pending checkout that is not yet expired and the mandate hash differs. The human decides
  it with the approval CLI or waits for it to expire. This keeps every reservation inside the
  session whose cap it was checked against.
- The approval CLI never reads the mandate file. It uses the snapshot stored in the session row
  and checks that it hashes to the stored mandate hash. Both processes therefore decide with the
  same mandate.
- A server whose session was closed by another process (started with a different mandate) answers
  every later call with a fixed `session_closed` error. That is a store error, not a policy
  `rule_id`, and it is audited.
- **The session lifecycle is audited** with actor `system`: `session_opened` (new session id,
  mandate hash), `session_resumed` (session id, mandate hash) and `session_superseded` (old
  session id and mandate hash, new session id and mandate hash). The event is not called
  `session_closed`, to keep it distinct from the `session_closed` error above. Closing the old
  session, opening the new one and both audit rows happen in one transaction.

**What lives where.** `tool_calls_used` and the cart revision are counters in the session row:
they have no other source. Committed spend and the number of approval requests are **derived**
inside the same transaction from the checkouts table (`sum(total_cents)` over `completed` and
`pending_approval`; `count(*)` over checkouts with `needed_approval = 1`). `needed_approval` is
set when the checkout is created and never changes, so a checkout that was later approved,
rejected or expired still counts as an approval request. A derived value cannot drift from the
rows it summarises, and the table is small.

### Processes and concurrency

| Process | Actor | Access |
|---|---|---|
| MCP server | `agent` | read and write, one transaction per tool call |
| Approval CLI | `approver` | read and write, one transaction per command; listing pending checkouts also takes the write lock, because it applies lazy expiry |
| Audit verify (CLI command and tests) | none | read-only |

All three use the same store module; the CLI does not import the MCP layer.

- Every connection sets `journal_mode=WAL` (readers do not block the single writer),
  `busy_timeout=5000` ms, `foreign_keys=ON` and `synchronous=FULL` (the audit trail is the asset;
  the cost is negligible at this volume).
- Python's implicit transactions are disabled (`isolation_level=None`). Every operation that reads
  state and then writes **opens with `BEGIN IMMEDIATE`**, which takes the database write lock
  before the first read. A second process blocks at `BEGIN` until the first commits, so it reads
  the committed spend *after* the first reservation. This covers every tool call (each one at
  least increments `tool_calls_used` and appends an audit row) and every approval.
- One tool call is one transaction: lazy expiry, count the call, decide, write state, append audit,
  commit. Either all of it happens or none of it.
- **Every tool call is counted and audited, whatever happens inside it:**
  - *Unexpected exception.* The work transaction is rolled back. A second, minimal transaction
    (`BEGIN IMMEDIATE`, lazy expiry, increment `tool_calls_used`, append one audit row) then
    records the call with event `internal_error` and a fixed reason. The exception text never
    reaches the audit trail or the agent, because it can contain arguments or product text.
  - *Malformed arguments.* The MCP SDK validates arguments against each tool's input schema
    before it calls the handler, so a call rejected there would never reach the store and would be
    neither counted nor audited. The tool input schemas are therefore permissive (an object that
    accepts any properties), or the low-level server API is used, so that raw arguments always
    reach the handler. The handler validates them (types, SKU format, quantity range, query
    length) and a malformed call is a counted, audited denial with a fixed `invalid_arguments`
    error. This is tested end to end over a real stdio connection with a wrong-typed argument, in
    the MCP-server step.
  - `internal_error`, `invalid_arguments`, `cart_empty` (`request_checkout` on an empty cart),
    `session_closed`, `store_busy` and `store_integrity` are store or protocol errors, not policy
    `rule_id`s: the policy function never returns them. `cart_empty` and `invalid_arguments`,
    including the state-dependent ones (unknown SKU, a line not in the cart), are counted once and
    audited like any other call.
- **There are exactly three exemptions, and all are cases where nothing can be written safely:**
  - `store_busy`: the lock was not obtained within the busy timeout. Nothing can be committed, so
    the call is neither counted nor audited. It is visible to the client as an error.
  - `store_integrity`: the session's mandate snapshot no longer parses or no longer matches its
    stored hash. The gateway cannot know which limits apply, so it fails closed: the call is
    rolled back, changes no state, is not counted and is not audited (writing to a store that
    failed its own check would mix trusted and untrusted rows). Every later call fails the same
    way, the approval CLI refuses to list or decide, and the server refuses to start, until the
    operator repairs the store.
  - *Double fault*: recording a failed call (the minimal `internal_error` or `invalid_arguments`
    transaction) itself raises something other than `store_busy` or `store_integrity`. Nothing
    can be recorded, so `call()` catches it, returns `internal_error`, and writes one fixed line to
    stderr (`commerce_gateway: double fault; a failed tool call could not be recorded`), never the
    exception text. Both transactions were rolled back, so no state changed and the call is not
    counted. `call()` never raises.
- Structural backstops in the schema, in case code is wrong: `UNIQUE(session_id, quote_hash)` on
  checkouts (idempotency), a partial unique index allowing at most one `pending_approval`
  checkout per session, a partial unique index allowing at most one open session per database
  (`closed_at_ms IS NULL`), and a fixed `needed_approval` column on checkouts (0 or 1, set at
  creation) that the approval-request count uses.
- Payment simulation is the state change to `completed` inside the same transaction that takes
  the stock and empties the cart. There is no separate payment function or record. A real payment
  provider would need an outbox and idempotent capture; that is out of scope (ADR 0003: payment is
  simulated).
- WAL needs a local file system (it relies on shared memory). A network share is unsupported.

### Time and lazy expiry

- Time is integer milliseconds since the Unix epoch, UTC, everywhere: storage, quotes and hashes.
  It comes only from the injected clock; the CLI uses the system clock, tests a fixed one.
- **Time never goes backwards within a session.** The session row stores `last_seen_ms`. Every
  transaction on the session uses the effective time `max(clock now, last_seen_ms)` and writes it
  back as the new `last_seen_ms`. A system clock moved backwards cannot revive an expired quote or
  price lock, and the MCP server and the approval CLI see one monotonic time even if their clock
  readings differ slightly.
- **Time is not guaranteed monotonic across sessions.** A new session starts with
  `last_seen_ms` equal to the clock reading. That is acceptable because nothing time-dependent
  crosses a session boundary: a session with an unexpired pending checkout cannot be superseded,
  carts and price locks belong to their session and do not carry over, and the audit `seq`, not
  `ts_ms`, orders the trail. A clock set forward only makes locks and quotes expire earlier, which
  fails safe.
- The server and CLI entry points have **no flag or environment variable to override the clock**.
  They always use the system clock; only tests construct another clock.
- **There is no background process.** A price lock or a quote is expired **when `now >=
  expires_at_ms`**. At `now == expires_at_ms` it is already expired.
- At the start of every transaction that touches a session (every tool call, every approval CLI
  command including listings), pending checkouts with `now >= expires_at_ms` are set to
  `expired`, their reservation is released, and one audit row (actor `system`) is appended per
  expiry. The approval CLI applies exactly the same function, so an approval at or after expiry
  finds the checkout already `expired` and fails with `quote_mismatch`.
- A listing is therefore not a pure read: it may expire checkouts, and so it takes the write lock
  like everything else.
- Stale price uses the same boundary: a cart line is stale when `now >= lock_expires_at_ms`.
  A quote's `expires_at_ms` is the minimum of its lines' lock expiries, so a quote can never be
  issued already expired.

### Catalogue and stock in the store

- The fixture catalogue file is versioned and loaded into a `products` table when the database is
  created. Its hash, `sha256(canonical(parsed document))`, is stored in a `meta` table. The hash
  is over the parsed content, not the file bytes, so a CRLF checkout on Windows or a reformatted
  file gives the same hash. Opening a database with a catalogue of different content is refused,
  so the store never mixes two catalogues.
- A product's price history is a list of `(price_version, unit_price_cents, effective_from_ms)`.
  The current price is the latest version with `effective_from_ms <= now`. That makes a price
  change happen deterministically as the injected clock advances, which is how `stale_price` is
  tested without editing data.
- Stock is a column in the store, decremented in the transaction that completes an order.
- Adding a SKU already in the cart increases its quantity and re-locks the whole line at the
  current price version. Removing decreases the quantity and deletes the line at zero. Any change
  increments the cart revision.

### Money

Integer cents in every column, every dataclass, the policy function and every hash input.
Floats are not converted or rounded anywhere: the canonical encoder below raises on a float, so a
float that reaches an audit row or a quote is a bug that fails loudly.

### Audit trail

**Table.** `audit(seq, ts_ms, session_id, actor, event, tool, allowed, rule_id, payload,
prev_hash, row_hash)`. `seq` is an integer primary key that starts at 1 and increases by one.
`ts_ms` is the effective time of the transaction that wrote the row (`max(clock now,
last_seen_ms)`), never the raw clock reading.
`actor` is one of `agent`, `approver`, `system`. `payload` is the canonical JSON of the event's
structured fields (SKUs, quantities, cents, quote hashes; never product text).

**Append-only, enforced by triggers:**

- `BEFORE UPDATE ON audit` and `BEFORE DELETE ON audit` raise `ABORT` with a fixed message.
- `BEFORE INSERT ON audit` raises unless `NEW.seq` is the last `seq + 1` (or 1 on an empty table)
  and `NEW.prev_hash` equals the last row's `row_hash` (or the genesis value). SQLite cannot
  compute SHA-256 without an extension, so `row_hash` itself is checked by `verify`, not by a
  trigger. Because appends happen inside `BEGIN IMMEDIATE`, two writers cannot fork the chain.

**Canonical serialization.** The row hash is
`row_hash = sha256(canonical(record)).hexdigest()` (64 lowercase hex characters), where `record` is
a JSON **array** with this exact field order:

```
[1, seq, ts_ms, session_id, actor, event, tool, allowed, rule_id, payload, prev_hash]
```

- The leading `1` is the hash format version.
- `canonical(x)` is `json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  allow_nan=False).encode("utf-8")`: keys of every nested object sorted by code point, no
  whitespace, UTF-8 bytes.
- Allowed values: `str`, `int`, `bool`, `None`, `list`, `tuple` (encoded exactly as a list), and
  `dict` with `str` keys. No other sequence or mapping type is accepted. An encoder
  check runs first and raises on anything else, including any `float`. Integers serialize as
  integers (`1250`, never `1250.0`). `tool` and `rule_id` are `null` when not applicable;
  `allowed` is `true`, `false` or `null` (for events that are not decisions). Policy decisions
  are tri-state (allowed, denied, needs approval); `allowed` is `false` only for a denial. A
  `request_checkout` that creates a `pending_approval` checkout is audited as `allowed: true`
  with `rule_id: "approval_required"`.
- Strings must encode to UTF-8 (a lone surrogate raises). Text is hashed as stored, with no
  Unicode normalization.
- The same `canonical` function computes `quote_hash` and the mandate content hash, so there is
  one serialization rule in the codebase.

**Genesis.** The first row's `prev_hash` is 64 `0` characters.

**Verify.** `verify(conn, anchor=None) -> VerifyResult` reads rows in `seq` order and checks
that `seq` is contiguous from 1, that each `prev_hash` equals the previous `row_hash` (genesis for
row 1), and that each `row_hash` recomputes. It returns `ok`, the number of rows, the head
`(seq, row_hash)`, and on failure the first bad `seq` and a fixed reason.

**Anchoring the head.** A hash chain alone cannot detect someone who rewrites every row and
recomputes all hashes, or who deletes the last rows (the shorter chain is still valid). So:

- `head(conn) -> (seq, row_hash)` and an `audit-head` CLI command export the latest hash. The
  approval CLI prints the head after every decision.
- `verify(conn, anchor=(seq, row_hash))` additionally requires that the chain contains row `seq`
  with exactly that hash. A rewrite of any row up to `seq` and a truncation below `seq` both fail.
- Where the anchor is kept is the operator's choice (a file outside the database, a commit, a
  message). The demo writes it to a separate file and verifies against it.

**Untrusted search text.** An accepted `search_products` call stores, in `payload`,
`query_sha256` (hash of the query as received), `query_text` (the sanitised query, which is within
the length limit) and `untrusted: true`.

**Rejected arguments never reach the audit trail as values.** An `invalid_arguments` row stores a
fixed `reason` plus a closed vocabulary describing the rejected value: `field` (one of `tool`,
`arguments`, `sku`, `quantity`, `query`), `expected` (a fixed token such as `sku_pattern` or
`text_within_limit`), `received` (its JSON type), `length` (for strings, arrays and objects) and
`sha256` (for strings only, over UTF-8 with surrogates passed through). A rejected query, even one
megabyte long, therefore leaves only its hash and length; no text, float, NaN or huge integer can
reach a payload.

**Sanitisation.** Every piece of untrusted text the gateway emits (the stored query text, product
text returned to the agent, product names shown to the approver) has the Unicode categories `Cc`
(control characters) and `Cf` (format characters, which include bidi overrides and zero-width
characters that can hide text from a human reader) removed before it is capped. No other audit field ever holds agent or
product text.

**Volume.** ADR 0003 audits every call, including calls rejected by `tool_call_budget`, so an
agent can keep growing the table after its budget is spent. That is accepted: the client is
bounded by its own step cap in practice and disk exhaustion is outside the threat model.

### Schema versioning

`PRAGMA user_version` holds the schema version (1). On open:

- `0` and no tables: create the schema, the triggers and the `meta` rows, set it to 1, in one
  transaction.
- `0` with tables present: refuse (not a gateway database, or a half-created one).
- `1`: open.
- Anything else: refuse with an error naming the found and supported versions.

There are no migrations in this phase. A future version adds an explicit, tested migration step.

## What the triggers do NOT protect against

The triggers stop the gateway's own code, and anyone using the store module, from editing or
deleting audit rows by mistake. They are not a security boundary against an operator with file
access (trusted and out of scope in ADR 0003), who can:

- `DROP TRIGGER`, edit rows and recreate the trigger, or edit the file bytes directly;
- delete the last rows, or replace the file with an older copy (a valid but shorter chain);
- rewrite the whole chain with recomputed hashes;
- delete the database.

`verify` detects an edited, reordered or removed row inside the chain. With an anchor recorded
outside the database it also detects truncation below the anchor and a whole-chain rewrite.
Without an anchor it detects neither. Nothing detects rows appended after the latest anchor
by someone who also computes the hashes correctly.

The state tables (cart, checkouts, products, sessions) are mutable by design and have no hash
chain. Editing them directly (for example, lowering a checkout total) is not detected by `verify`.
The audit trail records what was decided at the time, so a reconciliation between audit and
state is possible later; it is not part of this phase.

## Alternatives considered

- **New session at every server start** (the proposal above). Rejected for the reasons in
  *Session identity*: a restart or a crash resets every limit and orphans reservations.
- **Session id supplied by the agent or the MCP client.** Rejected: the agent could choose a new
  id to reset its limits.
- **An explicit `close-session` command in the approval CLI** instead of `session_label`. Viable;
  not chosen because it adds a second way to change the session's limits outside the mandate file.
  The mandate remains the single human-written source of limits.
- **Spend and approval counters stored in the session row.** Rejected for money: a counter can
  drift from the checkouts it summarises after a bug; a derived sum cannot. The tool-call counter
  is stored because it has no other source.
- **Deferred transactions with optimistic retry.** Rejected: SQLite cannot upgrade a read
  transaction to a write transaction once another writer has committed, so the code would need a
  retry loop on every decision. `BEGIN IMMEDIATE` is simpler and the throughput is tiny.
- **A server database (Postgres) or a single long-lived process owning the state.** Rejected:
  cost and setup for no benefit at this scale; a single owner would need an IPC channel for the
  CLI, which is a new surface the agent could target.
- **A background expiry job.** Rejected: another process, timing-dependent tests, and lazy expiry
  gives the same answer at every observation.
- **A Merkle tree or signed audit rows.** A signing key would sit on the same machine as the
  operator who is out of scope, so it adds nothing over an exported head hash here.
- **Pydantic or an ORM.** Not needed for a fixed schema; frozen dataclasses and SQL are enough
  (A1 justifies any exception).
- **Timestamps as ISO 8601 strings.** Rejected for hashes and comparisons: integers have one
  representation and compare without parsing or time-zone handling.

## Consequences

- The spend cap, tool-call budget and approval-request limit survive restarts and crashes. Only a
  human editing the mandate resets them.
- Two MCP servers on one database with the same mandate share one session, one cart and one
  budget. That is the correct reading of "per mandate", and the tests for concurrent connections
  rely on it.
- Every tool call serializes on the write lock. Fine for one agent; not a design for high
  throughput.
- Tests can prove the concurrency guarantee directly: two connections on one file, each trying to
  take the last approval slot or the last cents of the cap, and exactly one succeeds.
- The approval window is bounded by lazy expiry exactly as ADR 0003 states; nothing extends it.
- The audit trail is tamper-evident only to the degree the head is anchored outside the database.
  The README must say so rather than call it tamper-proof.
- New mandate field `session_label` (required). This is an addition to the mandate content listed
  in ADR 0003, not a change to any rule.
