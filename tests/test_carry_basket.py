"""Tests for the pure carry-basket strategy logic."""

from __future__ import annotations

import numpy as np
import pandas as pd

from novatrade.strategies.carry_basket import (
    CarryConfig,
    carry_backtest,
    derisk_scalar,
    rank_weights,
)


class TestRankWeights:
    def test_dollar_neutral_and_directional(self):
        rates = pd.Series({"USD": 1.0, "EUR": 0.5, "GBP": 2.0, "JPY": 0.1, "AUD": 5.0, "NZD": 4.0, "CAD": 3.0})
        w = rank_weights(rates, k_frac=0.34)
        assert abs(w.sum()) < 1e-9
        assert w["AUD"] > 0 and w["NZD"] > 0
        assert w["JPY"] < 0 and w["EUR"] < 0
        assert w["CAD"] == 0.0

    def test_too_few_currencies_returns_zero(self):
        rates = pd.Series({"USD": 1.0, "EUR": 0.5, "GBP": 2.0})
        w = rank_weights(rates, k_frac=0.34)
        assert (w == 0.0).all()


class TestDeriskScalar:
    def test_never_levers_above_cap(self):
        cfg = CarryConfig(vol_target=0.08, vol_window=6, lev_cap=1.0)
        calm = pd.Series([0.001] * 6)
        assert derisk_scalar(calm, cfg) == 1.0

    def test_derisks_when_vol_high(self):
        cfg = CarryConfig(vol_target=0.08, vol_window=6, lev_cap=1.0)
        wild = pd.Series([0.10, -0.10, 0.10, -0.10, 0.10, -0.10])
        assert derisk_scalar(wild, cfg) < 1.0

    def test_insufficient_history_returns_cap(self):
        cfg = CarryConfig(vol_window=6, lev_cap=1.0)
        assert derisk_scalar(pd.Series([0.01, 0.02]), cfg) == 1.0


class TestCarryBacktestReproducesResearch:
    def test_derisk_only_coherent_basket(self):
        # Bands bracket the COHERENT construction: carry_backtest uses rank_weights
        # as the single basket constructor (k = round(n_available * k_frac)), so the
        # long/short legs never overlap and live == backtest. These are NOT the
        # numbers from the probe's flawed fixed-k basket (which held some currencies
        # long AND short simultaneously in ~44% of months). Full-history measured:
        # Sharpe 0.535 / maxDD -0.311 (vs the old overlapping 0.47 / -0.22).
        from scripts.probe_carry_portfolio import build_panel

        S, R = build_panel()
        cfg = CarryConfig()
        r = carry_backtest(S, R, cfg).dropna()
        sharpe = r.mean() / r.std() * np.sqrt(12)
        eq = (1 + r).cumprod()
        maxdd = (eq / eq.cummax() - 1).min()
        assert 0.49 <= sharpe <= 0.58
        assert -0.34 <= maxdd <= -0.28


class TestNoOverlappingLegs:
    def test_no_overlapping_legs(self):
        # Over the full build_panel history, reconstruct each traded month's basket
        # via rank_weights on its available lagged rates and assert the long/short
        # name-sets are disjoint (the coherence guarantee of the single constructor).
        from scripts.probe_carry_portfolio import build_panel

        S, R = build_panel()
        cfg = CarryConfig()
        ccys = [c for c in cfg.currencies if c in S.columns and c in R.columns]
        Rr = R[ccys]
        spot_ret = S[ccys].pct_change()
        rdiff = Rr.sub(Rr["USD"], axis=0) / 1200.0
        xs = spot_ret + rdiff.shift(1)
        rrank = Rr.shift(1)
        traded = 0
        for dt in xs.index[2:]:
            rk = rrank.loc[dt].dropna()
            x = xs.loc[dt].dropna()
            avail = [c for c in rk.index if c in x.index]
            if len(avail) < 4:
                continue
            w = rank_weights(rk[avail], cfg.k_frac)
            if (w == 0.0).all():
                continue
            traded += 1
            longs = set(w[w > 0].index)
            shorts = set(w[w < 0].index)
            assert not (longs & shorts), f"overlap at {dt}: {longs & shorts}"
        assert traded > 0
