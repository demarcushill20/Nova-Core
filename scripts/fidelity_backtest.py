#!/usr/bin/env python3
"""Run a fidelity backtest: IRB decisions on a primary timeframe, fills resolved
on the finest bars in the input CSV.

    python3 scripts/fidelity_backtest.py data/candles/EURUSD_M5_10yr.csv \
        --primary 60min --cost 0.5

Pass --heuristic to use the stock single-bar fill guess (reproduces the vault)
and see, side by side, how much "edge" the fill heuristic invents.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.fidelity import run_fidelity_backtest
from novatrade.strategies.vault_irb_state import IRBConfig


def _fmt(label: str, r) -> str:
    return (
        f"  {label:14s} net=${r.net_pnl:>12,.0f}  trades={r.total_trades:>5d}  "
        f"edge={r.edge_r:+.4f}R  med_stop={r.median_stop_pips:.1f}p  "
        f"breakeven_cost={r.breakeven_cost_pips:+.2f}p  win={r.win_rate * 100:.1f}%"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", help="Fine-bar CSV (timestamp/time,open,high,low,close)")
    ap.add_argument("--primary", default="60min", help="Decision timeframe (pandas offset, e.g. 60min)")
    ap.add_argument("--cost", type=float, default=0.0, help="Round-trip cost in pips")
    ap.add_argument("--heuristic", action="store_true", help="Use single-bar fill guess (vault behaviour)")
    ap.add_argument("--compare", action="store_true", help="Run BOTH heuristic and fine fills, side by side")
    args = ap.parse_args()

    raw = pd.read_csv(args.csv)
    if "time" not in raw.columns and "timestamp" in raw.columns:
        raw["time"] = pd.to_datetime(raw["timestamp"])
    else:
        raw["time"] = pd.to_datetime(raw["time"])
    print(f"[load] {len(raw):,} fine bars  ({raw['time'].iloc[0]} → {raw['time'].iloc[-1]})")
    cfg = IRBConfig()
    print(f"[run] primary={args.primary} cost={args.cost}p config=canonical IRB\n")

    if args.compare:
        h = run_fidelity_backtest(raw, args.primary, cfg, cost_pips=args.cost, fine_fills=False)
        f = run_fidelity_backtest(raw, args.primary, cfg, cost_pips=args.cost, fine_fills=True)
        print("=== fill-heuristic vs real sub-bar fidelity ===")
        print(_fmt("heuristic", h))
        print(_fmt("fidelity", f))
        delta = f.edge_r - h.edge_r
        if f.breakeven_cost_pips > 0.5:
            verdict = "edge holds"
        elif f.edge_r < h.edge_r * 0.5:
            verdict = "EDGE IS A FILL ARTIFACT"
        else:
            verdict = "partially eroded"
        print(f"\n  edge change from honest fills: {delta:+.4f}R  ({verdict})")
    else:
        r = run_fidelity_backtest(raw, args.primary, cfg, cost_pips=args.cost, fine_fills=not args.heuristic)
        print("=== " + ("heuristic (vault)" if args.heuristic else "fidelity (real sub-bar fills)") + " ===")
        print(_fmt("result", r))


if __name__ == "__main__":
    main()
