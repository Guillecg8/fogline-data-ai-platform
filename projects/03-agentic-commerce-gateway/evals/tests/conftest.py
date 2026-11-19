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


FAKE_GIT = {"commit": "0" * 40, "dirty": False, "changed_paths": []}


@pytest.fixture(autouse=True)
def _no_real_git(request, monkeypatch):
    """Agent tests never read the real repository's git state (it changes while you work)."""
    if "real_git" in request.keywords:
        return
    import agent.runner

    monkeypatch.setattr(agent.runner, "git_state", lambda repo=None: dict(FAKE_GIT))
    monkeypatch.setattr(agent.runner, "git_tree_hash", lambda path, exclude=(), repo=None: "t" * 64)
