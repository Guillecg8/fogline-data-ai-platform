"""Daily demand series read from Gold, one per venue, ready to forecast.

Hotels are forecast as occupancy (``rooms_sold / rooms_available``), never raw ``rooms_sold``:
Marina drops from 62 to 40 rooms on 2026-09-01 and that is a capacity change, not a demand drop.
Restaurants are forecast as covers. See ``docs/decisions/0002-demand-forecast-backtest.md``.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from revenue_lakehouse.delta_io import read_rows
from revenue_lakehouse.paths import DATA_DIR

HOTEL_SQL = (
    "select venue, date, cast(rooms_sold as double) / cast(rooms_available as double) as value "
    "from t order by venue, date"
)
RESTAURANT_SQL = "select venue, date, cast(covers as double) as value from t order by venue, date"
TABLES = (("hotel_daily", HOTEL_SQL, "occupancy"), ("restaurant_daily", RESTAURANT_SQL, "covers"))


@dataclass(frozen=True, eq=False)
class Series:
    venue: str
    target: str  # "occupancy" or "covers"
    start: date
    values: np.ndarray  # one value per consecutive day, values[0] is ``start``

    @property
    def name(self) -> str:
        return f"{self.venue}/{self.target}"

    @property
    def first_weekday(self) -> int:
        """Weekday of ``values[0]``, Monday = 0."""
        return self.start.weekday()

    def date_at(self, index: int) -> date:
        return self.start + timedelta(days=index)


def series_from_rows(rows: list[dict], target: str) -> list[Series]:
    """Group ``venue, date, value`` rows (sorted by venue, date) into gap-free daily series."""
    by_venue: dict[str, list[dict]] = {}
    for row in rows:
        by_venue.setdefault(row["venue"], []).append(row)
    series = []
    for venue, venue_rows in sorted(by_venue.items()):
        start = venue_rows[0]["date"]
        for offset, row in enumerate(venue_rows):
            expected = start + timedelta(days=offset)
            if row["date"] != expected:
                raise ValueError(f"{venue} {target}: expected {expected}, found {row['date']}")
        values = np.array([row["value"] for row in venue_rows], dtype=float)
        series.append(Series(venue, target, start, values))
    return series


def load_series(data_dir: Path = DATA_DIR) -> list[Series]:
    """Hotel occupancy and restaurant covers series, hotels first, venues sorted by name."""
    series = []
    for table, sql, target in TABLES:
        rows = read_rows(data_dir / "gold" / table, sql)
        if not rows:
            raise SystemExit(f"gold {table} is empty: run uv run poe lh-all first")
        series += series_from_rows(rows, target)
    return series
