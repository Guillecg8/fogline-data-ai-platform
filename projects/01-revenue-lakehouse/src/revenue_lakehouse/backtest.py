"""Walk-forward backtest: does the global LightGBM model beat seasonal baselines on Gold demand?

For every series (hotel occupancy, restaurant covers) and every forecast origin ``s`` the harness
passes ``y[: s + 1]`` to each method and scores its 14-day forecast against ``y[s + 1 : s + 15]``.
It writes ``BACKTEST.md`` and ``backtest_results.json``. Design: ADR 0002 and its amendments.

    uv run poe lh-backtest
"""

import argparse
import json
import math
import os
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import lightgbm
import numpy as np

from revenue_lakehouse.forecast_models import (
    HORIZON,
    NUM_ROUNDS,
    PARAMS,
    baselines,
    model_forecast,
)
from revenue_lakehouse.forecast_series import Series, load_series
from revenue_lakehouse.generate_pos import (
    SATURDAY_SUNDAY_CLOSED_FROM,
    VOID_RATE,
    expected_covers,
)
from revenue_lakehouse.paths import DATA_DIR, PROJECT_DIR

FIRST_ORIGIN, STEP = 120, 7
REPLICATES, BLOCK_SIZES, SEED = 2000, (2, 4), 42
BASELINES = ("last_same_weekday", "mean4_same_weekday")
METHODS = (*BASELINES, "model")
TARGETS = ("occupancy", "covers")
HORIZON_BUCKETS = {"1-7": slice(0, 7), "8-14": slice(7, HORIZON)}  # pre-declared slice (a)


# --- forecasts --------------------------------------------------------------------------------


def origin_calendar(days: int) -> list[int]:
    """Origins from ``FIRST_ORIGIN`` every ``STEP`` days whose ``HORIZON`` target days exist."""
    return list(range(FIRST_ORIGIN, days - HORIZON, STEP))


def closure_origins(series: Series, origins: list[int]) -> list[int]:
    """Origins whose horizon reaches the Saltgrass Sunday closure (pre-declared slice (b))."""
    return [s for s in origins if series.date_at(s + HORIZON) >= SATURDAY_SUNDAY_CLOSED_FROM]


def floor_reference(series: Series, origin: int) -> np.ndarray:
    """Generator mean covers net of voids for the target days: a reference, not a forecast."""
    days = [series.date_at(origin + h) for h in range(1, HORIZON + 1)]
    return np.array([expected_covers(series.venue, day) * (1 - VOID_RATE) for day in days])


def forecast_at(series: Series, origin: int) -> dict[str, np.ndarray]:
    history = series.values[: origin + 1]  # the only demand data any method sees
    forecasts = baselines(history)
    forecasts["model"] = model_forecast(history, series.first_weekday, series.target)
    return forecasts


@dataclass
class SeriesRun:
    series: Series
    origins: list[int]
    actual: np.ndarray  # (origins, HORIZON)
    forecasts: dict[str, np.ndarray]  # method -> (origins, HORIZON)
    reference: np.ndarray | None  # restaurants only


def run_series(series: Series) -> SeriesRun:
    origins = origin_calendar(len(series.values))
    if not origins:
        raise ValueError(f"{series.name}: too short for a first origin at {FIRST_ORIGIN}")
    per_origin = [forecast_at(series, origin) for origin in origins]
    forecasts = {method: np.array([f[method] for f in per_origin]) for method in METHODS}
    actual = np.array([series.values[origin + 1 : origin + 1 + HORIZON] for origin in origins])
    reference = None
    if series.target == "covers":
        reference = np.array([floor_reference(series, origin) for origin in origins])
    return SeriesRun(series, origins, actual, forecasts, reference)


# --- metrics ----------------------------------------------------------------------------------


def mae(actual: np.ndarray, forecast: np.ndarray) -> float:
    return float(np.abs(forecast - actual).mean())


def wape(actual: np.ndarray, forecast: np.ndarray) -> float:
    return float(np.abs(forecast - actual).sum() / np.abs(actual).sum())


def pooled_wape(runs: list["SeriesRun"], method: str, keep: np.ndarray) -> float:
    """WAPE of ``method`` over several series, on the origins where ``keep`` is True."""
    errors = sum(np.abs(run.forecasts[method][keep] - run.actual[keep]).sum() for run in runs)
    return float(errors / sum(np.abs(run.actual[keep]).sum() for run in runs))


def horizon_wape(run: "SeriesRun", method: str) -> dict[str, float]:
    return {
        name: wape(run.actual[:, days], run.forecasts[method][:, days])
        for name, days in HORIZON_BUCKETS.items()
    }


def volume_shares(runs: list["SeriesRun"]) -> dict[str, float]:
    """Each series' share of the pooled WAPE denominator, i.e. its weight in the pooled number."""
    volumes = {run.series.name: float(np.abs(run.actual).sum()) for run in runs}
    total = sum(volumes.values())
    return {name: volume / total for name, volume in volumes.items()}


def origin_errors(runs: list[SeriesRun]) -> dict[str, np.ndarray]:
    """Method -> ``(series, origins)`` sum of absolute errors over the horizon."""
    return {
        method: np.array([np.abs(run.forecasts[method] - run.actual).sum(axis=1) for run in runs])
        for method in METHODS
    }


def lift_on(errors: dict[str, np.ndarray], picks: np.ndarray) -> float:
    """Model lift over each series' best baseline, chosen and scored on the origins ``picks``.

    Within a series every method shares the WAPE denominator, so the best baseline is the one
    with the smallest error sum, and pooled lift is ``1 - model errors / best-baseline errors``.
    """
    totals = {method: errors[method][:, picks].sum(axis=1) for method in METHODS}
    best = np.min([totals[name] for name in BASELINES], axis=0)
    return float(1 - totals["model"].sum() / best.sum())


def block_indices(origins: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """A moving-block resample of ``range(origins)``: random runs of ``block`` consecutive ones."""
    if block > origins:
        raise ValueError(f"block {block} is longer than the {origins} origins")
    starts = rng.integers(0, origins - block + 1, size=math.ceil(origins / block))
    return (starts[:, None] + np.arange(block)).ravel()[:origins]


def bootstrap_lift(errors: dict[str, np.ndarray], block: int) -> list[float]:
    """95% interval of the lift; the best baseline is re-selected inside every replicate."""
    origins = errors["model"].shape[1]
    rng = np.random.default_rng([SEED, block])
    lifts = [lift_on(errors, block_indices(origins, block, rng)) for _ in range(REPLICATES)]
    low, high = np.percentile(lifts, [2.5, 97.5])
    return [float(low), float(high)]


def summarize(runs: list[SeriesRun]) -> dict:
    """Metrics for one series, or for several series of the same target pooled together."""
    actual = np.concatenate([run.actual.ravel() for run in runs])
    stacked = {m: np.concatenate([run.forecasts[m].ravel() for run in runs]) for m in METHODS}
    errors = origin_errors(runs)
    everything = np.arange(len(runs[0].origins))
    best = [min(BASELINES, key=lambda name, i=i: errors[name][i].sum()) for i in range(len(runs))]
    best_errors = np.array([errors[name][i] for i, name in enumerate(best)])
    intervals = {str(block): bootstrap_lift(errors, block) for block in BLOCK_SIZES}
    headline = max(intervals, key=lambda block: intervals[block][1] - intervals[block][0])
    b_errors = errors["mean4_same_weekday"].sum()
    summary = {
        "series": [run.series.name for run in runs],
        "cells": int(actual.size),
        "mae": {m: mae(actual, stacked[m]) for m in METHODS},
        "wape": {m: wape(actual, stacked[m]) for m in METHODS},
        "best_baseline": {run.series.name: name for run, name in zip(runs, best, strict=True)},
        "lift_vs_best": lift_on(errors, everything),
        "lift_vs_mean4": float(1 - errors["model"].sum() / b_errors),
        "lift_ci95": intervals,
        "lift_ci95_headline_block": int(headline),
        "share_origins_model_beats_best": float(
            (errors["model"].sum(axis=0) < best_errors.sum(axis=0)).mean()
        ),
        "weights": volume_shares(runs),
    }
    if len(runs) == 1:
        summary["by_horizon"] = {
            "best_baseline": horizon_wape(runs[0], best[0]),
            "model": horizon_wape(runs[0], "model"),
        }
    if all(run.reference is not None for run in runs):
        reference = np.concatenate([run.reference.ravel() for run in runs])
        summary["floor_reference"] = {
            "mae": mae(actual, reference),
            "wape": wape(actual, reference),
            "bias": float((actual - reference).mean()),  # actual minus reference
        }
        excluded = closure_origins(runs[0].series, runs[0].origins)
        everyone = np.ones(len(runs[0].origins), dtype=bool)
        without = ~np.isin(runs[0].origins, excluded)
        summary["closure_slice"] = {
            "excluded_origins": excluded,
            "all_origins": {m: pooled_wape(runs, m, everyone) for m in METHODS},
            "without_closure": {m: pooled_wape(runs, m, without) for m in METHODS},
        }
    return summary


def per_origin(run: SeriesRun) -> list[dict]:
    rows = []
    for i, origin in enumerate(run.origins):
        row = {"origin": origin, "date": run.series.date_at(origin).isoformat()}
        for method in METHODS:
            actual, forecast = run.actual[i], run.forecasts[method][i]
            row[method] = {"mae": mae(actual, forecast), "wape": wape(actual, forecast)}
        if run.reference is not None:
            row["floor_reference"] = {"wape": wape(run.actual[i], run.reference[i])}
        rows.append(row)
    return rows


def backtest(series: list[Series]) -> dict:
    runs = [run_series(s) for s in series]
    if len({tuple(run.origins) for run in runs}) != 1:
        raise ValueError("all series must share one origin calendar")
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "environment": {
            "system": f"{platform.system()} {platform.machine()}",
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
            "lightgbm": lightgbm.__version__,
            "numpy": np.__version__,
        },
        "config": {
            "first_origin": FIRST_ORIGIN,
            "step": STEP,
            "horizon": HORIZON,
            "origins": runs[0].origins,
            "first_origin_date": runs[0].series.date_at(runs[0].origins[0]).isoformat(),
            "last_origin_date": runs[0].series.date_at(runs[0].origins[-1]).isoformat(),
            "bootstrap": {"replicates": REPLICATES, "block_sizes": list(BLOCK_SIZES), "seed": SEED},
            "lightgbm_params": PARAMS,
            "num_rounds": NUM_ROUNDS,
            "floor_reference": f"expected_covers * (1 - {VOID_RATE}), restaurants only",
            "horizon_buckets": list(HORIZON_BUCKETS),
            "closure_from": SATURDAY_SUNDAY_CLOSED_FROM.isoformat(),
        },
        "series": {run.series.name: summarize([run]) for run in runs},
        "targets": {
            target: summarize([run for run in runs if run.series.target == target])
            for target in TARGETS
            if any(run.series.target == target for run in runs)
        },
        "per_origin": {run.series.name: per_origin(run) for run in runs},
    }


# --- report -----------------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{100 * value:+.1f}%"


def _row(label: str, s: dict, covers: bool) -> str:
    headline = str(s["lift_ci95_headline_block"])
    other = next(block for block in s["lift_ci95"] if block != headline)
    low, high = s["lift_ci95"][headline]
    other_low, other_high = s["lift_ci95"][other]
    best = ", ".join(sorted(set(s["best_baseline"].values())))
    cells = [
        label,
        f"{100 * s['wape']['last_same_weekday']:.2f}%",
        f"{100 * s['wape']['mean4_same_weekday']:.2f}%",
        f"{100 * s['wape']['model']:.2f}%",
        f"{s['mae']['model']:.4g}",
        best,
        f"{_pct(s['lift_vs_best'])} [{_pct(low)}, {_pct(high)}] "
        f"(block {headline}; block {other}: [{_pct(other_low)}, {_pct(other_high)}])",
        _pct(s["lift_vs_mean4"]),
        f"{100 * s['share_origins_model_beats_best']:.0f}%",
    ]
    if covers:
        floor = s["floor_reference"]
        cells += [f"{100 * floor['wape']:.2f}%", f"{floor['bias']:+.2f}"]
    return "| " + " | ".join(cells) + " |"


def weights_note(weights: dict[str, float]) -> list[str]:
    """How much each series counts in the pooled WAPE, with the ratio computed, not asserted."""
    if len(weights) < 2:
        return []
    shares = ", ".join(f"{name} {100 * w:.0f}%" for name, w in weights.items())
    heavy, light = max(weights, key=weights.get), min(weights, key=weights.get)
    ratio = weights[heavy] / weights[light]
    note = (
        f"Pooled WAPE weights each series by its share of actual volume ({shares}): "
        f"{heavy} counts {ratio:.1f} times as much as {light}."
    )
    return [note, ""]


def _horizon_table(results: dict, names: list[str]) -> list[str]:
    lines = [
        "By horizon (pre-declared, descriptive, no interval; best baseline as chosen above):",
        "",
        "| Series | Best baseline days 1-7 | Model days 1-7 | Best baseline days 8-14 "
        "| Model days 8-14 |",
        "|---|---|---|---|---|",
    ]
    for name in names:
        by = results["series"][name]["by_horizon"]
        cells = [
            f"{100 * by[who][bucket]:.2f}%"
            for bucket in ("1-7", "8-14")
            for who in ("best_baseline", "model")
        ]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def _closure_table(results: dict, names: list[str]) -> list[str]:
    excluded = results["targets"]["covers"]["closure_slice"]["excluded_origins"]
    lines = [
        f"Without the {len(excluded)} origins whose horizon reaches the Saltgrass Sunday closure "
        f"({results['config']['closure_from']}; origins {', '.join(map(str, excluded))}). "
        "Pre-declared; the headline numbers above keep all origins.",
        "",
        "| Series | WAPE last same weekday, all / without | WAPE mean of 4, all / without "
        "| WAPE model, all / without |",
        "|---|---|---|---|",
    ]
    rows = [(name, results["series"][name]) for name in names]
    rows.append(("**pooled**", results["targets"]["covers"]))
    for label, s in rows:
        cut = s["closure_slice"]
        cells = [
            f"{100 * cut['all_origins'][m]:.2f}% / {100 * cut['without_closure'][m]:.2f}%"
            for m in METHODS
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def markdown(results: dict) -> str:
    env, config = results["environment"], results["config"]
    lines = [
        "# Demand forecast backtest: LightGBM vs seasonal baselines",
        "",
        f"Measured {results['generated_at']} on {env['system']}, {env['cpus']} CPUs, "
        f"Python {env['python']}, LightGBM {env['lightgbm']}, NumPy {env['numpy']}.",
        "",
        f"Walk-forward, expanding window: {len(config['origins'])} origins from "
        f"{config['first_origin_date']} to {config['last_origin_date']} (every {config['step']} "
        f"days), horizon {config['horizon']} days. Lift is the relative WAPE reduction of the "
        "model against the best baseline per series (positive = model better), with a 95% block "
        f"bootstrap interval over origins ({config['bootstrap']['replicates']} replicates, best "
        "baseline re-selected in each). The headline interval is the wider block size.",
        "",
    ]
    header = (
        "| Series | WAPE last same weekday | WAPE mean of 4 | WAPE model | MAE model "
        "| Best baseline | Lift vs best [95% CI] | Lift vs mean of 4 | Origins model wins |"
    )
    for target, title in (("occupancy", "Hotel occupancy"), ("covers", "Restaurant covers")):
        if target not in results["targets"]:
            continue
        covers = target == "covers"
        names = [n for n, s in results["series"].items() if n.endswith(f"/{target}")]
        lines += [f"## {title}", "", header + (" Floor ref WAPE | Floor bias |" if covers else "")]
        lines.append("|---" * (11 if covers else 9) + "|")
        lines += [_row(name, results["series"][name], covers) for name in names]
        lines += [_row("**pooled**", results["targets"][target], covers), ""]
        lines += weights_note(results["targets"][target]["weights"])
        lines += _horizon_table(results, names)
        if covers:
            lines += _closure_table(results, names)
    lines += [
        "Floor reference (restaurants only): the generator's mean covers net of voids. It is not a "
        "forecast any method could make; bias is actual minus reference, in covers per day.",
        "",
        "Run it again with `uv run poe lh-all` then `uv run poe lh-backtest`.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(results: dict, out_dir: Path) -> tuple[Path, Path]:
    json_path, md_path = out_dir / "backtest_results.json", out_dir / "BACKTEST.md"
    json_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(markdown(results), encoding="utf-8", newline="\n")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--out", type=Path, default=PROJECT_DIR)
    args = parser.parse_args()
    results = backtest(load_series(args.data_dir))
    for path in write_outputs(results, args.out):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
