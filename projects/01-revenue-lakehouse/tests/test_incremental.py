import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from revenue_lakehouse.bronze import ingest
from revenue_lakehouse.delta_io import read_rows, sql_list
from revenue_lakehouse.generate import FILE_PREFIX, generate
from revenue_lakehouse.generate_pos import generate_pos
from revenue_lakehouse.silver import build_silver, build_silver_pos, silver_path
from revenue_lakehouse.silver_incremental import update_silver, update_silver_pos
from revenue_lakehouse.silver_state import pending_files
from revenue_lakehouse.sources import PMS, POS

START = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
CHUNKS = 5
TABLES = {
    "reservation_events": "event_id",
    "reservations": "reservation_id",
    "pos_check_revisions": "check_id, revision",
    "pos_checks": "check_id",
}


def snapshot(data, table):
    """Table rows in a fixed order, without the load time (which depends on when it was loaded)."""
    order = TABLES[table]
    rows = read_rows(silver_path(data, table), f"select * from t order by {order}")
    return [{k: v for k, v in row.items() if k != "_ingested_at"} for row in rows]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    out = tmp_path_factory.mktemp("source")
    generate(out, seed=42)
    generate_pos(out, seed=42)
    return out


@pytest.fixture(scope="module")
def full(source, tmp_path_factory):
    """Everything loaded at once, silver rebuilt from scratch."""
    out = tmp_path_factory.mktemp("full")
    shutil.copytree(source / "landing", out / "landing")
    for src in (PMS, POS):
        ingest(out / "landing" / src.landing_dir, out, now=START, source=src)
    build_silver(out)
    build_silver_pos(out)
    return out


@pytest.fixture(scope="module")
def incremental(source, tmp_path_factory):
    """The same files arriving in five waves, silver updated after each one."""
    out = tmp_path_factory.mktemp("incremental")
    reports = []
    for src, update in ((PMS, update_silver), (POS, update_silver_pos)):
        files = sorted((source / "landing" / src.landing_dir).glob("*.jsonl"))
        size = -(-len(files) // CHUNKS)
        landing = out / "landing" / src.landing_dir
        landing.mkdir(parents=True)
        for wave in range(CHUNKS):
            for path in files[wave * size : (wave + 1) * size]:
                shutil.copy(path, landing)
            now = START + timedelta(days=wave + 1)
            ingest(landing, out, now=now, source=src)
            reports.append(update(out))
    return out, reports


@pytest.mark.parametrize("table", list(TABLES))
def test_incremental_silver_equals_a_full_rebuild(full, incremental, table):
    out, _ = incremental
    assert snapshot(out, table) == snapshot(full, table)


def test_each_wave_only_processes_its_own_batches(source, incremental):
    _, reports = incremental
    pms = [r for r in reports if r.source == PMS.name]
    total = len(list((source / "landing" / "pms").glob("*.jsonl")))
    assert len(pms) == CHUNKS
    assert sum(r.batches for r in pms) == total
    assert all(r.batches < total / 2 for r in pms)


def test_running_it_again_does_nothing(incremental):
    out, _ = incremental
    before = snapshot(out, "reservations")
    report = update_silver(out)
    assert (report.batches, report.rows, report.touched) == (0, 0, 0)
    assert snapshot(out, "reservations") == before


def test_after_a_full_rebuild_the_incremental_update_continues_from_there(source, tmp_path):
    folder = tmp_path / "landing" / "pms"
    folder.mkdir(parents=True)
    files = sorted((source / "landing" / "pms").glob("*.jsonl"))[:30]
    for path in files[:29]:
        shutil.copy(path, folder)
    ingest(folder, tmp_path, now=START)
    build_silver(tmp_path)
    assert pending_files(tmp_path, PMS) == []

    shutil.copy(files[29], folder)
    ingest(folder, tmp_path, now=START)
    assert [name for name, _ in pending_files(tmp_path, PMS)] == [files[29].name]
    assert update_silver(tmp_path).batches == 1


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


def step(tmp_path, day, rows, minutes):
    land(tmp_path / "landing", day, rows)
    ingest(tmp_path / "landing", tmp_path, now=START + timedelta(minutes=minutes))
    return update_silver(tmp_path)


def test_a_new_event_updates_only_the_reservation_it_belongs_to(tmp_path):
    step(tmp_path, "2026-01-05", [event("E1", "R1", "created", "2026-01-05T10:00:00Z")], 1)
    step(tmp_path, "2026-01-06", [event("E2", "R2", "created", "2026-01-06T10:00:00Z")], 2)
    report = step(
        tmp_path, "2026-01-08", [event("E3", "R1", "cancelled", "2026-01-08T09:00:00Z")], 3
    )
    found = reservations(tmp_path)
    assert report.touched == 1
    assert (found["R1"]["status"], found["R1"]["event_count"]) == ("cancelled", 2)
    assert found["R2"]["status"] == "active"


def test_a_late_older_event_does_not_overwrite_a_newer_state(tmp_path):
    step(tmp_path, "2026-01-05", [event("E1", "R1", "created", "2026-01-05T10:00:00Z")], 1)
    step(tmp_path, "2026-01-08", [event("E2", "R1", "cancelled", "2026-01-08T09:00:00Z")], 2)
    late = event("E3", "R1", "modified", "2026-01-06T12:00:00Z", nightly_rate=90.0)
    report = step(tmp_path, "2026-01-12", [late], 3)
    state = reservations(tmp_path)["R1"]
    assert report.touched == 1
    assert state["status"] == "cancelled" and state["event_count"] == 3
    assert state["last_event_ts"].isoformat() == "2026-01-08T09:00:00+00:00"


def test_an_event_delivered_twice_in_different_waves_counts_once(tmp_path):
    created = event("E1", "R1", "created", "2026-01-05T10:00:00Z")
    step(tmp_path, "2026-01-05", [created], 1)
    step(tmp_path, "2026-01-06", [created], 2)
    assert reservations(tmp_path)["R1"]["event_count"] == 1
    assert len(read_rows(silver_path(tmp_path, "reservation_events"))) == 1


def test_an_id_with_a_quote_is_handled_as_data_not_as_sql(tmp_path):
    assert sql_list(["a'b"]) == "'a''b'"
    step(tmp_path, "2026-01-05", [event("E1", "R'1", "created", "2026-01-05T10:00:00Z")], 1)
    step(tmp_path, "2026-01-06", [event("E2", "R'1", "cancelled", "2026-01-06T10:00:00Z")], 2)
    assert reservations(tmp_path)["R'1"]["status"] == "cancelled"


def test_the_merge_path_works_where_pyarrow_parquet_and_dataset_are_blocked(tmp_path):
    script = (
        "import sys\n"
        "for name in ('_parquet', 'parquet', '_dataset', 'dataset'):\n"
        "    sys.modules['pyarrow.' + name] = None\n"
        "import json\n"
        "from pathlib import Path\n"
        "from revenue_lakehouse.bronze import ingest\n"
        "from revenue_lakehouse.silver_incremental import update_silver\n"
        "root = Path(sys.argv[1])\n"
        "for day, kind in (('2026-01-05', 'created'), ('2026-01-06', 'cancelled')):\n"
        "    row = json.loads(sys.argv[2])\n"
        "    row['event_id'], row['event_type'] = 'E-' + kind, kind\n"
        "    folder = root / 'landing'\n"
        "    folder.mkdir(exist_ok=True)\n"
        "    (folder / f'pms_reservation_events_{day}.jsonl').write_text(json.dumps(row) + '\\n')\n"
        "    ingest(folder, root)\n"
        "    update_silver(root)\n"
        "print('ok')\n"
    )
    row = json.dumps(event("x", "R1", "created", "2026-01-05T10:00:00Z"))
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), row],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "ok"
    assert reservations(tmp_path)["R1"]["event_count"] == 2


def test_read_where_in_handles_many_ids(tmp_path):
    from revenue_lakehouse.delta_io import IN_CHUNK, read_where_in, write

    ids = [f"id-{i}" for i in range(IN_CHUNK * 5 + 7)]
    write(tmp_path / "t", pa.table({"k": ids, "v": list(range(len(ids)))}), "overwrite")
    got = read_where_in(tmp_path / "t", "k", ids + ["missing"])
    assert got.num_rows == len(ids)
    assert read_where_in(tmp_path / "t", "k", []).num_rows == 0
