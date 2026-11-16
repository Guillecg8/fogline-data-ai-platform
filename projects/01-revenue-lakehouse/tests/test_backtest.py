import json
from datetime import date, timedelta

import numpy as np
import pytest

from revenue_lakehouse.backtest import (
    BASELINES,
    METHODS,
    SeriesRun,
    backtest,
    block_indices,
    closure_origins,
    floor_reference,
    horizon_wape,
    lift_on,
    mae,
    origin_calendar,
    pooled_wape,
    run_series,
    volume_shares,
    wape,
    weights_note,
    write_outputs,
)
from revenue_lakehouse.forecast_series import Series
from revenue_lakehouse.generate_pos import VOID_RATE, expected_covers

START = date(2025, 11, 1)  # the first Gold day
PATTERN = np.array([100.0, 90.0, 95.0, 110.0, 150.0, 180.0, 130.0])
SHORT = 170  # six origins: 120, 127, ..., 155


def covers_series(days: int = SHORT, venue: str = "lumen", seed: int = 7) -> Series:
    rng = np.random.default_rng(seed)
    values = PATTERN[np.arange(days) % 7] * rng.normal(1.0, 0.05, days)
    return Series(venue, "covers", START, values)


def occupancy_series(days: int = SHORT, seed: int = 8) -> Series:
    values = np.clip(covers_series(days, seed=seed).values / 200.0, 0.0, 1.0)
    return Series("fogline-marina", "occupancy", START, values)


# --- (1) end-to-end no leakage ----------------------------------------------------------------


@pytest.mark.parametrize("garbage", ["times_ten", "nan"])
def test_an_origin_forecast_is_identical_whatever_comes_after_it(garbage):
    clean = covers_series()
    origin = 134  # a middle origin: later origins still train on the garbage
    values = clean.values.copy()
    values[origin + 1 :] = values[origin + 1 :] * 10 if garbage == "times_ten" else np.nan
    dirty = Series(clean.venue, clean.target, clean.start, values)

    before, after = run_series(clean), run_series(dirty)

    assert before.origins == after.origins
    for i, s in enumerate(before.origins):
        if s > origin:
            continue
        for method in METHODS:
            assert np.array_equal(before.forecasts[method][i], after.forecasts[method][i])
        assert np.array_equal(before.reference[i], after.reference[i])
    later = before.origins.index(origin) + 1
    assert not np.array_equal(before.forecasts["model"][later], after.forecasts["model"][later])


# --- (2) metrics ------------------------------------------------------------------------------


def test_mae_and_wape_match_hand_computed_values():
    actual = np.array([10.0, 20.0, 30.0, 40.0])
    forecast = np.array([12.0, 18.0, 33.0, 40.0])  # errors 2, 2, 3, 0
    assert mae(actual, forecast) == pytest.approx(7 / 4)
    assert wape(actual, forecast) == pytest.approx(7 / 100)


def test_lift_uses_each_series_own_best_baseline_on_the_picked_origins():
    errors = {  # two series, three origins
        "last_same_weekday": np.array([[1.0, 1.0, 9.0], [5.0, 5.0, 5.0]]),
        "mean4_same_weekday": np.array([[2.0, 2.0, 2.0], [4.0, 4.0, 4.0]]),
        "model": np.array([[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]]),
    }
    # all origins: series 0 best = mean4 (6 < 11), series 1 best = mean4 (12 < 15)
    assert lift_on(errors, np.arange(3)) == pytest.approx(1 - 12 / 18)
    # first two origins: series 0 best = last (2 < 4), series 1 still mean4 (8 < 10)
    assert lift_on(errors, np.array([0, 1])) == pytest.approx(1 - 8 / 10)
    # an origin drawn twice counts twice
    assert lift_on(errors, np.array([2, 2])) == pytest.approx(1 - 8 / 12)


def test_block_resample_draws_runs_of_consecutive_origins():
    picks = block_indices(33, 4, np.random.default_rng(0))
    assert len(picks) == 33
    assert picks.min() >= 0 and picks.max() <= 32
    for start in range(0, 32, 4):
        run = picks[start : start + 4]
        assert np.array_equal(np.diff(run), np.ones(len(run) - 1))


# --- (3) origin calendar ----------------------------------------------------------------------


def test_the_gold_year_has_33_origins_from_120_to_344():
    origins = origin_calendar(365)
    assert len(origins) == 33
    assert origins[0] == 120 and origins[-1] == 344
    assert set(np.diff(origins)) == {7}
    assert origins[-1] + 14 <= 364 < origins[-1] + 7 + 14  # last with a full future
    assert START + timedelta(days=120) == date(2026, 3, 1)
    assert START + timedelta(days=344) == date(2026, 10, 11)


def test_a_series_too_short_for_one_origin_is_rejected():
    with pytest.raises(ValueError, match="too short"):
        run_series(covers_series(134))


# --- (4) determinism --------------------------------------------------------------------------


def test_the_results_file_is_identical_on_every_run(tmp_path):
    series = [occupancy_series(), covers_series()]
    first, second = tmp_path / "first", tmp_path / "second"
    for out in (first, second):
        out.mkdir()
        write_outputs(backtest(series), out)

    def load(out):
        results = json.loads((out / "backtest_results.json").read_text(encoding="utf-8"))
        results.pop("generated_at")
        return results

    assert load(first) == load(second)
    results = load(first)
    assert results["config"]["origins"] == [120, 127, 134, 141, 148, 155]
    assert set(results["targets"]) == {"occupancy", "covers"}
    assert len(results["per_origin"]["lumen/covers"]) == 6
    for block in ("2", "4"):
        low, high = results["series"]["lumen/covers"]["lift_ci95"][block]
        assert low <= high
    report = (first / "BACKTEST.md").read_text(encoding="utf-8")
    assert "## Hotel occupancy" in report and "## Restaurant covers" in report
    assert report.count("By horizon") == 2 and report.count("Without the 0 origins") == 1


# --- (5) floor reference ----------------------------------------------------------------------


def test_the_floor_reference_is_the_generator_mean_net_of_voids():
    saltgrass = Series("saltgrass", "covers", START, np.zeros(365))
    origin = 334  # 2026-10-01, a Thursday; its horizon holds two closed Sundays
    floor = floor_reference(saltgrass, origin)
    days = [saltgrass.date_at(origin + h) for h in range(1, 15)]
    assert np.array_equal(
        floor, np.array([expected_covers("saltgrass", d) * (1 - VOID_RATE) for d in days])
    )
    closed = [i for i, d in enumerate(days) if d.weekday() == 6]
    assert [days[i] for i in closed] == [date(2026, 10, 4), date(2026, 10, 11)]
    assert (floor[closed] == 0).all() and (np.delete(floor, closed) > 0).all()


def test_the_floor_reference_is_for_restaurants_only():
    results = backtest([occupancy_series(), covers_series()])
    assert "floor_reference" not in results["series"]["fogline-marina/occupancy"]
    assert "floor_reference" not in results["targets"]["occupancy"]
    assert "floor_reference" in results["targets"]["covers"]
    assert set(results["series"]["lumen/covers"]["best_baseline"].values()) <= set(BASELINES)


# --- pre-declared slices (ADR 0002, amendment 3) -----------------------------------------------


def hand_run(name: str, actual: np.ndarray, forecasts: dict[str, np.ndarray]) -> SeriesRun:
    venue, target = name.split("/")
    origins = list(range(120, 120 + 7 * len(actual), 7))
    series = Series(venue, target, START, np.zeros(400))
    return SeriesRun(series, origins, actual, forecasts, None)


def test_the_closure_slice_drops_origins_323_330_337_344():
    gold_year = Series("saltgrass", "covers", START, np.zeros(365))
    excluded = closure_origins(gold_year, origin_calendar(365))
    assert excluded == [323, 330, 337, 344]
    assert gold_year.date_at(323 + 14) == date(2026, 10, 4)  # first horizon that reaches it
    assert gold_year.date_at(316 + 14) == date(2026, 9, 27)  # the origin before does not


def test_horizon_buckets_split_days_1_7_and_8_14():
    actual = np.full((1, 14), 10.0)
    model = actual + np.array([1.0] * 7 + [-3.0] * 7)  # 7 / 70 and 21 / 70
    run = hand_run("lumen/covers", actual, {"model": model})
    assert horizon_wape(run, "model") == pytest.approx({"1-7": 0.1, "8-14": 0.3})


def test_pooled_wape_on_kept_origins_and_volume_weights():
    big = hand_run(
        "lumen/covers",
        np.full((2, 14), 20.0),  # 280 per origin
        {"model": np.vstack([np.full(14, 22.0), np.full(14, 20.0)])},  # errors 28, 0
    )
    small = hand_run(
        "saltgrass/covers",
        np.full((2, 14), 10.0),  # 140 per origin
        {"model": np.vstack([np.full(14, 10.0), np.full(14, 0.0)])},  # errors 0, 140
    )
    runs = [big, small]
    assert pooled_wape(runs, "model", np.array([True, True])) == pytest.approx(168 / 840)
    assert pooled_wape(runs, "model", np.array([True, False])) == pytest.approx(28 / 420)
    assert volume_shares(runs) == pytest.approx({"lumen/covers": 2 / 3, "saltgrass/covers": 1 / 3})


def test_the_report_states_the_pooled_weights_it_computed():
    note = weights_note({"lumen/covers": 2 / 3, "saltgrass/covers": 1 / 3})[0]
    assert "lumen/covers 67%, saltgrass/covers 33%" in note
    assert "lumen/covers counts 2.0 times as much as saltgrass/covers" in note
    equal = weights_note({"fogline-marina/occupancy": 0.505, "fogline-mission/occupancy": 0.495})
    assert "counts 1.0 times as much as" in equal[0]
    assert weights_note({"lumen/covers": 1.0}) == []
