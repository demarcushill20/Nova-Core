#!/usr/bin/env python3
"""Tier 1B — Carry, Gate 1: does the FX carry premium exist for our pairs?

Carry = hold the positive-rate-differential direction of a pair; the return is
spot drift + the interest-rate-differential income. We have FX spot (15m parquet)
but no rate data, so we pull 3-month interbank rates per currency from FRED.

IMPORTANT — this is the UPPER BOUND (Gate 1). The carry income here is the full
*interbank* differential. A real broker pays marked-down swaps (often negative
both ways), which is the separate Gate 2 (broker swap table, needs the live
account). So we decompose every result into SPOT vs CARRY-INCOME:
  * if even this academic upper bound is unattractive -> carry is dead for us;
  * if the return is mostly CARRY-INCOME -> Gate 2 is make-or-break;
  * the SPOT component is venue-independent (we get it regardless of swaps).

Discipline mirrors the rest of the program: in-sample (<=2022) vs sealed
(2023-24) split, net of cost, Deflated Sharpe for the trials, spot/carry split.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from novatrade.backtest.research.walkforward import deflated_sharpe_ratio
from scripts.calendar_flow_gauntlet import PIP, load_close

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
SERIES = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "GBP": "IR3TIB01GBM156N",
    "AUD": "IR3TIB01AUM156N",
    "NZD": "IR3TIB01NZM156N",
    "CAD": "IR3TIB01CAM156N",
    "JPY": "IR3TIB01JPM156N",
    "CHF": "IR3TIB01CHM156N",
}
PAIRS = {
    "eurusd": ("EUR", "USD"),
    "gbpusd": ("GBP", "USD"),
    "audusd": ("AUD", "USD"),
    "nzdusd": ("NZD", "USD"),
    "usdcad": ("USD", "CAD"),
    "usdjpy": ("USD", "JPY"),
    "audnzd": ("AUD", "NZD"),
}
SPREAD_PIPS = {"eurusd": 0.6, "gbpusd": 0.9, "audusd": 0.8, "nzdusd": 1.3, "usdcad": 1.3, "usdjpy": 0.9, "audnzd": 1.8}
SEALED_FROM = 2023


def rate_panel() -> pd.DataFrame:
    cols = {}
    for cur, sid in SERIES.items():
        df = pd.read_csv(f"{FRED}{sid}", na_values=".")
        df.columns = ["date", "rate"]
        df["date"] = pd.to_datetime(df["date"])
        cols[cur] = df.set_index("date")["rate"].astype(float)
    panel = pd.DataFrame(cols).resample("MS").last().ffill()
    panel.index = panel.index.to_period("M")
    return panel


def monthly_spot(sym: str) -> pd.Series:
    c = load_close(sym, "ME")
    r = c.pct_change()
    r.index = r.index.tz_localize(None).to_period("M")
    return r.dropna()


def ann_sharpe(m: np.ndarray) -> float:
    m = np.asarray(m, float)
    return float(m.mean() / m.std(ddof=1) * np.sqrt(12)) if len(m) > 1 and m.std(ddof=1) > 0 else 0.0


def build_pair(sym: str, rates: pd.DataFrame) -> pd.DataFrame:
    base, quote = PAIRS[sym]
    spot = monthly_spot(sym)
    carry = (rates[base] - rates[quote]).rename("carry")  # annual % differential
    df = pd.concat([spot.rename("spot"), carry], axis=1).dropna()
    # position decided at START of month from the PRIOR month's carry (no look-ahead)
    df["pos"] = np.sign(df["carry"].shift(1))
    df["carry_used"] = df["carry"].shift(1)
    df = df.dropna(subset=["pos", "carry_used"])
    df = df[df["pos"] != 0]
    cost = SPREAD_PIPS[sym] * PIP[sym]  # ~one spread per flip, in return space (price~1 for majors)
    flip = df["pos"].ne(df["pos"].shift(1)).astype(float)
    df["spot_leg"] = df["pos"] * df["spot"]
    df["carry_leg"] = df["pos"] * df["carry_used"] / 1200.0  # monthly income from annual %
    df["net"] = df["spot_leg"] + df["carry_leg"] - flip * cost
    df["year"] = df.index.year
    return df


def report(label: str, d: pd.DataFrame) -> None:
    if d.empty:
        print(f"  {label:<10} (no data)")
        return
    tot, spot, carry = d["net"].to_numpy(), d["spot_leg"].to_numpy(), d["carry_leg"].to_numpy()
    print(
        f"  {label:<10} n={len(d):<4} netSharpe {ann_sharpe(tot):+.2f} | "
        f"spot {spot.mean() * 1200:+.2f}%/yr  carry {carry.mean() * 1200:+.2f}%/yr  "
        f"net {tot.mean() * 1200:+.2f}%/yr | win {(tot > 0).mean() * 100:.0f}%"
    )


def main() -> int:
    print("=" * 84)
    print("Tier 1B — Carry Gate 1 (academic upper bound: interbank differential as carry income)")
    print("=" * 84)
    rates = rate_panel()
    print(f"rate panel: {rates.index.min()}..{rates.index.max()}  currencies={list(rates.columns)}")

    per_pair = {sym: build_pair(sym, rates) for sym in PAIRS}
    # equal-weight portfolio: mean across pairs per month
    port = pd.concat({s: d["net"] for s, d in per_pair.items()}, axis=1).mean(axis=1).dropna()
    port_spot = pd.concat({s: d["spot_leg"] for s, d in per_pair.items()}, axis=1).mean(axis=1).reindex(port.index)
    port_carry = pd.concat({s: d["carry_leg"] for s, d in per_pair.items()}, axis=1).mean(axis=1).reindex(port.index)
    pf = pd.DataFrame({"net": port, "spot_leg": port_spot, "carry_leg": port_carry})
    pf["year"] = pf.index.year

    print("\n--- PER PAIR (full sample) ---")
    for sym, d in per_pair.items():
        report(sym, d)

    print("\n--- EQUAL-WEIGHT CARRY PORTFOLIO ---")
    report("FULL", pf)
    report("in-samp", pf[pf["year"] < SEALED_FROM])
    report("SEALED", pf[pf["year"] >= SEALED_FROM])

    ins = pf[pf["year"] < SEALED_FROM]["net"].to_numpy()
    n_trials = len(PAIRS) + 3  # pairs + a few design choices (rate tenor, rebal, weighting)
    dsr = deflated_sharpe_ratio(ann_sharpe(ins) / np.sqrt(12), len(ins), n_trials) if len(ins) > 2 else float("nan")
    print(f"\nDeflated Sharpe (in-sample, n_trials={n_trials}): {dsr:.3f}  (gate > 0.90)")

    spot_share = pf["spot_leg"].mean() / pf["net"].mean() if pf["net"].mean() != 0 else float("nan")
    print(
        f"return decomposition: spot {pf['spot_leg'].mean() * 1200:+.2f}%/yr | "
        f"carry-income {pf['carry_leg'].mean() * 1200:+.2f}%/yr | spot share {spot_share * 100:.0f}%"
    )

    print("\n" + "=" * 84)
    print("Read: a Gate-1 GO needs the SEALED portfolio Sharpe clearly positive AND the result")
    print("not driven entirely by carry-income (which Gate 2 / broker swaps would tax away).")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
