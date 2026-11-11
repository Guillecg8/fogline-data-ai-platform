"""Generate the synthetic KPI sample used by the copilot (seeded, so it is reproducible).

This stands in for the gold tables of project 01 until that project exports them.
The schema is fixed by contracts/gold_kpis.yaml.

    uv run python projects/02-ops-knowledge-copilot/scripts/make_kpi_sample.py
"""

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "samples"
START, END = date(2025, 11, 1), date(2026, 10, 31)
HOTELS = {"fogline-marina": (62, 289.0), "fogline-mission": (38, 219.0)}  # rooms, base rate
RESTAURANTS = {"saltgrass": (140, 68.0), "lumen": (310, 14.5)}  # covers per day, average check
RENOVATION_START = date(2026, 9, 1)  # Fogline Marina closes floors 3 and 4 (MEMO-2026-014)


def days():
    day = START
    while day <= END:
        yield day
        day += timedelta(days=1)


def season(day: date) -> float:
    """Demand multiplier: peaks in early autumn, lowest in January."""
    return 1.0 + 0.18 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 270) / 365)


def main() -> None:
    rng = random.Random(42)
    OUT.mkdir(exist_ok=True)

    with (OUT / "hotel_daily.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["date", "venue", "rooms_available", "rooms_sold", "room_revenue"])
        for day in days():
            for venue, (rooms, base_rate) in HOTELS.items():
                if venue == "fogline-marina" and day >= RENOVATION_START:
                    rooms = 40
                weekend = 1.12 if day.weekday() in (4, 5) else 1.0
                demand = 0.74 * season(day) * weekend * rng.gauss(1.0, 0.07)
                sold = max(0, min(rooms, round(rooms * demand)))
                rate = base_rate * season(day) * weekend * rng.gauss(1.0, 0.04)
                writer.writerow([day, venue, rooms, sold, f"{sold * rate:.2f}"])

    with (OUT / "restaurant_daily.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["date", "venue", "covers", "revenue"])
        for day in days():
            for venue, (base_covers, check) in RESTAURANTS.items():
                closed = venue == "saltgrass" and day.weekday() == 6 and day >= date(2026, 10, 4)
                weekend = 1.25 if day.weekday() in (4, 5) else 1.0
                covers = 0 if closed else round(base_covers * weekend * rng.gauss(1.0, 0.1))
                revenue = covers * check * rng.gauss(1.0, 0.05)
                writer.writerow([day, venue, covers, f"{revenue:.2f}"])


if __name__ == "__main__":
    main()
