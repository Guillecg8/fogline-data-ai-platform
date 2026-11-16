import numpy as np
import pytest

from revenue_lakehouse import forecast_models
from revenue_lakehouse.forecast_models import (
    FEATURES,
    HORIZON,
    MIN_ORIGIN,
    baselines,
    forecast_features,
    model_forecast,
    training_set,
)

FIRST_WEEKDAY = 3  # a Thursday, so weekday and index modulo 7 differ
PATTERN = np.array([100.0, 90.0, 95.0, 110.0, 150.0, 180.0, 130.0])


def noisy_weekly(days: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return PATTERN[np.arange(days) % 7] * rng.normal(1.0, 0.05, days)


# --- no leakage -------------------------------------------------------------------------------


def test_baselines_read_only_the_target_weekday_at_or_before_the_origin():
    history = np.arange(60, dtype=float)  # each value is its own index
    origin = len(history) - 1
    result = baselines(history)

    for h in range(1, HORIZON + 1):
        used = int(result["last_same_weekday"][h - 1])
        assert used <= origin
        assert used > origin - 7  # the latest such day, not an older one
        assert used % 7 == (origin + h) % 7
        assert result["mean4_same_weekday"][h - 1] == np.mean([used - 7 * w for w in range(4)])


def test_every_training_target_is_at_or_before_the_origin():
    history = noisy_weekly(80)
    origin = len(history) - 1
    features, targets, keys = training_set(history, FIRST_WEEKDAY)
    starts, horizons = keys[:, 0], keys[:, 1]

    assert (starts + horizons).max() == origin
    assert starts.min() == MIN_ORIGIN
    assert set(horizons.tolist()) == set(range(1, HORIZON + 1))
    residual = history[starts + horizons] - features[:, FEATURES.index("mean4_same_weekday")]
    assert np.array_equal(targets, residual)
    expected_rows = sum(origin - h - MIN_ORIGIN + 1 for h in range(1, HORIZON + 1))
    assert len(keys) == expected_rows == len(set(map(tuple, keys.tolist())))


def test_training_features_equal_features_computed_from_history_cut_at_their_own_origin():
    history = noisy_weekly(70)
    features, _, keys = training_set(history, FIRST_WEEKDAY)

    for row, (start, h) in zip(features, keys, strict=True):
        cut = forecast_features(history[: start + 1], FIRST_WEEKDAY)[h - 1]
        assert np.array_equal(row, cut)


def test_changing_one_day_only_touches_rows_allowed_to_see_it():
    history = noisy_weekly(90)
    features, targets, keys = training_set(history, FIRST_WEEKDAY)
    starts, horizons = keys[:, 0], keys[:, 1]

    for day in (MIN_ORIGIN, 45, 70, len(history) - 1):
        changed = history.copy()
        changed[day] += 1000.0
        new_features, new_targets, new_keys = training_set(changed, FIRST_WEEKDAY)
        assert np.array_equal(keys, new_keys)
        feature_moved = (features != new_features).any(axis=1)
        target_moved = targets != new_targets
        hits_target = starts + horizons == day
        assert (starts[feature_moved] >= day).all()
        assert target_moved[hits_target].all()
        # a residual moves only through its target day or through its own baseline (b)
        assert (hits_target | (starts >= day))[target_moved].all()


def test_target_weekday_is_the_calendar_weekday_of_the_target_date():
    history = noisy_weekly(60)
    features, _, keys = training_set(history, FIRST_WEEKDAY)
    column = FEATURES.index("target_weekday")
    assert np.array_equal(features[:, column], (FIRST_WEEKDAY + keys.sum(axis=1)) % 7)


# --- behaviour --------------------------------------------------------------------------------


def test_too_short_history_is_rejected():
    with pytest.raises(ValueError, match="at least 28 days"):
        baselines(np.ones(MIN_ORIGIN))
    with pytest.raises(ValueError, match="no training row"):
        training_set(np.ones(MIN_ORIGIN + 1), FIRST_WEEKDAY)


def test_model_forecast_is_deterministic_and_has_one_value_per_horizon():
    history = noisy_weekly(150)
    first = model_forecast(history, FIRST_WEEKDAY, "covers")
    second = model_forecast(history, FIRST_WEEKDAY, "covers")
    assert first.shape == (HORIZON,)
    assert np.array_equal(first, second)


def test_model_learns_a_weekly_pattern():
    history = noisy_weekly(200)
    truth = PATTERN[np.arange(200, 200 + HORIZON) % 7]
    forecast = model_forecast(history, FIRST_WEEKDAY, "covers")
    assert np.abs(forecast - truth).mean() < 10.0  # pattern ranges from 90 to 180


def test_with_a_zero_residual_everywhere_the_model_returns_baseline_b():
    history = PATTERN[np.arange(150) % 7]  # exactly weekly: y equals baseline (b) on every row
    _, residuals, _ = training_set(history, FIRST_WEEKDAY)
    assert not residuals.any()
    forecast = model_forecast(history, FIRST_WEEKDAY, "covers")
    np.testing.assert_allclose(forecast, baselines(history)["mean4_same_weekday"], atol=1e-12)


class ConstantBooster:
    def __init__(self, residual: float):
        self.residual = residual

    def predict(self, rows):
        return np.full(len(rows), self.residual)


@pytest.mark.parametrize(
    ("target", "residual", "low", "high"),
    [("occupancy", 5.0, 1.0, 1.0), ("occupancy", -5.0, 0.0, 0.0), ("covers", -500.0, 0.0, 0.0)],
)
def test_model_output_is_clipped_to_the_target_range(monkeypatch, target, residual, low, high):
    booster = ConstantBooster(residual)
    monkeypatch.setattr(forecast_models.lgb, "train", lambda *args, **kwargs: booster)
    history = noisy_weekly(150) / 200.0
    forecast = model_forecast(history, FIRST_WEEKDAY, target)
    assert forecast.min() == low and forecast.max() == high


def test_baselines_are_not_clipped():
    history = noisy_weekly(150) / 100.0  # "occupancy" above 1 passes through untouched
    assert baselines(history)["mean4_same_weekday"].max() > 1.0
