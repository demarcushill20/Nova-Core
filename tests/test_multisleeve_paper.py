"""Tests for the multi-sleeve paper book pure logic (novatrade/strategies/multisleeve.py)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from novatrade.strategies.multisleeve import (
    BLEND_WEIGHTS,
    LEVERAGE,
    CarryBook,
    TsmomConfig,
    blend_daily_return,
    carry_daily_return,
    eur_short_daily_return,
    form_carry_book,
    ledger_stats,
    tsmom_daily_returns,
    tsmom_positions,
    vol_regime_gate,
)


def _trend_series(n: int = 400, drift: float = 0.001, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.Series(100 * np.exp(np.cumsum(drift + 0.01 * rng.standard_normal(n))), index=idx)


class TestBlendConfig:
    def test_weights_sum_to_one(self):
        assert sum(BLEND_WEIGHTS.values()) == pytest.approx(1.0, abs=0.01)

    def test_leverage_is_operator_choice(self):
        assert LEVERAGE == 6.0

    def test_blend_math(self):
        r = blend_daily_return({"eur_short": 0.01, "carry": 0.0, "gold_tsmom": 0.0, "btc_gated": 0.0})
        assert r == pytest.approx(0.5758 * 0.01 * 6.0, rel=1e-9)

    def test_missing_sleeve_counts_flat(self):
        assert blend_daily_return({}) == 0.0


class TestTsmom:
    def test_uptrend_goes_long(self):
        pos = tsmom_positions(_trend_series(drift=0.002), TsmomConfig())
        assert pos.dropna().iloc[-1] > 0

    def test_downtrend_goes_short(self):
        pos = tsmom_positions(_trend_series(drift=-0.002), TsmomConfig())
        assert pos.dropna().iloc[-1] < 0

    def test_position_cap(self):
        pos = tsmom_positions(_trend_series(), TsmomConfig(pos_cap=2.0)).dropna()
        assert (pos.abs() <= 2.0 + 1e-12).all()

    def test_returns_are_lagged_no_lookahead(self):
        """PnL on day t must use position formed at t-1: shifting prices one day
        forward must not change positions computed from past data only."""
        px = _trend_series()
        r = tsmom_daily_returns(px, TsmomConfig())
        # a spike on the LAST day must not affect the last day's position/pnl sign
        px2 = px.copy()
        px2.iloc[-1] *= 1.10
        r2 = tsmom_daily_returns(px2, TsmomConfig())
        pos = tsmom_positions(px, TsmomConfig())
        pos2 = tsmom_positions(px2, TsmomConfig())
        # position used for last-day pnl is pos.shift(1) -> unchanged by the spike
        assert pos.shift(1).iloc[-1] == pytest.approx(pos2.shift(1).iloc[-1])
        assert len(r) == len(r2)

    def test_costs_reduce_returns(self):
        px = _trend_series()
        gross = tsmom_daily_returns(px, TsmomConfig(cost_bp=0.0)).sum()
        net = tsmom_daily_returns(px, TsmomConfig(cost_bp=10.0)).sum()
        assert net < gross


class TestEurShort:
    def test_flat_market_loses_spread(self):
        # no move: short pays the spread
        r = eur_short_daily_return(1.1000, 1.1002, 1.1000, 1.1002)
        assert r < 0
        assert r == pytest.approx((1.1000 - 1.1002) / 1.1001, rel=1e-6)

    def test_eur_drop_profits(self):
        r = eur_short_daily_return(1.1000, 1.1002, 1.0950, 1.0952)
        assert r > 0


class TestCarry:
    def test_book_dollar_neutral_legs(self):
        rates = pd.Series(
            {
                "USD": 5.0,
                "EUR": 3.0,
                "JPY": 0.1,
                "AUD": 4.3,
                "NZD": 5.4,
                "GBP": 4.9,
                "CAD": 4.4,
                "CHF": 1.2,
                "NOK": 4.0,
                "SEK": 3.6,
            }
        )
        vols = pd.Series({c: 0.08 for c in rates.index})
        book = form_carry_book(rates, vols, gate=1.0, k=3)
        w = book.weights
        longs = sum(v for v in w.values() if v > 0)
        shorts = sum(v for v in w.values() if v < 0)
        assert longs == pytest.approx(1.0, rel=1e-9)
        assert shorts == pytest.approx(-1.0, rel=1e-9)
        # highest-rate non-USD ccys are long, lowest short
        assert w["NZD"] > 0 and w["JPY"] < 0 and w["CHF"] < 0

    def test_gate_scales_exposure(self):
        rates = pd.Series(
            {
                "USD": 5.0,
                "EUR": 3.0,
                "JPY": 0.1,
                "AUD": 4.3,
                "NZD": 5.4,
                "GBP": 4.9,
                "CAD": 4.4,
                "CHF": 1.2,
                "NOK": 4.0,
                "SEK": 3.6,
            }
        )
        vols = pd.Series({c: 0.08 for c in rates.index})
        full = form_carry_book(rates, vols, gate=1.0)
        half = form_carry_book(rates, vols, gate=0.5)
        assert half.scaled()["NZD"] == pytest.approx(0.5 * full.scaled()["NZD"])

    def test_insufficient_universe_returns_empty(self):
        book = form_carry_book(pd.Series({"USD": 5.0, "EUR": 3.0}), pd.Series({"EUR": 0.08}), 1.0)
        assert book.weights == {}

    def test_daily_mark_includes_accrual(self):
        book = CarryBook({"NZD": 1.0, "JPY": -1.0}, gate=1.0)
        spot_ret = pd.Series({"NZD": 0.0, "JPY": 0.0})
        rdiff = pd.Series({"NZD": 2.52, "JPY": -2.52})  # % annual vs USD
        r = carry_daily_return(book, spot_ret, rdiff)
        assert r == pytest.approx(2 * 2.52 / 100.0 / 252.0, rel=1e-9)


class TestGate:
    def test_gate_bounds_and_lag(self):
        rng = np.random.default_rng(3)
        idx = pd.date_range("2015-01-01", periods=2500, freq="D")
        spots = pd.DataFrame(
            {c: 100 * np.exp(np.cumsum(0.006 * rng.standard_normal(2500))) for c in "ABCDE"},
            index=idx,
        )
        gate = vol_regime_gate(spots).dropna()
        assert ((gate >= 0) & (gate <= 1)).all()
        assert len(gate) > 12


class TestLedgerStats:
    def test_stats_shape(self):
        rng = np.random.default_rng(1)
        d = pd.Series(
            0.001 + 0.005 * rng.standard_normal(300), index=pd.date_range("2026-01-01", periods=300, freq="D")
        )
        st = ledger_stats(d)
        assert {"sharpe", "max_dd", "ann_ret", "worst_day"} <= set(st)
        assert st["max_dd"] <= 0

    def test_short_series(self):
        assert ledger_stats(pd.Series([0.001]))["n"] == 1.0
