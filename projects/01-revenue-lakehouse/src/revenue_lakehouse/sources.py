"""The data sources the lakehouse ingests. Adding a source means adding an entry here, a contract
file, and its silver/gold transformations; bronze, the ledger and the quarantine work unchanged."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    name: str  # also the bronze table name and the contract's ``source``
    landing_dir: str  # folder under data/landing
    file_prefix: str  # batch files are <prefix><YYYY-MM-DD>.jsonl
    contracts: dict[int, str]  # contract version -> file in contracts/

    @property
    def latest(self) -> int:
        return max(self.contracts)


PMS = Source(
    name="pms_reservation_events",
    landing_dir="pms",
    file_prefix="pms_reservation_events_",
    contracts={1: "pms_reservation_events.yaml", 2: "pms_reservation_events_v2.yaml"},
)
POS = Source(
    name="pos_checks",
    landing_dir="pos",
    file_prefix="pos_checks_",
    contracts={1: "pos_checks.yaml"},
)
SOURCES = (PMS, POS)
