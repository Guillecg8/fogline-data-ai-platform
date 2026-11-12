"""Measure what the incremental silver saves over a full rebuild.

The question: a lakehouse already holds N days of history and one more daily batch arrives. How
long does silver take to absorb it, rebuilding everything from bronze, versus merging only the new
batch? For each history size the benchmark builds that state, adds the next day's batch to two
copies of it, times each strategy (median of several runs), and checks that both give the same
tables.

    uv run poe lh-benchmark
"""

import argparse
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import deltalake
import duckdb

from revenue_lakehouse.bronze import ingest
from revenue_lakehouse.delta_io import read_rows
from revenue_lakehouse.paths import DATA_DIR, PROJECT_DIR
from revenue_lakehouse.silver import build_silver, build_silver_pos, silver_path
from revenue_lakehouse.silver_incremental import update_silver, update_silver_pos
from revenue_lakehouse.sources import PMS, POS

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
COMPARED = {
    "reservation_events": "event_id",
    "reservations": "reservation_id",
    "pos_check_revisions": "check_id, revision",
    "pos_checks": "check_id",
}


def timed(function, *args) -> float:
    started = time.perf_counter()
    function(*args)
    return time.perf_counter() - started


def rebuild(data: Path) -> None:
    build_silver(data)
    build_silver_pos(data)


def merge_new(data: Path) -> None:
    update_silver(data)
    update_silver_pos(data)


def tables(data: Path) -> dict:
    out = {}
    for table, order in COMPARED.items():
        rows = read_rows(silver_path(data, table), f"select * from t order by {order}")
        out[table] = [{k: v for k, v in row.items() if k != "_ingested_at"} for row in rows]
    return out


def measure(source_landing: Path, days: int, repeats: int, work: Path) -> dict:
    files = {s.name: sorted((source_landing / s.landing_dir).glob("*.jsonl")) for s in (PMS, POS)}
    base = work / f"base-{days}"
    for source in (PMS, POS):
        folder = base / "landing" / source.landing_dir
        folder.mkdir(parents=True)
        for path in files[source.name][:days]:
            shutil.copy(path, folder)
        ingest(folder, base, now=NOW, source=source)
    merge_new(base)  # the state of a lakehouse that has been running for `days` days

    full, incremental = [], []
    for run in range(repeats):
        a, b = work / f"full-{days}-{run}", work / f"inc-{days}-{run}"
        for copy in (a, b):
            shutil.copytree(base, copy)
            for source in (PMS, POS):  # the next day's batch lands
                folder = copy / "landing" / source.landing_dir
                shutil.copy(files[source.name][days], folder)
                ingest(folder, copy, now=NOW, source=source)
        full.append(timed(rebuild, a))
        incremental.append(timed(merge_new, b))
        if run == 0 and tables(a) != tables(b):
            raise SystemExit(f"incremental and full rebuild differ at {days} days of history")
        shutil.rmtree(a)
        shutil.rmtree(b)
    bronze_rows = sum(
        len(read_rows(base / "bronze" / s.name, "select _line_no from t")) for s in (PMS, POS)
    )
    return {
        "days": days,
        "rows": bronze_rows,
        "full": statistics.median(full),
        "incremental": statistics.median(incremental),
    }


def markdown(results: list[dict], repeats: int) -> str:
    lines = [
        "# Silver: full rebuild vs incremental MERGE",
        "",
        f"Measured {datetime.now(UTC):%Y-%m-%d} on {platform.system()} {platform.machine()}, "
        f"{os.cpu_count()} CPUs, Python {platform.python_version()}, DuckDB {duckdb.__version__}, "
        f"deltalake {deltalake.__version__}.",
        "",
        f"Time for silver to absorb one more daily batch (both sources), median of {repeats} runs. "
        "Both strategies were checked to produce identical tables.",
        "",
        "| History | Bronze rows | Full rebuild | Incremental MERGE | Speed-up |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        speedup = r["full"] / r["incremental"]
        lines.append(
            f"| {r['days']} days | {r['rows']:,} | {r['full']:.2f} s | "
            f"{r['incremental']:.2f} s | {speedup:.1f}x |"
        )
    lines += [
        "",
        "Run it again with `uv run poe lh-generate` then `uv run poe lh-benchmark`.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--landing", type=Path, default=DATA_DIR / "landing")
    parser.add_argument("--sizes", type=int, nargs="+", default=[30, 90, 180, 360])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=PROJECT_DIR / "BENCHMARK.md")
    args = parser.parse_args()
    if not (args.landing / "pms").exists():
        raise SystemExit(f"{args.landing} not found: run uv run poe lh-generate first")

    with tempfile.TemporaryDirectory() as folder:
        results = []
        for days in args.sizes:
            result = measure(args.landing, days, args.repeats, Path(folder))
            print(
                f"{days:>4} days: full {result['full']:.2f} s, "
                f"incremental {result['incremental']:.2f} s"
            )
            results.append(result)
    text = markdown(results, args.repeats)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"\n{text}\nwritten to {args.output}")


if __name__ == "__main__":
    sys.exit(main())
