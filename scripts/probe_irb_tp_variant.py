"""Validate the exit-probe lead through the REAL engine, across regimes.

The exit-manager probe reduced to a one-line rule: "close the full position at
~+1R instead of partial+runner trail." That's a strategy-parameter change, so we
test it properly — full fidelity engine, real M1 fills, 0.5p cost — and crucially
PER YEAR, because the probe's edge is suspected to be regime-specific (cutting at
1R helps in chop, hurts in trends; the runner exists to catch trends).

Variants (everything else = canonical v5):
  baseline : partial 50% @1R + 2xATR runner trail   (the validated champion)
  tp1.0    : close 100% @1R                          (no runner)
  tp1.5    : close 100% @1.5R
  tp2.0    : close 100% @2.0R

A real edge beats baseline in MOST years incl. trending ones. A regime artifact
only wins the range-bound years.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np
import pandas as pd

from novatrade.backtest.fidelity import PIP_DEFAULT, run_fidelity_backtest
from novatrade.strategies.vault_irb_state import IRBConfig


def per_year_net_r(state, pip: float) -> pd.DataFrame:
    rows = []
    for p in state._positions:
        risk = p["stop_pips"] * pip * p["qty"]
        if risk > 0 and p["stop_pips"] > 0:
            rows.append({"year": pd.Timestamp(p["entry_time"]).year, "net_r": p["net"] / risk})
    df = pd.DataFrame(rows)
    return df.groupby("year")["net_r"].agg(["mean", "count"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default="data/candles/EURUSD_M1.csv")
    ap.add_argument("--cost-pips", type=float, default=0.5)
    args = ap.parse_args()
    pip = PIP_DEFAULT

    fine = pd.read_csv(args.m1)
    fine.columns = [c.lower().strip() for c in fine.columns]
    if "time" not in fine.columns and "timestamp" in fine.columns:
        fine = fine.rename(columns={"timestamp": "time"})
    fine["time"] = pd.to_datetime(fine["time"])
    fine = fine[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)
    print(f"[load] {len(fine):,} M1 bars  {fine['time'].iloc[0]} -> {fine['time'].iloc[-1]}")

    base = IRBConfig()
    variants = {
        "baseline(50%@1R+runner)": base,
        "tp1.0(100%@1R)": replace(base, partial_exit_pct=100.0, partial_rr=1.0),
        "tp1.5(100%@1.5R)": replace(base, partial_exit_pct=100.0, partial_rr=1.5),
        "tp2.0(100%@2.0R)": replace(base, partial_exit_pct=100.0, partial_rr=2.0),
    }

    per_year = {}
    summary = []
    for name, cfg in variants.items():
        res = run_fidelity_backtest(
            fine, primary_tf="60min", cfg=cfg, cost_pips=args.cost_pips, fine_fills=True, pip=pip
        )
        yr = per_year_net_r(res.raw_state, pip)
        per_year[name] = yr["mean"]
        summary.append(
            {
                "variant": name,
                "trades": res.total_trades,
                "mean_net_r": res.edge_r,
                "gross_r": float(
                    np.mean(
                        [
                            p["gross"] / (p["stop_pips"] * pip * p["qty"])
                            for p in res.raw_state._positions
                            if p["stop_pips"] > 0
                        ]
                    )
                ),
                "win_rate": res.win_rate,
                "yrs_pos": int((yr["mean"] > 0).sum()),
                "yrs": len(yr),
            }
        )

    print(f"\n=== Full-sample (net of {args.cost_pips:.1f}p cost) ===")
    sdf = pd.DataFrame(summary)
    print(sdf.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))

    print("\n=== Per-year mean net-R (regime check) ===")
    ydf = pd.DataFrame(per_year)
    print(ydf.to_string(float_format=lambda v: f"{v:+.3f}"))

    # how often does the best TP variant beat baseline, year by year?
    best_tp = sdf.loc[sdf.variant != "baseline(50%@1R+runner)", "mean_net_r"].idxmax()
    best_name = sdf.loc[best_tp, "variant"]
    wins = int((ydf[best_name] > ydf["baseline(50%@1R+runner)"]).sum())
    print(f"\n{best_name} beats baseline in {wins}/{len(ydf)} years")


if __name__ == "__main__":
    main()
