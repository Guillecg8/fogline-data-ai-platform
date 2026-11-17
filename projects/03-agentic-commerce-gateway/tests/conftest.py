"""Shared fixtures: a fixed clock, mandates, stores on a temporary file, bound gateways.

Every store opened through `open_store` is closed at teardown, so temporary files can be removed
on Windows too.
"""

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from commerce_gateway.catalogue import Catalogue
from commerce_gateway.clock import FixedClock
from commerce_gateway.gateway import Gateway, ToolResult
from commerce_gateway.mandate import Mandate, mandate_from_content
from commerce_gateway.sessions import start_session
from commerce_gateway.store import Store

T0 = 1_795_564_800_000  # 2026-11-25T00:00:00Z, before the fixture price change on 2026-12-01

BASE_MANDATE = {
    "version": 1,
    "session_label": "test-1",
    "budget_cents": 20_000,
    "approval_threshold_cents": 5_000,
    "allowed_categories": [
        "dry_goods",
        "beverages",
        "dairy",
        "produce",
        "cleaning",
        "kitchen_supplies",
    ],
    "price_lock_ttl_seconds": 1800,
    "max_approval_requests": 2,
    "tool_call_budget": 100,
}


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def make_mandate() -> Callable[..., Mandate]:
    def make(**overrides: object) -> Mandate:
        return mandate_from_content({**BASE_MANDATE, **overrides})

    return make


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "gateway.db"


@pytest.fixture
def open_store(db_path: Path) -> Iterator[Callable[..., Store]]:
    stores: list[Store] = []

    def open_(
        busy_timeout_ms: int = 5000, catalogue: Catalogue | None = None, path: Path | None = None
    ) -> Store:
        store = Store.open(path or db_path, catalogue=catalogue, busy_timeout_ms=busy_timeout_ms)
        stores.append(store)
        return store

    yield open_
    for store in stores:
        store.close()


@pytest.fixture
def session_ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"{next(counter):032x}"


@dataclass
class Env:
    store: Store
    gateway: Gateway
    session_id: str
    clock: FixedClock
    mandate: Mandate

    def call(self, tool: str, **arguments: object) -> ToolResult:
        return self.gateway.call(tool, arguments)

    def scalar(self, sql: str, params: tuple = ()) -> object:
        return self.store.conn.execute(sql, params).fetchall()[0][0]

    def calls_used(self) -> int:
        return self.scalar(
            "SELECT tool_calls_used FROM sessions WHERE session_id = ?", (self.session_id,)
        )

    def audit_count(self) -> int:
        return self.scalar("SELECT count(*) FROM audit")


@pytest.fixture
def make_env(
    open_store: Callable[..., Store],
    clock: FixedClock,
    make_mandate: Callable[..., Mandate],
    session_ids: Callable[[], str],
) -> Callable[..., Env]:
    def make(
        catalogue: Catalogue | None = None,
        path: Path | None = None,
        new_session_id: Callable[[], str] | None = None,
        **overrides: object,
    ) -> Env:
        store = open_store(catalogue=catalogue, path=path)
        mandate = make_mandate(**overrides)
        start = start_session(store, mandate, clock, new_session_id=new_session_id or session_ids)
        return Env(store, Gateway(store, start.session_id, clock), start.session_id, clock, mandate)

    return make
