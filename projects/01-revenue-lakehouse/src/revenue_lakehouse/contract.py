"""Data contracts for incoming batches.

A contract says which columns a source must send and what type each one has. Comparing a
batch with its contract tells us whether it can go on (``ok``), can go on with a warning
(``compatible``: new columns) or must be stopped (``breaking``).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from revenue_lakehouse.paths import CONTRACTS_DIR

Status = Literal["ok", "compatible", "breaking"]
# A column is "retyped" when at least this share of its values has the wrong type.
RETYPED_SHARE = 0.5


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "number", "date", "timestamp"]
    required: bool = True
    allowed: list[str] | None = None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    version: int
    format: Literal["jsonl"]
    primary_key: list[str]
    columns: dict[str, ColumnSpec]


@dataclass
class BatchReport:
    status: Status
    rows: int
    missing: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    retyped: list[str] = field(default_factory=list)
    bad_rows: dict[int, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"missing {', '.join(self.missing)}")
        if self.retyped:
            parts.append(f"retyped {', '.join(self.retyped)}")
        if self.added:
            parts.append(f"new {', '.join(self.added)}")
        if self.bad_rows and self.status == "ok":
            parts.append(f"{len(self.bad_rows)} bad rows")
        return "; ".join(parts) or "-"


def load_contract(
    name: str = "pms_reservation_events", directory: Path = CONTRACTS_DIR
) -> Contract:
    text = (directory / f"{name}.yaml").read_text(encoding="utf-8")
    return Contract.model_validate(yaml.safe_load(text))


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def has_type(value: Any, kind: str) -> bool:
    """bool is excluded from numbers on purpose: True is an int in Python, not in JSON."""
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "date":
        return isinstance(value, str) and _is_date(value)
    if kind == "timestamp":
        return isinstance(value, str) and _is_timestamp(value)
    return False


def check_row(contract: Contract, row: dict[str, Any]) -> list[str]:
    """Reasons why one row breaks the contract. Empty list means the row is valid."""
    reasons = []
    for name, spec in contract.columns.items():
        value = row.get(name)
        if value is None:
            if spec.required:
                reasons.append(f"{name}:null")
        elif not has_type(value, spec.type):
            reasons.append(f"{name}:type")
        elif spec.allowed is not None and value not in spec.allowed:
            reasons.append(f"{name}:value")
    return reasons


def compare_batch(contract: Contract, rows: list[dict[str, Any]]) -> BatchReport:
    present = {key for row in rows for key in row}
    missing = sorted(
        name for name, spec in contract.columns.items() if spec.required and name not in present
    )
    added = sorted(present - set(contract.columns))

    retyped = []
    for name, spec in contract.columns.items():
        values = [row[name] for row in rows if row.get(name) is not None]
        if values:
            wrong = sum(not has_type(value, spec.type) for value in values)
            if wrong / len(values) >= RETYPED_SHARE:
                retyped.append(name)

    bad_rows = {}
    for index, row in enumerate(rows):
        reasons = check_row(contract, row)
        if reasons:
            bad_rows[index] = reasons

    if missing or retyped:
        status: Status = "breaking"
    elif added:
        status = "compatible"
    else:
        status = "ok"
    return BatchReport(status, len(rows), missing, added, retyped, bad_rows)
