"""Synthetic PMS reservation events, with the problems real exports have.

Everything is seeded, so the same seed gives byte-identical files. The generator also writes
the *truth* (what the hotel KPIs really were), so later stages can be tested against it
instead of being trusted.

Problems injected on purpose:
  - cancellations and modifications (every event is a full snapshot of the reservation);
  - late events: about 3% arrive 1 to 5 days after they happened;
  - duplicates: about 1.5% of events are delivered twice;
  - an additive schema change (``loyalty_tier``) from 2026-03-01: compatible;
  - a breaking schema change from 2026-06-15: ``nightly_rate`` becomes ``rate_amount``, a string.

    uv run poe lh-generate
"""

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from revenue_lakehouse.paths import DATA_DIR, REFERENCE_DIR

START, END = date(2025, 11, 1), date(2026, 10, 31)  # nights covered by the KPIs
WARMUP_DAYS = 7  # arrivals before START, so the first KPI days are not artificially empty
MAX_LEAD_DAYS = 60
LOYALTY_FROM = date(2026, 3, 1)  # batches from this day carry loyalty_tier (compatible)
RENAME_FROM = date(2026, 6, 15)  # batches from this day rename nightly_rate (breaking)

VENUES = {"fogline-marina": 289.0, "fogline-mission": 219.0}  # base nightly rate
ROOM_TYPES = {"standard": (1.0, 0.6), "deluxe": (1.25, 0.3), "suite": (1.7, 0.1)}
CHANNELS = (("direct", "ota", "corporate"), (0.45, 0.40, 0.15))
LOYALTY = (("none", "silver", "gold"), (0.7, 0.2, 0.1))
LENGTHS = ((1, 2, 3, 4, 5, 7), (0.25, 0.33, 0.20, 0.10, 0.07, 0.05))
MEAN_LENGTH = sum(n * w for n, w in zip(*LENGTHS, strict=True))
MEAN_MULTIPLIER = sum(m * w for m, w in ROOM_TYPES.values())
TARGET_OCCUPANCY = 0.74
CANCEL_RATE, MODIFY_RATE = 0.12, 0.08
LATE_RATE, DUPLICATE_RATE = 0.03, 0.015

FILE_PREFIX = "pms_reservation_events_"


@dataclass
class Reservation:
    venue: str
    room_type: str
    check_in: date
    check_out: date
    guests: int
    rate: float
    channel: str
    loyalty_tier: str
    cancelled: bool
    created_ts: datetime
    modified_ts: datetime | None = None
    modified_rate: float | None = None
    cancelled_ts: datetime | None = None
    key: int = 0

    @property
    def final_rate(self) -> float:
        return self.modified_rate if self.modified_rate is not None else self.rate


@dataclass
class Summary:
    reservations: int = 0
    events: int = 0
    batches: int = 0
    duplicates: int = 0
    late: int = 0
    defects: dict[str, str] = field(default_factory=dict)


def season(day: date) -> float:
    """Demand multiplier: peaks in early autumn, lowest in January."""
    return 1.0 + 0.18 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 270) / 365)


def load_capacity(path: Path | None = None) -> list[tuple[str, date, date, int]]:
    path = path or REFERENCE_DIR / "hotel_capacity.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            (
                row["venue"],
                date.fromisoformat(row["valid_from"]),
                date.fromisoformat(row["valid_to"]) if row["valid_to"] else date.max,
                int(row["rooms"]),
            )
            for row in csv.DictReader(handle)
        ]


def rooms_on(capacity: list[tuple[str, date, date, int]], venue: str, day: date) -> int:
    for name, valid_from, valid_to, rooms in capacity:
        if name == venue and valid_from <= day <= valid_to:
            return rooms
    raise ValueError(f"no capacity for {venue} on {day}")


def _stamp(day: date, rng: random.Random) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
        seconds=rng.randint(6 * 3600, 20 * 3600)
    )


def _later(start: datetime, end_day: date, rng: random.Random) -> datetime:
    end = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59, tzinfo=UTC)
    moment = start + timedelta(seconds=60) + (end - start) * rng.random() * 0.98
    return moment.replace(microsecond=0)


def build_reservations(rng: random.Random, capacity) -> list[Reservation]:
    occupied: dict[str, dict[date, int]] = {venue: defaultdict(int) for venue in VENUES}
    reservations = []
    day = START - timedelta(days=WARMUP_DAYS)
    while day <= END:
        weekend = 1.12 if day.weekday() in (4, 5) else 1.0
        for venue, base_rate in VENUES.items():
            rooms = rooms_on(capacity, venue, day)
            mean = rooms * TARGET_OCCUPANCY * season(day) * weekend / MEAN_LENGTH
            mean /= 1 - CANCEL_RATE
            for _ in range(max(0, round(mean * rng.gauss(1.0, 0.12)))):
                cancelled = rng.random() < CANCEL_RATE
                nights = rng.choices(*LENGTHS)[0]
                stay = [day + timedelta(days=i) for i in range(nights)]
                if not cancelled:
                    if any(occupied[venue][d] >= rooms_on(capacity, venue, d) for d in stay):
                        continue  # sold out: the guest books elsewhere
                    for d in stay:
                        occupied[venue][d] += 1
                room_type = rng.choices(list(ROOM_TYPES), [w for _, w in ROOM_TYPES.values()])[0]
                multiplier = ROOM_TYPES[room_type][0]
                rate = base_rate / MEAN_MULTIPLIER * multiplier * season(day) * weekend
                rate = round(rate * rng.gauss(1.0, 0.04), 2)
                lead = min(MAX_LEAD_DAYS, int(rng.expovariate(1 / 14)))
                reservation = Reservation(
                    venue=venue,
                    room_type=room_type,
                    check_in=day,
                    check_out=day + timedelta(days=nights),
                    guests=rng.randint(1, 4),
                    rate=rate,
                    channel=rng.choices(*CHANNELS)[0],
                    loyalty_tier=rng.choices(*LOYALTY)[0],
                    cancelled=cancelled,
                    created_ts=_stamp(day - timedelta(days=lead), rng),
                )
                if cancelled:
                    reservation.cancelled_ts = _later(reservation.created_ts, day, rng)
                elif rng.random() < MODIFY_RATE:
                    reservation.modified_ts = _later(reservation.created_ts, day, rng)
                    reservation.modified_rate = round(rate * rng.uniform(0.88, 0.97), 2)
                reservations.append(reservation)
        day += timedelta(days=1)
    reservations.sort(key=lambda r: (r.created_ts, r.venue, r.check_in))
    for number, reservation in enumerate(reservations, start=1):
        reservation.key = number
    return reservations


def build_events(reservations: list[Reservation]) -> list[dict]:
    """One full snapshot per event, ordered in time; event ids follow that order."""
    events = []
    for r in reservations:
        base = {"reservation": r, "rate": r.rate}
        events.append({**base, "type": "created", "ts": r.created_ts})
        if r.cancelled_ts:
            events.append({**base, "type": "cancelled", "ts": r.cancelled_ts})
        if r.modified_ts:
            events.append(
                {"reservation": r, "rate": r.modified_rate, "type": "modified", "ts": r.modified_ts}
            )
    events.sort(key=lambda e: (e["ts"], e["reservation"].key, e["type"]))
    for number, event in enumerate(events, start=1):
        event["id"] = f"EVT-{number:07d}"
    return events


def render(event: dict, batch_date: date) -> dict:
    """The row as the PMS wrote it on the day of the batch (the schema changes over time)."""
    r = event["reservation"]
    row = {
        "event_id": event["id"],
        "reservation_id": f"RES-{r.key:06d}",
        "event_type": event["type"],
        "event_ts": event["ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "venue": r.venue,
        "room_type": r.room_type,
        "check_in": r.check_in.isoformat(),
        "check_out": r.check_out.isoformat(),
        "guests": r.guests,
    }
    if batch_date >= RENAME_FROM:
        row["rate_amount"] = f"{event['rate']:.2f}"
    else:
        row["nightly_rate"] = event["rate"]
    row["channel"] = r.channel
    if batch_date >= LOYALTY_FROM:
        row["loyalty_tier"] = r.loyalty_tier
    return row


def schedule_deliveries(events: list[dict], rng: random.Random) -> tuple[list, int, int]:
    """(batch_date, event) pairs: most events arrive the same day, some late, some twice."""
    deliveries, late, duplicates = [], 0, 0
    for event in events:
        happened = event["ts"].date()
        delay = rng.randint(1, 5) if rng.random() < LATE_RATE else 0
        late += delay > 0
        batch = min(happened + timedelta(days=delay), END)
        deliveries.append((batch, event))
        if rng.random() < DUPLICATE_RATE:
            duplicates += 1
            deliveries.append((min(batch + timedelta(days=rng.choice([0, 1])), END), event))
    return deliveries, late, duplicates


def hotel_daily_truth(reservations: list[Reservation], capacity) -> list[dict]:
    sold: dict[tuple[str, date], int] = defaultdict(int)
    revenue: dict[tuple[str, date], float] = defaultdict(float)
    for r in reservations:
        if r.cancelled:
            continue
        night = max(r.check_in, START)
        while night < r.check_out and night <= END:
            sold[r.venue, night] += 1
            revenue[r.venue, night] += r.final_rate
            night += timedelta(days=1)
    rows = []
    day = START
    while day <= END:
        for venue in VENUES:
            rows.append(
                {
                    "date": day.isoformat(),
                    "venue": venue,
                    "rooms_available": rooms_on(capacity, venue, day),
                    "rooms_sold": sold[venue, day],
                    "room_revenue": f"{revenue[venue, day]:.2f}",
                }
            )
        day += timedelta(days=1)
    return rows


def generate(out: Path = DATA_DIR, seed: int = 42) -> Summary:
    rng = random.Random(seed)
    capacity = load_capacity()
    reservations = build_reservations(rng, capacity)
    events = build_events(reservations)
    deliveries, late, duplicates = schedule_deliveries(events, rng)

    batches: dict[date, list[dict]] = defaultdict(list)
    for batch_date, event in deliveries:
        batches[batch_date].append(render(event, batch_date))

    landing = out / "landing" / "pms"
    landing.mkdir(parents=True, exist_ok=True)
    for old in landing.glob(f"{FILE_PREFIX}*.jsonl"):
        old.unlink()
    for batch_date, rows in sorted(batches.items()):
        rows.sort(key=lambda row: (row["event_ts"], row["event_id"]))
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        path = landing / f"{FILE_PREFIX}{batch_date.isoformat()}.jsonl"
        path.write_text(text, encoding="utf-8", newline="\n")

    truth = out / "truth"
    truth.mkdir(parents=True, exist_ok=True)
    columns = ["date", "venue", "rooms_available", "rooms_sold", "room_revenue"]
    with (truth / "hotel_daily.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(hotel_daily_truth(reservations, capacity))

    summary = Summary(
        reservations=len(reservations),
        events=len(events),
        batches=len(batches),
        duplicates=duplicates,
        late=late,
        defects={
            "compatible_change_from": LOYALTY_FROM.isoformat(),
            "breaking_change_from": RENAME_FROM.isoformat(),
        },
    )
    (truth / "defects.json").write_text(
        json.dumps(summary.__dict__, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    summary = generate(args.out, args.seed)
    print(
        f"{summary.reservations} reservations, {summary.events} events in {summary.batches} "
        f"batches ({summary.late} late, {summary.duplicates} duplicated)\nwritten to {args.out}"
    )


if __name__ == "__main__":
    main()
