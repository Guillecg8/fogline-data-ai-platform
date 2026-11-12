"""Synthetic restaurant POS exports, with the problems that make restaurant data tricky.

Seeded like the PMS generator, with its own random stream, so adding this source did not change a
single byte of the PMS data. It also writes the *truth*: what each restaurant really sold per
business day.

Problems injected on purpose:
  - business day != calendar day: Saltgrass closes some checks after midnight, and they belong to
    the previous service day (the day ends at 04:00 local);
  - local timestamps with a UTC offset that changes with daylight saving time;
  - corrections (a new revision with a lower amount) and voids (a revision that cancels the check);
  - late files (about 3%) and duplicated rows (about 1.5%);
  - an additive schema change (``server_id``) from 2026-04-01: compatible.

    uv run poe lh-generate
"""

import csv
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from revenue_lakehouse.generate import END, START, season
from revenue_lakehouse.paths import DATA_DIR
from revenue_lakehouse.sources import POS

LOCAL = ZoneInfo("America/Los_Angeles")
DAY_STARTS_AT = timedelta(hours=4)  # a check closed before 04:00 belongs to the previous day
SERVER_ID_FROM = date(2026, 4, 1)  # batches from this day carry server_id (compatible)
SATURDAY_SUNDAY_CLOSED_FROM = date(2026, 10, 4)  # Saltgrass stops opening on Sundays

# covers per day, spend per cover, party sizes, closing window (hours after local midnight)
RESTAURANTS = {
    "saltgrass": {
        "covers": 140,
        "per_cover": 68.0,
        "parties": ((1, 2, 3, 4, 5, 6), (0.10, 0.40, 0.15, 0.20, 0.10, 0.05)),
        "window": (17.5, 23.0),
        "after_midnight": 0.06,
        "tip": (0.12, 0.22),
    },
    "lumen": {
        "covers": 310,
        "per_cover": 14.5,
        "parties": ((1, 2, 3, 4, 5), (0.45, 0.35, 0.10, 0.07, 0.03)),
        "window": (7.0, 15.0),
        "after_midnight": 0.0,
        "tip": (0.0, 0.15),
    },
}
ADJUST_RATE, VOID_RATE = 0.06, 0.02
LATE_RATE, DUPLICATE_RATE = 0.03, 0.015


@dataclass
class Check:
    venue: str
    business_date: date
    closed_at: datetime  # local, timezone-aware
    opened_at: datetime
    covers: int
    net_cents: int
    tip_cents: int
    channel: str
    server_id: str
    adjusted_cents: int | None = None
    voided: bool = False
    revision_at: datetime | None = None
    key: int = 0

    @property
    def final_cents(self) -> int:
        return self.adjusted_cents if self.adjusted_cents is not None else self.net_cents


def local_stamp(day: date, hours: float) -> datetime:
    """A local wall-clock time on ``day``; hours above 24 fall on the next calendar day."""
    naive = datetime(day.year, day.month, day.day) + timedelta(hours=hours)
    return naive.replace(tzinfo=LOCAL)


def business_date_of(moment: datetime) -> date:
    return (moment.replace(tzinfo=None) - DAY_STARTS_AT).date()


def build_checks(rng: random.Random) -> list[Check]:
    checks = []
    day = START
    while day <= END:
        weekend = 1.25 if day.weekday() in (4, 5) else 1.0
        for venue, spec in RESTAURANTS.items():
            if venue == "saltgrass" and day.weekday() == 6 and day >= SATURDAY_SUNDAY_CLOSED_FROM:
                continue
            target = spec["covers"] * weekend * (0.9 + 0.1 * season(day)) * rng.gauss(1.0, 0.1)
            served = 0
            while served < target:
                covers = rng.choices(*spec["parties"])[0]
                served += covers
                late_night = rng.random() < spec["after_midnight"]
                hours = rng.uniform(24.0, 25.5) if late_night else rng.uniform(*spec["window"])
                closed = local_stamp(day, hours)
                opened = closed - timedelta(minutes=rng.randint(25, 110))
                net = round(covers * spec["per_cover"] * rng.gauss(1.0, 0.18), 2)
                tip = round(net * rng.uniform(*spec["tip"]), 2)
                check = Check(
                    venue=venue,
                    business_date=business_date_of(closed),
                    closed_at=closed,
                    opened_at=opened,
                    covers=covers,
                    net_cents=round(net * 100),
                    tip_cents=round(tip * 100),
                    channel="takeout" if venue == "lumen" and rng.random() < 0.35 else "dine_in",
                    server_id=f"S{rng.randint(1, 30):02d}",
                )
                roll = rng.random()
                if roll < VOID_RATE:
                    check.voided = True
                    check.revision_at = closed + timedelta(minutes=rng.randint(10, 20 * 60))
                elif roll < VOID_RATE + ADJUST_RATE:
                    check.adjusted_cents = round(check.net_cents * rng.uniform(0.8, 0.95))
                    check.revision_at = closed + timedelta(minutes=rng.randint(10, 20 * 60))
                checks.append(check)
        day += timedelta(days=1)
    checks.sort(key=lambda c: (c.closed_at, c.venue))
    for number, check in enumerate(checks, start=1):
        check.key = number
    return checks


def render(check: Check, revision: int, batch_date: date) -> dict:
    """The row as the POS wrote it. Revision 2 is either the adjusted amount or the void."""
    amount = check.net_cents if revision == 1 else check.final_cents
    row = {
        "check_id": f"CHK-{check.key:07d}",
        "revision": revision,
        "venue": check.venue,
        "opened_at": check.opened_at.isoformat(timespec="seconds"),
        "closed_at": check.closed_at.isoformat(timespec="seconds"),
        "covers": check.covers,
        "net_amount": amount / 100,
        "tip": check.tip_cents / 100,
        "status": "void" if revision == 2 and check.voided else "closed",
        "channel": check.channel,
    }
    if batch_date >= SERVER_ID_FROM:
        row["server_id"] = check.server_id
    return row


def schedule(checks: list[Check], rng: random.Random):
    """(batch date, check, revision) for every row exported, including late and duplicated ones."""
    deliveries, late, duplicates = [], 0, 0

    def deliver(moment: datetime, check: Check, revision: int) -> None:
        nonlocal late, duplicates
        delay = rng.randint(1, 5) if rng.random() < LATE_RATE else 0
        late += delay > 0
        batch = min(moment.date() + timedelta(days=delay), END)
        deliveries.append((batch, check, revision))
        if rng.random() < DUPLICATE_RATE:
            duplicates += 1
            deliveries.append(
                (min(batch + timedelta(days=rng.choice([0, 1])), END), check, revision)
            )

    for check in checks:
        deliver(check.closed_at, check, 1)
        if check.revision_at is not None:
            deliver(check.revision_at, check, 2)
    return deliveries, late, duplicates


def restaurant_daily_truth(checks: list[Check]) -> list[dict]:
    covers: dict[tuple[str, date], int] = defaultdict(int)
    cents: dict[tuple[str, date], int] = defaultdict(int)
    for check in checks:
        if not check.voided:
            covers[check.venue, check.business_date] += check.covers
            cents[check.venue, check.business_date] += check.final_cents
    rows = []
    day = START
    while day <= END:
        for venue in sorted(RESTAURANTS):
            rows.append(
                {
                    "date": day.isoformat(),
                    "venue": venue,
                    "covers": covers[venue, day],
                    "revenue": f"{cents[venue, day] / 100:.2f}",
                }
            )
        day += timedelta(days=1)
    return rows


def generate_pos(out: Path = DATA_DIR, seed: int = 42) -> dict:
    rng = random.Random(f"{seed}-pos")  # its own stream: the PMS data does not move
    checks = build_checks(rng)
    deliveries, late, duplicates = schedule(checks, rng)

    batches: dict[date, list[dict]] = defaultdict(list)
    for batch_date, check, revision in deliveries:
        batches[batch_date].append(render(check, revision, batch_date))

    landing = out / "landing" / POS.landing_dir
    landing.mkdir(parents=True, exist_ok=True)
    for old in landing.glob(f"{POS.file_prefix}*.jsonl"):
        old.unlink()
    for batch_date, rows in sorted(batches.items()):
        rows.sort(key=lambda row: (row["closed_at"], row["check_id"], row["revision"]))
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        path = landing / f"{POS.file_prefix}{batch_date.isoformat()}.jsonl"
        path.write_text(text, encoding="utf-8", newline="\n")

    truth = out / "truth"
    truth.mkdir(parents=True, exist_ok=True)
    with (truth / "restaurant_daily.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["date", "venue", "covers", "revenue"], lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(restaurant_daily_truth(checks))

    summary = {
        "checks": len(checks),
        "rows": sum(len(rows) for rows in batches.values()),
        "batches": len(batches),
        "late": late,
        "duplicates": duplicates,
        "after_midnight": sum(c.closed_at.date() != c.business_date for c in checks),
        "adjusted": sum(c.adjusted_cents is not None for c in checks),
        "voided": sum(c.voided for c in checks),
    }
    (truth / "pos_defects.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return summary
