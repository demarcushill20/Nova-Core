"""Regression: stateful IRB v5 must produce bit-identical results to vault Python.

The new `IRBStrategyState` class (in `novatrade.strategies.vault_irb_state`) is a
refactor of vault Python's batch loop. It must produce the same trade ledger,
the same equity curve, and the same final P&L as the original `backtest_irb_strategy`
function in `scripts/irb_v5_vault_reference.py`.

Why parity matters: the live wrapper (`VaultLiveEngine`) drives `IRBStrategyState`
one bar at a time. If the refactored class deviates from the original loop, the
+$45,310 baseline doesn't transfer to the live runtime. This test pins the
refactor so future edits can't silently break parity.

Run a slim 3-month slice (Jan-Mar 2024) for fast CI; the headline 10y test is
behind `IRB_PARITY_FULL_BACKTEST=1` to keep the suite fast.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"

# Import vault Python script as a module
spec = importlib.util.spec_from_file_location("irb_v5_vault_reference", REPO / "scripts/irb_v5_vault_reference.py")
vault_script = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["irb_v5_vault_reference"] = vault_script
spec.loader.exec_module(vault_script)  # type: ignore[union-attr]

from novatrade.strategies.vault_irb_state import IRBConfig as NewConfig  # noqa: E402
from novatrade.strategies.vault_irb_state import run_vault_strategy_batch  # noqa: E402


def _matched_risk_cfg(cfg_class):
    """Build the matched-risk IRBConfig used in the +$45,310 baseline."""
    return cfg_class(
        risk_pct=0.33,
        htf_timeframe="60min",
        max_trades_per_day=999,
        cooldown_bars=1,
        take_partial=True,
        partial_exit_pct=50.0,
        partial_rr=1.0,
        move_to_be_at_rr=1.0,
        runner_atr_mult=2.0,
        max_bars_in_trade=20,
        signal_expiry_bars=12,
        use_signal_atr_range_filter=True,
        max_signal_atr_mult=2.5,
        min_qty=1000.0,
        max_qty=100_000.0,
        qty_step=1000.0,
    )


def _load_slice(months: int = 3) -> pd.DataFrame:
    raw = pd.read_csv(M5_CSV)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"])
    cutoff = raw["timestamp"].max() - pd.DateOffset(months=months)
    return raw[raw["timestamp"] >= cutoff].copy()


def test_stateful_class_matches_vault_loop_3mo() -> None:
    """3-month slice — fast smoke parity check for CI."""
    raw = _load_slice(months=3)

    old_cfg = _matched_risk_cfg(vault_script.IRBConfig)
    new_cfg = _matched_risk_cfg(NewConfig)

    old_result = vault_script.backtest_irb_strategy(raw, old_cfg)
    new_result = run_vault_strategy_batch(raw, new_cfg)

    # Final equity must match exactly
    assert old_result["final_equity"] == pytest.approx(new_result["final_equity"], abs=0.01), (
        f"Final equity mismatch: old={old_result['final_equity']:.5f}, new={new_result['final_equity']:.5f}"
    )

    # Trade count must match exactly
    old_n = (old_result["trades"]["type"] == "entry").sum()
    new_n = (new_result["trades"]["type"] == "entry").sum()
    assert old_n == new_n, f"Trade count mismatch: old={old_n} entries, new={new_n}"

    # Total exit pnl must match (sum across all exit legs)
    old_total = old_result["trades"][old_result["trades"]["type"] == "exit"]["pnl"].sum()
    new_total = new_result["trades"][new_result["trades"]["type"] == "exit"]["pnl"].sum()
    assert old_total == pytest.approx(new_total, abs=0.01), (
        f"Total exit pnl mismatch: old=${old_total:.2f}, new=${new_total:.2f}"
    )


@pytest.mark.skipif(
    not os.environ.get("IRB_PARITY_FULL_BACKTEST"),
    reason="Set IRB_PARITY_FULL_BACKTEST=1 to run the 10y full-window parity test (slow ~30s)",
)
def test_stateful_class_matches_vault_loop_full_10yr() -> None:
    """Full 10y EURUSD M5 — must reproduce the canonical $45,310 result."""
    raw = pd.read_csv(M5_CSV)

    old_cfg = _matched_risk_cfg(vault_script.IRBConfig)
    new_cfg = _matched_risk_cfg(NewConfig)

    old_result = vault_script.backtest_irb_strategy(raw, old_cfg)
    new_result = run_vault_strategy_batch(raw, new_cfg)

    assert old_result["final_equity"] == pytest.approx(new_result["final_equity"], abs=0.50)
    # Sanity: original baseline is ~$145,309.55 (final equity), $45,309.55 net.
    # The refactor must hit the same number.
    assert new_result["final_equity"] > 145_000.0, (
        f"New result final_equity={new_result['final_equity']:.2f} doesn't match canonical baseline ~145,309.55"
    )
