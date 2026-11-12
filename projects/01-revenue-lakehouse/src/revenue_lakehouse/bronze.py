"""Bronze layer: land raw batches in Delta tables, and stop the ones that break the contract.

- ``bronze/pms_reservation_events``: every accepted line, untouched (``_payload``), plus load
  metadata. Keeping the raw line means a schema change at the source cannot break this layer.
- ``ops/batch_ledger``: one entry per decision about a batch file (loaded or quarantined and
  why). The newest entry for a file is its current state.
- Quarantine is a state in the ledger, not a copy of the data: the file stays in the landing
  zone and is evaluated again whenever a newer contract is published (a *replay*).

Ingestion is idempotent: running it twice loads nothing twice. A file that was loaded and
later changes under the same name is an error, because bronze is immutable.

    uv run poe lh-ingest
"""

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa

from revenue_lakehouse.contract import LATEST_CONTRACT, compare_batch, load_contract
from revenue_lakehouse.delta_io import read_rows, write
from revenue_lakehouse.generate import FILE_PREFIX
from revenue_lakehouse.paths import DATA_DIR

BRONZE_SCHEMA = pa.schema(
    [
        ("_batch_date", pa.date32()),
        ("_source_file", pa.string()),
        ("_file_sha256", pa.string()),
        ("_line_no", pa.int64()),
        ("_ingested_at", pa.timestamp("us", tz="UTC")),
        ("_contract_version", pa.int32()),
        ("_row_errors", pa.string()),
        ("_payload", pa.string()),
    ]
)
LEDGER_SCHEMA = pa.schema(
    [
        ("seq", pa.int64()),
        ("source_file", pa.string()),
        ("batch_date", pa.date32()),
        ("file_sha256", pa.string()),
        ("rows", pa.int64()),
        ("bad_rows", pa.int64()),
        ("status", pa.string()),
        ("action", pa.string()),
        ("contract_version", pa.int32()),
        ("reasons", pa.string()),
        ("processed_at", pa.timestamp("us", tz="UTC")),
    ]
)


class ChangedFileError(RuntimeError):
    """A batch that is already in bronze arrived again with different content."""


@dataclass
class IngestReport:
    contract_version: int
    loaded: list[str] = field(default_factory=list)
    quarantined: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    rows: int = 0
    bad_rows: int = 0


def bronze_path(data_dir: Path) -> Path:
    return data_dir / "bronze" / "pms_reservation_events"


def ledger_path(data_dir: Path) -> Path:
    return data_dir / "ops" / "batch_ledger"


def loaded_batches(data_dir: Path) -> dict[str, dict]:
    """File name -> sha, rows and contract version, taken from bronze itself."""
    rows = read_rows(
        bronze_path(data_dir),
        "select _source_file, _file_sha256, max(_contract_version) as contract_version, "
        "count(*) as n from t group by _source_file, _file_sha256",
    )
    return {
        row["_source_file"]: {
            "sha": row["_file_sha256"],
            "rows": row["n"],
            "contract_version": row["contract_version"],
        }
        for row in rows
    }


def last_ledger_entries(data_dir: Path) -> tuple[dict[str, dict], int]:
    entries: dict[str, dict] = {}
    last = 0
    for row in sorted(read_rows(ledger_path(data_dir)), key=lambda r: r["seq"]):
        entries[row["source_file"]] = row
        last = row["seq"]
    return entries, last


def parse_lines(raw: bytes) -> tuple[list[str], dict[int, dict], dict[int, str]]:
    """Non-empty lines, the ones that are JSON objects, and why the others are not."""
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    objects, errors = {}, {}
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            errors[index] = "json:invalid"
            continue
        if isinstance(value, dict):
            objects[index] = value
        else:
            errors[index] = "json:not_an_object"
    return lines, objects, errors


def ledger_row(seq: int, name: str, sha: str, now: datetime, **values) -> dict:
    batch = date.fromisoformat(name.removeprefix(FILE_PREFIX).removesuffix(".jsonl"))
    return {
        "seq": seq,
        "source_file": name,
        "batch_date": batch,
        "file_sha256": sha,
        "rows": 0,
        "bad_rows": 0,
        "reasons": "",
        "processed_at": now,
        **values,
    }


def ingest(
    landing: Path,
    data_dir: Path = DATA_DIR,
    contract_version: int = LATEST_CONTRACT,
    now: datetime | None = None,
) -> IngestReport:
    contract = load_contract(contract_version)
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    report = IngestReport(contract_version)

    loaded = loaded_batches(data_dir)
    ledger, seq = last_ledger_entries(data_dir)
    bronze_rows: list[dict] = []
    new_entries: list[dict] = []

    for path in sorted(landing.glob(f"{FILE_PREFIX}*.jsonl")):
        name = path.name
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        entry = ledger.get(name)

        if name in loaded:
            if loaded[name]["sha"] != sha:
                raise ChangedFileError(f"{name} is already in bronze with different content")
            if entry is None or entry["action"] != "loaded":
                # bronze was written but the ledger was not: complete the record
                seq += 1
                new_entries.append(
                    ledger_row(
                        seq,
                        name,
                        sha,
                        now,
                        rows=loaded[name]["rows"],
                        status="repaired",
                        action="loaded",
                        contract_version=loaded[name]["contract_version"],
                        reasons="ledger entry rebuilt from bronze",
                    )
                )
                report.repaired.append(name)
            else:
                report.skipped.append(name)
            continue

        if (
            entry is not None
            and entry["file_sha256"] == sha
            and entry["contract_version"] >= contract.version
        ):
            report.skipped.append(name)  # still quarantined and no newer contract to try
            continue

        lines, objects, errors = parse_lines(raw)
        batch = compare_batch(contract, list(objects.values()))
        seq += 1
        if batch.status == "breaking":
            reasons = batch.summary()
            report.quarantined[name] = reasons
            new_entries.append(
                ledger_row(
                    seq,
                    name,
                    sha,
                    now,
                    rows=len(lines),
                    status="breaking",
                    action="quarantined",
                    contract_version=contract.version,
                    reasons=reasons,
                )
            )
            continue

        indexes = list(objects)
        for position, reasons in batch.bad_rows.items():
            errors[indexes[position]] = ",".join(reasons)
        batch_date = date.fromisoformat(name.removeprefix(FILE_PREFIX).removesuffix(".jsonl"))
        for index, line in enumerate(lines):
            bronze_rows.append(
                {
                    "_batch_date": batch_date,
                    "_source_file": name,
                    "_file_sha256": sha,
                    "_line_no": index + 1,
                    "_ingested_at": now,
                    "_contract_version": contract.version,
                    "_row_errors": errors.get(index),
                    "_payload": line,
                }
            )
        new_entries.append(
            ledger_row(
                seq,
                name,
                sha,
                now,
                rows=len(lines),
                bad_rows=len(errors),
                status=batch.status,
                action="loaded",
                contract_version=contract.version,
                reasons=batch.summary(),
            )
        )
        report.loaded.append(name)
        report.rows += len(lines)
        report.bad_rows += len(errors)

    # bronze first, ledger second: if we stop in between, the next run repairs the ledger
    if bronze_rows:
        write(bronze_path(data_dir), pa.Table.from_pylist(bronze_rows, schema=BRONZE_SCHEMA))
    if new_entries:
        write(ledger_path(data_dir), pa.Table.from_pylist(new_entries, schema=LEDGER_SCHEMA))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--landing", type=Path, default=DATA_DIR / "landing" / "pms")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--contract", type=int, default=LATEST_CONTRACT)
    args = parser.parse_args()
    if not args.landing.exists():
        raise SystemExit(f"{args.landing} not found: run uv run poe lh-generate first")

    report = ingest(args.landing, args.data_dir, args.contract)
    print(
        f"contract v{report.contract_version}: {len(report.loaded)} batches loaded "
        f"({report.rows} rows, {report.bad_rows} bad rows), {len(report.quarantined)} quarantined, "
        f"{len(report.skipped)} skipped, {len(report.repaired)} repaired"
    )
    for reason in sorted(set(report.quarantined.values())):
        names = [name for name, why in report.quarantined.items() if why == reason]
        print(f"  quarantined {len(names)} batches ({names[0]} ...): {reason}")


if __name__ == "__main__":
    main()
