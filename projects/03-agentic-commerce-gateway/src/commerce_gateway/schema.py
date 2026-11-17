"""SQLite schema, version 1 (ADR 0004). Every table is STRICT."""

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64

STATEMENTS = (
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE products (
        sku TEXT PRIMARY KEY,
        policy_category TEXT NOT NULL,
        stock INTEGER NOT NULL CHECK (stock >= 0),
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        ingredients TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE product_prices (
        sku TEXT NOT NULL REFERENCES products (sku),
        price_version INTEGER NOT NULL CHECK (price_version >= 1),
        unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
        effective_from_ms INTEGER NOT NULL CHECK (effective_from_ms >= 0),
        PRIMARY KEY (sku, price_version)
    ) STRICT
    """,
    """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        mandate_hash TEXT NOT NULL CHECK (length(mandate_hash) = 64),
        mandate TEXT NOT NULL,
        opened_at_ms INTEGER NOT NULL,
        closed_at_ms INTEGER,
        last_seen_ms INTEGER NOT NULL,
        cart_revision INTEGER NOT NULL DEFAULT 0 CHECK (cart_revision >= 0),
        tool_calls_used INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls_used >= 0)
    ) STRICT
    """,
    "CREATE UNIQUE INDEX one_open_session ON sessions ((1)) WHERE closed_at_ms IS NULL",
    """
    CREATE TABLE cart_lines (
        session_id TEXT NOT NULL REFERENCES sessions (session_id),
        sku TEXT NOT NULL REFERENCES products (sku),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
        price_version INTEGER NOT NULL CHECK (price_version >= 1),
        lock_expires_at_ms INTEGER NOT NULL,
        PRIMARY KEY (session_id, sku)
    ) STRICT
    """,
    """
    CREATE TABLE checkouts (
        checkout_id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions (session_id),
        cart_revision INTEGER NOT NULL,
        quote_hash TEXT NOT NULL CHECK (length(quote_hash) = 64),
        quote TEXT NOT NULL,
        total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
        expires_at_ms INTEGER NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('completed', 'pending_approval', 'rejected', 'expired')),
        needed_approval INTEGER NOT NULL CHECK (needed_approval IN (0, 1)),
        created_at_ms INTEGER NOT NULL,
        decided_at_ms INTEGER,
        decided_by TEXT CHECK (decided_by IN ('gateway', 'approver', 'system')),
        decision_rule_id TEXT,
        UNIQUE (session_id, quote_hash)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX one_pending_checkout ON checkouts (session_id)
    WHERE status = 'pending_approval'
    """,
    """
    CREATE TRIGGER checkouts_needed_approval_fixed
    BEFORE UPDATE OF needed_approval ON checkouts
    WHEN NEW.needed_approval IS NOT OLD.needed_approval
    BEGIN SELECT RAISE(ABORT, 'needed_approval is fixed at creation'); END
    """,
    """
    CREATE TABLE audit (
        seq INTEGER PRIMARY KEY CHECK (seq >= 1),
        ts_ms INTEGER NOT NULL,
        session_id TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('agent', 'approver', 'system')),
        event TEXT NOT NULL,
        tool TEXT,
        allowed INTEGER CHECK (allowed IN (0, 1)),
        rule_id TEXT,
        payload TEXT NOT NULL,
        prev_hash TEXT NOT NULL CHECK (length(prev_hash) = 64),
        row_hash TEXT NOT NULL CHECK (length(row_hash) = 64)
    ) STRICT
    """,
    """
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END
    """,
    """
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END
    """,
    f"""
    CREATE TRIGGER audit_chain_on_insert BEFORE INSERT ON audit
    WHEN NEW.seq IS NOT coalesce((SELECT max(seq) FROM audit), 0) + 1
      OR NEW.prev_hash IS NOT coalesce(
            (SELECT row_hash FROM audit ORDER BY seq DESC LIMIT 1), '{GENESIS_HASH}')
    BEGIN SELECT RAISE(ABORT, 'audit chain violation'); END
    """,
)
