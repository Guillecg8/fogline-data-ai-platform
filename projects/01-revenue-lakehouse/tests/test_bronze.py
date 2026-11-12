import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from revenue_lakehouse.bronze import (
    ChangedFileError,
    bronze_path,
    ingest,
    ledger_path,
    loaded_batches,
    read_rows,
)
from revenue_lakehouse.generate import FILE_PREFIX, generate

DAYS = ["2026-02-27", "2026-03-02", "2026-06-14", "2026-06-16", "2026-06-17"]
OLD, CHANGED = DAYS[:3], DAYS[3:]  # before and after the vendor release of 2026-06-15
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def full_landing(tmp_path_factory):
    out = tmp_path_factory.mktemp("full")
    generate(out, seed=42)
    return out / "landing" / "pms"


@pytest.fixture
def landing(tmp_path, full_landing):
    folder = tmp_path / "landing"
    folder.mkdir()
    for day in DAYS:
        shutil.copy(full_landing / f"{FILE_PREFIX}{day}.jsonl", folder)
    return folder


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


rows = read_rows


def lines_in(folder, days):
    return sum(len((folder / f"{FILE_PREFIX}{day}.jsonl").read_text().splitlines()) for day in days)


def test_version_1_loads_the_old_batches_and_quarantines_the_new_ones(landing, data):
    report = ingest(landing, data, contract_version=1, now=NOW)
    assert len(report.loaded) == 3 and len(report.quarantined) == 2
    assert set(report.quarantined.values()) == {
        "missing nightly_rate; new loyalty_tier, rate_amount"
    }
    bronze = rows(bronze_path(data))
    assert len(bronze) == lines_in(landing, OLD)
    assert {row["_batch_date"].isoformat() for row in bronze} == set(OLD)
    ledger = rows(ledger_path(data))
    assert sorted((r["batch_date"].isoformat(), r["action"]) for r in ledger) == sorted(
        [(day, "loaded") for day in OLD] + [(day, "quarantined") for day in CHANGED]
    )


def test_bronze_keeps_the_raw_line_and_the_load_metadata(landing, data):
    ingest(landing, data, contract_version=1, now=NOW)
    first = sorted(rows(bronze_path(data)), key=lambda r: (r["_source_file"], r["_line_no"]))[0]
    original = (landing / first["_source_file"]).read_text().splitlines()[0]
    assert first["_payload"] == original
    assert first["_line_no"] == 1 and first["_contract_version"] == 1
    assert first["_ingested_at"] == NOW and len(first["_file_sha256"]) == 64


def test_running_it_twice_loads_nothing_twice(landing, data):
    ingest(landing, data, contract_version=1, now=NOW)
    before = (len(rows(bronze_path(data))), len(rows(ledger_path(data))))
    again = ingest(landing, data, contract_version=1, now=NOW)
    assert not again.loaded and not again.quarantined and len(again.skipped) == 5
    assert (len(rows(bronze_path(data))), len(rows(ledger_path(data)))) == before


def test_a_newer_contract_replays_the_quarantined_batches(landing, data):
    ingest(landing, data, contract_version=1, now=NOW)
    replay = ingest(landing, data, contract_version=2, now=NOW)
    assert sorted(day[len(FILE_PREFIX) : -6] for day in replay.loaded) == CHANGED
    assert not replay.quarantined and len(replay.skipped) == 3
    bronze = rows(bronze_path(data))
    assert len(bronze) == lines_in(landing, DAYS)
    versions = {r["_batch_date"].isoformat(): r["_contract_version"] for r in bronze}
    assert versions == {day: 1 if day in OLD else 2 for day in DAYS}
    states = {}
    for entry in sorted(rows(ledger_path(data)), key=lambda r: r["seq"]):
        states[entry["batch_date"].isoformat()] = entry["action"]
    assert set(states.values()) == {"loaded"}


def test_the_same_contract_does_not_retry_quarantined_batches(landing, data):
    ingest(landing, data, contract_version=1, now=NOW)
    again = ingest(landing, data, contract_version=1, now=NOW)
    assert not again.quarantined and len(again.skipped) == 5


def test_the_latest_contract_accepts_everything_from_the_start(landing, data):
    report = ingest(landing, data, now=NOW)
    assert len(report.loaded) == 5 and not report.quarantined and report.bad_rows == 0


def test_a_loaded_batch_that_changes_is_an_error(landing, data):
    ingest(landing, data, now=NOW)
    path = landing / f"{FILE_PREFIX}{DAYS[0]}.jsonl"
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    with pytest.raises(ChangedFileError):
        ingest(landing, data, now=NOW)


def test_if_the_ledger_write_was_lost_the_next_run_repairs_it(landing, data):
    ingest(landing, data, now=NOW)
    shutil.rmtree(ledger_path(data))  # as if the process died between the two writes
    before = len(rows(bronze_path(data)))
    report = ingest(landing, data, now=NOW)
    assert len(report.repaired) == 5 and not report.loaded
    assert len(rows(bronze_path(data))) == before
    assert {r["status"] for r in rows(ledger_path(data))} == {"repaired"}
    assert loaded_batches(data).keys() == {f"{FILE_PREFIX}{day}.jsonl" for day in DAYS}


def test_bad_lines_are_loaded_but_flagged(landing, data):
    path = landing / f"{FILE_PREFIX}{DAYS[1]}.jsonl"
    lines = path.read_text().splitlines()
    broken = json.loads(lines[1])
    broken["venue"] = "fogline-nowhere"
    lines[1] = json.dumps(broken)
    lines[2] = "{this is not json"
    path.write_text("\n".join(lines) + "\n")

    report = ingest(landing, data, now=NOW)
    assert report.bad_rows == 2
    flagged = {
        r["_line_no"]: r["_row_errors"]
        for r in rows(bronze_path(data))
        if r["_source_file"] == path.name and r["_row_errors"]
    }
    assert flagged == {2: "venue:value", 3: "json:invalid"}
    assert (
        next(e for e in rows(ledger_path(data)) if e["source_file"] == path.name)["bad_rows"] == 2
    )


def test_ingestion_works_where_pyarrow_parquet_is_blocked(landing, data):
    """Some corporate Windows policies block the pyarrow parquet DLL; bronze must not need it."""
    script = (
        "import sys\n"
        "sys.modules['pyarrow._parquet'] = None\n"
        "sys.modules['pyarrow.parquet'] = None\n"
        "from pathlib import Path\n"
        "from revenue_lakehouse.bronze import ingest\n"
        "landing, data = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "ingest(landing, data)\n"
        "print(len(ingest(landing, data).skipped))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(landing), str(data)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "5"
