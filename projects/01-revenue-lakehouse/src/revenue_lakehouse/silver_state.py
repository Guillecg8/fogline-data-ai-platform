"""Which bronze batches silver has already processed, so it only reads the new ones."""

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from revenue_lakehouse.bronze import loaded_batches
from revenue_lakehouse.delta_io import read_rows, write
from revenue_lakehouse.sources import Source

STATE_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("file_sha256", pa.string()),
        ("processed_at", pa.timestamp("us", tz="UTC")),
    ]
)


def state_path(data_dir: Path, source: Source) -> Path:
    return data_dir / "ops" / f"silver_state_{source.name}"


def pending_files(data_dir: Path, source: Source) -> list[tuple[str, str]]:
    """Bronze batches (name, sha) that silver has not processed yet, oldest first."""
    done = {(r["source_file"], r["file_sha256"]) for r in read_rows(state_path(data_dir, source))}
    loaded = loaded_batches(data_dir, source)
    return sorted(
        (name, info["sha"]) for name, info in loaded.items() if (name, info["sha"]) not in done
    )


def record_processed(
    data_dir: Path, source: Source, files: list[tuple[str, str]], replace: bool = False
) -> None:
    """Remember the batches silver has now seen. ``replace`` is for a full rebuild."""
    now = datetime.now(UTC).replace(microsecond=0)
    rows = [{"source_file": n, "file_sha256": s, "processed_at": now} for n, s in files]
    if rows or replace:
        table = pa.Table.from_pylist(rows, schema=STATE_SCHEMA)
        write(state_path(data_dir, source), table, "overwrite" if replace else "append")
