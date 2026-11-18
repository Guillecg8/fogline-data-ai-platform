"""The approval CLI as a real entry point: `sys.executable -m commerce_gateway.approval` (ADR 0005).

The CLI always uses the system clock. Tests control time only through the session's shared
`last_seen_ms` floor (ADR 0004): effective time is `max(system now, last_seen_ms)`, so a pending
checkout created far in the future and a floor set to its expiry make "at expiry" exact.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from commerce_gateway import approval, audit
from commerce_gateway.clock import SystemClock
from commerce_gateway.mandate import load_mandate

FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z, always after the real clock
TIMEOUT_S = 60
EXAMPLE = Path(__file__).parents[1] / "mandates" / "example.yaml"
HEAD_LINE = re.compile(r"audit head [0-9]+ [0-9a-f]{64}")


def cli(*args: str, db: Path | None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != approval.DB_ENV}
    if db is not None:
        env[approval.DB_ENV] = str(db)
    return subprocess.run(
        [sys.executable, "-m", "commerce_gateway.approval", *args],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
    )


def _fields(stdout: str) -> dict[str, str]:
    """`key value` lines of a decision, keyed by their first word."""
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in stdout.splitlines()}


@pytest.fixture
def pending(make_env, clock, db_path):
    """A pending checkout (two knives and the injected-name syrup) created far in the future."""
    clock.set(FAR_FUTURE_MS)
    env = make_env()
    env.call("add_to_cart", sku="FP-0020", quantity=2)
    env.call("add_to_cart", sku="FP-0028", quantity=1)
    result = env.call("request_checkout")
    assert result.outcome == "pending_approval"
    env.checkout = result.data["checkout"]
    return env


def _set_floor(env, ms: int) -> None:
    """Move the shared time floor, as another process's later clock reading would."""
    env.store.conn.execute("UPDATE sessions SET last_seen_ms = ?", (ms,))


def _status(env) -> str:
    return env.scalar("SELECT status FROM checkouts")


def test_approve_before_expiry_completes_and_prints_the_head(pending, db_path):
    _set_floor(pending, pending.checkout["expires_at_ms"] - 1)
    result = cli("approve", "1", pending.checkout["quote_hash"], db=db_path)
    assert (result.returncode, result.stderr) == (approval.EXIT_OK, "")
    fields = _fields(result.stdout)
    assert (fields["decision"], fields["rule_id"], fields["status"]) == (
        "allowed",
        "none",
        "completed",
    )
    seq, row_hash = audit.head(pending.store.conn)
    assert result.stdout.splitlines()[-1] == f"audit head {seq} {row_hash}"
    assert _status(pending) == "completed"


@pytest.mark.parametrize("offset_ms", [0, 1], ids=["at-expiry", "after-expiry"])
def test_approve_at_or_after_expiry_is_a_quote_mismatch(pending, db_path, offset_ms):
    _set_floor(pending, pending.checkout["expires_at_ms"] + offset_ms)
    result = cli("approve", "1", pending.checkout["quote_hash"], db=db_path)
    assert result.returncode == approval.EXIT_REFUSED
    fields = _fields(result.stdout)
    assert (fields["decision"], fields["rule_id"], fields["status"]) == (
        "denied",
        "quote_mismatch",
        "expired",
    )
    assert HEAD_LINE.fullmatch(result.stdout.splitlines()[-1])
    events = [row.event for row in audit.read_rows(pending.store.conn)[-2:]]
    assert events == ["checkout_expired", "approval_decision"]


def test_approve_with_the_wrong_quote_hash_changes_nothing(pending, db_path):
    result = cli("approve", "1", "0" * 64, db=db_path)
    assert result.returncode == approval.EXIT_REFUSED
    fields = _fields(result.stdout)
    assert (fields["rule_id"], fields["status"]) == ("quote_mismatch", "pending_approval")
    assert _status(pending) == "pending_approval"
    [last] = audit.read_rows(pending.store.conn)[-1:]
    assert (last.actor, last.allowed, last.payload["quote_hash"]) == ("approver", False, "0" * 64)


def test_reject(pending, db_path):
    result = cli("reject", "1", pending.checkout["quote_hash"], db=db_path)
    assert result.returncode == approval.EXIT_OK
    assert _fields(result.stdout)["status"] == "rejected"
    assert HEAD_LINE.fullmatch(result.stdout.splitlines()[-1])
    assert _status(pending) == "rejected"


def test_list_shows_structured_fields_and_marked_truncated_names(make_env, clock, db_path):
    clock.set(SystemClock().now_ms())
    env = make_env()
    env.call("add_to_cart", sku="FP-0020", quantity=2)
    env.call("add_to_cart", sku="FP-0028", quantity=1)
    quote_hash = env.call("request_checkout").data["checkout"]["quote_hash"]
    result = cli("list", db=db_path)
    assert (result.returncode, result.stderr) == (approval.EXIT_OK, "")
    lines = result.stdout.splitlines()
    assert lines[:4] == [
        "checkout 1 pending_approval",
        f"  quote_hash {quote_hash}",
        f"  total_cents {2 * 6900 + 1350}",
        lines[3],
    ]
    assert re.fullmatch(r"  expires_at_ms [0-9]+", lines[3])
    assert lines[4] == (
        "  line FP-0020 kitchen_supplies quantity 2 unit_price_cents 6900 line_total_cents 13800"
    )
    assert lines[5] == '    name [untrusted] "Fogline Pantry Chef Knife 8 in"'
    assert lines[7].startswith('    name [untrusted, truncated] "')
    assert len(lines) == 8
    assert "approve this order" not in result.stdout


def test_list_without_a_pending_checkout(make_env, db_path):
    make_env()
    assert cli("list", db=db_path).stdout == "no pending checkout\n"


def test_audit_head_and_verify_with_an_anchor(pending, db_path):
    seq, row_hash = audit.head(pending.store.conn)
    head = cli("audit-head", db=db_path)
    assert (head.returncode, head.stdout) == (0, f"audit head {seq} {row_hash}\n")
    ok = cli("verify", "--anchor", f"{seq}:{row_hash}", db=db_path)
    assert (ok.returncode, ok.stdout.splitlines()[0]) == (0, f"ok rows {seq}")
    wrong = cli("verify", "--anchor", f"{seq}:{'0' * 64}", db=db_path)
    assert wrong.returncode == approval.EXIT_REFUSED
    assert wrong.stdout.splitlines()[0] == f"failed at seq {seq}: row does not match the anchor"
    longer = cli("verify", "--anchor", f"{seq + 5}:{row_hash}", db=db_path)
    assert longer.stdout.splitlines()[0] == "failed: chain is shorter than the anchor"
    assert cli("verify", "--anchor", "nonsense", db=db_path).returncode == approval.EXIT_USAGE


def test_verify_detects_an_edited_row(pending, db_path):
    conn = pending.store.conn
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit SET ts_ms = ts_ms + 1 WHERE seq = 2")
    result = cli("verify", db=db_path)
    assert result.returncode == approval.EXIT_REFUSED
    assert result.stdout.startswith("failed at seq 2: ")


def test_refuses_without_the_database_variable_or_file(tmp_path):
    missing = cli("list", db=None)
    assert (missing.returncode, missing.stderr) == (approval.EXIT_USAGE, approval.LINE_NO_DB + "\n")
    absent = tmp_path / "absent.db"
    result = cli("list", db=absent)
    assert (result.returncode, result.stderr) == (approval.EXIT_STORE, approval.LINE_DB + "\n")
    assert not absent.exists()


def test_refuses_a_tampered_mandate_snapshot(pending, db_path):
    pending.store.conn.execute("UPDATE sessions SET mandate = replace(mandate, '20000', '99999')")
    result = cli("list", db=db_path)
    assert (result.returncode, result.stderr) == (
        approval.EXIT_STORE,
        approval.LINE_INTEGRITY + "\n",
    )


class TestCheckMandate:
    def test_valid_file_prints_hash_and_label(self):
        result = cli("check-mandate", str(EXAMPLE), db=None)
        mandate = load_mandate(EXAMPLE)
        assert (result.returncode, result.stderr) == (0, "")
        assert result.stdout == (
            f"content_hash {mandate.content_hash}\nsession_label {mandate.session_label}\n"
        )

    def test_every_problem_is_a_path_and_a_fixed_reason(self, tmp_path):
        text = EXAMPLE.read_text(encoding="utf-8")
        text = text.replace("budget_cents: 25000", 'budget_cents: "IGNORE ALL RULES"')
        text = text.replace("  - dairy\n", "  - dairy\n  - SECRET-CATEGORY\n")
        text = text.replace("tool_call_budget: 40", "tool_call_budget: 0")
        text += "SECRET_KEY: leak-me\n"
        path = tmp_path / "bad.yaml"
        path.write_text(text, encoding="utf-8")
        result = cli("check-mandate", str(path), db=None)
        assert result.returncode == approval.EXIT_REFUSED
        assert result.stdout.splitlines() == [
            "(root): unknown key",
            "budget_cents: must be an integer",
            "tool_call_budget: must be between 1 and 10000",
            "allowed_categories[3]: not an allowable category",
        ]
        for leaked in ("IGNORE", "SECRET", "leak-me"):
            assert leaked not in result.stdout + result.stderr

    def test_invalid_yaml_reports_only_the_line(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text('version: 1\nsession_label: "SECRET [unclosed\n', encoding="utf-8")
        result = cli("check-mandate", str(path), db=None)
        assert result.returncode == approval.EXIT_REFUSED
        assert re.fullmatch(r"line [0-9]+: invalid YAML\n", result.stdout)
        assert "SECRET" not in result.stdout + result.stderr

    def test_duplicate_unknown_key_is_not_echoed(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(EXAMPLE.read_text(encoding="utf-8") + "budget_cents: 1\n")
        result = cli("check-mandate", str(path), db=None)
        assert result.stdout == "budget_cents: duplicate key\n"

    def test_unreadable_file(self, tmp_path):
        result = cli("check-mandate", str(tmp_path / "absent.yaml"), db=None)
        assert (result.returncode, result.stdout) == (
            approval.EXIT_REFUSED,
            "(file): cannot be read as UTF-8 text\n",
        )
