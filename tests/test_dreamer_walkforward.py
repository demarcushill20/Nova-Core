"""Task-1061 — tests for fixed-size sliding walk-forward folds + orchestration.

The transcript is explicit: the research method is NOT a single
train/val/test split, but a fixed-size *sliding* walk-forward where all three
windows move forward together and old data falls off the back of the training
window. These tests pin that geometry plus the gate-driven test-set discipline
(test data touched exactly once, only when the consistency gate passes).
"""

from __future__ import annotations

import itertools

import pytest

from novatrade.research.dreamer.config import DEFAULT
from novatrade.research.dreamer.selection import Checkpoint
from novatrade.research.dreamer.walkforward import (
    Fold,
    FoldOutcome,
    WalkForwardResult,
    generate_folds,
    run_walk_forward,
)


# --------------------------------------------------------------------------- #
# Fold geometry
# --------------------------------------------------------------------------- #
def test_windows_are_contiguous_and_correctly_sized():
    folds = generate_folds(n_samples=100, train_size=50, val_size=20, test_size=10)
    f = folds[0]
    assert (f.train_start, f.train_end) == (0, 50)
    assert (f.val_start, f.val_end) == (50, 70)
    assert (f.test_start, f.test_end) == (70, 80)
    # contiguous, non-overlapping
    assert f.train_end == f.val_start
    assert f.val_end == f.test_start
    assert f.train_len == 50 and f.val_len == 20 and f.test_len == 10


def test_train_window_is_fixed_size_and_slides_old_data_off_the_back():
    folds = generate_folds(n_samples=120, train_size=50, val_size=20, test_size=10)
    assert len(folds) >= 2
    # Every fold's train window is exactly train_size (fixed-size, not expanding).
    assert all(f.train_len == 50 for f in folds)
    # The start advances, so the oldest training bars drop off the back.
    assert folds[1].train_start > folds[0].train_start
    assert folds[1].train_start == folds[0].train_start + 10  # default step == test_size


def test_default_step_tiles_test_windows_with_no_overlap_or_gap():
    folds = generate_folds(n_samples=120, train_size=50, val_size=20, test_size=10)
    test_ranges = [f.test_range() for f in folds]
    for prev, nxt in itertools.pairwise(test_ranges):
        assert prev[1] == nxt[0]  # previous test_end == next test_start: no gap, no overlap


def test_custom_step_overlaps_test_windows():
    folds = generate_folds(n_samples=120, train_size=50, val_size=20, test_size=10, step=5)
    assert folds[1].train_start - folds[0].train_start == 5


def test_fold_indices_are_sequential():
    folds = generate_folds(n_samples=200, train_size=50, val_size=20, test_size=10)
    assert [f.fold_index for f in folds] == list(range(len(folds)))


def test_all_folds_have_identical_shape():
    folds = generate_folds(n_samples=200, train_size=50, val_size=20, test_size=10)
    shapes = {(f.train_len, f.val_len, f.test_len) for f in folds}
    assert shapes == {(50, 20, 10)}


def test_partial_trailing_block_is_dropped():
    # 95 samples; block = 80; step = 10. Starts 0 and 10 fit (10+80=90<=95); 20 doesn't.
    folds = generate_folds(n_samples=95, train_size=50, val_size=20, test_size=10)
    assert len(folds) == 2
    assert folds[-1].test_end <= 95


def test_too_few_samples_raises():
    with pytest.raises(ValueError):
        generate_folds(n_samples=50, train_size=50, val_size=20, test_size=10)


@pytest.mark.parametrize("bad", [{"train_size": 0}, {"val_size": -1}, {"test_size": 0}])
def test_non_positive_window_sizes_raise(bad):
    kwargs = {"train_size": 50, "val_size": 20, "test_size": 10}
    kwargs.update(bad)
    with pytest.raises(ValueError):
        generate_folds(n_samples=200, **kwargs)


def test_non_positive_step_raises():
    with pytest.raises(ValueError):
        generate_folds(n_samples=200, train_size=50, val_size=20, test_size=10, step=0)


# --------------------------------------------------------------------------- #
# FoldOutcome checkpoint selection
# --------------------------------------------------------------------------- #
def _ckpt(step, train_tail, val):
    return Checkpoint(step=step, train_tail_score=train_tail, validation_score=val)


def test_fold_outcome_selects_best_worst_case_checkpoint():
    fold = Fold(0, 0, 50, 70, 80)
    cks = [
        _ckpt(100, 5.0, 1.0),  # worst-case 1.0
        _ckpt(200, 3.0, 3.0),  # worst-case 3.0  <- winner
        _ckpt(300, 9.0, 0.5),  # worst-case 0.5
    ]
    outcome = FoldOutcome(fold=fold, checkpoints=cks, validation_profit=3.0)
    assert outcome.selected_checkpoint.step == 200


# --------------------------------------------------------------------------- #
# Orchestration + gate-driven test discipline
# --------------------------------------------------------------------------- #
def _make_outcome(fold, profit, model):
    cks = [_ckpt(1_500_000, profit, profit)]
    return FoldOutcome(fold=fold, checkpoints=cks, validation_profit=profit, model=model)


def test_gate_pass_promotes_last_fold_and_runs_test_once():
    folds = generate_folds(n_samples=200, train_size=50, val_size=20, test_size=10)
    # All folds profitable -> gate passes.
    profits = {f.fold_index: 1.0 for f in folds}
    models = {f.fold_index: f"model-{f.fold_index}" for f in folds}

    def run_fold(fold):
        return _make_outcome(fold, profits[fold.fold_index], models[fold.fold_index])

    test_calls = []

    def run_test(fold, outcome):
        test_calls.append(fold.fold_index)
        return {"test_return": 0.42}

    result = run_walk_forward(folds, run_fold=run_fold, run_test=run_test, config=DEFAULT)
    assert isinstance(result, WalkForwardResult)
    assert result.has_champion
    assert result.champion_model == models[folds[-1].fold_index]  # last fold promoted
    assert result.champion_checkpoint is not None
    assert result.test_result == {"test_return": 0.42}
    # Test data touched EXACTLY once, on the last fold.
    assert test_calls == [folds[-1].fold_index]


def test_gate_fail_returns_no_champion_and_never_touches_test():
    folds = generate_folds(n_samples=200, train_size=50, val_size=20, test_size=10)

    # All folds unprofitable -> gate fails.
    def run_fold(fold):
        return _make_outcome(fold, -1.0, f"model-{fold.fold_index}")

    test_calls = []

    def run_test(fold, outcome):
        test_calls.append(fold.fold_index)
        return {"test_return": 0.0}

    result = run_walk_forward(folds, run_fold=run_fold, run_test=run_test, config=DEFAULT)
    assert not result.has_champion
    assert result.champion_model is None
    assert result.champion_checkpoint is None
    assert result.test_result is None
    assert test_calls == []  # never ran on test data when the edge isn't proven
    assert not result.consistency.passed


def test_fold_profits_property_tracks_validation_profits():
    folds = generate_folds(n_samples=120, train_size=50, val_size=20, test_size=10)
    profits = [1.0 if i % 2 == 0 else -1.0 for i in range(len(folds))]

    def run_fold(fold):
        return _make_outcome(fold, profits[fold.fold_index], "m")

    result = run_walk_forward(folds, run_fold=run_fold, run_test=lambda f, o: None)
    assert result.fold_profits == profits


def test_empty_folds_raises():
    with pytest.raises(ValueError):
        run_walk_forward([], run_fold=lambda f: None, run_test=lambda f, o: None)
