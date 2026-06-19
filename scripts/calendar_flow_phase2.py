#!/usr/bin/env python3
"""Tier 1A — Phase 2: harden the pre-registered shortlist from the sweep.

Phase 1 swept ~1,056 calendar cells; the broad basket cleared deflated-Sharpe in
discovery but went NEGATIVE on the sealed 2023-24 years. Two mechanism-grounded,
cross-instrument-replicated cells were pre-registered for isolated confirmation:

  CELL A — "Friday 10:00 UTC long-USD": SHORT EUR/GBP/AUD/NZD at 10:00 UTC Friday
           (replicated on 4 pairs in discovery). Mechanism: pre-weekend USD demand.
  CELL B — "Month-end fix": LONG GBP/AUD (USD weakness) into the 16:00 London fix
           on the last business day. Phase-1 daily version was MIS-TIMED (UTC
           midnight); here we re-time it to the actual fix window.

Because these were selected on discovery (<=2022) ONLY, the sealed years (2023-24)
are a clean out-of-sample read, and we're now testing 2 hypotheses (not 1,056),
so the multiple-testing burden is already paid.

Cost: data-driven. We measure EURUSD median spread by UTC hour from the tick
parquet (proves our hours are liquid AND that 22:00 is a trap), build an hour
multiplier, and charge base_spread[pair] * mult[hour] (+ a 2x stress).
"""

from __future__ import annotations

import glob

import numpy as np
import pandas as pd

from scripts.calendar_flow_gauntlet import CANDLES, PIP, load_close

MAJORS = ["eurusd", "gbpusd", "audusd", "nzdusd"]
BASE_SPREAD = {"eurusd": 0.6, "gbpusd": 0.9, "audusd": 0.8, "nzdusd": 1.3}  # pips, ECN-typical
SEALED_FROM = 2023  # data ends 2024 -> sealed = 2023-2024, discovery <= 2022


def eurusd_hourly_spread() -> pd.Series:
    """Median EURUSD spread (pips) by UTC hour from 2024 tick data."""
    files = sorted(glob.glob(str(CANDLES.parent / "ticks/EURUSD/2024-*.parquet")))
    if not files:
        return pd.Series(dtype=float)
    tk = pd.concat([pd.read_parquet(f, columns=["time", "bid", "ask"]) for f in files], ignore_index=True)
    tk["hour"] = pd.to_datetime(tk["time"], utc=True).dt.hour
    tk["spread"] = (tk["ask"] - tk["bid"]) / 1e-4
    return tk.groupby("hour")["spread"].median()


def stat(net: np.ndarray) -> str:
    net = np.asarray(net, float)
    net = net[~np.isnan(net)]
    if len(net) < 2:
        return f"n={len(net):<5} (too few)"
    sd = net.std(ddof=1)
    t = net.mean() / (sd / np.sqrt(len(net))) if sd > 0 else 0.0
    return f"n={len(net):<5} mean {net.mean() * 1e4:+.3f}bp  t {t:+.2f}  win {(net > 0).mean() * 100:.0f}%"


def hourly_frame(sym: str) -> pd.DataFrame:
    s = load_close(sym, "1h")
    f = pd.DataFrame({"close": s})
    f["ret1"] = s.shift(-1) / s - 1.0  # 1-hour hold
    idx = f.index
    f["weekday"], f["hour"], f["year"], f["date"] = idx.weekday, idx.hour, idx.year, idx.normalize()
    return f.dropna(subset=["ret1"])


# ---------------------------------------------------------------- CELL A
def cell_a(mult: pd.Series, cost_mult: float = 1.0):
    print(f"\n=== CELL A — Friday 10:00 UTC long-USD (SHORT pair), cost x{cost_mult:g} ===")
    legs = {}
    for sym in MAJORS:
        f = hourly_frame(sym)
        g = f[(f["weekday"] == 4) & (f["hour"] == 10)].copy()
        cost = BASE_SPREAD[sym] * float(mult.get(10, 1.0)) * cost_mult * PIP[sym] / g["close"]
        g["net"] = -g["ret1"] - cost  # SHORT the pair
        disc = g[g["year"] < SEALED_FROM]["net"].to_numpy()
        seal = g[g["year"] >= SEALED_FROM]["net"].to_numpy()
        print(f"  {sym:<7} discovery {stat(disc)}   ||  SEALED {stat(seal)}")
        legs[sym] = g.set_index("date")["net"]

    basket = pd.concat(legs, axis=1).mean(axis=1).dropna()  # equal-weight USD basket per Friday
    byr = basket.index.year
    print("  " + "-" * 70)
    print(f"  BASKET  discovery {stat(basket[byr < SEALED_FROM].to_numpy())}")
    print(f"  BASKET  SEALED    {stat(basket[byr >= SEALED_FROM].to_numpy())}")
    return basket


# ---------------------------------------------------------------- CELL B
def cell_b(mult: pd.Series, entry_h: int, exit_h: int, cost_mult: float = 1.0):
    print(f"\n=== CELL B — Month-end fix LONG {entry_h:02d}:00->{exit_h:02d}:00, cost x{cost_mult:g} ===")
    legs = {}
    for sym in ["gbpusd", "audusd", "eurusd", "nzdusd"]:
        f = hourly_frame(sym)
        f["ym"] = f["date"].dt.tz_localize(None).dt.to_period("M")
        last_day = f.groupby("ym")["date"].transform("max")
        me = f[f["date"] == last_day]
        piv = me.pivot_table(index="date", columns="hour", values="close", aggfunc="last")
        if entry_h not in piv.columns or exit_h not in piv.columns:
            continue
        ret = piv[exit_h] / piv[entry_h] - 1.0
        # round-trip cost across the in/out legs at those hours
        cpips = BASE_SPREAD[sym] * (float(mult.get(entry_h, 1.0)) + float(mult.get(exit_h, 1.0))) / 2
        cost = cpips * cost_mult * PIP[sym] / piv[entry_h]
        net = (ret - cost).dropna()  # LONG the pair
        yr = net.index.year
        d_s = stat(net[yr < SEALED_FROM].to_numpy())
        s_s = stat(net[yr >= SEALED_FROM].to_numpy())
        print(f"  {sym:<7} discovery {d_s}   ||  SEALED {s_s}")
        legs[sym] = net
    if legs:
        basket = pd.concat(legs, axis=1).mean(axis=1).dropna()
        byr = basket.index.year
        print("  " + "-" * 70)
        print(f"  BASKET  discovery {stat(basket[byr < SEALED_FROM].to_numpy())}")
        print(f"  BASKET  SEALED    {stat(basket[byr >= SEALED_FROM].to_numpy())}")


def main() -> int:
    print("=" * 80)
    print("Tier 1A — Phase 2: sealed-hold-out confirmation of the pre-registered shortlist")
    print("=" * 80)

    prof = eurusd_hourly_spread()
    print("\nEURUSD median spread by UTC hour (2024 ticks, pips):")
    if not prof.empty:
        for h in range(24):
            bar = "#" * round(float(prof.get(h, 0)) * 4)
            flag = "  <- our hours" if h in (10, 15, 16) else ("  <- ROLLOVER trap" if h in (21, 22, 23) else "")
            print(f"   {h:02d}:00  {float(prof.get(h, float('nan'))):.2f}p {bar}{flag}")
        liquid_ref = float(prof.loc[8:15].median())
        mult = (prof / liquid_ref).clip(lower=1.0)
        print(
            f"\n   liquid-hours ref spread: {liquid_ref:.2f}p | hour-10 mult {float(mult.get(10, 1)):.2f}, "
            f"hour-16 mult {float(mult.get(16, 1)):.2f}, hour-22 mult {float(mult.get(22, 1)):.2f}"
        )
    else:
        print("   (no tick files found — falling back to flat cost)")
        mult = pd.Series(1.0, index=range(24))

    cell_a(mult, cost_mult=1.0)
    cell_a(mult, cost_mult=2.0)  # cost stress

    for eh, xh in ((15, 16), (15, 17), (16, 17)):
        cell_b(mult, eh, xh, cost_mult=1.0)

    print("\n" + "=" * 80)
    print("Read: a GO needs the SEALED basket positive with t>2 net of realistic (and 2x) cost.")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
