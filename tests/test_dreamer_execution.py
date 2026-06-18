"""Task-1057 — tests for the H1-decision / M1-execution engine.

Every numeric assertion below traces to a worked example in the task spec
(sections 6/7) or to one of the four exit types (section 8). Pure-Python, no
data files, runs in milliseconds.
"""

from __future__ import annotations

import pytest

from novatrade.research.dreamer.execution import (
    BUY,
    HOLD,
    LONG,
    SELL,
    SHORT,
    Bar,
    Decision,
    open_trade,
    resolve_path,
    run_backtest,
    stop_loss,
    take_profit,
)


# --------------------------------------------------------------------------- #
# Section 6 — ATR stop loss (spec worked examples)
# --------------------------------------------------------------------------- #
def test_long_stop_matches_spec_example():
    # entry 2400, ATR 10, mult 1.5 -> 2385.00
    assert stop_loss(LONG, 2400.0, 10.0, 1.5) == pytest.approx(2385.0)


def test_short_stop_matches_spec_example():
    # entry 2400, ATR 10, mult 1.5 -> 2415.00
    assert stop_loss(SHORT, 2400.0, 10.0, 1.5) == pytest.approx(2415.0)


def test_stop_requires_positive_atr():
    with pytest.raises(ValueError):
        stop_loss(LONG, 2400.0, 0.0, 1.5)


# --------------------------------------------------------------------------- #
# Section 7 — take profit at tp_rr x risk (the 1.5 ATR / 2R == 3 ATR case)
# --------------------------------------------------------------------------- #
def test_long_target_is_two_r():
    # risk = 1.5*10 = 15; target = entry + 2*15 = 2430 (== entry + 3 ATR)
    stop = stop_loss(LONG, 2400.0, 10.0, 1.5)
    assert take_profit(LONG, 2400.0, stop, 2.0) == pytest.approx(2430.0)


def test_short_target_is_two_r():
    stop = stop_loss(SHORT, 2400.0, 10.0, 1.5)
    assert take_profit(SHORT, 2400.0, stop, 2.0) == pytest.approx(2370.0)


def test_open_trade_sets_levels_and_risk():
    t = open_trade(LONG, 2400.0, 10.0, Decision(BUY, 1.5, 2.0), entry_index=0)
    assert t.side == LONG
    assert t.stop == pytest.approx(2385.0)
    assert t.target == pytest.approx(2430.0)
    assert t.risk == pytest.approx(15.0)


# --------------------------------------------------------------------------- #
# Decision validation
# --------------------------------------------------------------------------- #
def test_hold_decision_ignores_sl_tp_and_is_flat():
    d = Decision(HOLD)
    assert d.side == 0


def test_entry_decision_rejects_nonpositive_params():
    with pytest.raises(ValueError):
        Decision(BUY, sl_atr_mult=0.0)
    with pytest.raises(ValueError):
        Decision(SELL, tp_rr=-1.0)


# --------------------------------------------------------------------------- #
# Section 8 — intrabar stop / target resolution
# --------------------------------------------------------------------------- #
def test_long_target_hit_on_path():
    t = open_trade(LONG, 2400.0, 10.0, Decision(BUY, 1.5, 2.0), 0)
    path = [Bar(2405, 2399, 2404), Bar(2431, 2425, 2430)]  # 2nd bar tags 2430
    assert resolve_path(t, path) == ("target", 2430.0)


def test_long_stop_hit_on_path():
    t = open_trade(LONG, 2400.0, 10.0, Decision(BUY, 1.5, 2.0), 0)
    path = [Bar(2402, 2384, 2386)]  # low 2384 < stop 2385
    assert resolve_path(t, path) == ("stop", 2385.0)


def test_short_stop_and_target_directions():
    t = open_trade(SHORT, 2400.0, 10.0, Decision(SELL, 1.5, 2.0), 0)
    assert resolve_path(t, [Bar(2416, 2410, 2415)]) == ("stop", 2415.0)
    assert resolve_path(t, [Bar(2402, 2369, 2370)]) == ("target", 2370.0)


def test_same_bar_straddle_takes_stop_first():
    # One M1 bar whose range covers BOTH levels -> conservative stop fill.
    t = open_trade(LONG, 2400.0, 10.0, Decision(BUY, 1.5, 2.0), 0)
    straddle = [Bar(2431, 2384, 2400)]  # high>=target AND low<=stop
    assert resolve_path(t, straddle) == ("stop", 2385.0)


def test_no_touch_returns_none():
    t = open_trade(LONG, 2400.0, 10.0, Decision(BUY, 1.5, 2.0), 0)
    assert resolve_path(t, [Bar(2405, 2395, 2400)]) is None


# --------------------------------------------------------------------------- #
# Backtest driver — full exit-type coverage
# --------------------------------------------------------------------------- #
def _flat_path(price: float) -> list[Bar]:
    """An M1 path that touches nothing (tight range around price)."""
    return [Bar(price + 0.1, price - 0.1, price)]


def test_target_exit_closes_with_positive_r():
    decisions = {0: Decision(BUY, 1.5, 2.0), 1: Decision(HOLD)}
    closes = [2400.0, 2432.0]
    atr = [10.0, 10.0]
    paths = [
        [Bar(2431.0, 2399.0, 2430.0)],  # bar 0: tags the 2430 target
        _flat_path(2432.0),
    ]
    trades = run_backtest(closes, atr, paths, lambda i: decisions[i])
    assert len(trades) == 1
    assert trades[0].exit_reason == "target"
    assert trades[0].r_multiple() == pytest.approx(2.0)


def test_stop_exit_closes_with_minus_one_r():
    decisions = {0: Decision(BUY, 1.5, 2.0), 1: Decision(HOLD)}
    closes = [2400.0, 2384.0]
    atr = [10.0, 10.0]
    paths = [[Bar(2401.0, 2384.0, 2385.0)], _flat_path(2384.0)]
    trades = run_backtest(closes, atr, paths, lambda i: decisions[i])
    assert trades[0].exit_reason == "stop"
    assert trades[0].r_multiple() == pytest.approx(-1.0)


def test_flip_close_then_reopen_opposite():
    # Long opened at bar 0 (never hit), SELL at bar 1 flips it flat then short.
    decisions = {0: Decision(BUY, 1.5, 2.0), 1: Decision(SELL, 1.5, 2.0), 2: Decision(HOLD)}
    closes = [2400.0, 2405.0, 2405.0]
    atr = [10.0, 10.0, 10.0]
    paths = [_flat_path(2400.0), _flat_path(2405.0), _flat_path(2405.0)]
    trades = run_backtest(closes, atr, paths, lambda i: decisions[i])
    # First trade closed by flip at the bar-1 decision price (2405).
    flip = trades[0]
    assert flip.exit_reason == "flip"
    assert flip.side == LONG
    assert flip.exit_price == pytest.approx(2405.0)
    # A new short was opened at the same close and force-closed at series end.
    assert trades[1].side == SHORT
    assert trades[1].entry_price == pytest.approx(2405.0)


def test_same_side_signal_does_not_reopen():
    # BUY while already long -> trade keeps running, no second trade created.
    decisions = {0: Decision(BUY, 1.5, 2.0), 1: Decision(BUY, 1.5, 2.0)}
    closes = [2400.0, 2401.0]
    atr = [10.0, 10.0]
    paths = [_flat_path(2400.0), _flat_path(2401.0)]
    trades = run_backtest(closes, atr, paths, lambda i: decisions[i])
    assert len(trades) == 1  # one trade, force-closed at the end
    assert trades[0].entry_index == 0


def test_manual_timeout_close():
    # Hold a long for 3 bars with max_holding_bars=2 -> manual close at bar 2.
    decisions = {i: (Decision(BUY, 1.5, 2.0) if i == 0 else Decision(HOLD)) for i in range(3)}
    closes = [2400.0, 2401.0, 2402.0]
    atr = [10.0, 10.0, 10.0]
    paths = [_flat_path(c) for c in closes]
    trades = run_backtest(closes, atr, paths, lambda i: decisions[i], max_holding_bars=2)
    assert trades[0].exit_reason == "manual"
    assert trades[0].exit_index == 2
    assert trades[0].exit_price == pytest.approx(2402.0)


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        run_backtest([2400.0, 2401.0], [10.0], [[], []], lambda i: Decision(HOLD))
