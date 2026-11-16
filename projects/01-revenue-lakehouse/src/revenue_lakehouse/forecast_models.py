"""Seasonal baselines and the global multi-horizon LightGBM model for one daily series.

Every public function takes ``history``: the series up to and including the forecast origin.
The origin is ``len(history) - 1`` and nothing after it is passed in, so nothing after it can be
read. ``first_weekday`` is the weekday (Monday = 0) of ``history[0]``; the weekday of a future
date is calendar, not data. Design and reasons: ``docs/decisions/0002-demand-forecast-backtest.md``
(see its amendment: the model learns the residual over baseline (b), and its output is clipped).
"""

import lightgbm as lgb
import numpy as np

HORIZON = 14
WEEKS = 4  # baseline (b) averages this many same-weekday values
MIN_ORIGIN = 27  # first origin with 28 days of history, needed by mean_28d and baseline (b)
FEATURES = (
    "horizon",
    "target_weekday",
    "last_same_weekday",
    "mean4_same_weekday",
    "last_value",
    "mean_7d",
    "mean_28d",
)
# Fixed, not tuned or validated (ADR 0002).
PARAMS = {
    "objective": "regression",
    "num_leaves": 7,
    "min_data_in_leaf": 10,
    "learning_rate": 0.05,
    "seed": 42,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
    "verbose": -1,
}
NUM_ROUNDS = 200
BASE = FEATURES.index("mean4_same_weekday")  # the model learns y minus this baseline
BOUNDS = {"occupancy": (0.0, 1.0), "covers": (0.0, np.inf)}  # clip the model output only


def _same_weekday_values(y: np.ndarray, origins: np.ndarray, h: int) -> np.ndarray:
    """``(WEEKS, len(origins))`` values on the weekday of ``origin + h``, latest first.

    The latest one is the last such day at or before the origin: ``origin + h - 7 * ceil(h / 7)``,
    which lies in ``[origin - 6, origin]``. Each further row is one week earlier.
    """
    latest = origins + h - 7 * ((h + 6) // 7)
    return y[latest - 7 * np.arange(WEEKS)[:, None]]


def _feature_matrix(y: np.ndarray, origins: np.ndarray, h: int, first_weekday: int) -> np.ndarray:
    """One feature row per origin for horizon ``h``. Each row reads only ``y[: origin + 1]``."""
    if origins.min() < MIN_ORIGIN or origins.max() >= len(y):
        raise ValueError(f"origins must be in [{MIN_ORIGIN}, {len(y) - 1}]")
    sums = np.concatenate(([0.0], np.cumsum(y)))  # sums[i] = y[0] + ... + y[i - 1]
    weekly = _same_weekday_values(y, origins, h)
    columns = (
        np.full(len(origins), h, dtype=float),
        ((first_weekday + origins + h) % 7).astype(float),
        weekly[0],
        weekly.mean(axis=0),
        y[origins],
        (sums[origins + 1] - sums[origins - 6]) / 7,
        (sums[origins + 1] - sums[origins - 27]) / 28,
    )
    return np.column_stack(columns)


def _as_history(history) -> tuple[np.ndarray, int]:
    y = np.asarray(history, dtype=float)
    origin = len(y) - 1
    if origin < MIN_ORIGIN:
        raise ValueError(f"need at least {MIN_ORIGIN + 1} days of history, got {len(y)}")
    return y, origin


def baselines(history, horizon: int = HORIZON) -> dict[str, np.ndarray]:
    """Forecasts for horizons ``1..horizon`` from the origin ``len(history) - 1``.

    (a) ``last_same_weekday``: last value on the target's weekday at or before the origin.
    (b) ``mean4_same_weekday``: mean of the last four such values.
    """
    y, origin = _as_history(history)
    weekly = np.column_stack(
        [_same_weekday_values(y, np.array([origin]), h)[:, 0] for h in range(1, horizon + 1)]
    )
    return {"last_same_weekday": weekly[0], "mean4_same_weekday": weekly.mean(axis=0)}


def forecast_features(history, first_weekday: int, horizon: int = HORIZON) -> np.ndarray:
    """Feature rows the model predicts from at the origin, one per horizon ``1..horizon``."""
    y, origin = _as_history(history)
    origins = np.array([origin])
    return np.vstack([_feature_matrix(y, origins, h, first_weekday) for h in range(1, horizon + 1)])


def training_set(
    history, first_weekday: int, horizon: int = HORIZON
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rows ``(s', h)`` with ``s' + h <= origin``: features, residual targets, and keys.

    The target is ``y[s' + h]`` minus that row's baseline (b), ``mean4_same_weekday``.
    ``keys`` is an ``(n, 2)`` integer array of ``(s', h)`` so callers can audit every row.
    """
    y, origin = _as_history(history)
    features, targets, keys = [], [], []
    for h in range(1, horizon + 1):
        origins = np.arange(MIN_ORIGIN, origin - h + 1)
        if len(origins) == 0:
            continue
        rows = _feature_matrix(y, origins, h, first_weekday)
        features.append(rows)
        targets.append(y[origins + h] - rows[:, BASE])
        keys.append(np.column_stack((origins, np.full(len(origins), h))))
    if not features:
        raise ValueError(f"no training row has its target at or before origin {origin}")
    return np.vstack(features), np.concatenate(targets), np.vstack(keys)


def model_forecast(history, first_weekday: int, target: str, horizon: int = HORIZON) -> np.ndarray:
    """Fit the global model on ``history`` and forecast horizons ``1..horizon``.

    The prediction is baseline (b) plus the learned residual, clipped to ``BOUNDS[target]``.
    """
    features, residuals, _ = training_set(history, first_weekday, horizon)
    data = lgb.Dataset(features, residuals, feature_name=list(FEATURES))
    booster = lgb.train(PARAMS, data, num_boost_round=NUM_ROUNDS)
    rows = forecast_features(history, first_weekday, horizon)
    low, high = BOUNDS[target]
    return np.clip(rows[:, BASE] + booster.predict(rows), low, high)
