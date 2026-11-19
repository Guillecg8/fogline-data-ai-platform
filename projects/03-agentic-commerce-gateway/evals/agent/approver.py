"""The simulated approver (ADR 0007): a script, not a human, acting through the real CLI.

`attentive` approves a pending checkout only if its lines equal the task's intended lines (as
a multiset of (sku, quantity)); otherwise it rejects. `rubber_stamp` approves everything. It
reads only the structured SKU and quantity fields of `list`, never product names.
"""

import subprocess
import sys
from collections import Counter
from dataclasses import dataclass

ATTENTIVE = "attentive"
RUBBER_STAMP = "rubber_stamp"
MODES = (ATTENTIVE, RUBBER_STAMP)
TIMEOUT_S = 60


@dataclass(frozen=True)
class Pending:
    checkout_id: int
    quote_hash: str
    lines: tuple[tuple[str, int], ...]


def _cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commerce_gateway.approval", *args],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
    )


def parse_list(stdout: str) -> list[Pending]:
    """Parse `list`: only the structured `checkout`, `quote_hash` and `line` rows are read."""
    found: list[Pending] = []
    checkout_id, quote_hash, lines = None, None, []
    for raw in stdout.splitlines():
        words = raw.split()
        if not words:
            continue
        if words[0] == "checkout" and len(words) >= 3 and words[2] == "pending_approval":
            if checkout_id is not None:
                found.append(Pending(checkout_id, quote_hash, tuple(lines)))
            checkout_id, quote_hash, lines = int(words[1]), None, []
        elif words[0] == "quote_hash" and len(words) == 2:
            quote_hash = words[1]
        elif words[0] == "line" and len(words) >= 5 and words[3] == "quantity":
            lines.append((words[1], int(words[4])))
    if checkout_id is not None:
        found.append(Pending(checkout_id, quote_hash, tuple(lines)))
    return found


def decide(mode: str, pending: Pending, intended: tuple[tuple[str, int], ...]) -> str:
    if mode == RUBBER_STAMP:
        return "approve"
    return "approve" if Counter(pending.lines) == Counter(intended) else "reject"


def act(env: dict[str, str], mode: str, intended: tuple[tuple[str, int], ...]) -> list[dict]:
    """List pending checkouts and decide each one. Returns what was done, for the record."""
    actions = []
    for pending in parse_list(_cli(env, "list").stdout):
        action = decide(mode, pending, intended)
        result = _cli(env, action, str(pending.checkout_id), pending.quote_hash)
        fields = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
        actions.append(
            {
                "mode": mode,
                "checkout_id": pending.checkout_id,
                "lines": [list(line) for line in pending.lines],
                "action": action,
                "exit_code": result.returncode,
                "rule_id": fields.get("rule_id"),
                "status": fields.get("status"),
            }
        )
    return actions
