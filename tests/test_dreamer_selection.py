"""Tests for task-1059 model-selection layer (spec sections 13-15).

Every numeric assertion is traced to a worked example from the spec so the
selection rules can't silently drift.
"""

from __future__ import annotations

import pytest

from novatrade.research.dreamer import config
from novatrade.research.dreamer.selection import (
    Checkpoint,
    ConsistencyResult,
    consistency_gate,
    drawdown_penalty,
    flag_outside_good_region,
    promote_champion_fold,
    select_champion_checkpoint,
    window_score,
)

# --------------------------------------------------------------------------- #
# Section 13 — scoring
# --------------------------------------------------------------------------- #


def test_window_score_subtracts_drawdown_penalty():
    # score = cumulative_reward - weight * max_drawdown
    assert window_score(10.0, 4.0, weight=1.0) == 6.0
    assert window_score(10.0, 4.0, weight=0.5) == 8.0


def test_zero_drawdown_means_score_equals_reward():
    assert window_score(7.5, 0.0, weight=2.0) == 7.5


def test_dangerous_drawdown_can_make_a_profitable_model_score_worse():
    # "punished if it makes money with dangerous drawdown"
    safe = window_score(5.0, 1.0, weight=1.0)  # 4.0
    risky = window_score(8.0, 6.0, weight=1.0)  # 2.0
    assert risky < safe  # higher reward, but the drawdown sinks it


def test_drawdown_penalty_rejects_negative_inputs():
    with pytest.raises(ValueError):
        drawdown_penalty(-1.0)
    with pytest.raises(ValueError):
        drawdown_penalty(1.0, weight=-0.5)


def test_default_weight_comes_from_config():
    # window_score with no weight uses config DEFAULT.drawdown_penalty_weight.
    expected = 10.0 - config.DEFAULT.drawdown_penalty_weight * 3.0
    assert window_score(10.0, 3.0) == expected


# --------------------------------------------------------------------------- #
# Section 13 — worst-case checkpoint selection
# --------------------------------------------------------------------------- #


def test_checkpoint_score_is_the_weaker_window():
    ckpt = Checkpoint(step=1_000_000, train_tail_score=9.0, validation_score=4.0)
    assert ckpt.checkpoint_score == 4.0


def test_picks_best_worst_case_not_best_average():
    # A: avg=(10+1)/2=5.5, worst=1   -> overfit (crushes train, fails val)
    # B: avg=(6+5)/2=5.5,  worst=5   -> balanced
    # Equal average; worst-case rule must prefer B.
    a = Checkpoint(step=2_000_000, train_tail_score=10.0, validation_score=1.0)
    b = Checkpoint(step=1_500_000, train_tail_score=6.0, validation_score=5.0)
    assert select_champion_checkpoint([a, b]) is b


def test_rejects_crushing_validation_but_weak_on_training_tail():
    # Symmetric: great validation, weak train tail must also lose to balanced.
    overfit_val = Checkpoint(step=900_000, train_tail_score=1.0, validation_score=12.0)
    balanced = Checkpoint(step=1_200_000, train_tail_score=5.5, validation_score=5.0)
    assert select_champion_checkpoint([overfit_val, balanced]) is balanced


def test_tie_break_prefers_earlier_less_overfit_step():
    early = Checkpoint(step=1_000_000, train_tail_score=5.0, validation_score=5.0)
    late = Checkpoint(step=4_000_000, train_tail_score=5.0, validation_score=5.0)
    assert select_champion_checkpoint([late, early]) is early


def test_empty_checkpoint_list_raises():
    with pytest.raises(ValueError):
        select_champion_checkpoint([])


def test_flag_outside_good_region():
    lo, hi = config.DEFAULT.expected_good_region  # (1_000_000, 2_000_000)
    inside = Checkpoint(step=(lo + hi) // 2, train_tail_score=5.0, validation_score=5.0)
    outside = Checkpoint(step=hi * 2, train_tail_score=5.0, validation_score=5.0)
    assert flag_outside_good_region(inside) is False
    assert flag_outside_good_region(outside) is True


# --------------------------------------------------------------------------- #
# Section 15 — consistency gate
# --------------------------------------------------------------------------- #


def test_single_profitable_fold_is_rejected():
    # "If only one fold is profitable, reject it."
    result = consistency_gate([5.0, -1.0, -2.0, -0.5, -3.0], min_profitable_fold_ratio=0.60)
    assert isinstance(result, ConsistencyResult)
    assert result.profitable_folds == 1
    assert result.total_folds == 5
    assert result.passed is False


def test_enough_profitable_folds_passes_at_60pct():
    # 4 of 5 profitable = 0.80 >= 0.60 -> pass
    result = consistency_gate([1.0, 2.0, 3.0, -1.0, 4.0], min_profitable_fold_ratio=0.60)
    assert result.profitable_ratio == pytest.approx(0.80)
    assert result.passed is True


def test_stricter_70pct_threshold_can_fail_what_60pct_passes():
    profits = [1.0, 2.0, -1.0, -2.0, 3.0]  # 3/5 = 0.60
    assert consistency_gate(profits, 0.60).passed is True
    assert consistency_gate(profits, 0.70).passed is False


def test_breakeven_fold_is_not_profitable():
    # strictly > 0 required; a 0.0 fold does not count as a win.
    result = consistency_gate([0.0, 1.0], min_profitable_fold_ratio=0.60)
    assert result.profitable_folds == 1


def test_consistency_gate_validates_inputs():
    with pytest.raises(ValueError):
        consistency_gate([])
    with pytest.raises(ValueError):
        consistency_gate([1.0], min_profitable_fold_ratio=1.5)


# --------------------------------------------------------------------------- #
# Section 15 — champion promotion (last fold)
# --------------------------------------------------------------------------- #


def test_promote_last_fold_when_gate_passes():
    models = ["m_fold0", "m_fold1", "m_fold2"]
    profits = [1.0, 2.0, 3.0]  # all profitable
    champion, result = promote_champion_fold(models, profits, 0.60)
    assert result.passed is True
    assert champion == "m_fold2"  # last fold = most recent data


def test_no_champion_when_gate_fails():
    models = ["m_fold0", "m_fold1", "m_fold2"]
    profits = [1.0, -2.0, -3.0]  # 1/3 profitable
    champion, result = promote_champion_fold(models, profits, 0.60)
    assert result.passed is False
    assert champion is None


def test_promote_length_mismatch_raises():
    with pytest.raises(ValueError):
        promote_champion_fold(["a", "b"], [1.0], 0.60)
