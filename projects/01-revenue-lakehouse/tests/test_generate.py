import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date

import pytest

from revenue_lakehouse.check_landing import check_all, read_batch
from revenue_lakehouse.generate import (
    END,
    FILE_PREFIX,
    LOYALTY_FROM,
    RENAME_FROM,
    START,
    generate,
    load_capacity,
    rooms_on,
)


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    path = tmp_path_factory.mktemp("lakehouse")
    generate(path, seed=42)
    return path


@pytest.fixture(scope="module")
def landing(out):
    return out / "landing" / "pms"


@pytest.fixture(scope="module")
def events(landing):
    """(batch date, row) for every delivered row."""
    rows = []
    for path in sorted(landing.glob(f"{FILE_PREFIX}*.jsonl")):
        day = date.fromisoformat(path.stem.removeprefix(FILE_PREFIX))
        rows += [(day, row) for row in read_batch(path)]
    return rows


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()


def test_the_same_seed_gives_identical_files(out, tmp_path):
    generate(tmp_path, seed=42)
    assert tree_hash(tmp_path) == tree_hash(out)


def test_another_seed_gives_different_data(out, tmp_path):
    generate(tmp_path, seed=7)
    assert tree_hash(tmp_path) != tree_hash(out)


def test_the_schema_changes_when_the_generator_says_it_does(landing):
    reports = check_all(landing)
    for day, report in reports.items():
        when = date.fromisoformat(day)
        expected = (
            "breaking" if when >= RENAME_FROM else "compatible" if when >= LOYALTY_FROM else "ok"
        )
        assert report.status == expected, (day, report.summary())
    assert reports[RENAME_FROM.isoformat()].missing == ["nightly_rate"]


def test_some_events_are_delivered_twice(events):
    copies = Counter(row["event_id"] for _, row in events)
    duplicated = [event_id for event_id, n in copies.items() if n > 1]
    assert 0.005 < len(duplicated) / len(copies) < 0.03


def test_some_events_arrive_after_the_day_they_happened(events):
    late = [1 for day, row in events if day > date.fromisoformat(row["event_ts"][:10])]
    assert 0.01 < len(late) / len(events) < 0.08
    assert not [1 for day, row in events if day < date.fromisoformat(row["event_ts"][:10])]


def test_a_reservation_is_created_first_and_closed_at_most_once(events):
    by_reservation = defaultdict(dict)
    for _, row in events:
        by_reservation[row["reservation_id"]][row["event_type"]] = row["event_ts"]
    for kinds in by_reservation.values():
        assert "created" in kinds
        for kind in ("modified", "cancelled"):
            if kind in kinds:
                assert kinds[kind] > kinds["created"]
        assert not ("modified" in kinds and "cancelled" in kinds)


def test_the_truth_respects_capacity_and_covers_every_day(out):
    capacity = load_capacity()
    with (out / "truth" / "hotel_daily.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == ((END - START).days + 1) * 2
    for row in rows:
        day = date.fromisoformat(row["date"])
        assert int(row["rooms_available"]) == rooms_on(capacity, row["venue"], day)
        assert 0 <= int(row["rooms_sold"]) <= int(row["rooms_available"])
    marina_after = [r for r in rows if r["venue"] == "fogline-marina" and r["date"] >= "2026-09-01"]
    assert {r["rooms_available"] for r in marina_after} == {"40"}


def test_occupancy_is_realistic(out):
    with (out / "truth" / "hotel_daily.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for venue in ("fogline-marina", "fogline-mission"):
        mine = [r for r in rows if r["venue"] == venue]
        rate = sum(int(r["rooms_sold"]) for r in mine) / sum(
            int(r["rooms_available"]) for r in mine
        )
        assert 0.6 < rate < 0.85, (venue, rate)
    assert json.loads((out / "truth" / "defects.json").read_text())["reservations"] > 5000
