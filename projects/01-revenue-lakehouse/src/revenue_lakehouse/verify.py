"""Compare gold with the truth the generator wrote. Only possible because the data is synthetic.

This is the proof that the pipeline is correct: every day and every hotel must match.

    uv run poe lh-verify
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from revenue_lakehouse.delta_io import read_arrow
from revenue_lakehouse.paths import DATA_DIR

MISMATCHES = """
select
    coalesce(g.date, t.date) as date,
    coalesce(g.venue, t.venue) as venue,
    g.rooms_available as gold_available, t.rooms_available as truth_available,
    g.rooms_sold as gold_sold, t.rooms_sold as truth_sold,
    g.room_revenue as gold_revenue, t.room_revenue as truth_revenue
from gold g
full outer join truth t on g.date = t.date and g.venue = t.venue
where g.date is null or t.date is null
    or g.rooms_available != t.rooms_available
    or g.rooms_sold != t.rooms_sold
    or abs(g.room_revenue - t.room_revenue) > 0.005
order by 1, 2
"""


@dataclass
class VerifyReport:
    rows: int
    mismatches: list[dict]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def verify(data_dir: Path = DATA_DIR) -> VerifyReport:
    gold = read_arrow(data_dir / "gold" / "hotel_daily")
    truth_file = data_dir / "truth" / "hotel_daily.csv"
    if gold is None or not truth_file.exists():
        raise SystemExit("nothing to verify: run uv run poe lh-generate and the pipeline first")
    con = duckdb.connect()
    con.register("gold", gold)
    con.execute(
        "create temp table truth as select * from read_csv(?, header = true, columns = "
        "{'date': 'DATE', 'venue': 'VARCHAR', 'rooms_available': 'INTEGER', "
        "'rooms_sold': 'INTEGER', 'room_revenue': 'DOUBLE'})",
        [truth_file.as_posix()],
    )
    cursor = con.execute(MISMATCHES)
    names = [column[0] for column in cursor.description]
    mismatches = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    return VerifyReport(con.execute("select count(*) from truth").fetchall()[0][0], mismatches)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    report = verify(args.data_dir)
    if report.ok:
        print(f"gold matches the truth on all {report.rows} hotel-days")
        return
    print(f"gold differs from the truth on {len(report.mismatches)} of {report.rows} hotel-days")
    for row in report.mismatches[:5]:
        print(f"  {row['date']} {row['venue']}: {row}")
    sys.exit(1)


if __name__ == "__main__":
    main()
