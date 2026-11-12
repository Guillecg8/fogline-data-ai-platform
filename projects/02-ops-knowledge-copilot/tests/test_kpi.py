import csv
from datetime import date

import duckdb
import pytest

from ops_copilot import kpi


def read_rows(table: str) -> list[dict[str, str]]:
    with (kpi.SAMPLES_DIR / f"{table}.csv").open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_sample_follows_the_data_contract():
    contract = kpi.load_contract()
    connection = duckdb.connect(":memory:")
    for table, spec in contract["tables"].items():
        assert list(read_rows(table)[0]) == list(spec["columns"])
        path = (kpi.SAMPLES_DIR / f"{table}.csv").as_posix()
        connection.read_csv(path, header=True, columns=spec["columns"]).create_view(table)
        for check in spec["checks"]:
            sql = f"SELECT count(*) FROM {table} WHERE NOT ({check})"
            broken = connection.execute(sql).fetchall()[0][0]
            assert broken == 0, f"{table}: rows break the check {check!r}"


def test_occupancy_matches_a_manual_calculation():
    january = [
        row
        for row in read_rows("hotel_daily")
        if row["venue"] == "fogline-mission" and row["date"].startswith("2026-01")
    ]
    expected = sum(int(r["rooms_sold"]) for r in january) / sum(
        int(r["rooms_available"]) for r in january
    )
    result = kpi.query_kpi(
        "occupancy", "fogline-mission", date(2026, 1, 1), date(2026, 1, 31), "total"
    )
    assert result["rows"] == [{"period": "total", "value": round(expected, 4)}]


def test_monthly_granularity_returns_one_row_per_month():
    result = kpi.query_kpi("revpar", "fogline-marina", date(2026, 7, 1), date(2026, 9, 30))
    assert [row["period"] for row in result["rows"]] == ["2026-07-01", "2026-08-01", "2026-09-01"]
    assert result["unit"] == "USD"


def test_range_without_data_returns_no_rows():
    result = kpi.query_kpi("covers", "saltgrass", date(2030, 1, 1), date(2030, 1, 31), "total")
    assert result["rows"] == []


@pytest.mark.parametrize(
    ("metric", "venue", "start", "end", "message"),
    [
        ("revpar; DROP TABLE hotel_daily", "fogline-marina", "2026-01-01", "2026-01-31", "unknown"),
        ("revpar", "' OR 1=1 --", "2026-01-01", "2026-01-31", "not available"),
        ("revpar", "saltgrass", "2026-01-01", "2026-01-31", "not available"),
        ("revpar", "fogline-marina", "2026-02-01", "2026-01-01", "earlier"),
        ("revpar", "fogline-marina", "2020-01-01", "2026-01-01", "longer"),
    ],
)
def test_requests_outside_the_allowed_set_are_rejected(metric, venue, start, end, message):
    with pytest.raises(kpi.KpiError, match=message):
        kpi.query_kpi(metric, venue, date.fromisoformat(start), date.fromisoformat(end))


# ------------------------------------------------------------ data exported by the lakehouse

HOTEL_HEADER = "date,venue,rooms_available,rooms_sold,room_revenue"


def export(folder, header=HOTEL_HEADER):
    folder.mkdir(exist_ok=True)
    (folder / "hotel_daily.csv").write_text(
        f"{header}\n2026-10-01,fogline-marina,40,30,9000.0\n2026-10-02,fogline-marina,40,20,5000.0\n",
        encoding="utf-8",
    )


def test_a_table_exported_by_the_lakehouse_replaces_the_sample(tmp_path, monkeypatch):
    export(tmp_path)
    monkeypatch.setenv(kpi.KPI_DIR_VARIABLE, str(tmp_path))
    result = kpi.query_kpi(
        "occupancy", "fogline-marina", date(2026, 10, 1), date(2026, 10, 2), "total"
    )
    assert result["rows"] == [{"period": "total", "value": 0.625}]  # 50 of 80 room-nights
    assert result["source"].startswith(kpi.LAKEHOUSE_SOURCE)


def test_tables_the_lakehouse_does_not_export_yet_still_come_from_the_sample(tmp_path, monkeypatch):
    export(tmp_path)
    monkeypatch.setenv(kpi.KPI_DIR_VARIABLE, str(tmp_path))
    result = kpi.query_kpi("covers", "lumen", date(2026, 1, 1), date(2026, 1, 31), "total")
    assert result["source"].startswith(kpi.SAMPLE_SOURCE)
    assert result["rows"]


def test_an_export_that_breaks_the_contract_is_refused(tmp_path, monkeypatch):
    export(tmp_path, header="date,venue,rooms_available,rooms_sold,revenue")
    monkeypatch.setenv(kpi.KPI_DIR_VARIABLE, str(tmp_path))
    with pytest.raises(kpi.KpiError, match="does not follow the contract"):
        kpi.query_kpi("occupancy", "fogline-marina", date(2026, 10, 1), date(2026, 10, 2))


def test_without_the_variable_the_sample_is_used(monkeypatch):
    monkeypatch.delenv(kpi.KPI_DIR_VARIABLE, raising=False)
    result = kpi.query_kpi("adr", "fogline-mission", date(2026, 1, 1), date(2026, 1, 31), "total")
    assert result["source"].startswith(kpi.SAMPLE_SOURCE)
