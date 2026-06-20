"""Tests for the pure two-sleeve risk allocator + combined backtest."""

from __future__ import annotations

import numpy as np
import pandas as pd

from novatrade.portfolio.two_sleeve_allocator import (
    AllocatorConfig,
    combined_backtest,
    risk_weights,
    sleeve_sizing,
)


class TestRiskWeights:
    def test_sharpe_proportional_at_full_ramp(self):
        w_eur, w_carry = risk_weights(1.1, 0.4, carry_cap=0.27)
        assert abs(w_carry - 0.2667) < 1e-3
        assert abs(w_eur + w_carry - 1.0) < 1e-9

    def test_ramp_cap_binds_early(self):
        w_eur, w_carry = risk_weights(1.1, 0.4, carry_cap=0.15)
        assert abs(w_carry - 0.15) < 1e-9
        assert abs(w_eur - 0.85) < 1e-9


class TestSleeveSizing:
    def test_capital_split_matches_weights(self):
        cfg = AllocatorConfig(sharpe_eur=1.1, sharpe_carry=0.4, carry_cap=0.27)
        out = sleeve_sizing(100_000, cfg)
        assert abs(out["eur"]["risk_weight"] + out["carry"]["risk_weight"] - 1.0) < 1e-9
        assert out["carry"]["risk_weight"] < out["eur"]["risk_weight"]


class TestCombinedBacktest:
    def test_uncorrelated_blend_is_well_formed(self):
        rng = np.random.default_rng(0)
        idx = pd.date_range("2010-01-31", periods=120, freq="ME")
        eur = pd.Series(rng.normal(0.010, 0.03, 120), index=idx)
        carry = pd.Series(rng.normal(0.003, 0.02, 120), index=idx)
        out = combined_backtest(eur, carry, w_eur=0.73, w_carry=0.27)
        assert -0.25 < out["corr"] < 0.25
        assert out["blended_sharpe"] >= 0.0
        assert out["n"] == 120

    def test_maxdd_is_sane_not_degenerate(self):
        rng = np.random.default_rng(1)
        idx = pd.date_range("2010-01-31", periods=200, freq="ME")
        eur = pd.Series(rng.normal(0.010, 0.03, 200), index=idx)
        carry = pd.Series(rng.normal(0.003, 0.02, 200), index=idx)
        out = combined_backtest(eur, carry, 0.73, 0.27)
        assert -1.0 < out["maxdd"] <= 0.0  # realistic drawdown, not degenerate
