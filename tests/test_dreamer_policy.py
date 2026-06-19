"""Task-1060 — tests for the actor->Decision bridge and position sizing."""

import pytest

from novatrade.research.dreamer import config, execution, policy


# --- position_size --------------------------------------------------------- #
def test_position_size_risks_exactly_risk_pct():
    # 0.5% of $100k = $500 risk; $5 stop => 100 units.
    assert policy.position_size(100_000, 5.0, risk_pct=0.005) == pytest.approx(100.0)


def test_position_size_scales_with_value_per_point():
    base = policy.position_size(100_000, 5.0, risk_pct=0.005, value_per_point=1.0)
    assert policy.position_size(100_000, 5.0, risk_pct=0.005, value_per_point=2.0) == pytest.approx(base / 2)


def test_position_size_uses_config_default_risk_pct():
    assert policy.position_size(10_000, 1.0) == pytest.approx(10_000 * config.DEFAULT.risk_pct)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"equity": 0.0, "stop_distance": 1.0},
        {"equity": 1.0, "stop_distance": 0.0},
        {"equity": 1.0, "stop_distance": 1.0, "risk_pct": 0.0},
        {"equity": 1.0, "stop_distance": 1.0, "risk_pct": 1.0},
        {"equity": 1.0, "stop_distance": 1.0, "value_per_point": 0.0},
    ],
)
def test_position_size_rejects_bad_inputs(kwargs):
    with pytest.raises(ValueError):
        policy.position_size(**kwargs)


# --- actions_to_policy ----------------------------------------------------- #
def test_actions_to_policy_maps_index_to_decision():
    pol = policy.actions_to_policy([execution.HOLD, execution.BUY, execution.SELL])
    assert pol(0).action == execution.HOLD
    assert pol(1).side == execution.LONG
    assert pol(2).side == execution.SHORT
    # defaults come from config
    assert pol(1).sl_atr_mult == config.DEFAULT.atr_stop_mult
    assert pol(1).tp_rr == config.DEFAULT.take_profit_rr


def test_actions_to_policy_per_bar_levels_override():
    pol = policy.actions_to_policy([execution.BUY], levels=[(2.0, 3.0)])
    d = pol(0)
    assert d.sl_atr_mult == 2.0
    assert d.tp_rr == 3.0


def test_actions_to_policy_rejects_mismatched_levels():
    with pytest.raises(ValueError):
        policy.actions_to_policy([execution.BUY, execution.HOLD], levels=[(1.0, 2.0)])


def test_actions_to_policy_out_of_range():
    pol = policy.actions_to_policy([execution.HOLD])
    with pytest.raises(IndexError):
        pol(5)


# --- actor_actions --------------------------------------------------------- #
def test_actor_actions_maps_observations():
    # actor that buys on positive obs, sells on negative, holds on zero.
    def actor_fn(x):
        return execution.BUY if x > 0 else execution.SELL if x < 0 else execution.HOLD

    assert policy.actor_actions(actor_fn, [1.0, -1.0, 0.0]) == [
        execution.BUY,
        execution.SELL,
        execution.HOLD,
    ]


def test_actor_actions_rejects_bad_action():
    with pytest.raises(ValueError):
        policy.actor_actions(lambda x: 7, [1.0])


# --- end-to-end: bridge drives the real execution engine ------------------- #
def test_policy_drives_run_backtest_end_to_end():
    # Two H1 bars: buy at bar 0, then a flip-to-sell signal at bar 1.
    actions = [execution.BUY, execution.SELL]
    pol = policy.actions_to_policy(actions, sl_atr_mult=1.0, tp_rr=2.0)

    decision_close = [100.0, 101.0]
    atr = [1.0, 1.0]
    # M1 path after bar 0 never hits the 99.0 stop or 102.0 target, so the
    # bar-1 opposite signal closes the long via "flip" at 101.0.
    m1_paths = [
        [execution.Bar(high=100.5, low=99.5, close=100.0)],
        [execution.Bar(high=101.0, low=101.0, close=101.0)],
    ]
    trades = execution.run_backtest(decision_close, atr, m1_paths, pol)

    # The bar-1 SELL flips the long: trade 0 closes "flip" at 101.0, and the
    # engine reverses into a short which is force-closed "manual" at series end.
    assert len(trades) == 2
    assert trades[0].side == execution.LONG
    assert trades[0].exit_reason == "flip"
    assert trades[0].pnl() == pytest.approx(1.0)  # 101 - 100
    assert trades[1].side == execution.SHORT
    assert trades[1].exit_reason == "manual"
    assert trades[1].pnl() == pytest.approx(0.0)  # opened and closed at 101.0
