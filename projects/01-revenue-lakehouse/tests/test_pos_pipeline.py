import csv
import json
import re
from datetime import UTC, date, datetime

import duckdb
import pytest

from revenue_lakehouse.bronze import ingest, ledger_path
from revenue_lakehouse.check_landing import read_batch
from revenue_lakehouse.contract import compare_batch, load_contract
from revenue_lakehouse.delta_io import read_arrow, read_rows
from revenue_lakehouse.generate_pos import generate_pos
from revenue_lakehouse.gold import build_gold_restaurants
from revenue_lakehouse.silver import build_silver_pos, silver_path
from revenue_lakehouse.sources import POS
from revenue_lakehouse.verify import verify

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def run_pos(out, date_from=None, date_to=None):
    ingest(out / "landing" / "pos", out, now=NOW, source=POS)
    silver = build_silver_pos(out)
    args = [] if date_from is None else [date_from, date_to]
    build_gold_restaurants(out, *args)
    return silver


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("pos")
    summary = generate_pos(out, seed=42)
    return out, summary, run_pos(out)


def test_gold_matches_the_truth_on_every_restaurant_day(built):
    out, _, _ = built
    report = verify(out, "restaurant_daily")
    assert report.rows == 730
    assert report.mismatches == []


def test_silver_counts_match_what_the_generator_injected(built):
    _, injected, silver = built
    assert silver.checks == injected["checks"]
    assert silver.duplicates_removed == injected["duplicates"]
    assert silver.voided == injected["voided"]
    assert silver.rejected == 0


def test_the_business_day_is_not_the_calendar_day_nor_the_utc_day(built):
    out, injected, _ = built
    table = read_arrow(silver_path(out, "pos_checks"))
    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")
    con.register("checks", table)

    def count(condition):
        return con.execute(f"select count(*) from checks where {condition}").fetchall()[0][0]

    local_day = "cast(closed_at at time zone 'America/Los_Angeles' as date)"
    assert count(f"{local_day} != business_date") == injected["after_midnight"] > 500
    # a naive pipeline that cut days at UTC midnight would misplace every evening check
    assert count("cast(closed_at as date) != business_date") > 10_000


def test_timestamps_follow_daylight_saving_time(built):
    out, _, _ = built
    offsets = set()
    for name in ("pos_checks_2025-11-05.jsonl", "pos_checks_2026-04-05.jsonl"):
        offsets |= {r["closed_at"][-6:] for r in read_batch(out / "landing" / "pos" / name)}
    assert offsets == {"-08:00", "-07:00"}


def test_the_additive_column_is_compatible_and_the_rest_is_ok(built):
    out, _, _ = built
    contract = load_contract(source=POS)
    statuses = {}
    for path in sorted((out / "landing" / "pos").glob("*.jsonl")):
        day = date.fromisoformat(re.search(r"\d{4}-\d{2}-\d{2}", path.name).group())
        statuses[day] = compare_batch(contract, read_batch(path)).status
    assert {s for d, s in statuses.items() if d < date(2026, 4, 1)} == {"ok"}
    assert {s for d, s in statuses.items() if d >= date(2026, 4, 1)} == {"compatible"}


def test_pos_has_its_own_ledger(built):
    out, _, _ = built
    entries = read_rows(ledger_path(out, POS))
    assert len(entries) == 365 and {e["action"] for e in entries} == {"loaded"}


def test_the_copilot_reads_the_restaurant_export(built, monkeypatch):
    kpi = pytest.importorskip("ops_copilot.kpi")
    out, _, _ = built
    monkeypatch.setenv(kpi.KPI_DIR_VARIABLE, str(out / "exports"))
    result = kpi.query_kpi("covers", "lumen", date(2025, 11, 1), date(2026, 10, 31), "total")
    with (out / "truth" / "restaurant_daily.csv").open(encoding="utf-8") as handle:
        expected = sum(int(r["covers"]) for r in csv.DictReader(handle) if r["venue"] == "lumen")
    assert result["source"].startswith(kpi.LAKEHOUSE_SOURCE)
    assert result["rows"] == [{"period": "total", "value": float(expected)}]


# ------------------------------------------------------------------ small, hand-made cases


def check(check_id, revision, closed_at, **changes):
    row = {
        "check_id": check_id,
        "revision": revision,
        "venue": "saltgrass",
        "opened_at": "2026-01-09T19:00:00-08:00",
        "closed_at": closed_at,
        "covers": 2,
        "net_amount": 100.0,
        "tip": 15.0,
        "status": "closed",
        "channel": "dine_in",
    }
    row.update(changes)
    return row


def land(folder, day, rows):
    folder.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row) + "\n" for row in rows)
    (folder / f"{POS.file_prefix}{day}.jsonl").write_text(text, encoding="utf-8")


def saltgrass(out, day):
    rows = read_rows(out / "gold" / "restaurant_daily", "select * from t order by date, venue")
    return next(r for r in rows if r["venue"] == "saltgrass" and r["date"] == day)


def test_the_day_ends_at_four_in_the_morning_local_time(tmp_path):
    land(
        tmp_path / "landing" / "pos",
        "2026-01-10",
        [
            check("A", 1, "2026-01-10T00:30:00-08:00"),  # after midnight: still the 9th
            check("B", 1, "2026-01-10T03:59:59-08:00"),  # last second of the 9th
            check("C", 1, "2026-01-10T04:00:00-08:00"),  # first second of the 10th
        ],
    )
    run_pos(tmp_path, date(2026, 1, 9), date(2026, 1, 10))
    assert saltgrass(tmp_path, date(2026, 1, 9))["covers"] == 4
    assert saltgrass(tmp_path, date(2026, 1, 10))["covers"] == 2


def test_the_highest_revision_wins_even_if_it_arrives_first(tmp_path):
    folder = tmp_path / "landing" / "pos"
    land(folder, "2026-01-09", [check("A", 1, "2026-01-09T20:00:00-08:00")])
    land(folder, "2026-01-10", [check("A", 2, "2026-01-09T20:00:00-08:00", net_amount=80.0)])
    land(folder, "2026-01-11", [check("A", 1, "2026-01-09T20:00:00-08:00")])  # old revision, late
    run_pos(tmp_path, date(2026, 1, 9), date(2026, 1, 9))
    assert saltgrass(tmp_path, date(2026, 1, 9))["revenue"] == 80.0


def test_a_voided_check_counts_for_nothing(tmp_path):
    folder = tmp_path / "landing" / "pos"
    land(folder, "2026-01-09", [check("A", 1, "2026-01-09T20:00:00-08:00")])
    land(folder, "2026-01-10", [check("A", 2, "2026-01-09T20:00:00-08:00", status="void")])
    run_pos(tmp_path, date(2026, 1, 9), date(2026, 1, 9))
    day = saltgrass(tmp_path, date(2026, 1, 9))
    assert (day["covers"], day["revenue"]) == (0, 0.0)


def test_a_day_without_checks_is_a_row_of_zeros_not_a_missing_row(tmp_path):
    land(tmp_path / "landing" / "pos", "2026-01-09", [check("A", 1, "2026-01-09T20:00:00-08:00")])
    run_pos(tmp_path, date(2026, 1, 8), date(2026, 1, 9))
    rows = read_rows(tmp_path / "gold" / "restaurant_daily")
    assert len(rows) == 4  # two days, two restaurants
    assert saltgrass(tmp_path, date(2026, 1, 8))["covers"] == 0
