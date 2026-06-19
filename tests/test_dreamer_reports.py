"""Task-1061 — tests for the DreamerV3 gold-bot performance report layer.

Pins every metric the spec's "Reports" section names: training/validation/test
return, annualized return, max drawdown, monthly/weekday/hour performance, trade
counts, exit-reason breakdown, SL-multiplier and TP-R distributions, rolling
Sharpe, and the EMA baseline comparison.
"""

from __future__ import annotations

import pandas as pd
import pytest

from novatrade.research.dreamer import reports
from novatrade.research.dreamer.execution import (
    BUY,
    HOLD,
    LONG,
    SHORT,
    Bar,
    Trade,
    run_backtest,
)


# --------------------------------------------------------------------------- #
# Trade builders
# --------------------------------------------------------------------------- #
def _trade(
    *,
    side=LONG,
    r,
    exit_reason="target",
    entry_index=0,
    exit_index=0,
    sl_atr_mult=1.5,
    tp_rr=2.0,
):
    """A closed trade engineered to realize exactly ``r`` R.

    risk is fixed at 1.0 (entry 100, stop 99 for long), so the signed price move
    needed for ``r`` R is just ``r`` (long) or ``-r`` (short).
    """
    entry = 100.0
    if side == LONG:
        stop = 99.0
        exit_price = entry + r  # r_multiple = (exit-entry)/1
    else:
        stop = 101.0
        exit_price = entry - r  # short: side*(exit-entry) = -(exit-100) = r => exit = 100-r
    return Trade(
        side=side,
        entry_price=entry,
        stop=stop,
        target=entry + 2 if side == LONG else entry - 2,
        entry_index=entry_index,
        sl_atr_mult=sl_atr_mult,
        tp_rr=tp_rr,
        exit_price=exit_price,
        exit_index=exit_index,
        exit_reason=exit_reason,
    )


# --------------------------------------------------------------------------- #
# Returns / drawdown / Sharpe
# --------------------------------------------------------------------------- #
def test_total_return_sums_r_multiples():
    trades = [_trade(r=1.0), _trade(r=-0.5), _trade(r=2.0)]
    assert reports.total_return(trades) == pytest.approx(2.5)


def test_open_trades_are_excluded():
    closed = _trade(r=1.0)
    open_t = Trade(side=LONG, entry_price=100, stop=99, target=102, entry_index=0)
    assert reports.total_return([closed, open_t]) == pytest.approx(1.0)
    assert reports.trade_counts([closed, open_t])["total"] == 1


def test_equity_curve_is_cumulative():
    trades = [_trade(r=1.0), _trade(r=-0.5), _trade(r=2.0)]
    assert reports.equity_curve(trades) == pytest.approx([1.0, 0.5, 2.5])


def test_max_drawdown_is_non_negative_peak_to_trough():
    # cumulative: +2, -1(=1), -3(=-2). Peak 2 -> trough -2 => drawdown 4.
    trades = [_trade(r=2.0), _trade(r=-1.0), _trade(r=-3.0)]
    assert reports.max_drawdown(trades) == pytest.approx(4.0)


def test_max_drawdown_zero_when_monotonic_up():
    trades = [_trade(r=1.0), _trade(r=1.0)]
    assert reports.max_drawdown(trades) == pytest.approx(0.0)


def test_rolling_sharpe_empty_when_too_few_trades():
    trades = [_trade(r=1.0) for _ in range(5)]
    assert reports.rolling_sharpe(trades, window=20) == []


def test_rolling_sharpe_produces_values_with_enough_trades():
    trades = [_trade(r=1.0 if i % 2 else -0.5) for i in range(25)]
    sharpe = reports.rolling_sharpe(trades, window=10)
    # All closed trades yield a value (warmup positions report 0.0 rather than
    # being dropped, since their zero-dispersion std is coerced to 0.0).
    assert len(sharpe) == 25
    assert all(isinstance(x, float) for x in sharpe)
    # The steady alternating series has a genuine, non-zero Sharpe once warmed up.
    assert any(x != 0.0 for x in sharpe)


def test_rolling_sharpe_window_must_exceed_one():
    with pytest.raises(ValueError):
        reports.rolling_sharpe([_trade(r=1.0)], window=1)


# --------------------------------------------------------------------------- #
# Counts and distributions
# --------------------------------------------------------------------------- #
def test_trade_counts_split_long_short():
    trades = [_trade(side=LONG, r=1.0), _trade(side=SHORT, r=1.0), _trade(side=LONG, r=-1.0)]
    counts = reports.trade_counts(trades)
    assert counts == {"total": 3, "long": 2, "short": 1}


def test_exit_reason_breakdown_maps_engine_labels_and_zero_fills():
    trades = [
        _trade(r=1.0, exit_reason="stop"),
        _trade(r=1.0, exit_reason="target"),
        _trade(r=1.0, exit_reason="target"),
        _trade(r=1.0, exit_reason="flip"),
    ]
    breakdown = reports.exit_reason_breakdown(trades)
    assert breakdown == {
        "stop_loss": 1,
        "take_profit": 2,
        "flip_close": 1,
        "manual_close": 0,  # present even with zero occurrences
    }


def test_sl_and_tp_distributions():
    trades = [
        _trade(r=1.0, sl_atr_mult=1.5, tp_rr=2.0),
        _trade(r=1.0, sl_atr_mult=1.5, tp_rr=3.0),
        _trade(r=1.0, sl_atr_mult=2.0, tp_rr=2.0),
    ]
    assert reports.sl_mult_distribution(trades) == {1.5: 2, 2.0: 1}
    assert reports.tp_rr_distribution(trades) == {2.0: 2, 3.0: 1}


# --------------------------------------------------------------------------- #
# Time-bucketed metrics (require timestamps)
# --------------------------------------------------------------------------- #
def _timestamps(n, start="2026-01-01", freq="h"):
    return list(pd.date_range(start=start, periods=n, freq=freq))


def test_time_metrics_empty_without_timestamps():
    trades = [_trade(r=1.0)]
    assert reports.monthly_returns(trades, None) == {}
    assert reports.weekday_performance(trades, None) == {}
    assert reports.hour_performance(trades, None) == {}
    assert reports.annualized_return(trades, None) is None


def test_monthly_returns_grouped_by_exit_month():
    ts = _timestamps(2000, freq="h")
    # exit_index 10 -> Jan, exit_index 1000 (~41 days later) -> Feb.
    t1 = _trade(r=1.0, exit_index=10)
    t2 = _trade(r=2.0, exit_index=1000)
    monthly = reports.monthly_returns([t1, t2], ts)
    assert ts[10].strftime("%Y-%m") == "2026-01"
    assert monthly["2026-01"] == pytest.approx(1.0)
    assert monthly[ts[1000].strftime("%Y-%m")] == pytest.approx(2.0)


def test_weekday_and_hour_performance_keys():
    ts = _timestamps(100, start="2026-01-01", freq="h")  # 2026-01-01 is a Thursday (=3)
    t = _trade(r=1.5, exit_index=5)
    wk = reports.weekday_performance([t], ts)
    hr = reports.hour_performance([t], ts)
    assert wk == {ts[5].weekday(): pytest.approx(1.5)}
    assert hr == {ts[5].hour: pytest.approx(1.5)}


def test_annualized_return_scales_by_span():
    # 1 year span (8760 hourly bars), total +2R -> ~2R/yr.
    ts = _timestamps(9000, freq="h")
    t = _trade(r=2.0, entry_index=0, exit_index=8760)
    ann = reports.annualized_return([t], ts)
    assert ann == pytest.approx(2.0, rel=0.02)


# --------------------------------------------------------------------------- #
# EMA baseline
# --------------------------------------------------------------------------- #
def test_ema_baseline_holds_until_slow_ema_defined():
    closes = [100.0] * 40
    policy = reports.ema_baseline_policy(closes, fast=10, slow=30)
    assert policy(5).action == HOLD  # before slow EMA is meaningful
    # flat closes -> fast == slow -> HOLD even after warmup
    assert policy(35).action == HOLD


def test_ema_baseline_goes_long_on_uptrend():
    closes = [100.0] * 30 + [100.0 + i for i in range(1, 30)]  # strong uptrend after warmup
    policy = reports.ema_baseline_policy(closes, fast=10, slow=30)
    assert policy(len(closes) - 1).action == BUY


def test_ema_baseline_rejects_bad_spans():
    with pytest.raises(ValueError):
        reports.ema_baseline_policy([100.0] * 10, fast=30, slow=10)


def test_ema_baseline_runs_through_backtest():
    closes = [100.0 + i for i in range(60)]
    atr = [1.0] * 60
    # M1 path that never hits stop/target so trades close via flip/manual only.
    paths = [[Bar(high=c + 0.1, low=c - 0.1, close=c)] for c in closes]
    policy = reports.ema_baseline_policy(closes, fast=5, slow=15)
    trades = run_backtest(closes, atr, paths, policy, max_holding_bars=100)
    assert isinstance(trades, list)


def test_baseline_comparison():
    strat = [_trade(r=2.0), _trade(r=1.0)]  # +3R
    base = [_trade(r=0.5), _trade(r=0.5)]  # +1R
    cmp = reports.baseline_comparison(strat, base)
    assert cmp["strategy_return"] == pytest.approx(3.0)
    assert cmp["baseline_return"] == pytest.approx(1.0)
    assert cmp["delta"] == pytest.approx(2.0)
    assert cmp["strategy_outperforms"] is True


# --------------------------------------------------------------------------- #
# Assembled report
# --------------------------------------------------------------------------- #
def test_build_report_assembles_every_field():
    ts = _timestamps(2000, freq="h")
    trades = [
        _trade(side=LONG, r=2.0, exit_reason="target", exit_index=10),
        _trade(side=SHORT, r=-1.0, exit_reason="stop", exit_index=50),
        _trade(side=LONG, r=1.0, exit_reason="flip", exit_index=100),
    ]
    report = reports.build_report(trades, ts, rolling_window=20)
    assert report.total_return == pytest.approx(2.0)
    assert report.max_drawdown >= 0.0
    assert report.trade_counts == {"total": 3, "long": 2, "short": 1}
    assert report.exit_reason_breakdown["take_profit"] == 1
    assert report.exit_reason_breakdown["stop_loss"] == 1
    assert report.exit_reason_breakdown["flip_close"] == 1
    assert set(report.sl_mult_distribution) <= {1.5, 2.0}
    assert report.monthly_returns  # non-empty with timestamps
    assert isinstance(report.rolling_sharpe, list)
    assert report.rolling_sharpe == []  # only 3 trades < window 20


def test_build_report_without_timestamps_leaves_time_fields_empty():
    trades = [_trade(r=1.0), _trade(r=-0.5)]
    report = reports.build_report(trades, None)
    assert report.annualized_return is None
    assert report.monthly_returns == {}
    assert report.weekday_performance == {}
    assert report.hour_performance == {}
    assert report.total_return == pytest.approx(0.5)
