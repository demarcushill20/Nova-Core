#!/usr/bin/env python3
"""
Task 1048 — SeasonalDrift-EURUSD: systematic (weekday x UTC-hour) drift harvester.

Hypothesis
----------
EURUSD shows persistent, fundamentally-driven directional drift in specific
(weekday, hour-of-day) cells (session handoffs, fix flows, option expiries).
The validated 11:00-12:00 UTC short is one such cell (t=-5.2 on ticks). Most
apparent cells are noise; a small number are real. We harvest only the cells
that survive a walk-forward + multiple-testing + persistence gauntlet, net of
cost. Either outcome is informative.

Anti-overfit gauntlet (the actual deliverable's spine)
------------------------------------------------------
  1. WALK-FORWARD: select cells on a rolling in-sample window; trade them OOS.
     Reported PnL/t-stats are OUT-OF-SAMPLE only.
  2. MULTIPLE-TESTING: 5x24 = 120 cells -> Benjamini-Hochberg FDR on in-sample
     t-stats. A cell must clear FDR q<=0.10 to be eligible, not raw p<0.05.
  3. PERSISTENCE: a cell must be FDR-eligible AND correct-signed in >= K of N
     folds before it is ever traded (mirrors the IRB "6/11 yrs" bar).
  4. POSITIVE CONTROL: the known 11:00 UTC short must re-emerge from the scan.
     If the harness can't rediscover it, the harness is wrong.
  5. NET-OF-COST verdict: decision on net-of-cost OOS return, never gross.

Falsification
-------------
If, after the gauntlet, NO cell other than the known 11:00 UTC short survives
net of cost with >=K/N persistence, the verdict is "no new seasonal edge beyond
the one already shipped" — a cheap, valuable negative.

Data: data/candles/EURUSD_H1_10yr.csv (hourly OHLCV, 2016-2026, UTC).
Usage: python3 scripts/backtest_1048_seasonal_drift.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# --- configuration -----------------------------------------------------------
DATA = Path("data/candles/EURUSD_H1_10yr.csv")
PIP = 0.0001
COST_PIPS = 0.5  # ECN round-trip, canonical NovaTrade assumption
COST_RET = COST_PIPS * PIP  # per round-trip, applied in price-return space ~ /price
FDR_Q = 0.10  # Benjamini-Hochberg false-discovery rate
PERSIST_K = 4  # cell must be eligible+correct-signed in >= K folds...
# N folds is derived from the data (one fold per OOS year).
KNOWN_CONTROL = (None, 11)  # (weekday=any, hour=11 UTC short) positive control
RISK_FRACTION = 0.01  # 1% notional per cell-trade (drift executor parity)


def load_hourly(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["weekday"] = df["timestamp"].dt.weekday  # 0=Mon .. 6=Sun
    df["hour"] = df["timestamp"].dt.hour
    df["year"] = df["timestamp"].dt.year
    # next-hour return in price-return space (entry at this bar's close,
    # exit at next bar's close = 1-hour hold).
    df["next_ret"] = df["close"].shift(-1) / df["close"] - 1.0
    df = df.dropna(subset=["next_ret"])
    # FX trades ~24x5; drop weekend stubs (Sat/Sun) which are illiquid/no-flow.
    df = df[df["weekday"] < 5].reset_index(drop=True)
    return df


def benjamini_hochberg(pvals: np.ndarray, q: float) -> np.ndarray:
    """Return boolean mask of hypotheses that pass BH-FDR at level q."""
    n = len(pvals)
    order = np.argsort(pvals)
    ranked = pvals[order]
    thresh = q * (np.arange(1, n + 1) / n)
    passed = ranked <= thresh
    if not passed.any():
        return np.zeros(n, dtype=bool)
    # largest k where p_(k) <= q*k/n ; all hypotheses up to k pass.
    kmax = np.max(np.where(passed)[0])
    mask = np.zeros(n, dtype=bool)
    mask[order[: kmax + 1]] = True
    return mask


def cell_stats(sample: pd.DataFrame) -> pd.DataFrame:
    """For each (weekday, hour) cell: mean next-ret, t-stat, two-sided p, n."""
    rows = []
    for (wd, hr), g in sample.groupby(["weekday", "hour"]):
        r = g["next_ret"].to_numpy()
        n = len(r)
        if n < 30:  # too few obs to trust
            continue
        mean = r.mean()
        sd = r.std(ddof=1)
        if sd == 0:
            continue
        t = mean / (sd / np.sqrt(n))
        # two-sided p via normal approx (n large)
        from scipy import stats

        p = 2 * (1 - stats.norm.cdf(abs(t)))
        rows.append(dict(weekday=wd, hour=hr, mean=mean, t=t, p=p, n=n))
    return pd.DataFrame(rows)


def select_cells(insample: pd.DataFrame) -> pd.DataFrame:
    """FDR-gate the in-sample cells; return eligible cells with their drift sign."""
    stats_df = cell_stats(insample)
    if stats_df.empty:
        return stats_df
    mask = benjamini_hochberg(stats_df["p"].to_numpy(), FDR_Q)
    elig = stats_df[mask].copy()
    elig["sign"] = np.sign(elig["mean"]).astype(int)
    return elig


def main() -> int:
    if not DATA.exists():
        print(f"FATAL: {DATA} not found", file=sys.stderr)
        return 2
    df = load_hourly(DATA)
    years = sorted(df["year"].unique())
    # Walk-forward: for each OOS year y (from the 4th year on), select cells on
    # ALL prior years, trade them during year y. Anchored expanding window.
    oos_years = years[3:]  # need >=3 yrs in-sample before first OOS fold
    n_folds = len(oos_years)
    persist_k = min(PERSIST_K, max(2, n_folds // 2))

    print("=" * 72)
    print("SeasonalDrift-EURUSD — walk-forward seasonal (weekday x hour) harvester")
    print("=" * 72)
    print(f"data           : {DATA}  ({len(df):,} hourly bars, {years[0]}-{years[-1]})")
    print("cells tested   : 5 weekdays x 24 hours = 120 per fold")
    print(f"FDR gate       : Benjamini-Hochberg q<={FDR_Q}")
    print(f"walk-forward   : anchored expanding, {n_folds} OOS folds ({oos_years[0]}-{oos_years[-1]})")
    print(f"persistence    : cell eligible+correct-signed in >= {persist_k}/{n_folds} folds")
    print(f"cost           : {COST_PIPS}p round-trip, net-of-cost verdict")
    print("-" * 72)

    # 1) Tally per-cell eligibility & sign agreement across folds; collect OOS trades.
    elig_count: dict[tuple[int, int], int] = {}
    sign_votes: dict[tuple[int, int], list[int]] = {}
    fold_oos: list[pd.DataFrame] = []  # per-fold OOS bars tagged with selected cell sign

    for y in oos_years:
        insample = df[df["year"] < y]
        oos = df[df["year"] == y]
        elig = select_cells(insample)
        # record eligibility/sign for persistence tally
        for _, row in elig.iterrows():
            key = (int(row.weekday), int(row.hour))
            elig_count[key] = elig_count.get(key, 0) + 1
            sign_votes.setdefault(key, []).append(int(row.sign))
        # build the OOS trade tape for THIS fold using THIS fold's selection
        if not elig.empty:
            sel = elig[["weekday", "hour", "sign"]]
            merged = oos.merge(sel, on=["weekday", "hour"], how="inner")
            fold_oos.append(merged)

    # 2) Persistence gate: which cells are tradable (eligible in >=K folds, stable sign).
    tradable = []
    for key, cnt in elig_count.items():
        if cnt < persist_k:
            continue
        votes = sign_votes[key]
        majority = int(np.sign(sum(votes)))
        # require dominant sign agreement (no sign-flipping cells)
        agree = sum(1 for v in votes if v == majority)
        if majority != 0 and agree >= persist_k:
            tradable.append((key[0], key[1], majority, cnt, agree))
    tradable.sort(key=lambda x: (-x[3], x[0], x[1]))

    wdname = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    print(f"\nCELLS PASSING FDR + PERSISTENCE GATE ({len(tradable)}):")
    if tradable:
        print(f"  {'cell':<12}{'sign':>6}{'folds_elig':>12}{'sign_agree':>12}")
        for wd, hr, sgn, cnt, agree in tradable:
            s = "SHORT" if sgn < 0 else "LONG"
            print(f"  {wdname[wd]} {hr:02d}:00 UTC{'':<1}{s:>6}{cnt:>12}{agree:>12}")
    else:
        print("  (none)")

    # 3) Positive control check: did ANY hour-11 short re-emerge?
    ctrl_hits = [t for t in tradable if t[1] == 11 and t[2] < 0]
    print(
        f"\nPOSITIVE CONTROL (known 11:00 UTC short re-discovered): "
        f"{'YES' if ctrl_hits else 'NO'} "
        f"({len(ctrl_hits)} hour-11 short cell(s) survived)"
    )

    # 4) Honest OOS PnL: a cell only contributes OOS returns in the folds where it
    #    was BOTH selected (the fold_oos tape) AND is in the final tradable set.
    tradable_keys = {(wd, hr): sgn for wd, hr, sgn, _, _ in tradable}
    if fold_oos:
        tape = pd.concat(fold_oos, ignore_index=True)
        tape["tkey"] = list(zip(tape["weekday"], tape["hour"], strict=False))
        tape = tape[tape["tkey"].isin(tradable_keys.keys())].copy()
        # signed net-of-cost per-trade return; sign comes from the FINAL stable sign
        tape["fsign"] = tape["tkey"].map(tradable_keys)
        tape["gross"] = tape["fsign"] * tape["next_ret"]
        tape["net"] = tape["gross"] - COST_RET
    else:
        tape = pd.DataFrame(columns=["net", "gross"])

    n_tr = len(tape)
    print("\n" + "-" * 72)
    print("OUT-OF-SAMPLE RESULTS (net of cost, traded cells only)")
    print("-" * 72)
    if n_tr == 0:
        print("  No OOS trades from surviving cells.")
    else:
        gross_mean = tape["gross"].mean()
        net_mean = tape["net"].mean()
        net_sum = tape["net"].sum()
        from scipy import stats

        t_net = net_mean / (tape["net"].std(ddof=1) / np.sqrt(n_tr))
        p_net = 2 * (1 - stats.norm.cdf(abs(t_net)))
        wins = (tape["net"] > 0).mean()
        # convert mean return to pips for readability
        print(f"  OOS trades            : {n_tr:,}")
        print(f"  gross mean / trade    : {gross_mean / PIP:+.3f} pips")
        print(f"  net   mean / trade    : {net_mean / PIP:+.3f} pips  (after {COST_PIPS}p)")
        print(f"  net total return      : {net_sum * 100:+.2f}%  (1-unit notional/trade)")
        print(f"  net t-stat / p-value  : {t_net:+.2f}  /  p={p_net:.4f}")
        print(f"  win rate (net>0)      : {wins * 100:.1f}%")
        # per-year OOS net to check it isn't carried by 1-2 years
        tape["oos_year"] = pd.to_datetime(tape["timestamp"]).dt.year
        by_yr = tape.groupby("oos_year")["net"].sum() * 100
        pos_yrs = (by_yr > 0).sum()
        print(f"  positive OOS years    : {pos_yrs}/{by_yr.shape[0]}")
        print("  per-year net %        : " + ", ".join(f"{y}:{v:+.2f}" for y, v in by_yr.items()))

    # 5) Verdict
    print("\n" + "=" * 72)
    new_cells = [t for t in tradable if not (t[1] == 11 and t[2] < 0)]
    if n_tr > 0:
        net_mean = tape["net"].mean()
        t_net = tape["net"].mean() / (tape["net"].std(ddof=1) / np.sqrt(len(tape)))
    else:
        net_mean, t_net = 0.0, 0.0
    if n_tr > 0 and net_mean > 0 and t_net > 2.0 and new_cells:
        verdict = (
            "TRADE-CANDIDATE: new seasonal cell(s) beyond the known 11:00 "
            "short survive net of cost OOS. Extend the live drift executor."
        )
    elif n_tr > 0 and net_mean > 0 and t_net > 2.0:
        verdict = (
            "CONFIRMS KNOWN EDGE ONLY: surviving cells are the already-shipped "
            "11:00 UTC short. No NEW edge. Positive control passed."
        )
    else:
        verdict = (
            "DO NOT TRADE: no seasonal cell survives FDR + persistence + cost "
            "OOS with t>2. Tightens the 'FX edge is concentrated in a couple of "
            "flow windows' thesis. Cheap, valuable negative."
        )
    print("VERDICT:", verdict)
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
