"""Leakage + shape + scaler-freeze tests for the DreamerV3 gold-bot foundation.

These are the highest-ROI guards in the whole build: a feature-lookahead or
scaler-leak bug found *after* a 1M-step GPU run wastes the entire run. They use
small synthetic M1 data so they run in well under a second.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from novatrade.research.dreamer import config, data, env, features


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _synthetic_m1(start: str, periods: int, freq: str = "1min", seed: int = 0) -> pd.DataFrame:
    """Deterministic random-walk OHLC frame at ``freq`` granularity."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=periods, freq=freq)
    steps = rng.normal(0, 0.2, size=periods).cumsum()
    close = 1800.0 + steps
    high = close + np.abs(rng.normal(0, 0.1, size=periods))
    low = close - np.abs(rng.normal(0, 0.1, size=periods))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


@pytest.fixture(scope="module")
def m1() -> pd.DataFrame:
    # The W1 EMA-50 feature needs ~50 closed weekly bars before any matrix row
    # survives the warmup dropna, so the fixture must span ~1.5y. Generate at
    # 5-minute granularity (the M5 base) to keep the test fast; the M1->TF
    # pipeline is granularity-agnostic for the leakage/shape properties tested.
    return _synthetic_m1("2021-01-01", periods=540 * 288, freq="5min")


# --------------------------------------------------------------------------- #
# Phase A — resampling / no-lookahead alignment
# --------------------------------------------------------------------------- #
def test_resample_shapes_and_monotonic(m1):
    for tf in config.TIMEFRAMES:
        frame = data.resample_ohlc(m1, tf)
        assert frame.index.is_monotonic_increasing
        assert not frame.isna().any().any()
        assert list(frame.columns) == ["open", "high", "low", "close"]


def test_no_lookahead_alignment(m1):
    """Every higher-TF value at M5 row t must come from a bar closed <= t."""
    tf_frames = data.build_tf_frames(m1)
    base_index = tf_frames[config.BASE_TF].index

    for tf in config.TIMEFRAMES:
        if tf == config.BASE_TF:
            continue
        closed = data.to_close_indexed(tf_frames[tf], tf)
        aligned = data.align_backward(base_index, tf_frames[tf], tf)
        # The 'close' carried at row t must equal the most recent closed bar's
        # close, and that bar's close_time must be <= t.
        for t in [aligned.index[len(aligned) // 2], aligned.index[-1]]:
            eligible = closed.loc[closed.index <= t]
            if eligible.empty:
                continue
            expected_close = eligible["close"].iloc[-1]
            got = aligned.loc[t, "close"]
            assert got == pytest.approx(expected_close)
            # Hard no-lookahead: latest eligible bar closed at or before t.
            assert eligible.index[-1] <= t


def test_train_end_trim_excludes_future_bars(m1):
    """build_tf_frames(end=...) must not emit any bar at/after the boundary."""
    end = pd.Timestamp("2021-11-01")
    tf_frames = data.build_tf_frames(m1, end=end)
    for tf, frame in tf_frames.items():
        assert (frame.index < end).all(), tf


# --------------------------------------------------------------------------- #
# Phase B — feature matrix shape + scaler freeze
# --------------------------------------------------------------------------- #
def test_feature_matrix_is_152_and_clean(m1):
    matrix = features.build_features(m1)
    assert matrix.shape[1] == config.N_FEATURES == 152
    assert len(matrix) > 1000, "warmup dropna left too few rows — check fixture span"
    assert not matrix.isna().any().any()
    assert matrix.index.is_monotonic_increasing


def test_scaler_fit_on_train_only_is_frozen(m1):
    """Scaler stats must depend only on the train slice, never on val/test.

    Build the full matrix, split at a boundary, and confirm: (a) a scaler fit
    on the train slice gives identical stats whether or not the val rows exist,
    and (b) a scaler that *wrongly* fit on the full series has different stats —
    i.e. the leak is detectable, not silently absorbed.
    """
    matrix = features.build_features(m1)
    split = matrix.index[len(matrix) // 2]
    train = matrix.loc[matrix.index < split]
    full = matrix  # includes val/test rows

    correct = features.FeatureScaler().fit(train)
    # Re-fitting on the same train slice carved from the full frame: identical.
    refit = features.FeatureScaler().fit(full.loc[full.index < split])
    pd.testing.assert_series_equal(correct.mean_, refit.mean_)
    pd.testing.assert_series_equal(correct.std_, refit.std_)

    # A scaler fit on the full series leaks the future -> stats must differ.
    leaky = features.FeatureScaler().fit(full)
    assert not np.allclose(correct.mean_.to_numpy(), leaky.mean_.to_numpy())

    # transform applies train stats unchanged to held-out rows (no re-fit).
    z = correct.transform(matrix.loc[matrix.index >= split])
    assert z.shape[1] == config.N_FEATURES
    assert np.isfinite(z.to_numpy()).all()


def test_scaler_roundtrip(m1):
    matrix = features.build_features(m1)
    s = features.FeatureScaler().fit(matrix)
    s2 = features.FeatureScaler.from_dict(s.to_dict())
    pd.testing.assert_frame_equal(s.transform(matrix), s2.transform(matrix))


# --------------------------------------------------------------------------- #
# Phase C — environment fill/reward timing
# --------------------------------------------------------------------------- #
def test_env_reward_and_fill_timing():
    # Monotonically rising price: long should earn, flat should earn nothing.
    n, window = 40, 8
    close = np.linspace(100.0, 120.0, n)
    feats = np.tile(close.reshape(-1, 1), (1, 4)).astype("float32")
    fwd = env.forward_returns(close)

    # holding_penalty=0.0 isolates fill timing from Term 3 (task-1058).
    e = env.TradingEnv(feats, fwd, window=window, cost=0.0, holding_penalty=0.0)
    e.reset()
    _, r_long, _, info = e.step(1)
    # Reward uses the forward return at the entry bar, position applied next bar.
    assert r_long == pytest.approx(fwd[window - 1], rel=1e-5)
    assert info["position"] == 1.0

    e.reset()
    _, r_flat, _, _ = e.step(0)
    assert r_flat == 0.0


def test_env_turnover_cost_charged_on_position_change():
    n, window = 20, 4
    feats = np.zeros((n, 3), dtype="float32")
    fwd = np.zeros(n, dtype="float32")  # isolate cost from PnL
    cost = 0.001

    # holding_penalty=0.0 isolates turnover cost from Term 3 (task-1058).
    e = env.TradingEnv(feats, fwd, window=window, cost=cost, holding_penalty=0.0)
    e.reset()
    _, r1, _, _ = e.step(1)  # flat -> long: charged once
    assert r1 == pytest.approx(-cost, rel=1e-5)
    _, r2, _, _ = e.step(1)  # long -> long: no change, no cost
    assert r2 == pytest.approx(0.0, abs=1e-9)


def test_env_rejects_short_when_long_only():
    feats = np.zeros((10, 2), dtype="float32")
    fwd = np.zeros(10, dtype="float32")
    e = env.TradingEnv(feats, fwd, window=3, allow_short=False)
    e.reset()
    with pytest.raises(ValueError):
        e.step(2)
