"""The evals live outside the package and are run as scripts, so their directory is put on the
import path here. The scenarios and the self-test run once per test session."""

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    import report

    return report.generate(tmp_path_factory.mktemp("evals"))
