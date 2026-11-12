"""Quality gates over the saved eval results (docs/decisions/0001).

CI has no language model, so these tests read the numbers that were measured locally and
committed in ``evals/results``. The thresholds sit below the measured values, with margin,
so ordinary noise does not break the build but a real regression does. The freshness test
in ``test_evals.py`` guarantees the saved numbers belong to the current prompt, data and code.
"""

import json

import pytest

from ops_copilot.evals import run as eval_run
from ops_copilot.generation.llm import DEFAULT_MODEL

RETRIEVAL_HYBRID = {"recall_at_6": 0.95, "mrr": 0.90}

# split -> (minimum pass rate, maximum wrong-answer rate) for the default configuration
GENERATION = {
    "dev": (0.85, 0.05),
    "holdout": (0.75, 0.15),
}


def load(name: str) -> dict:
    path = eval_run.RESULTS_DIR / f"{name}.json"
    assert path.exists(), f"{path.name} is missing: run uv run poe eval-all and commit the results"
    return json.loads(path.read_text(encoding="utf-8"))


def test_hybrid_retrieval_meets_the_gate():
    hybrid = load("retrieval")["modes"]["hybrid"]
    assert hybrid["recall_at_6"] >= RETRIEVAL_HYBRID["recall_at_6"], hybrid
    assert hybrid["mrr"] >= RETRIEVAL_HYBRID["mrr"], hybrid


@pytest.mark.parametrize("split", sorted(GENERATION))
def test_default_configuration_meets_the_answer_gate(split):
    min_pass, max_wrong = GENERATION[split]
    name = eval_run.result_name(DEFAULT_MODEL, eval_run.BASELINE, split)
    summary = load(name)["summary"]
    assert summary["pass_rate"] >= min_pass, (name, summary["pass_rate"])
    assert summary["wrong_answer_rate"] <= max_wrong, (name, summary["wrong_answer_rate"])
