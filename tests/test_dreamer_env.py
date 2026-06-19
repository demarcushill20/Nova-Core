"""Tests for the DreamerV3 trading environment reward terms.

Focus (task-1058): Term 3, the holding penalty — a tiny negative constant
subtracted on every bar a position stays open.
"""

from __future__ import annotations

import numpy as np
import pytest

from novatrade.research.dreamer import config
from novatrade.research.dreamer.env import TradingEnv, forward_returns


def _flat_env(holding_penalty: float, *, window: int = 3, n: int = 12) -> TradingEnv:
    """A zero-return env so reward == -costs/penalties only (no PnL noise)."""
    features = np.zeros((n, 4), dtype="float32")
    fwd = np.zeros(n, dtype="float32")  # every fwd return is exactly 0
    return TradingEnv(
        features,
        fwd,
        window=window,
        cost=0.0,  # isolate the holding penalty from turnover cost
        allow_short=True,
        holding_penalty=holding_penalty,
    )


def test_holding_penalty_charged_only_while_open():
    pen = 2e-5
    env = _flat_env(pen)
    env.reset()

    # Open a long: position becomes non-zero -> penalty applies this bar.
    _, r_open, _, info_open = env.step(1)
    assert info_open["position"] == 1.0
    assert info_open["holding_penalty"] == pytest.approx(pen)
    assert r_open == pytest.approx(-pen)

    # Hold the long another bar -> still open -> still charged.
    _, r_hold, _, info_hold = env.step(1)
    assert info_hold["holding_penalty"] == pytest.approx(pen)
    assert r_hold == pytest.approx(-pen)

    # Go flat -> position is 0 -> no penalty on the flat bar.
    _, r_flat, _, info_flat = env.step(0)
    assert info_flat["position"] == 0.0
    assert info_flat["holding_penalty"] == 0.0
    assert r_flat == pytest.approx(0.0)


def test_holding_penalty_applies_to_shorts_too():
    pen = 2e-5
    env = _flat_env(pen)
    env.reset()
    _, reward, _, info = env.step(2)  # short
    assert info["position"] == -1.0
    assert info["holding_penalty"] == pytest.approx(pen)
    assert reward == pytest.approx(-pen)


def test_zero_penalty_disables_the_term():
    env = _flat_env(0.0)
    env.reset()
    _, reward, _, info = env.step(1)
    assert info["holding_penalty"] == 0.0
    assert reward == pytest.approx(0.0)


def test_penalty_stacks_with_gross_and_cost():
    # Non-zero forward return + non-zero cost: reward must net all three terms.
    features = np.zeros((8, 4), dtype="float32")
    fwd = np.zeros(8, dtype="float32")
    fwd[3] = 0.01  # the bar we will earn on
    pen = 2e-5
    cost = 1e-4
    env = TradingEnv(features, fwd, window=3, cost=cost, allow_short=True, holding_penalty=pen)
    env.reset()  # _t == window-1 == 2
    _, reward, _, info = env.step(1)  # earns fwd[2]==0 but pays cost+penalty
    # gross = 1*fwd[2] = 0; turnover = cost*|1-0|; penalty = pen
    assert reward == pytest.approx(-cost - pen)
    # next bar holds long, earns fwd[3]=0.01, no turnover, still pays penalty
    _, reward2, _, info2 = env.step(1)
    assert info2["gross"] == pytest.approx(0.01)
    assert reward2 == pytest.approx(0.01 - pen)


def test_negative_penalty_rejected():
    features = np.zeros((8, 4), dtype="float32")
    fwd = np.zeros(8, dtype="float32")
    with pytest.raises(ValueError):
        TradingEnv(features, fwd, window=3, holding_penalty=-1e-5)


def test_default_holding_penalty_is_tiny_and_nonnegative():
    # Guards the config invariant: tiny nudge, never dominates, never negative.
    assert 0.0 <= config.DEFAULT.holding_penalty <= config.DEFAULT.cost
    assert config.DEFAULT.holding_penalty == pytest.approx(2e-5)


def test_forward_returns_helper_unchanged():
    close = np.array([100.0, 101.0, 101.0, 99.0], dtype="float64")
    fwd = forward_returns(close)
    assert fwd[0] == pytest.approx(0.01)
    assert fwd[1] == pytest.approx(0.0)
    assert fwd[2] == pytest.approx(-2.0 / 101.0)
    assert fwd[-1] == 0.0  # no future bar for the last close
