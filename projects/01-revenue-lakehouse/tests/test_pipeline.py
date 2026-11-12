import json
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal

import duckdb
import pytest

from revenue_lakehouse.bronze import ingest
from revenue_lakehouse.delta_io import read_rows
from revenue_lakehouse.generate import FILE_PREFIX, generate
from revenue_lakehouse.gold import build_gold, contract_violations, load_gold_contract
from revenue_lakehouse.silver import build_silver, silver_path
from revenue_lakehouse.verify import verify

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def run_pipeline(out, contract_version=None):
    kwargs = {} if contract_version is None else {"contract_version": contract_version}
    ingest(out / "landing" / "pms", out, now=NOW, **kwargs)
    silver = build_silver(out)
    build_gold(out)
    return silver


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    out = tmp_path_factory.mktemp("source")
    generate(out, seed=42)
    return out


@pytest.fixture(scope="module")
def built(source, tmp_path_factory):
    out = tmp_path_factory.mktemp("built")
    shutil.copytree(source, out, dirs_exist_ok=True)
    return out, run_pipeline(out)


def test_gold_matches_the_truth_on_every_hotel_day(built):
    out, _ = built
    report = verify(out)
    assert report.rows == 730
    assert report.mismatches == []


def test_silver_counts_match_what_the_generator_injected(built):
    out, silver = built
    injected = json.loads((out / "truth" / "defects.json").read_text())
    assert silver.events == injected["events"]
    assert silver.duplicates_removed == injected["duplicates"]
    assert silver.reservations == injected["reservations"]
    assert silver.rejected == 0


def test_silver_is_the_same_every_time_it_is_built(built):
    out, _ = built
    before = read_rows(silver_path(out, "reservations"), "select * from t order by reservation_id")
    build_silver(out)
    after = read_rows(silver_path(out, "reservations"), "select * from t order by reservation_id")
    assert before == after


def test_the_gold_export_has_exactly_the_contract_columns(built):
    out, _ = built
    header = (out / "exports" / "hotel_daily.csv").read_text().splitlines()[0]
    assert header.split(",") == list(load_gold_contract()["hotel_daily"]["columns"])


def test_quarantined_batches_leave_a_visible_hole_in_gold(source, tmp_path):
    """With contract 1 the 139 batches after the vendor release never reach silver."""
    shutil.copytree(source, tmp_path, dirs_exist_ok=True)
    run_pipeline(tmp_path, contract_version=1)
    report = verify(tmp_path)
    assert not report.ok
    first = min(row["date"] for row in report.mismatches)
    assert date(2026, 6, 8) <= first <= date(2026, 6, 20)
    assert len(report.mismatches) > 100


def test_the_pipeline_runs_where_pyarrow_parquet_and_dataset_are_blocked(source, tmp_path):
    shutil.copytree(source, tmp_path, dirs_exist_ok=True)
    script = (
        "import sys\n"
        "for name in ('_parquet', 'parquet', '_dataset', 'dataset'):\n"
        "    sys.modules['pyarrow.' + name] = None\n"
        "from pathlib import Path\n"
        "from revenue_lakehouse.bronze import ingest\n"
        "from revenue_lakehouse.silver import build_silver\n"
        "from revenue_lakehouse.gold import build_gold\n"
        "from revenue_lakehouse.verify import verify\n"
        "out = Path(sys.argv[1])\n"
        "ingest(out / 'landing' / 'pms', out)\n"
        "build_silver(out)\n"
        "build_gold(out)\n"
        "print(len(verify(out).mismatches))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "0"


# ------------------------------------------------------------------ small, hand-made cases


def event(event_id, reservation, kind, ts, **changes):
    row = {
        "event_id": event_id,
        "reservation_id": reservation,
        "event_type": kind,
        "event_ts": ts,
        "venue": "fogline-marina",
        "room_type": "standard",
        "check_in": "2026-01-20",
        "check_out": "2026-01-22",
        "guests": 2,
        "nightly_rate": 100.0,
        "channel": "direct",
    }
    row.update(changes)
    return row


def land(folder, day, rows):
    folder.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row) + "\n" for row in rows)
    (folder / f"{FILE_PREFIX}{day}.jsonl").write_text(text, encoding="utf-8")


def reservations(data):
    return {r["reservation_id"]: r for r in read_rows(silver_path(data, "reservations"))}


def test_a_late_event_does_not_overwrite_a_newer_state(tmp_path):
    landing, data = tmp_path / "landing", tmp_path / "data"
    created = event("E1", "R1", "created", "2026-01-05T10:00:00Z")
    cancelled = event("E2", "R1", "cancelled", "2026-01-08T09:00:00Z")
    late_change = event("E3", "R1", "modified", "2026-01-06T12:00:00Z", nightly_rate=90.0)
    land(landing, "2026-01-10", [created, cancelled])
    land(landing, "2026-01-11", [late_change, created])  # E3 arrives late, E1 arrives twice
    ingest(landing, data, now=NOW)

    report = build_silver(data)
    state = reservations(data)["R1"]
    assert (report.events, report.duplicates_removed) == (3, 1)
    assert state["status"] == "cancelled"
    assert state["event_count"] == 3
    assert state["last_event_ts"].isoformat() == "2026-01-08T09:00:00+00:00"
    assert state["created_ts"].isoformat() == "2026-01-05T10:00:00+00:00"


def test_both_rate_spellings_become_the_same_decimal(tmp_path):
    landing, data = tmp_path / "landing", tmp_path / "data"
    old = event("E1", "R1", "created", "2026-01-05T10:00:00Z", nightly_rate=123.45)
    new = event("E2", "R2", "created", "2026-06-16T10:00:00Z", check_in="2026-06-20")
    del new["nightly_rate"]
    new["rate_amount"] = "123.45"
    land(landing, "2026-01-05", [old])
    land(landing, "2026-06-16", [new])
    ingest(landing, data, now=NOW)
    build_silver(data)
    found = reservations(data)
    assert found["R1"]["rate"] == found["R2"]["rate"] == Decimal("123.45")


def test_invalid_rows_are_set_aside_with_their_reason(tmp_path):
    landing, data = tmp_path / "landing", tmp_path / "data"
    good = event("E1", "R1", "created", "2026-01-05T10:00:00Z")
    bad = event("E2", "R2", "created", "2026-01-05T11:00:00Z", venue="fogline-nowhere")
    land(landing, "2026-01-05", [good, bad])
    ingest(landing, data, now=NOW)

    report = build_silver(data)
    assert (report.events, report.rejected) == (1, 1)
    rejected = read_rows(silver_path(data, "rejected_rows"))[0]
    assert rejected["_row_errors"] == "venue:value" and "R2" in rejected["_payload"]
    assert set(reservations(data)) == {"R1"}


def test_the_gold_check_catches_a_table_that_breaks_the_contract():
    con = duckdb.connect()
    con.execute(
        "create table hotel_daily as select date '2026-01-01' as date, 'fogline-marina' as venue, "
        "10 as rooms_available, 12 as rooms_sold, 100.0 as room_revenue"
    )
    problems = contract_violations(con, "hotel_daily", load_gold_contract()["hotel_daily"])
    assert any("rooms_sold BETWEEN 0 AND rooms_available" in p for p in problems)
    assert any("room_revenue is DECIMAL" in p for p in problems)
