"""Check every landed batch against the contract and print what would be stopped.

uv run poe lh-contracts
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from revenue_lakehouse.contract import BatchReport, compare_batch, load_contract
from revenue_lakehouse.generate import FILE_PREFIX
from revenue_lakehouse.paths import DATA_DIR


def read_batch(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def check_all(landing: Path, version: int | None = None) -> dict[str, BatchReport]:
    contract = load_contract(version)
    files = sorted(landing.glob(f"{FILE_PREFIX}*.jsonl"))
    if not files:
        raise SystemExit(f"no batches in {landing}: run uv run poe lh-generate first")
    return {
        path.stem.removeprefix(FILE_PREFIX): compare_batch(contract, read_batch(path))
        for path in files
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--landing", type=Path, default=DATA_DIR / "landing" / "pms")
    parser.add_argument("--contract", type=int, default=None)
    args = parser.parse_args()
    reports = check_all(args.landing, args.contract)

    counts = Counter(report.status for report in reports.values())
    print(f"{len(reports)} batches: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    previous = None
    for day, report in reports.items():
        if report.status != previous:
            print(f"from {day}: {report.status:<10} {report.summary()}")
            previous = report.status
    if counts["breaking"]:
        print("breaking batches would go to quarantine and never reach silver")


if __name__ == "__main__":
    main()
