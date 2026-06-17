#!/usr/bin/env python3
"""Task-1036: Markov regime transition matrix with stride sampling.

⚠️  DESCRIPTIVE ONLY — NOT A BACKTEST, NOT TRADE EVIDENCE  ⚠️
This tool builds the transition matrix from the ENTIRE state series
(``states[::step]``) and a full-series std band. That is the full-dataset
construction spec §17 explicitly forbids for backtesting (it leaks future
information). It is safe here only because this script never trades and never
computes PnL — it answers one descriptive question about the current regime.
Do NOT promote it to a backtest or cite its numbers as evidence to trade. Any
signal it inspires must be recomputed walk-forward (see
``backtest_1035_markov_regime.py``, which rebuilds the matrix from labels ≤ t).


Implements the regime-transition model described in TASKS/1036:

  1. Label each bar's market regime from its trailing ``lookback``-bar return:
        ret = close[i] / close[i-lookback] - 1
        Bull     if ret >  +band
        Bear     if ret <  -band
        Sideways otherwise
  2. Count regime->regime transitions and row-normalise into probabilities.
  3. The probability-matrix DIAGONAL is "stickiness" -- how often a state persists.

  4. THE CORRECTION -- stride sampling (the "autocorrelation fix"):
        naive  : count a transition on EVERY bar  (i -> i+1).  Consecutive
                 ``lookback``-bar windows overlap by lookback-1 bars, which
                 manufactures FAKE persistence (inflated diagonal).
        v2.0   : count transitions only between NON-OVERLAPPING windows,
                 stride == lookback:  i -> i+lookback, stepping by lookback.

We run BOTH so the inflation is visible, then answer the task's question:
given today's regime, what is the probability the NEXT sampled state is
Sideways (and Bull / Bear)?

Default: daily bars (EURUSD H1 resampled to D1), lookback = stride = 20 days.
This makes "20-day rolling return" literal, matching the TradingView source.
"""

from __future__ import annotations

import argparse
import sys
from itertools import pairwise
from pathlib import Path

import pandas as pd

STATES = ["Bear", "Sideways", "Bull"]


def load_daily_closes(path: Path) -> pd.Series:
    """Load an OHLC CSV and resample close to daily (last close of each UTC day)."""
    df = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
    daily = df["close"].resample("1D").last().dropna()
    return daily


def label_regimes(closes: pd.Series, lookback: int, band: float) -> pd.Series:
    """Trailing lookback-bar return -> {Bear, Sideways, Bull}."""
    ret = closes / closes.shift(lookback) - 1.0
    state = pd.Series(index=closes.index, dtype=object)
    state[ret > band] = "Bull"
    state[ret < -band] = "Bear"
    state[(ret <= band) & (ret >= -band)] = "Sideways"
    return state.dropna()


def count_matrix(states: list[str], step: int) -> pd.DataFrame:
    """Count from->to transitions sampling the state series every ``step`` bars."""
    counts = {s: {t: 0 for t in STATES} for s in STATES}
    sampled = states[::step]
    for a, b in pairwise(sampled):
        counts[a][b] += 1
    m = pd.DataFrame(counts).T  # rows=from, cols=to
    return m.reindex(index=STATES, columns=STATES).fillna(0).astype(int)


def to_prob(counts: pd.DataFrame) -> pd.DataFrame:
    totals = counts.sum(axis=1).replace(0, pd.NA)
    return counts.div(totals, axis=0)


def fmt_counts(counts: pd.DataFrame) -> str:
    out = ["            Next Bear   Next Sideways   Next Bull   |  Total"]
    for s in STATES:
        row = counts.loc[s]
        out.append(
            f"{s:<11}{int(row['Bear']):>8}{int(row['Sideways']):>15}{int(row['Bull']):>12}   |{int(row.sum()):>7}"
        )
    return "\n".join(out)


def fmt_prob(prob: pd.DataFrame) -> str:
    out = ["            Next Bear   Next Sideways   Next Bull   |  Stickiness"]
    for s in STATES:
        row = prob.loc[s]
        stick = row[s]
        cells = []
        for t in STATES:
            v = row[t]
            cells.append("   n/a" if pd.isna(v) else f"{v * 100:5.1f}%")
        stick_s = "n/a" if pd.isna(stick) else f"{stick * 100:.1f}%"
        out.append(f"{s:<11}{cells[0]:>9}{cells[1]:>15}{cells[2]:>12}   |  {stick_s}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/candles/EURUSD_H1_10yr.csv")
    ap.add_argument("--lookback", type=int, default=20, help="rolling-return window (bars)")
    ap.add_argument("--stride", type=int, default=0, help="sampling stride; 0 => lookback")
    ap.add_argument(
        "--band",
        type=float,
        default=0.0,
        help="flat band for Sideways as a return fraction; 0 => 0.5*std of lookback returns",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    stride = args.stride or args.lookback
    closes = load_daily_closes(Path(args.data))

    # Data-driven band so the three states are non-degenerate.
    ret = (closes / closes.shift(args.lookback) - 1.0).dropna()
    band = args.band or 0.5 * float(ret.std())

    states = label_regimes(closes, args.lookback, band)
    state_list = states.tolist()

    naive_c = count_matrix(state_list, step=1)
    stride_c = count_matrix(state_list, step=stride)
    naive_p = to_prob(naive_c)
    stride_p = to_prob(stride_c)

    dist = states.value_counts().reindex(STATES).fillna(0).astype(int)
    cur = state_list[-1]

    lines: list[str] = []
    P = lines.append
    P("=" * 72)
    P("TASK-1036  Markov regime transition matrix  (EURUSD daily)")
    P("** DESCRIPTIVE ONLY — full-dataset matrix, NOT walk-forward.")
    P("** Do NOT use as backtest / trade evidence (spec §17). See backtest_1035.")
    P("=" * 72)
    P(f"data            : {args.data} (H1 -> D1 close)")
    P(f"daily bars      : {len(closes)}   ({closes.index[0].date()} .. {closes.index[-1].date()})")
    P(f"lookback        : {args.lookback} days   stride : {stride} days")
    P(f"flat band       : +/-{band * 100:.3f}%   (Sideways if |20d return| <= band)")
    P(f"labelled bars   : {len(states)}")
    P(f"state mix       : Bear={dist['Bear']}  Sideways={dist['Sideways']}  Bull={dist['Bull']}")
    P("")
    P("-" * 72)
    P("A) NAIVE every-bar sampling (i -> i+1)  --  the autocorrelation BUG")
    P("-" * 72)
    P("counts:")
    P(fmt_counts(naive_c))
    P("\nprobabilities:")
    P(fmt_prob(naive_p))
    P(f"\nmean diagonal (avg stickiness) = {naive_p.values.diagonal().mean() * 100:.1f}%  <-- INFLATED")
    P("")
    P("-" * 72)
    P(f"B) STRIDE sampling (i -> i+{stride}, step {stride})  --  the v2.0 FIX")
    P("-" * 72)
    P("counts:")
    P(fmt_counts(stride_c))
    P("\nprobabilities:")
    P(fmt_prob(stride_p))
    P(f"\nmean diagonal (avg stickiness) = {stride_p.values.diagonal().mean() * 100:.1f}%  <-- honest")
    P("")
    P("-" * 72)
    P("C) ANSWER  --  next SAMPLED state given the current regime")
    P("-" * 72)
    P(f"current regime (last labelled bar) : {cur}")
    if cur in stride_p.index and not stride_p.loc[cur].isna().all():
        row = stride_p.loc[cur]
        P(f"  P(next = Bear)     = {row['Bear'] * 100:5.1f}%")
        P(f"  P(next = Sideways) = {row['Sideways'] * 100:5.1f}%   <-- task focus")
        P(f"  P(next = Bull)     = {row['Bull'] * 100:5.1f}%")
    P("\nP(next = Sideways) by current state (stride model):")
    for s in STATES:
        v = stride_p.loc[s, "Sideways"]
        P(f"  from {s:<9}-> Sideways = {'n/a' if pd.isna(v) else f'{v * 100:5.1f}%'}")

    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
