#!/usr/bin/env python3
"""Robustness pass for task-1045: GBPUSD is the only net-positive pair, so stress it.
  (1) cost sensitivity (1.0 / 1.5 / 2.0 / 2.5p round-turn) for the faithful variant + 1R
  (2) per-year NET total-R (cost 1.5p, the realistic GBPUSD ECN) for the faithful variant
A thin edge that only survives at an optimistic cost, or is carried by 2-3 years, is not
tradeable. This is the deflation gate the project memory keeps flagging for FX TA.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backtest_1045_daybreak_m1 import build_daily_atr, build_h1, load_m1, simulate, summarize

GBP = Path("data/candles/histdata/GBPUSD_M1.csv")

df = load_m1(GBP)
h1 = build_h1(df)
atr = build_daily_atr(df)
print(f"GBPUSD loaded: {len(df):,} M1 bars")

print("\n=== COST SENSITIVITY (NET) ===")
print(f"{'variant':18} {'cost':>5} {'WR%':>6} {'PF':>6} {'expR':>9} {'totalR':>8} {'ret%':>8} {'DD%':>6}")
for variant in ("A_faithful_min50", "B_1R", "C_1.5R", "D_2R"):
    for cost in (1.0, 1.5, 2.0, 2.5):
        tr = simulate(df, h1, atr, variant=variant, buffer_pips=10.0, cost_pips=cost, max_hold_min=4320)
        m = summarize(tr, True, 0.0033, 100_000.0)
        print(
            f"{variant:18} {cost:5.1f} {m['win_rate'] * 100:6.1f} {m['pf']:6.3f} "
            f"{m['expectancy_r']:+9.4f} {m['total_r']:+8.1f} {m['return_pct']:+8.1f} {m['maxdd_pct']:6.1f}"
        )

print("\n=== PER-YEAR NET total-R, faithful (A), cost 1.5p ===")
tr = simulate(df, h1, atr, variant="A_faithful_min50", buffer_pips=10.0, cost_pips=1.5, max_hold_min=4320)
by_year_r: defaultdict[str, float] = defaultdict(float)
by_year_n: defaultdict[str, int] = defaultdict(int)
by_year_w: defaultdict[str, int] = defaultdict(int)
for t in tr:
    y = t.day[:4]
    by_year_r[y] += t.r_net
    by_year_n[y] += 1
    if t.outcome == "tp":
        by_year_w[y] += 1
pos = 0
print(f"{'year':6} {'N':>4} {'WR%':>6} {'netR':>8}")
for y in sorted(by_year_r):
    wr = by_year_w[y] / by_year_n[y] * 100
    r = by_year_r[y]
    pos += 1 if r > 0 else 0
    print(f"{y:6} {by_year_n[y]:4d} {wr:6.1f} {r:+8.2f}  {'+' if r > 0 else '-'}")
print(f"\npositive years: {pos}/{len(by_year_r)}")
