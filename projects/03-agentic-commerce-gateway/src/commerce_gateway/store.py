"""SQLite store: connection settings, schema version check, transactions, product reads.

Every read-then-write decision runs inside `Store.transaction()`, which opens with
`BEGIN IMMEDIATE` so the write lock is held before the first read (ADR 0004).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from commerce_gateway.catalogue import Catalogue, load_catalogue
from commerce_gateway.models import ProductFacts
from commerce_gateway.schema import SCHEMA_VERSION, STATEMENTS

MIN_SQLITE_VERSION = (3, 37, 0)
DEFAULT_BUSY_TIMEOUT_MS = 5000


class StoreError(Exception):
    """The database cannot be opened or is inconsistent."""


class StoreIntegrityError(StoreError):
    """Stored state fails an integrity check (a mandate snapshot that no longer matches its hash).

    The gateway fails closed: the call changes nothing and is not counted, every later call fails
    the same way, and the server refuses to start (ADR 0004).
    """


class StoreBusy(Exception):
    """The write lock was not obtained within the busy timeout."""


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    return conn.execute(sql, params).fetchall()[0][0]


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(
        cls,
        path: Path,
        catalogue: Catalogue | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> "Store":
        """Open (or create) the store at `path`. Refuses unknown schema versions and catalogues."""
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise StoreError(f"SQLite {sqlite3.sqlite_version} is too old; STRICT needs 3.37")
        catalogue = catalogue or load_catalogue()
        conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchall()
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            store = cls(conn)
            store._prepare(catalogue)
        except BaseException:
            conn.close()
            raise
        return store

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` ... `COMMIT`, rolled back on any exception."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorname", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
                raise StoreBusy("store busy") from exc
            raise
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _prepare(self, catalogue: Catalogue) -> None:
        with self.transaction() as conn:
            version = _scalar(conn, "PRAGMA user_version")
            tables = _scalar(conn, "SELECT count(*) FROM sqlite_master WHERE type = 'table'")
            if version == 0 and tables == 0:
                for statement in STATEMENTS:
                    conn.execute(statement)
                _seed(conn, catalogue)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            if version == 0:
                raise StoreError("not a commerce gateway database (tables but no schema version)")
            if version != SCHEMA_VERSION:
                raise StoreError(
                    f"schema version {version} is not supported (supported: {SCHEMA_VERSION})"
                )
            stored = _scalar(conn, "SELECT value FROM meta WHERE key = 'catalogue_sha256'")
            if stored != catalogue.sha256:
                raise StoreError("the database was created from a different catalogue file")


def _seed(conn: sqlite3.Connection, catalogue: Catalogue) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('catalogue_sha256', ?), ('catalogue_version', ?)",
        (catalogue.sha256, str(catalogue.version)),
    )
    for product in catalogue.products:
        conn.execute(
            "INSERT INTO products (sku, policy_category, stock, name, description, ingredients)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                product.sku,
                product.policy_category,
                product.stock,
                product.name,
                product.description,
                product.ingredients,
            ),
        )
        for price in product.prices:
            conn.execute(
                "INSERT INTO product_prices"
                " (sku, price_version, unit_price_cents, effective_from_ms) VALUES (?, ?, ?, ?)",
                (product.sku, price.price_version, price.unit_price_cents, price.effective_from_ms),
            )


_FACTS_SQL = """
SELECT p.sku, p.policy_category, pp.unit_price_cents, pp.price_version, p.stock
FROM products AS p
JOIN product_prices AS pp ON pp.sku = p.sku
WHERE p.sku = ?
  AND pp.price_version = (
      SELECT max(price_version) FROM product_prices
      WHERE sku = p.sku AND effective_from_ms <= ?
  )
"""


def product_facts(
    conn: sqlite3.Connection, skus: tuple[str, ...], now_ms: int
) -> tuple[ProductFacts, ...]:
    """Facts at `now_ms` for the SKUs that exist. This query never selects a text column."""
    facts = []
    for sku in skus:
        rows = conn.execute(_FACTS_SQL, (sku, now_ms)).fetchall()
        if rows:
            facts.append(ProductFacts(*rows[0]))
    return tuple(facts)


def product_texts(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """sku -> (name, description, ingredients). Untrusted text, for display only."""
    rows = conn.execute("SELECT sku, name, description, ingredients FROM products ORDER BY sku")
    return {sku: (name, description, ingredients) for sku, name, description, ingredients in rows}
