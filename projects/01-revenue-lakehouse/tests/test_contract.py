import pytest

from revenue_lakehouse.contract import compare_batch, load_contract

CONTRACT = load_contract()

ROW = {
    "event_id": "EVT-0000001",
    "reservation_id": "RES-000001",
    "event_type": "created",
    "event_ts": "2026-02-01T10:15:00Z",
    "venue": "fogline-marina",
    "room_type": "deluxe",
    "check_in": "2026-02-10",
    "check_out": "2026-02-12",
    "guests": 2,
    "nightly_rate": 312.5,
    "channel": "direct",
}


def batch(count=10, **changes):
    rows = [dict(ROW, event_id=f"EVT-{n:07d}") for n in range(count)]
    return [{**row, **changes} for row in rows]


def test_the_contract_loads_and_has_a_primary_key_column():
    assert CONTRACT.source == "pms_reservation_events"
    assert set(CONTRACT.primary_key) <= set(CONTRACT.columns)


def test_a_valid_batch_is_ok():
    report = compare_batch(CONTRACT, batch())
    assert report.status == "ok"
    assert report.bad_rows == {}


def test_a_new_column_is_compatible_and_reported():
    report = compare_batch(CONTRACT, batch(loyalty_tier="gold"))
    assert report.status == "compatible"
    assert report.added == ["loyalty_tier"]


def test_a_renamed_column_is_breaking():
    rows = batch()
    for row in rows:
        row["rate_amount"] = f"{row.pop('nightly_rate'):.2f}"
    report = compare_batch(CONTRACT, rows)
    assert report.status == "breaking"
    assert report.missing == ["nightly_rate"]
    assert report.added == ["rate_amount"]


def test_a_column_that_changes_type_is_breaking():
    report = compare_batch(CONTRACT, batch(nightly_rate="312.50"))
    assert report.status == "breaking"
    assert report.retyped == ["nightly_rate"]


def test_a_few_bad_rows_are_flagged_but_do_not_stop_the_batch():
    rows = batch()
    rows[3]["venue"] = "fogline-nowhere"
    rows[7]["check_in"] = "10/02/2026"
    report = compare_batch(CONTRACT, rows)
    assert report.status == "ok"
    assert report.bad_rows == {3: ["venue:value"], 7: ["check_in:type"]}


@pytest.mark.parametrize(
    "column, value, reason",
    [
        ("guests", True, "guests:type"),
        ("guests", 2.5, "guests:type"),
        ("event_ts", "yesterday", "event_ts:type"),
        ("event_type", "deleted", "event_type:value"),
        ("event_id", None, "event_id:null"),
    ],
)
def test_row_level_problems(column, value, reason):
    rows = batch()
    rows[0][column] = value
    assert compare_batch(CONTRACT, rows).bad_rows[0] == [reason]


def test_an_empty_batch_is_not_valid_evidence_of_anything():
    report = compare_batch(CONTRACT, [])
    assert report.rows == 0
    assert report.status == "breaking"  # every required column is missing
