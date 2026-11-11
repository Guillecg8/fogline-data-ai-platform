"""KPI queries over the gold tables, without free-form SQL.

A caller chooses a metric, a venue, a date range and a granularity. The SQL text is built
only from the constants in this file; every value that comes from the caller is passed as a
bound parameter. There is no way to send SQL through this module.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

import duckdb
import yaml

from ops_copilot.paths import PROJECT_DIR

SAMPLES_DIR = PROJECT_DIR / "samples"
CONTRACT_FILE = PROJECT_DIR.parents[1] / "contracts" / "gold_kpis.yaml"
MAX_RANGE_DAYS = 366

Granularity = Literal["day", "week", "month", "total"]


@dataclass(frozen=True)
class Metric:
    table: str
    expression: str
    unit: str
    definition: str


METRICS: dict[str, Metric] = {
    "occupancy": Metric(
        "hotel_daily",
        "sum(rooms_sold) / sum(rooms_available)",
        "ratio",
        "Rooms sold divided by rooms available.",
    ),
    "adr": Metric(
        "hotel_daily",
        "sum(room_revenue) / nullif(sum(rooms_sold), 0)",
        "USD",
        "Average daily rate: room revenue divided by rooms sold.",
    ),
    "revpar": Metric(
        "hotel_daily",
        "sum(room_revenue) / sum(rooms_available)",
        "USD",
        "Revenue per available room: room revenue divided by rooms available.",
    ),
    "covers": Metric(
        "restaurant_daily",
        "sum(covers)",
        "guests",
        "Number of guests served.",
    ),
    "average_check": Metric(
        "restaurant_daily",
        "sum(revenue) / nullif(sum(covers), 0)",
        "USD",
        "Restaurant revenue divided by guests served.",
    ),
}

VENUES: dict[str, set[str]] = {
    "hotel_daily": {"fogline-marina", "fogline-mission"},
    "restaurant_daily": {"saltgrass", "lumen"},
}

_PERIOD = {
    "day": "date",
    "week": "CAST(date_trunc('week', date) AS DATE)",
    "month": "CAST(date_trunc('month', date) AS DATE)",
}


class KpiError(ValueError):
    """The request asks for something this module does not allow."""


def load_contract() -> dict:
    return yaml.safe_load(CONTRACT_FILE.read_text(encoding="utf-8"))


def _connect() -> duckdb.DuckDBPyConnection:
    """In-memory database with one view per contract table, typed as the contract says."""
    connection = duckdb.connect(":memory:")
    for table, spec in load_contract()["tables"].items():
        path = (SAMPLES_DIR / f"{table}.csv").as_posix()
        relation = connection.read_csv(path, header=True, columns=spec["columns"])
        relation.create_view(table)
    return connection


def list_kpis() -> list[dict[str, object]]:
    return [
        {
            "metric": name,
            "unit": metric.unit,
            "definition": metric.definition,
            "venues": sorted(VENUES[metric.table]),
        }
        for name, metric in METRICS.items()
    ]


def query_kpi(
    metric: str,
    venue: str,
    start_date: date,
    end_date: date,
    granularity: Granularity = "month",
) -> dict[str, object]:
    if metric not in METRICS:
        raise KpiError(f"unknown metric {metric!r}; choose one of {sorted(METRICS)}")
    spec = METRICS[metric]
    if venue not in VENUES[spec.table]:
        raise KpiError(f"{metric} is not available for venue {venue!r}")
    if granularity != "total" and granularity not in _PERIOD:
        raise KpiError(f"unknown granularity {granularity!r}")
    if end_date < start_date:
        raise KpiError("end_date is earlier than start_date")
    if (end_date - start_date).days > MAX_RANGE_DAYS:
        raise KpiError(f"date range is longer than {MAX_RANGE_DAYS} days")

    where = "WHERE venue = ? AND date BETWEEN ? AND ?"
    if granularity == "total":
        sql = f"SELECT {spec.expression} AS value, count(*) AS days FROM {spec.table} {where}"
    else:
        sql = (
            f"SELECT {_PERIOD[granularity]} AS period, {spec.expression} AS value "
            f"FROM {spec.table} {where} GROUP BY 1 ORDER BY 1"
        )
    with _connect() as connection:
        records = connection.execute(sql, [venue, start_date, end_date]).fetchall()

    if granularity == "total":
        value, days = records[0]
        rows = [] if days == 0 else [{"period": "total", "value": round(float(value), 4)}]
    else:
        rows = [
            {"period": period.isoformat(), "value": round(float(value), 4)}
            for period, value in records
            if value is not None
        ]
    return {
        "metric": metric,
        "unit": spec.unit,
        "definition": spec.definition,
        "venue": venue,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "granularity": granularity,
        "rows": rows,
        "source": "synthetic sample following contracts/gold_kpis.yaml",
    }
