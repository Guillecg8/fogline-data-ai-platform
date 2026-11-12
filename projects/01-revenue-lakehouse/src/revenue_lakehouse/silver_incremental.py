"""Incremental silver: process only the bronze batches that are new, and MERGE them in.

The full rebuild in ``silver.py`` reads all of bronze every time. This does the same work for the
new batches only:

1. parse the new bronze rows and drop duplicates within them;
2. MERGE the events into ``reservation_events`` (insert if the event id is new; the first
   delivery of an event wins, exactly as in the full rebuild);
3. take the reservations touched by those events, recompute their state from *all* their events
   with the same SQL the full rebuild uses, and MERGE the result (upsert) into ``reservations``;
4. remember which batches were processed.

A late event for an old reservation simply makes that reservation "touched" again, so the
result is identical to a full rebuild. The tests check that, and the benchmark measures the gain.

    uv run poe lh-silver
"""

import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

from revenue_lakehouse.bronze import bronze_path
from revenue_lakehouse.delta_io import exists, merge, read_where_in, write
from revenue_lakehouse.paths import DATA_DIR
from revenue_lakehouse.silver import (
    EVENTS,
    PARSED,
    POS_CHECKS,
    POS_PARSED,
    POS_REJECTED,
    POS_REVISIONS,
    REJECTED,
    RESERVATIONS,
    silver_path,
)
from revenue_lakehouse.silver_state import pending_files, record_processed
from revenue_lakehouse.sources import PMS, POS, Source


@dataclass
class IncrementalReport:
    source: str
    batches: int = 0
    rows: int = 0
    touched: int = 0
    rejected: int = 0
    seconds: float = 0.0


def new_bronze_rows(data_dir: Path, source: Source, files: list[tuple[str, str]]):
    return read_where_in(bronze_path(data_dir, source), "_source_file", [n for n, _ in files])


def connect(bronze) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")
    con.register("bronze", bronze)
    return con


def append_rejected(data_dir: Path, name: str, con: duckdb.DuckDBPyConnection, table: str) -> int:
    rejected = con.table(table).to_arrow_table()
    if rejected.num_rows:
        write(silver_path(data_dir, name), rejected, "append")
    return rejected.num_rows


def upsert(data_dir: Path, name: str, table, keys: list[str], update: bool) -> None:
    path = silver_path(data_dir, name)
    if exists(path):
        merge(path, table, keys, update)
    else:
        write(path, table, "overwrite")


def update_silver(data_dir: Path = DATA_DIR) -> IncrementalReport:
    started = time.perf_counter()
    report = IncrementalReport(PMS.name)
    files = pending_files(data_dir, PMS)
    if not files:
        return report
    bronze = new_bronze_rows(data_dir, PMS, files)
    con = connect(bronze)
    con.execute(PARSED)
    con.execute(f"create temp table new_events as {EVENTS}")
    con.execute(f"create temp table rejected_rows as {REJECTED}")

    first_load = not exists(silver_path(data_dir, "reservation_events"))
    new_events = con.table("new_events").to_arrow_table()
    upsert(data_dir, "reservation_events", new_events, ["event_id"], update=False)

    touched = [
        r[0] for r in con.execute("select distinct reservation_id from new_events").fetchall()
    ]
    if first_load:
        con.execute("create temp table reservation_events as select * from new_events")
    else:
        events = read_where_in(
            silver_path(data_dir, "reservation_events"), "reservation_id", touched
        )
        con.register("reservation_events", events)
    con.execute(f"create temp table reservations as {RESERVATIONS}")
    upsert(
        data_dir,
        "reservations",
        con.table("reservations").to_arrow_table(),
        ["reservation_id"],
        True,
    )

    report.rejected = append_rejected(data_dir, "rejected_rows", con, "rejected_rows")
    record_processed(data_dir, PMS, files)
    report.batches, report.rows, report.touched = len(files), bronze.num_rows, len(touched)
    report.seconds = time.perf_counter() - started
    return report


def update_silver_pos(data_dir: Path = DATA_DIR) -> IncrementalReport:
    started = time.perf_counter()
    report = IncrementalReport(POS.name)
    files = pending_files(data_dir, POS)
    if not files:
        return report
    bronze = new_bronze_rows(data_dir, POS, files)
    con = connect(bronze)
    con.execute(POS_PARSED)
    con.execute(f"create temp table new_revisions as {POS_REVISIONS}")
    con.execute(f"create temp table pos_rejected_rows as {POS_REJECTED}")

    first_load = not exists(silver_path(data_dir, "pos_check_revisions"))
    new_revisions = con.table("new_revisions").to_arrow_table()
    upsert(data_dir, "pos_check_revisions", new_revisions, ["check_id", "revision"], update=False)

    touched = [r[0] for r in con.execute("select distinct check_id from new_revisions").fetchall()]
    if first_load:
        con.execute("create temp table pos_check_revisions as select * from new_revisions")
    else:
        events = read_where_in(silver_path(data_dir, "pos_check_revisions"), "check_id", touched)
        con.register("pos_check_revisions", events)
    con.execute(f"create temp table pos_checks as {POS_CHECKS}")
    upsert(data_dir, "pos_checks", con.table("pos_checks").to_arrow_table(), ["check_id"], True)

    report.rejected = append_rejected(data_dir, "pos_rejected_rows", con, "pos_rejected_rows")
    record_processed(data_dir, POS, files)
    report.batches, report.rows, report.touched = len(files), bronze.num_rows, len(touched)
    report.seconds = time.perf_counter() - started
    return report
