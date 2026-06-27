#!/usr/bin/env python3
"""
Forex Momentum-Squeeze strategy — built from the "Trade Tactics" video methodology.

The video (transcript chunks 1092-1096) does not give a forex rule set; it gives a
*methodology* for autonomously building + validating one:

    phase 1  momentum-squeeze backbone
    phase 2  directional filter
    phase 3  chop / flat filter (rule out bad trades)
    phase 4  exit rules
    phase 5  iterate, judged on in-sample vs out-of-sample divergence + Monte-Carlo
             robustness, targeting Sharpe ~1 with controlled drawdown.

This module implements that full pipeline for EUR/USD H1 with an honest cost model.
It is a methodology translation, not a claim that the video contained this strategy.

Indicators are TTM-squeeze style (John Carter): Bollinger Bands inside Keltner
Channels = volatility compression; the squeeze "release" (BB expands outside KC)
is the breakout trigger, and a linear-regression momentum oscillator sets direction.

Run:
    python3 scripts/forex_momentum_squeeze.py
    python3 scripts/forex_momentum_squeeze.py --csv data/candles/EURUSD_H1_10yr.csv --mc 2000

Dependencies: pandas, numpy only (no vectorbt — not installed on this box).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

# EUR/USD: 1 pip = 0.0001. Round-turn cost is applied in price terms at entry+exit.
PIP = 0.0001
HOURS_PER_YEAR = 24 * 365  # H1 bars; annualisation factor for Sharpe


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, low, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - low), (h - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder ADX — trend-strength gauge used as the chop/flat filter."""
    h, low, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = c.shift(1)
    tr = pd.concat([(h - low), (h - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    atr_n = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def linreg_momentum(c: pd.Series, n: int) -> pd.Series:
    """TTM squeeze momentum: linreg value of price de-trended vs its mid-channel."""
    highest = c.rolling(n).max()
    lowest = c.rolling(n).min()
    sma = c.rolling(n).mean()
    baseline = (highest + lowest) / 2
    de = c - (baseline + sma) / 2
    x = np.arange(n)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()

    def _slope_val(window: np.ndarray) -> float:
        ym = window.mean()
        slope = ((x - xm) * (window - ym)).sum() / denom
        intercept = ym - slope * xm
        return intercept + slope * (n - 1)  # linreg value at last point

    return de.rolling(n).apply(_slope_val, raw=True)


# --------------------------------------------------------------------------- #
# Strategy params
# --------------------------------------------------------------------------- #
@dataclass
class Params:
    bb_len: int = 20
    bb_mult: float = 2.0
    kc_len: int = 20
    kc_mult: float = 1.5
    mom_len: int = 20
    trend_len: int = 200  # directional filter (phase 2)
    adx_len: int = 14
    adx_min: float = 20.0  # chop filter (phase 3): skip when trend too weak
    atr_len: int = 14
    stop_atr: float = 2.0  # exit rules (phase 4)
    target_atr: float = 3.0
    trail_atr: float = 2.5
    risk_frac: float = 0.01  # 1% equity risk per trade
    cost_pips: float = 0.8  # round-turn spread+commission (ECN-ish EUR/USD)


def build_signals(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df.copy()
    basis = out["close"].rolling(p.bb_len).mean()
    dev = p.bb_mult * out["close"].rolling(p.bb_len).std(ddof=0)
    out["bb_upper"], out["bb_lower"] = basis + dev, basis - dev
    kc_mid = ema(out["close"], p.kc_len)
    kc_rng = p.kc_mult * atr(out, p.kc_len)
    out["kc_upper"], out["kc_lower"] = kc_mid + kc_rng, kc_mid - kc_rng

    # squeeze ON when Bollinger band sits inside Keltner channel (compression)
    out["squeeze_on"] = (out["bb_lower"] > out["kc_lower"]) & (out["bb_upper"] < out["kc_upper"])
    out["squeeze_off"] = ~out["squeeze_on"]
    # "fired" = squeeze just released this bar (was on previous bar, off now)
    out["fired"] = out["squeeze_off"] & out["squeeze_on"].shift(1).fillna(False)

    out["mom"] = linreg_momentum(out["close"], p.mom_len)
    out["trend"] = ema(out["close"], p.trend_len)
    out["adx"] = adx(out, p.adx_len)
    out["atr"] = atr(out, p.atr_len)
    return out


# --------------------------------------------------------------------------- #
# Event-driven backtest (single position, next-bar-open fills)
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    side: int
    entry_i: int
    exit_i: int
    entry: float
    exit: float
    r_multiple: float
    ret_frac: float


def backtest(df: pd.DataFrame, p: Params, start_equity: float = 10_000.0):  # noqa: C901
    d = build_signals(df, p)
    cols = {c: d.columns.get_loc(c) for c in ["open", "high", "low", "close", "fired", "mom", "trend", "adx", "atr"]}
    v = d.values
    n = len(d)
    cost = p.cost_pips * PIP

    equity = start_equity
    trades: list[Trade] = []
    eq_curve = [equity]
    pos = 0  # 0 flat, 1 long, -1 short
    entry = stop = target = trail = 0.0
    entry_i = 0
    qty = 0.0

    def g(i, c):  # cell accessor
        return v[i, cols[c]]

    for i in range(1, n - 1):
        if pos == 0:
            if not g(i, "fired"):
                eq_curve.append(equity)
                continue
            mom, trend, adxv, a, close = (g(i, "mom"), g(i, "trend"), g(i, "adx"), g(i, "atr"), g(i, "close"))
            if np.isnan(a) or np.isnan(trend) or np.isnan(mom) or a <= 0:
                eq_curve.append(equity)
                continue
            if adxv < p.adx_min:  # chop filter
                eq_curve.append(equity)
                continue
            long_ok = mom > 0 and close > trend  # squeeze dir + directional filter
            short_ok = mom < 0 and close < trend
            if not (long_ok or short_ok):
                eq_curve.append(equity)
                continue
            pos = 1 if long_ok else -1
            entry = g(i + 1, "open")  # next-bar-open fill
            entry_i = i + 1
            risk_per_unit = p.stop_atr * a
            stop = entry - pos * risk_per_unit
            target = entry + pos * p.target_atr * a
            trail = entry - pos * p.trail_atr * a
            qty = (equity * p.risk_frac) / risk_per_unit
            eq_curve.append(equity)
        else:
            hi, lo = g(i, "high"), g(i, "low")
            exit_price = None
            # intrabar: stop checked before target (conservative)
            if pos == 1:
                if lo <= stop:
                    exit_price = stop
                elif hi >= target:
                    exit_price = target
            else:
                if hi >= stop:
                    exit_price = stop
                elif lo <= target:
                    exit_price = target
            # trailing stop (chandelier on close)
            a = g(i, "atr")
            if exit_price is None and not np.isnan(a):
                if pos == 1:
                    trail = max(trail, g(i, "close") - p.trail_atr * a)
                    if lo <= trail:
                        exit_price = trail
                else:
                    trail = min(trail, g(i, "close") + p.trail_atr * a)
                    if hi >= trail:
                        exit_price = trail
            # momentum-flip exit
            if exit_price is None:
                mom = g(i, "mom")
                if (pos == 1 and mom < 0) or (pos == -1 and mom > 0):
                    exit_price = g(i + 1, "open")

            if exit_price is not None:
                gross = pos * (exit_price - entry) * qty
                pnl = gross - cost * qty
                r_mult = (pos * (exit_price - entry) - cost) / (p.stop_atr * g(entry_i, "atr"))
                ret_frac = pnl / equity
                equity += pnl
                trades.append(Trade(pos, entry_i, i, entry, exit_price, r_mult, ret_frac))
                pos = 0
            eq_curve.append(equity)

    return d, trades, np.array(eq_curve), equity


# --------------------------------------------------------------------------- #
# Metrics + Monte-Carlo stress test (video's robustness gate)
# --------------------------------------------------------------------------- #
def metrics(trades: list[Trade], eq: np.ndarray, start_equity: float) -> dict:
    if not trades:
        return {"trades": 0}
    rets = np.array([t.ret_frac for t in trades])
    wins = rets > 0
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    # per-trade Sharpe annualised by avg trades/year (approx via H1 horizon)
    sharpe = (rets.mean() / rets.std(ddof=1) * np.sqrt(len(rets))) if rets.std(ddof=1) > 0 else 0.0
    net = (eq[-1] - start_equity) / start_equity
    gross_win = rets[wins].sum()
    gross_loss = -rets[~wins].sum()
    return {
        "trades": len(trades),
        "win_rate": round(float(wins.mean()), 4),
        "net_return": round(float(net), 4),
        "avg_R": round(float(np.mean([t.r_multiple for t in trades])), 4),
        "expectancy_frac": round(float(rets.mean()), 6),
        "profit_factor": round(float(gross_win / gross_loss), 3) if gross_loss > 0 else float("inf"),
        "sharpe": round(float(sharpe), 3),
        "max_drawdown": round(float(dd.min()), 4),
    }


def monte_carlo(trades: list[Trade], start_equity: float, runs: int, seed: int = 7) -> dict:
    """Bootstrap-resample trade returns; a robust edge survives reshuffled sequences."""
    if len(trades) < 10:
        return {"runs": 0, "note": "too few trades for MC"}
    rng = np.random.default_rng(seed)
    rets = np.array([t.ret_frac for t in trades])
    finals_list: list[float] = []
    dds_list: list[float] = []
    for _ in range(runs):
        sample = rng.choice(rets, size=len(rets), replace=True)
        eq = start_equity * np.cumprod(1 + sample)
        peak = np.maximum.accumulate(eq)
        finals_list.append((eq[-1] - start_equity) / start_equity)
        dds_list.append(float(((eq - peak) / peak).min()))
    finals, dds = np.array(finals_list), np.array(dds_list)
    return {
        "runs": runs,
        "pct_profitable": round(float((finals > 0).mean()), 4),
        "net_return_p05": round(float(np.percentile(finals, 5)), 4),
        "net_return_median": round(float(np.percentile(finals, 50)), 4),
        "max_dd_p95_worst": round(float(np.percentile(dds, 5)), 4),
        "max_dd_median": round(float(np.percentile(dds, 50)), 4),
    }


def load(csv: str) -> pd.DataFrame:
    df = pd.read_csv(csv)
    if np.issubdtype(df["timestamp"].dtype, np.number):
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def run(csv: str, mc_runs: int, split: float, p: Params) -> dict:
    df = load(csv)
    cut = int(len(df) * split)
    is_df = df.iloc[:cut].reset_index(drop=True)
    oos_df = df.iloc[cut:].reset_index(drop=True)

    report = {
        "instrument": "EUR/USD H1",
        "csv": csv,
        "bars": len(df),
        "period": f"{df.timestamp.iloc[0].date()} -> {df.timestamp.iloc[-1].date()}",
        "split": f"{int(split * 100)}% in-sample / {int((1 - split) * 100)}% out-of-sample",
        "params": asdict(p),
    }
    for name, seg in (("in_sample", is_df), ("out_of_sample", oos_df), ("full", df)):
        _, tr, eq, _ = backtest(seg, p)
        report[name] = metrics(tr, eq, 10_000.0)
        if name == "out_of_sample":
            report["out_of_sample_monte_carlo"] = monte_carlo(tr, 10_000.0, mc_runs)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/candles/EURUSD_H1_10yr.csv")
    ap.add_argument("--mc", type=int, default=2000)
    ap.add_argument("--split", type=float, default=0.7)
    args = ap.parse_args()
    rep = run(args.csv, args.mc, args.split, Params())
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
