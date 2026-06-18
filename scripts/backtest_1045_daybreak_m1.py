#!/usr/bin/env python3
"""Backtest task-1045: London Daybreak as a STANDALONE system on EURUSD + GBPUSD
using 1-MINUTE execution data, across the faithful rule + 5 target variants.

This is the M1-execution follow-up to task-1042 (which ran on H1 bars only).

Operator spec (TASKS/1045...):
  - Test EURUSD and GBPUSD with 1-minute execution data.
  - Faithful rule:
      * At 4:00 AM ET, use the 3:00 AM 1H candle (closed at 4:00 AM ET).
      * If the prior 5 H1 candles were MOSTLY BEARISH -> buy a 10-pip break ABOVE it.
      * If the prior 5 H1 candles were MOSTLY BULLISH -> sell a 10-pip break BELOW it.
        (This is a CONTRARIAN/fade rule -- distinct from the momentum/close-position
         readings tested in task-1042.)
      * Stop on the OPPOSITE wick of the 3AM candle.
      * Target >= 50 pips, or 1R if risk is larger than 50 pips  => max(50, R).
      * Cancel any untriggered pending order at 12:00 PM ET.
  - Target variants:
      A: faithful  -> target = max(50p, R)
      B: 1R        -> target = R
      C: 1.5R      -> target = 1.5R
      D: 2R        -> target = 2R
      E: ATR-min   -> target = max(DailyATR14, R)   (volatility analogue of the fixed 50p)

WHY M1 MATTERS: the H1 backtest (1042) had to assume "stop-first" whenever a single
H1 bar straddled both SL and TP -- a pessimistic but coarse tie-break. With M1 data we
resolve which level is touched first to the minute, so the win-rate / expectancy are
materially more faithful. Entry-trigger detection is also minute-accurate.

Timezone: HistData M1 timestamps were shifted EST(UTC-5,no-DST)+5h => true UTC
(see scripts/fetch_histdata_m1.py). We convert UTC -> America/New_York (DST-aware via
zoneinfo) so the 3AM ET setup candle and noon-ET cancel are correct year-round.

GROSS (zero cost) and NET (round-turn ECN cost in pips) are both reported, matching the
house harness convention used in tasks 1024/1028/1030/1042.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PIP = 0.0001  # both EURUSD and GBPUSD are 4-decimal majors

# target-variant definitions: name -> (kind, param)
VARIANTS = {
    "A_faithful_min50": ("min_fixed", 50.0),
    "B_1R": ("mult", 1.0),
    "C_1.5R": ("mult", 1.5),
    "D_2R": ("mult", 2.0),
    "E_min_dailyATR": ("min_atr", 1.0),
}


@dataclass
class Trade:
    day: str
    side: str
    entry: float
    stop: float
    stop_pips: float
    target_pips: float
    rr: float
    outcome: str  # tp / sl / open
    r_gross: float
    r_net: float


def load_m1(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts = pd.to_datetime(df["time"], utc=True)
    ny = ts.dt.tz_convert("America/New_York")
    out = pd.DataFrame(
        {
            "o": df["open"].to_numpy(float),
            "h": df["high"].to_numpy(float),
            "lo": df["low"].to_numpy(float),
            "c": df["close"].to_numpy(float),
            "ny_date": ny.dt.strftime("%Y-%m-%d").to_numpy(),
            "ny_hour": ny.dt.hour.to_numpy(),
        }
    )
    return out


def build_h1(df: pd.DataFrame) -> pd.DataFrame:
    """NY-clock-aligned H1 candles (TradingView 'America/New_York' hourly)."""
    g = df.groupby(["ny_date", "ny_hour"], sort=True)
    h1 = g.agg(o=("o", "first"), h=("h", "max"), lo=("lo", "min"), c=("c", "last")).reset_index()
    return h1


def build_daily_atr(df: pd.DataFrame, period: int = 14) -> dict[str, float]:
    """NY-day daily candles -> ATR(period) in pips, available as of the PRIOR day."""
    g = df.groupby("ny_date", sort=True)
    d = g.agg(h=("h", "max"), lo=("lo", "min"), c=("c", "last")).reset_index()
    prev_c = d["c"].shift(1)
    tr = np.maximum.reduce(
        [
            (d["h"] - d["lo"]).to_numpy(),
            (d["h"] - prev_c).abs().to_numpy(),
            (d["lo"] - prev_c).abs().to_numpy(),
        ]
    )
    atr = pd.Series(tr).rolling(period).mean()
    atr_prior = atr.shift(1)  # value known at the open of day d (prior completed day)
    return {date: (val / PIP) for date, val in zip(d["ny_date"], atr_prior, strict=False) if pd.notna(val)}


def simulate(  # noqa: C901  # research backtest; complexity is inherent
    df: pd.DataFrame,
    h1: pd.DataFrame,
    daily_atr: dict[str, float],
    *,
    variant: str,
    buffer_pips: float,
    cost_pips: float,
    max_hold_min: int,
) -> list[Trade]:
    kind, param = VARIANTS[variant]

    # ---- M1 arrays for fast execution scan ----
    m_h = df["h"].to_numpy()
    m_lo = df["lo"].to_numpy()
    m_date = df["ny_date"].to_numpy()
    m_hour = df["ny_hour"].to_numpy()
    n = len(df)

    # per-day [start,end) index bounds in the M1 array
    dates, starts = np.unique(m_date, return_index=True)
    order = np.argsort(starts)
    dates = dates[order]
    starts = starts[order]
    ends = np.append(starts[1:], n)
    day_bounds = {d: (int(s), int(e)) for d, s, e in zip(dates, starts, ends, strict=False)}

    # ---- H1 lookup: (date,hour) -> row index, plus ordered arrays ----
    h_date = h1["ny_date"].to_numpy()
    h_hour = h1["ny_hour"].to_numpy()
    h_o = h1["o"].to_numpy()
    h_h = h1["h"].to_numpy()
    h_lo = h1["lo"].to_numpy()
    h_c = h1["c"].to_numpy()
    h_idx = {(d, hr): i for i, (d, hr) in enumerate(zip(h_date, h_hour, strict=False))}

    trades: list[Trade] = []

    for day, (s, e) in day_bounds.items():
        key = (day, 3)
        i = h_idx.get(key)
        if i is None or i < 5:
            continue
        # prior 5 H1 candles (the 5 immediately before the 3AM candle)
        po = h_o[i - 5 : i]
        pc = h_c[i - 5 : i]
        if len(po) < 5:
            continue
        n_bull = int(np.sum(pc > po))
        n_bear = int(np.sum(pc < po))
        if n_bull == n_bear:
            continue  # no clear majority -> no trade
        # CONTRARIAN faithful rule: mostly bearish -> buy ; mostly bullish -> sell
        side = "buy" if n_bear > n_bull else "sell"

        c_hi = h_h[i]  # 3AM setup candle high
        c_lo = h_lo[i]  # 3AM setup candle low
        if side == "buy":
            entry = c_hi + buffer_pips * PIP
            stop = c_lo
        else:
            entry = c_lo - buffer_pips * PIP
            stop = c_hi
        stop_pips = abs(entry - stop) / PIP
        if stop_pips <= 0:
            continue

        # ---- target per variant ----
        if kind == "min_fixed":
            target_pips = max(param, stop_pips)
        elif kind == "mult":
            target_pips = param * stop_pips
        elif kind == "min_atr":
            atr_p = daily_atr.get(day)
            if atr_p is None or atr_p <= 0:
                continue
            target_pips = max(param * atr_p, stop_pips)
        else:  # pragma: no cover
            raise ValueError(variant)
        target = entry + target_pips * PIP if side == "buy" else entry - target_pips * PIP

        # ---- find trigger: scan M1 in [4:00, 12:00) ET that day ----
        hod = m_hour[s:e]
        # within a single NY day the hour array is non-decreasing
        lo_off = int(np.searchsorted(hod, 4, side="left"))
        hi_off = int(np.searchsorted(hod, 12, side="left"))
        ws, we = s + lo_off, s + hi_off
        if we <= ws:
            continue
        if side == "buy":
            breach = m_h[ws:we] >= entry
        else:
            breach = m_lo[ws:we] <= entry
        if not breach.any():
            continue  # never triggered before noon -> cancel
        t = ws + int(np.argmax(breach))  # first triggering M1 bar (inclusive in resolution)

        # ---- resolve SL vs TP at MINUTE resolution from trigger bar forward ----
        re = min(n, t + max_hold_min)
        seg_h = m_h[t:re]
        seg_lo = m_lo[t:re]
        if side == "buy":
            sl_hits = seg_lo <= stop
            tp_hits = seg_h >= target
        else:
            sl_hits = seg_h >= stop
            tp_hits = seg_lo <= target
        sl_i = int(np.argmax(sl_hits)) if sl_hits.any() else None
        tp_i = int(np.argmax(tp_hits)) if tp_hits.any() else None

        if sl_i is None and tp_i is None:
            outcome = "open"
        elif tp_i is None:
            outcome = "sl"
        elif sl_i is None:
            outcome = "tp"
        elif sl_i < tp_i:
            outcome = "sl"
        elif tp_i < sl_i:
            outcome = "tp"
        else:
            # same M1 bar touches both -> conservative: stop first
            outcome = "sl"

        if outcome == "open":
            continue
        if outcome == "tp":
            r_gross = target_pips / stop_pips
            r_net = (target_pips - cost_pips) / stop_pips
        else:
            r_gross = -1.0
            r_net = -(stop_pips + cost_pips) / stop_pips

        trades.append(
            Trade(
                day=day,
                side=side,
                entry=entry,
                stop=stop,
                stop_pips=stop_pips,
                target_pips=target_pips,
                rr=target_pips / stop_pips,
                outcome=outcome,
                r_gross=r_gross,
                r_net=r_net,
            )
        )
    return trades


def summarize(trades: list[Trade], use_net: bool, risk_frac: float, init_eq: float) -> dict:
    if not trades:
        return {"n": 0}
    rs = [t.r_net if use_net else t.r_gross for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp = sum(wins)
    gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else float("inf")
    eq = init_eq
    peak = eq
    maxdd = 0.0
    for r in rs:
        eq += eq * risk_frac * r
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, (peak - eq) / peak)
    n_win = sum(1 for r in rs if r > 0)
    return {
        "n": len(trades),
        "win_rate": n_win / len(trades),
        "pf": pf,
        "expectancy_r": sum(rs) / len(rs),
        "total_r": sum(rs),
        "final_eq": eq,
        "return_pct": (eq / init_eq - 1) * 100,
        "maxdd_pct": maxdd * 100,
        "avg_stop_pips": sum(t.stop_pips for t in trades) / len(trades),
        "avg_rr": sum(t.rr for t in trades) / len(trades),
        "n_buy": sum(1 for t in trades if t.side == "buy"),
        "n_sell": sum(1 for t in trades if t.side == "sell"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, nargs="+", help="M1 CSV(s); label inferred from filename")
    ap.add_argument("--buffer-pips", type=float, default=10.0)
    ap.add_argument("--cost-pips", type=float, default=1.0, help="round-turn ECN cost in pips (NET run)")
    ap.add_argument("--max-hold-min", type=int, default=4320, help="max M1 bars held after trigger (~3 trading days)")
    ap.add_argument("--risk", type=float, default=0.0033)
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--out", type=Path, default=None, help="optional CSV of the summary table")
    args = ap.parse_args()

    rows = []
    for path in args.data:
        label = path.stem.replace("_M1", "")
        df = load_m1(path)
        h1 = build_h1(df)
        atr = build_daily_atr(df)
        span = f"{df['ny_date'].iloc[0]} -> {df['ny_date'].iloc[-1]}"
        print(f"\n########## {label}   ({len(df):,} M1 bars, NY {span}) ##########")
        for variant in VARIANTS:
            trades = simulate(
                df,
                h1,
                atr,
                variant=variant,
                buffer_pips=args.buffer_pips,
                cost_pips=args.cost_pips,
                max_hold_min=args.max_hold_min,
            )
            g = summarize(trades, False, args.risk, args.initial_equity)
            net = summarize(trades, True, args.risk, args.initial_equity)
            if g.get("n", 0) == 0:
                print(f"\n--- {variant}: no trades ---")
                continue
            be_wr = 1.0 / (1.0 + g["avg_rr"])  # gross breakeven win rate at avg RR
            print(f"\n--- {label} / {variant} ---")
            print(
                f"  trades {g['n']:,} (buy {g['n_buy']}/sell {g['n_sell']})  "
                f"avg stop {g['avg_stop_pips']:.1f}p  avg RR {g['avg_rr']:.2f}:1  "
                f"breakeven WR {be_wr * 100:.1f}%"
            )
            print(
                f"  GROSS: WR {g['win_rate'] * 100:.1f}%  PF {g['pf']:.3f}  "
                f"exp {g['expectancy_r']:+.4f}R  total {g['total_r']:+.1f}R  "
                f"ret {g['return_pct']:+.1f}%  DD {g['maxdd_pct']:.1f}%"
            )
            print(
                f"  NET  : WR {net['win_rate'] * 100:.1f}%  PF {net['pf']:.3f}  "
                f"exp {net['expectancy_r']:+.4f}R  total {net['total_r']:+.1f}R  "
                f"ret {net['return_pct']:+.1f}%  DD {net['maxdd_pct']:.1f}%"
            )
            rows.append(
                {
                    "pair": label,
                    "variant": variant,
                    "trades": g["n"],
                    "buy": g["n_buy"],
                    "sell": g["n_sell"],
                    "avg_stop_p": round(g["avg_stop_pips"], 1),
                    "avg_rr": round(g["avg_rr"], 2),
                    "be_wr_pct": round(be_wr * 100, 1),
                    "g_wr_pct": round(g["win_rate"] * 100, 1),
                    "g_pf": round(g["pf"], 3),
                    "g_exp_r": round(g["expectancy_r"], 4),
                    "g_total_r": round(g["total_r"], 1),
                    "g_ret_pct": round(g["return_pct"], 1),
                    "g_dd_pct": round(g["maxdd_pct"], 1),
                    "n_wr_pct": round(net["win_rate"] * 100, 1),
                    "n_pf": round(net["pf"], 3),
                    "n_exp_r": round(net["expectancy_r"], 4),
                    "n_total_r": round(net["total_r"], 1),
                    "n_ret_pct": round(net["return_pct"], 1),
                    "n_dd_pct": round(net["maxdd_pct"], 1),
                }
            )

    if rows and args.out:
        pd.DataFrame(rows).to_csv(args.out, index=False)
        print(f"\nWrote summary table -> {args.out}")
    # also print a compact verdict table to stdout
    if rows:
        print("\n================ SUMMARY (NET of ECN cost) ================")
        print(f"{'pair':8} {'variant':18} {'N':>5} {'WR%':>6} {'PF':>6} {'expR':>8} {'ret%':>8} {'DD%':>6}")
        for r in rows:
            print(
                f"{r['pair']:8} {r['variant']:18} {r['trades']:5d} {r['n_wr_pct']:6.1f} "
                f"{r['n_pf']:6.3f} {r['n_exp_r']:+8.4f} {r['n_ret_pct']:+8.1f} {r['n_dd_pct']:6.1f}"
            )


if __name__ == "__main__":
    main()
