from datetime import date, timedelta

import pyarrow as pa
import pytest

from revenue_lakehouse.delta_io import write
from revenue_lakehouse.forecast_series import load_series, series_from_rows

START = date(2026, 8, 30)  # a Sunday; Marina changes capacity two days later


def write_minimal_gold(data_dir):
    days = [START + timedelta(days=i) for i in range(4)]
    hotel = pa.table(
        {
            "date": pa.array(days * 2, pa.date32()),
            "venue": ["fogline-marina"] * 4 + ["fogline-mission"] * 4,
            "rooms_available": pa.array([62, 62, 40, 40, 38, 38, 38, 38], pa.int32()),
            "rooms_sold": pa.array([31, 31, 20, 20, 19, 38, 0, 19], pa.int32()),
            "room_revenue": [1.0] * 8,
        }
    )
    restaurant = pa.table(
        {
            "date": pa.array(days * 2, pa.date32()),
            "venue": ["saltgrass"] * 4 + ["lumen"] * 4,
            "covers": pa.array([0, 140, 150, 160, 300, 310, 320, 330], pa.int32()),
            "revenue": [1.0] * 8,
        }
    )
    write(data_dir / "gold" / "hotel_daily", hotel, "overwrite")
    write(data_dir / "gold" / "restaurant_daily", restaurant, "overwrite")


def test_load_series_reads_occupancy_and_covers_from_gold(tmp_path):
    write_minimal_gold(tmp_path)
    series = {s.name: s for s in load_series(tmp_path)}

    assert list(series) == [
        "fogline-marina/occupancy",
        "fogline-mission/occupancy",
        "lumen/covers",
        "saltgrass/covers",
    ]
    marina = series["fogline-marina/occupancy"]
    assert marina.values.tolist() == [0.5, 0.5, 0.5, 0.5]  # capacity drop is not a demand drop
    assert marina.start == START
    assert marina.first_weekday == 6
    assert marina.date_at(3) == date(2026, 9, 2)
    assert series["fogline-mission/occupancy"].values.tolist() == [0.5, 1.0, 0.0, 0.5]
    assert series["saltgrass/covers"].values.tolist() == [0.0, 140.0, 150.0, 160.0]
    assert series["lumen/covers"].values.tolist() == [300.0, 310.0, 320.0, 330.0]


def test_load_series_without_gold_stops(tmp_path):
    with pytest.raises(SystemExit):
        load_series(tmp_path)


def test_a_missing_day_is_an_error():
    rows = [
        {"venue": "lumen", "date": START, "value": 1.0},
        {"venue": "lumen", "date": START + timedelta(days=2), "value": 2.0},
    ]
    with pytest.raises(ValueError, match="expected 2026-08-31"):
        series_from_rows(rows, "covers")
