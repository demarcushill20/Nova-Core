"""Tests for the fidelity backtester (novatrade.backtest.fidelity).

Pins:
1. With the default single-bar resolver it reproduces the stock vault engine
   (the injectable fill hook is behaviour-preserving). [fast slice]
2. Resolving fills from real sub-bars changes the outcome. [fast slice]
3. REGRESSION: on the FULL 10yr sample, canonical IRB-on-H1's apparent edge
   collapses under real-sub-bar fills — it was a fill artifact. Run on the full
   dataset on purpose: the effect is a large-sample property and is noisy on
   short slices, so a slice-based assertion would be flaky.

Skipped when the M5 fixture isn't present.
"""

from __future__ import annotations

import os

import pytest

from novatrade.strategies.vault_irb_state import IRBConfig

_M5_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "candles", "EURUSD_M5_10yr.csv")


def _load(tail: int | None):
    import pandas as pd

    raw = pd.read_csv(_M5_PATH)
    raw["time"] = pd.to_datetime(raw["timestamp"])
    if tail:
        raw = raw.tail(tail).reset_index(drop=True)
    return raw


@pytest.mark.skipif(not os.path.exists(_M5_PATH), reason="EURUSD_M5_10yr.csv fixture not present")
class TestFidelityStructure:
    """Fast structural tests on a small slice."""

    @pytest.fixture(scope="class")
    def runs(self):
        from novatrade.backtest.fidelity import _resample_with_subs, run_fidelity_backtest
        from novatrade.strategies.vault_irb_state import run_vault_strategy_batch

        raw = _load(tail=120_000)
        cfg = IRBConfig()
        primary, _ = _resample_with_subs(raw, "60min")
        return {
            "vault": run_vault_strategy_batch(primary, cfg),
            "heuristic": run_fidelity_backtest(raw, "60min", cfg, fine_fills=False),
            "fidelity": run_fidelity_backtest(raw, "60min", cfg, fine_fills=True),
        }

    def test_default_resolver_reproduces_vault(self, runs):
        v_net = runs["vault"]["net_profit"]
        h_net = runs["heuristic"].net_pnl
        assert abs(v_net - h_net) <= max(1.0, abs(v_net) * 1e-6), f"vault {v_net} vs heuristic {h_net}"

    def test_fine_fills_change_outcome(self, runs):
        assert runs["fidelity"].net_pnl != runs["heuristic"].net_pnl
        assert runs["heuristic"].total_trades > 50

    def test_edge_metrics_are_finite(self, runs):
        import math

        for r in (runs["heuristic"], runs["fidelity"]):
            assert math.isfinite(r.edge_r)
            assert math.isfinite(r.breakeven_cost_pips)
            assert r.median_stop_pips > 0


@pytest.mark.skipif(not os.path.exists(_M5_PATH), reason="EURUSD_M5_10yr.csv fixture not present")
class TestFidelityArtifactRegression:
    """The headline regression on the FULL 10yr sample (~90s): canonical
    IRB-on-H1's +0.117R edge collapses to ~0 / slightly negative under honest
    intra-bar fills. Guards against ever re-trusting the artifact."""

    @pytest.fixture(scope="class")
    def full(self):
        from novatrade.backtest.fidelity import run_fidelity_backtest

        raw = _load(tail=None)
        cfg = IRBConfig()
        return {
            "heuristic": run_fidelity_backtest(raw, "60min", cfg, fine_fills=False),
            "fidelity": run_fidelity_backtest(raw, "60min", cfg, fine_fills=True),
        }

    def test_heuristic_shows_phantom_edge(self, full):
        h = full["heuristic"]
        assert h.edge_r > 0.08, f"heuristic edge {h.edge_r}"
        assert h.breakeven_cost_pips > 0.5, f"heuristic breakeven {h.breakeven_cost_pips}"

    def test_edge_collapses_under_fidelity(self, full):
        h, f = full["heuristic"], full["fidelity"]
        # edge erased: drops to <=20% of the heuristic AND to roughly zero
        assert f.edge_r <= h.edge_r * 0.2, f"edge did not collapse: {f.edge_r} vs {h.edge_r}"
        assert f.edge_r < 0.03, f"fidelity edge unexpectedly high: {f.edge_r}"
        assert f.breakeven_cost_pips < 0.2, f"fidelity breakeven {f.breakeven_cost_pips}"
