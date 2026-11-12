"""Silver layer: typed, deduplicated events and the current state of every reservation.

Built from bronze by a full, deterministic rebuild, so running it twice gives the same tables.
(Rebuilding is the simple choice at this size; an incremental MERGE is a later optimisation,
measured against this version.)

- ``silver/reservation_events``: one row per event, parsed from the raw line and typed. The
  nightly rate is a decimal and comes from either ``nightly_rate`` or ``rate_amount``. Events
  delivered twice are kept once.
- ``silver/reservations``: one row per reservation, taken from its latest event *by event time*,
  so a late event never overwrites something newer.
- ``silver/rejected_rows``: bronze lines flagged as invalid, with the reason. They never reach
  the tables above.

The restaurant POS source has its own silver tables (``pos_check_revisions``, ``pos_checks``,
``pos_rejected_rows``), built the same way: the latest *revision* of each check wins, and its
business day is computed in the restaurant's own time zone, with the day ending at 04:00.

    uv run poe lh-silver
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import duckdb

from revenue_lakehouse.bronze import bronze_path
from revenue_lakehouse.delta_io import read_arrow, write
from revenue_lakehouse.paths import DATA_DIR
from revenue_lakehouse.sources import POS

PARSED = """
create temp table parsed as
select
    json_extract_string(_payload, '$.event_id') as event_id,
    json_extract_string(_payload, '$.reservation_id') as reservation_id,
    json_extract_string(_payload, '$.event_type') as event_type,
    cast(json_extract_string(_payload, '$.event_ts') as timestamptz) as event_ts,
    json_extract_string(_payload, '$.venue') as venue,
    json_extract_string(_payload, '$.room_type') as room_type,
    cast(json_extract_string(_payload, '$.check_in') as date) as check_in,
    cast(json_extract_string(_payload, '$.check_out') as date) as check_out,
    cast(json_extract_string(_payload, '$.guests') as integer) as guests,
    cast(coalesce(
        cast(json_extract(_payload, '$.nightly_rate') as varchar),
        json_extract_string(_payload, '$.rate_amount')
    ) as decimal(10, 2)) as rate,
    json_extract_string(_payload, '$.channel') as channel,
    json_extract_string(_payload, '$.loyalty_tier') as loyalty_tier,
    _source_file,
    _line_no,
    _ingested_at
from bronze
where _row_errors is null
"""

EVENTS = """
select * from parsed
qualify row_number() over (
    partition by event_id order by _ingested_at, _source_file, _line_no
) = 1
order by event_ts, event_id
"""

RESERVATIONS = """
select
    reservation_id, venue, room_type, check_in, check_out, guests, rate, channel,
    max(loyalty_tier) over (partition by reservation_id) as loyalty_tier,
    case when event_type = 'cancelled' then 'cancelled' else 'active' end as status,
    min(event_ts) over (partition by reservation_id) as created_ts,
    event_ts as last_event_ts,
    count(*) over (partition by reservation_id) as event_count
from reservation_events
qualify row_number() over (
    partition by reservation_id order by event_ts desc, event_id desc
) = 1
order by reservation_id
"""

REJECTED = """
select _source_file, _line_no, _ingested_at, _row_errors, _payload
from bronze where _row_errors is not null
order by _source_file, _line_no
"""


POS_PARSED = """
create temp table pos_parsed as
select
    json_extract_string(_payload, '$.check_id') as check_id,
    cast(json_extract_string(_payload, '$.revision') as integer) as revision,
    json_extract_string(_payload, '$.venue') as venue,
    cast(json_extract_string(_payload, '$.opened_at') as timestamptz) as opened_at,
    cast(json_extract_string(_payload, '$.closed_at') as timestamptz) as closed_at,
    cast(json_extract_string(_payload, '$.covers') as integer) as covers,
    cast(cast(json_extract(_payload, '$.net_amount') as varchar) as decimal(10, 2)) as net_amount,
    cast(cast(json_extract(_payload, '$.tip') as varchar) as decimal(10, 2)) as tip,
    json_extract_string(_payload, '$.status') as status,
    json_extract_string(_payload, '$.channel') as channel,
    json_extract_string(_payload, '$.server_id') as server_id,
    _source_file,
    _line_no,
    _ingested_at
from bronze
where _row_errors is null
"""

POS_REVISIONS = """
select * from pos_parsed
qualify row_number() over (
    partition by check_id, revision order by _ingested_at, _source_file, _line_no
) = 1
order by closed_at, check_id, revision
"""

# The business day is the local date of the closing time minus four hours. Taking the UTC date
# instead would put every dinner after 16:00 in the wrong day.
POS_CHECKS = """
select
    check_id, venue, opened_at, closed_at,
    cast((closed_at at time zone 'America/Los_Angeles') - interval 4 hour as date) as business_date,
    covers, net_amount, tip, status, channel, server_id, revision,
    count(*) over (partition by check_id) as revision_count
from pos_check_revisions
qualify row_number() over (partition by check_id order by revision desc) = 1
order by check_id
"""

POS_REJECTED = """
select _source_file, _line_no, _ingested_at, _row_errors, _payload
from bronze where _row_errors is not null
order by _source_file, _line_no
"""


@dataclass
class PosSilverReport:
    parsed: int
    revisions: int
    duplicates_removed: int
    checks: int
    voided: int
    rejected: int


@dataclass
class SilverReport:
    parsed: int
    events: int
    duplicates_removed: int
    reservations: int
    cancelled: int
    rejected: int


def silver_path(data_dir: Path, name: str) -> Path:
    return data_dir / "silver" / name


def build_silver(data_dir: Path = DATA_DIR) -> SilverReport:
    bronze = read_arrow(bronze_path(data_dir))
    if bronze is None:
        raise SystemExit("bronze is empty: run uv run poe lh-ingest first")

    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")  # same results on every machine
    con.register("bronze", bronze)
    con.execute(PARSED)
    con.execute(f"create temp table reservation_events as {EVENTS}")
    con.execute(f"create temp table reservations as {RESERVATIONS}")
    con.execute(f"create temp table rejected_rows as {REJECTED}")

    for name in ("reservation_events", "reservations", "rejected_rows"):
        write(silver_path(data_dir, name), con.table(name).to_arrow_table(), mode="overwrite")

    def count(sql: str) -> int:
        return con.execute(sql).fetchall()[0][0]

    return SilverReport(
        parsed=count("select count(*) from parsed"),
        events=count("select count(*) from reservation_events"),
        duplicates_removed=count("select count(*) from parsed")
        - count("select count(*) from reservation_events"),
        reservations=count("select count(*) from reservations"),
        cancelled=count("select count(*) from reservations where status = 'cancelled'"),
        rejected=count("select count(*) from rejected_rows"),
    )


def build_silver_pos(data_dir: Path = DATA_DIR) -> PosSilverReport:
    bronze = read_arrow(bronze_path(data_dir, POS))
    if bronze is None:
        raise SystemExit("POS bronze is empty: run uv run poe lh-ingest first")

    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")
    con.register("bronze", bronze)
    con.execute(POS_PARSED)
    con.execute(f"create temp table pos_check_revisions as {POS_REVISIONS}")
    con.execute(f"create temp table pos_checks as {POS_CHECKS}")
    con.execute(f"create temp table pos_rejected_rows as {POS_REJECTED}")

    for name in ("pos_check_revisions", "pos_checks", "pos_rejected_rows"):
        write(silver_path(data_dir, name), con.table(name).to_arrow_table(), mode="overwrite")

    def count(sql: str) -> int:
        return con.execute(sql).fetchall()[0][0]

    parsed = count("select count(*) from pos_parsed")
    revisions = count("select count(*) from pos_check_revisions")
    return PosSilverReport(
        parsed=parsed,
        revisions=revisions,
        duplicates_removed=parsed - revisions,
        checks=count("select count(*) from pos_checks"),
        voided=count("select count(*) from pos_checks where status = 'void'"),
        rejected=count("select count(*) from pos_rejected_rows"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    report = build_silver(args.data_dir)
    print(
        f"{report.events} events ({report.duplicates_removed} duplicates removed), "
        f"{report.reservations} reservations ({report.cancelled} cancelled), "
        f"{report.rejected} rejected rows"
    )
    pos = build_silver_pos(args.data_dir)
    print(
        f"{pos.revisions} POS check revisions ({pos.duplicates_removed} duplicates removed), "
        f"{pos.checks} checks ({pos.voided} voided), {pos.rejected} rejected rows"
    )


if __name__ == "__main__":
    main()
