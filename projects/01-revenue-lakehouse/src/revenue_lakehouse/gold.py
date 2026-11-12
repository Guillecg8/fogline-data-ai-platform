"""Gold layer: the KPI tables other projects consume, checked against their contract first.

``hotel_daily`` has one row per hotel per day. The table is built from the current reservations
and the hotel capacity calendar, then checked against ``contracts/gold_kpis.yaml`` (column names,
types and every row-level check). If the check fails nothing is published.

    uv run poe lh-gold
"""

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import yaml

from revenue_lakehouse.delta_io import read_arrow, write
from revenue_lakehouse.paths import DATA_DIR, GOLD_CONTRACT, REFERENCE_DIR
from revenue_lakehouse.silver import silver_path

# The year the synthetic source covers. Days outside it have only partial reservations.
REPORT_FROM, REPORT_TO = date(2025, 11, 1), date(2026, 10, 31)

HOTEL_DAILY = """
select
    cast(d.date as date) as date,
    c.venue as venue,
    cast(c.rooms as integer) as rooms_available,
    cast(count(r.reservation_id) as integer) as rooms_sold,
    cast(round(coalesce(sum(r.rate), 0), 2) as double) as room_revenue
from (
    select cast(day as date) as date
    from generate_series(date '{start}', date '{end}', interval 1 day) as t(day)
) d
join capacity c on d.date between c.valid_from and coalesce(c.valid_to, date '9999-12-31')
left join reservations r
    on r.venue = c.venue and r.status = 'active' and d.date >= r.check_in and d.date < r.check_out
group by d.date, c.venue, c.rooms
order by d.date, c.venue
"""


class ContractViolation(RuntimeError):
    """The table does not match what its consumers were promised."""


@dataclass
class GoldReport:
    rows: int
    date_from: date
    date_to: date
    exported: Path


def load_gold_contract(path: Path = GOLD_CONTRACT) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["tables"]


def contract_violations(con: duckdb.DuckDBPyConnection, table: str, spec: dict) -> list[str]:
    """Differences between a table and its contract. Empty list means it conforms."""
    found = {row[0]: row[1] for row in con.execute(f"describe {table}").fetchall()}
    wanted = spec["columns"]
    problems = []
    if list(found) != list(wanted):
        problems.append(f"columns are {list(found)}, the contract says {list(wanted)}")
    for name, kind in wanted.items():
        if name in found and found[name] != kind:
            problems.append(f"column {name} is {found[name]}, the contract says {kind}")
    for check in spec.get("checks", []):
        bad = con.execute(f"select count(*) from {table} where ({check}) is not true").fetchall()[
            0
        ][0]
        if bad:
            problems.append(f"{bad} rows break the check: {check}")
    return problems


def build_gold(
    data_dir: Path = DATA_DIR, date_from: date = REPORT_FROM, date_to: date = REPORT_TO
) -> GoldReport:
    reservations = read_arrow(silver_path(data_dir, "reservations"))
    if reservations is None:
        raise SystemExit("silver is empty: run uv run poe lh-silver first")

    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")
    con.register("reservations", reservations)
    capacity_file = (REFERENCE_DIR / "hotel_capacity.csv").as_posix()
    con.execute(
        "create temp table capacity as select * from read_csv(?, header = true, columns = "
        "{'venue': 'VARCHAR', 'valid_from': 'DATE', 'valid_to': 'DATE', 'rooms': 'INTEGER'})",
        [capacity_file],
    )
    query = HOTEL_DAILY.format(start=date_from.isoformat(), end=date_to.isoformat())
    con.execute(f"create temp table hotel_daily as {query}")

    problems = contract_violations(con, "hotel_daily", load_gold_contract()["hotel_daily"])
    if problems:
        raise ContractViolation("hotel_daily not published: " + "; ".join(problems))

    write(data_dir / "gold" / "hotel_daily", con.table("hotel_daily").to_arrow_table(), "overwrite")
    exported = data_dir / "exports" / "hotel_daily.csv"
    exported.parent.mkdir(parents=True, exist_ok=True)
    con.execute("copy hotel_daily to ? (header, delimiter ',')", [exported.as_posix()])
    rows = con.execute("select count(*) from hotel_daily").fetchall()[0][0]
    return GoldReport(rows, date_from, date_to, exported)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, default=REPORT_FROM)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, default=REPORT_TO)
    args = parser.parse_args()
    report = build_gold(args.data_dir, args.date_from, args.date_to)
    print(
        f"hotel_daily: {report.rows} rows from {report.date_from} to {report.date_to}, "
        f"contract checked, exported to {report.exported}"
    )


if __name__ == "__main__":
    main()
