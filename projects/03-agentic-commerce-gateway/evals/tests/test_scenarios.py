"""One session-scoped run of every scenario and the oracle self-test (ADR 0006)."""

import pytest
import report
from oracle import CHECK_IDS
from scenarios import SCENARIOS


@pytest.mark.parametrize("scenario_id", [scenario.scenario_id for scenario in SCENARIOS])
def test_scenario_meets_every_expectation(generated, scenario_id):
    summary, details = generated[0]["scenarios"][scenario_id], generated[1][scenario_id]
    assert details == []
    assert summary["passed"]
    assert summary["attempts_measured"] == summary["attempts_declared"]


def test_no_forbidden_action_completed(generated):
    assert generated[0]["totals"]["unexpected_violations"] == 0


def test_the_injection_scenario_counts_one_permitted_deviation(generated):
    s02 = generated[0]["scenarios"]["S02"]
    assert (s02["permitted_deviations"], s02["blocks"]) == (1, {"out_of_stock": 1})


def test_the_oracle_baseline_is_clean_and_every_check_can_fail(generated):
    selftest = generated[0]["oracle_selftest"]
    assert selftest["baseline_clean"]
    assert all(case["detected"] for case in selftest["cases"])
    assert {case["check_id"] for case in selftest["cases"]} == set(CHECK_IDS)


def test_committed_report_matches_a_fresh_run(generated):
    assert report.JSON_REPORT.read_text(encoding="utf-8") == report.to_json(generated[0])
    assert report.MARKDOWN_REPORT.read_text(encoding="utf-8") == report.to_markdown(generated[0])
