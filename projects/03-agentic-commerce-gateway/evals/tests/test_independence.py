"""The judge shares no code with the judged (ADR 0006)."""

import ast
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
ORACLE_ALLOWED = {"hashlib", "json", "sqlite3", "collections", "dataclasses", "pathlib", "typing"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").split(".")[0])
    return found


def test_the_oracle_imports_only_the_allowed_standard_library():
    imports = _imports(EVALS / "oracle.py")
    assert imports <= ORACLE_ALLOWED
    assert imports <= set(sys.stdlib_module_names)


@pytest.mark.parametrize("path", sorted(EVALS.rglob("*.py")), ids=lambda path: path.name)
def test_no_evals_module_imports_the_gateway(path):
    assert "commerce_gateway" not in _imports(path)


def test_the_oracle_hashes_a_mandate_as_the_gateway_does():
    """Same JSON-syntax file, two independent implementations, one content hash."""
    import subprocess

    import oracle

    twin = EVALS.parent / "mandates" / "example.json"
    result = subprocess.run(
        [sys.executable, "-m", "commerce_gateway.approval", "check-mandate", str(twin)],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    gateway_hash = result.stdout.splitlines()[0].split(" ", 1)[1]
    assert oracle.sha256_hex(oracle.load_mandate_file(twin)) == gateway_hash


@pytest.mark.parametrize("first", "0123456789abcdef")
def test_the_self_test_copy_of_a_session_never_collides(first):
    """Regression: the copy's id was 'f' + id[1:], equal to the original 1 time in 16."""
    import oracle_selftest

    session_id = first + "a" * 31
    copy = oracle_selftest.other_session_id(session_id)
    assert copy != session_id and len(copy) == len(session_id)
