# 0002. Demand forecasting: targets, baselines, model and backtest design

- Status: accepted (design); results available (2026-11-16)
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

## Amendment 2 (2026-11-16): restaurant floor reference

Decided before any backtest was run.

- **What it is.** For restaurants only, the report shows a **reference** WAPE next to the
  methods: the generator's mean covers for each target day, `expected_covers(venue, day)`, times
  `(1 - VOID_RATE)`. It reads the generator, not the data, so no method could produce it; it
  shows how much error is irreducible day-to-day noise. It is not a forecast and not a baseline.
- **Why `(1 - VOID_RATE)`.** The correction comes from the generator's mechanism, not from a
  measured gap: Gold counts only closed checks, and each check is voided with probability
  `VOID_RATE` independently of its covers, so expected Gold covers are `(1 - VOID_RATE)` times
  the target. Fitting the reference to the bias measured on one seed would tune it to that seed.
- **Known residual bias.** The generator seats parties until the day's target is reached, so the
  last party overshoots it; we expect actual minus reference to be roughly +1 to +2 covers a
  day. The report prints the measured bias per restaurant next to the reference WAPE.
- **Hotels.** No reference: a hotel equivalent would need a Monte Carlo of the booking process.

## Amendment 3 (2026-11-16): two pre-declared slices

Decided before any backtest was run. The report adds exactly these two slices and nothing else;
no other slicing will be added after seeing results.

- **By horizon bucket.** For each series, WAPE of the model and of that series' best baseline
  (chosen on the full backtest, as for the headline lift) on days 1-7 and on days 8-14 of the
  horizon. Descriptive only, no confidence interval.
- **Saltgrass Sunday closure.** Covers WAPE (per restaurant and pooled) with all origins and
  without the origins whose 14-day horizon reaches 2026-10-04 or later. The excluded origins are
  computed from the dates in the code. The headline numbers keep all origins.

## Results (2026-11-16)

From `uv run poe lh-backtest` (`projects/01-revenue-lakehouse/BACKTEST.md` and
`backtest_results.json`), measured on Linux x86_64, 22 CPUs, Python 3.12.16, LightGBM 4.7.0,
NumPy 2.5.4. 33 origins from 2026-03-01 to 2026-10-11, horizon 14. The best baseline is
`mean4_same_weekday` for all four series. The report computes the lift interval with blocks of 2
and of 4 origins and uses the wider one as the headline; both are quoted below.

**Hotel occupancy: the model is worse than the best baseline.**

- Pooled lift -8.7% [-17.0%, -1.7%] (block 4; block 2: [-16.8%, -1.9%]). The model wins 33% of
  origins. WAPE 12.54% (model) vs 11.54% (mean of 4).
- Per hotel: Marina -7.7% [-17.2%, +1.2%], 30% of origins won; this interval contains zero, so
  on Marina alone the loss is not distinguishable from a tie. Mission -9.6% [-20.8%, -1.0%],
  39% of origins won.
- By horizon bucket the model is worse on both hotels in both buckets: Marina days 1-7 11.29% vs
  10.32%, days 8-14 11.54% vs 10.88%; Mission days 1-7 13.36% vs 12.28%, days 8-14 14.03% vs
  12.72% (model vs best baseline).

**Restaurant covers: a statistical tie.**

- Pooled lift +0.6% [-3.7%, +5.1%] (block 2; block 4: [-2.7%, +5.4%]). The model wins 48% of
  origins. Per restaurant: Lumen +0.3% [-5.0%, +6.5%], Saltgrass +1.3% [-3.6%, +8.7%]. Every
  interval contains zero.
- Headroom: the best baseline's pooled WAPE is 8.86% against a floor reference WAPE of 7.43%,
  about 1.4 points of apparent headroom (Lumen 8.71% vs 7.63%, Saltgrass 9.19% vs 7.00%); the
  reference is not a strict bound (its measured bias is -2.55 and +2.11 covers/day). The model
  did not take a measurable part of it.

**Floor reference bias (actual minus reference, covers per day, backtest window).** Saltgrass
+2.11, consistent with the roughly +1 to +2 expected in amendment 2 (slightly above the top of
that range). Lumen -2.55: the opposite sign, so the expectation did not hold for Lumen. The cause
has not been established; the reference is reported as measured and was not adjusted.

**Saltgrass closure slice (descriptive only).** Removing the 4 origins whose horizon reaches
2026-10-04 (origins 323, 330, 337, 344), Saltgrass WAPE goes from 9.07% to 7.86% for the model
and from 9.19% to 8.21% for the mean of 4. No interval is computed for this slice and it does not
change the headline, which keeps all origins.

**Limitations.**

- The data is synthetic with a simple generating process (weekday multiplier times a smooth annual
  cosine times noise). A same-weekday mean is close to optimal by construction, so these results
  say little about real demand data.
- 33 overlapping origins (consecutive origins share 7 target days) over one year. The intervals
  are wide and the block bootstrap only partly accounts for the overlap.
- Hyperparameters are fixed and arbitrary, never validated.
- Possible reasons for the hotel loss, **none of them tested**: the trees fit noise in the
  residual with less than a year of training history; the 7-day and 28-day mean features add
  variance without information the mean of 4 lacks; Marina's capacity change on 2026-09-01 shifts
  the occupancy level within the window (this cannot explain Mission, which also loses);
  occupancy is bounded and the residual correction behaves differently near the bounds.

**Notes on differences from the plan.**

- The plan text said "about 34 origins"; the implementation has 33.
- The plan text said bootstrap blocks of 2. Before the backtest was run the decision changed to
  report blocks of 2 and 4 with the wider interval as the headline. This was decided before
  running but was **not recorded in this ADR until now**, so git history does not prove its
  timing.

**What happens next.** The hyperparameters were not tuned after seeing these results, and they
will not be: tuning on this backtest would fit the model to the test. The legitimate next
experiment is new information the baselines do not have, such as rooms on the books at the
forecast origin (booking pace), which will be designed in a future ADR before it is run.
