"""Compare gold with the truth the generator wrote. Only possible because the data is synthetic.

This is the proof that the pipeline is correct: every day and every venue must match.

    uv run poe lh-verify
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from revenue_lakehouse.delta_io import read_arrow
from revenue_lakehouse.paths import DATA_DIR

# table -> (column types in the truth csv, the measures that must match)
TABLES = {
    "hotel_daily": (
        {"rooms_available": "INTEGER", "rooms_sold": "INTEGER", "room_revenue": "DOUBLE"},
        ["rooms_available", "rooms_sold", "room_revenue"],
    ),
    "restaurant_daily": ({"covers": "INTEGER", "revenue": "DOUBLE"}, ["covers", "revenue"]),
}


@dataclass
class VerifyReport:
    table: str
    rows: int
    mismatches: list[dict]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def mismatch_query(measures: list[str]) -> str:
    shown = ", ".join(f"g.{m} as gold_{m}, t.{m} as truth_{m}" for m in measures)
    differs = " or ".join(f"abs(g.{m} - t.{m}) > 0.005" for m in measures)
    return f"""
        select coalesce(g.date, t.date) as date, coalesce(g.venue, t.venue) as venue, {shown}
        from gold g
        full outer join truth t on g.date = t.date and g.venue = t.venue
        where g.date is null or t.date is null or {differs}
        order by 1, 2
    """


def verify(data_dir: Path = DATA_DIR, table: str = "hotel_daily") -> VerifyReport:
    types, measures = TABLES[table]
    gold = read_arrow(data_dir / "gold" / table)
    truth_file = data_dir / "truth" / f"{table}.csv"
    if gold is None or not truth_file.exists():
        raise SystemExit("nothing to verify: run uv run poe lh-generate and the pipeline first")
    con = duckdb.connect()
    con.register("gold", gold)
    columns = {"date": "DATE", "venue": "VARCHAR", **types}
    con.execute(
        "create temp table truth as select * from read_csv(?, header = true, columns = "
        + repr(columns)
        + ")",
        [truth_file.as_posix()],
    )
    cursor = con.execute(mismatch_query(measures))
    names = [column[0] for column in cursor.description]
    mismatches = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    return VerifyReport(
        table, con.execute("select count(*) from truth").fetchall()[0][0], mismatches
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    failed = False
    for table in TABLES:
        report = verify(args.data_dir, table)
        if report.ok:
            print(f"{table}: gold matches the truth on all {report.rows} venue-days")
            continue
        failed = True
        print(f"{table}: gold differs from the truth on {len(report.mismatches)} of {report.rows}")
        for row in report.mismatches[:5]:
            print(f"  {row}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
