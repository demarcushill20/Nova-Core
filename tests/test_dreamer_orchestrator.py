"""Task-1065 — tests for the end-to-end DreamerV3 gold-bot orchestrator.

These pin the orchestration *contract* (not the GPU training, which is injected):

    * folds slice the DecisionSeries correctly and the scaler is fit on the
      fold's TRAIN window only (no val/test leakage into standardisation);
    * the cross-fold consistency gate still gates the single test read — on
      gate-pass the champion is evaluated ONCE on held-out test data and a full
      report is produced; on gate-fail the test set is never touched;
    * the default actor->execution evaluator turns actions into closed trades
      via the H1-decision / M1-execution engine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from novatrade.research.dreamer.execution import Bar, Trade
from novatrade.research.dreamer.orchestrator import (
    DecisionSeries,
    OrchestrationResult,
    TrainedFold,
    actor_evaluator,
    make_fold_data,
    run_orchestration,
)
from novatrade.research.dreamer.selection import Checkpoint
from novatrade.research.dreamer.walkforward import generate_folds


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _series(n: int, n_features: int = 3, close: float = 100.0, atr: float = 1.0) -> DecisionSeries:
    """A synthetic decision series whose M1 paths always hit a LONG target.

    decision_close is constant ``close``; ATR constant ``atr``. Each H1 bar's
    single M1 bar reaches well above the take-profit but never the stop, so any
    long opened at a bar closes ``target`` in the same bar at exactly +tp_rr R.
    Features vary per row so a train-only scaler is distinguishable from a
    full-series one.
    """
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    feats = pd.DataFrame(
        np.add.outer(np.arange(n, dtype="float64"), np.arange(n_features, dtype="float64")),
        index=idx,
        columns=[f"f{j}" for j in range(n_features)],
    )
    decision_close = np.full(n, close)
    atr_arr = np.full(n, atr)
    # high far above target (entry + 2*1.5*atr = +3), low == entry so the stop
    # (entry - 1.5*atr) is never threatened.
    m1_paths = [[Bar(high=close + 10.0, low=close, close=close + 5.0)] for _ in range(n)]
    return DecisionSeries(features=feats, decision_close=decision_close, atr=atr_arr, m1_paths=m1_paths)


def _ckpt(step: int = 1_500_000, score: float = 1.0) -> Checkpoint:
    return Checkpoint(step=step, train_tail_score=score, validation_score=score)


def _train_fn(validation_profit: float):
    """Stub trainer: model identifies its fold, one checkpoint in the good region."""

    def train_fn(fold_data) -> TrainedFold:
        return TrainedFold(
            model=f"model-fold-{fold_data.fold.fold_index}",
            checkpoints=[_ckpt()],
            validation_profit=validation_profit,
        )

    return train_fn


def _always_buy(model, obs) -> int:
    return 1  # BUY


# --------------------------------------------------------------------------- #
# DecisionSeries
# --------------------------------------------------------------------------- #
def test_decision_series_length_mismatch_raises():
    feats = pd.DataFrame({"f0": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        DecisionSeries(features=feats, decision_close=np.array([1.0, 2.0]), atr=np.ones(3), m1_paths=[[], [], []])


def test_decision_series_slice_is_aligned_and_positional():
    s = _series(10)
    sub = s.slice(2, 5)
    assert len(sub) == 3
    assert list(sub.features.index) == list(s.features.index[2:5])
    assert np.array_equal(sub.atr, s.atr[2:5])
    assert len(sub.m1_paths) == 3


# --------------------------------------------------------------------------- #
# Scaler is fit on TRAIN only (no leakage)
# --------------------------------------------------------------------------- #
def test_make_fold_data_fits_scaler_on_train_window_only():
    s = _series(30)
    fold = generate_folds(n_samples=30, train_size=8, val_size=4, test_size=6)[0]
    fd = make_fold_data(s, fold)
    train_slice = s.slice(*fold.train_range()).features
    # Scaler mean must equal the TRAIN slice mean, not the full-series mean.
    pd.testing.assert_series_equal(fd.scaler.mean_, train_slice.mean())
    assert not np.allclose(fd.scaler.mean_.to_numpy(), s.features.mean().to_numpy())


# --------------------------------------------------------------------------- #
# Default actor -> execution evaluator
# --------------------------------------------------------------------------- #
def test_actor_evaluator_produces_target_trades():
    s = _series(30)
    evaluate = actor_evaluator(_always_buy, window=3, sl_atr_mult=1.5, tp_rr=2.0)
    from novatrade.research.dreamer.features import FeatureScaler

    scaler = FeatureScaler().fit(s.features)
    trades = evaluate("model", s, scaler)
    assert trades, "expected at least one trade"
    assert all(isinstance(t, Trade) for t in trades)
    assert all(t.exit_reason == "target" for t in trades)
    # +tp_rr R per winning long.
    assert all(abs(t.r_multiple() - 2.0) < 1e-9 for t in trades)


def test_actor_evaluator_observation_shape():
    s = _series(12, n_features=3)
    seen = []

    def actor(model, obs):
        seen.append(obs.shape)
        return 1

    evaluate = actor_evaluator(actor, window=4)
    from novatrade.research.dreamer.features import FeatureScaler

    evaluate("m", s, FeatureScaler().fit(s.features))
    assert seen and all(shape == (4, 3) for shape in seen)


def test_actor_evaluator_rejects_bad_action():
    s = _series(12)
    evaluate = actor_evaluator(lambda m, o: 7, window=3)
    from novatrade.research.dreamer.features import FeatureScaler

    with pytest.raises(ValueError):
        evaluate("m", s, FeatureScaler().fit(s.features))


# --------------------------------------------------------------------------- #
# End-to-end orchestration: gate discipline
# --------------------------------------------------------------------------- #
def test_gate_pass_runs_test_once_and_reports():
    s = _series(30)
    result = run_orchestration(
        s,
        train_fn=_train_fn(validation_profit=1.0),  # all folds profitable -> gate passes
        evaluate_fn=actor_evaluator(_always_buy, window=3),
        train_size=8,
        val_size=4,
        test_size=6,
        rolling_window=2,
    )
    assert isinstance(result, OrchestrationResult)
    assert result.has_champion
    assert result.consistency.passed
    # Champion is the LAST fold's model.
    n_folds = len(generate_folds(n_samples=30, train_size=8, val_size=4, test_size=6))
    assert result.walk_forward.champion_model.model == f"model-fold-{n_folds - 1}"
    # Test read happened exactly once and produced a real report.
    assert result.test_trades, "test window should have produced trades"
    assert result.test_report is not None
    assert result.test_report.total_return > 0
    assert result.test_report.trade_counts["total"] == len(result.test_trades)
    # Checkpoint at 1.5M sits inside the expected good region.
    assert result.champion_outside_good_region is False


def test_gate_fail_never_touches_test_set():
    s = _series(30)
    result = run_orchestration(
        s,
        train_fn=_train_fn(validation_profit=-1.0),  # no fold profitable -> gate fails
        evaluate_fn=actor_evaluator(_always_buy, window=3),
        train_size=8,
        val_size=4,
        test_size=6,
    )
    assert not result.has_champion
    assert not result.consistency.passed
    assert result.test_trades is None
    assert result.test_report is None
    assert result.champion_outside_good_region is None


def test_evaluate_fn_only_called_for_test_window_on_gate_pass():
    s = _series(30)
    calls = {"n": 0}

    def evaluate_fn(model, win, scaler):
        calls["n"] += 1
        return []

    run_orchestration(
        s,
        train_fn=_train_fn(validation_profit=1.0),
        evaluate_fn=evaluate_fn,
        train_size=8,
        val_size=4,
        test_size=6,
    )
    # Exactly one test read (the promoted champion's held-out window).
    assert calls["n"] == 1
