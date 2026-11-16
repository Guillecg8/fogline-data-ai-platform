# 0002. Demand forecasting: targets, baselines, model and backtest design

- Status: accepted (design); results pending
- Date: 2026-11-16
- Project: `projects/01-revenue-lakehouse`

## Context

Gold has one year (2025-11-01 to 2026-10-31) of `hotel_daily` and `restaurant_daily`, two venues
each. We want a forecast that a revenue manager could act on, and an honest answer to whether a
learned model beats simple seasonal rules on this data.

How these decisions were made: **after reading the synthetic generator and before running any
backtest.** This is not a blind pre-registration. We know the demand process (weekday multiplier
times a smooth annual cosine times noise, Saltgrass closed on Sundays from 2026-10-04, Marina
dropping from 62 to 40 rooms on 2026-09-01), and that knowledge shaped the choices below.

## Decision

- **Targets.** Hotel occupancy (`rooms_sold / rooms_available`) and restaurant covers. Raw
  `rooms_sold` is never forecast, because Marina's capacity change would look like a demand drop.
- **Baselines, measured from the forecast origin `s`:** (a) the last observed value on the target
  date's weekday at or before `s`; (b) the mean of the last four such values. Measuring "last week"
  from the target date would read data after `s` for horizons above 7, so it is not used.
- **Model.** LightGBM, one global multi-horizon model per series (4 series), refit at every origin.
  A training row is (origin `s'`, horizon `h` in 1..14) with target `y[s' + h]`. Its features are
  computed only from data at or before `s'`: `h`, weekday of the target date, the two baseline
  values, `y[s']`, and 7-day and 28-day means ending at `s'`. At origin `s` only rows with
  `s' + h <= s` are used. Month and day of month are excluded: one year of data cannot teach a
  monthly effect, and the generator has no day-of-month effect.
- **Hyperparameters are fixed but arbitrary**, chosen once as reasonable small-data defaults and
  **not validated or tuned**: `num_leaves=7`, `min_data_in_leaf=10`, `learning_rate=0.05`,
  200 rounds, `seed=42`, `deterministic=True`, `force_row_wise=True`, `num_threads=1`.
- **Walk-forward backtest.** Expanding window, first origin after 120 days of history, a new origin
  every 7 days, horizon 14, and only origins whose 14 target days are all observed.
- **Metrics.** MAE and WAPE (`sum |error| / sum actual`) per venue and per target. Occupancy and
  covers are never pooled into one number. Lift = relative WAPE reduction against the best baseline
  for that venue, chosen on the full backtest and kept fixed.
- **Uncertainty.** 95% bootstrap interval for the lift, resampling forecast origins with a fixed
  seed. Consecutive origins share 7 target days, so origins are resampled in blocks of two
  consecutive origins. We also report the share of origins where the model beats the best baseline.
- **Outputs.** A Markdown report and a JSON file, both written by `uv run poe lh-backtest`.
- **Out of scope for this step:** MLflow, hyperparameter search, and booking-pace ("rooms on the
  books") features.

## Alternatives considered

- **One model whose lags are all >= 14 days.** Simpler and leak-free, but it ignores the most
  recent 13 days that the baselines use, so the comparison would be unfair to the model.
- **One model per horizon.** Fourteen times the fits for little expected gain on 365 days.
- **Pooling venues into one model per target.** More rows, but restaurant covers differ in scale
  (about 140 vs 310 a day). Rejected to keep each series self-contained.

## Consequences

- Expected outcome, stated before running: on this generator, baseline (b) is close to the best
  you can do from history, so **the model may tie with it or lose to it.** Whatever the backtest
  shows is published, including a negative lift.
- The lift interval is wide with about 34 origins. If it contains zero, the report says so.
- The Saltgrass Sunday closure falls in the last month of the backtest. No method is told about
  it, so it will appear as error.
- New dependencies in `revenue-lakehouse`: `lightgbm` and `numpy`, the latter declared because the
  code imports it.

## Amendment (2026-11-16): residual target and output clipping

Decided after code review of the baselines and model code, and **before any backtest was run.**

- **Residual target.** The model now learns `y[s' + h] - mean4_same_weekday` (baseline (b) for
  that row) and adds baseline (b) back at prediction time. The features are unchanged. Reason:
  regression trees are piecewise constant, so predicting a noisy, slowly moving level directly
  is a poor approximation of it; the baseline carries the level and the trees only have to learn
  a correction. If there is nothing to learn, the model falls back to baseline (b).
- **Clipping.** The model's output is clipped to the valid range of its target: occupancy to
  `[0, 1]`, covers to `>= 0`. The clip applies to the model output only; baselines are never
  clipped (they are averages of observed values and are already in range).
