#!/usr/bin/env python3
"""
Task 1047 — Single 21-SMMA pullback "big candle" continuation system, ATR exits.

Spec (from TASKS/1047_21_SMMA_is_sloping_downward_.md — truncated on write but the
strategy is fully specified):

  SHORT signal (mirror for LONG):
    bearish_trend        : close on the bearish side of SMMA_21 (close < SMMA_21)
    smma21_slope_down    : SMMA_21[i] - SMMA_21[i-slope_lb] < -min_slope
    close_near_smma21    : |close - SMMA_21| <= near_pips   (pullback to the MA)
    bearish_big_candle   : current candle bearish AND
                             (a) range >= big_mult x previous candle range AND
                             (b) range >= atr_mult x ATR(14)
    confirm on candle N close -> enter at N+1 open.

  EXITS: ATR-based, set from entry price using ATR at the signal candle.
    SHORT: stop = entry + sl_mult*atr ; tp = entry - tp_mult*atr
    LONG : stop = entry - sl_mult*atr ; tp = entry + tp_mult*atr
  Variants tested (sl, tp): (1.0,1.5) default, (1.0,1.0), (1.0,2.0),
                            (1.5,2.0), (2.0,3.0).

  Trade management: one trade at a time, no pyramiding/averaging/trailing/BE,
  ignore new signals while in a position. Symmetric long+short.

Costs applied as round-turn pips (0 / 1.0 / 2.0).
"""

import numpy as np
import pandas as pd

PIP = 0.0001


def smma(series, period):
    s = series.to_numpy(dtype=float)
    out = np.full(len(s), np.nan)
    if len(s) < period:
        return pd.Series(out, index=series.index)
    out[period - 1] = s[:period].mean()
    a = 1.0 / period
    for i in range(period, len(s)):
        out[i] = out[i - 1] + a * (s[i] - out[i - 1])
    return pd.Series(out, index=series.index)


def atr(df, period=14):
    h, lo, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
    out = np.full(len(tr), np.nan)
    if len(tr) >= period:
        out[period - 1] = tr[:period].mean()
        a = 1.0 / period
        for i in range(period, len(tr)):
            out[i] = out[i - 1] + a * (tr[i] - out[i - 1])
    return out


def load_eurusd_h1():
    df = pd.read_csv("data/candles/EURUSD_H1_10yr.csv", parse_dates=["timestamp"])
    df = df.rename(columns={"timestamp": "time"}).set_index("time")
    return df[["open", "high", "low", "close"]]


def load_gbpusd_h1():
    df = pd.read_parquet("data/candles/gbpusd_15m.parquet")
    o = df["open"].resample("1h").first()
    h = df["high"].resample("1h").max()
    lo = df["low"].resample("1h").min()
    c = df["close"].resample("1h").last()
    return pd.concat([o, h, lo, c], axis=1, keys=["open", "high", "low", "close"]).dropna()


def backtest(
    df,
    slope_lb=3,
    slope_min_pips=0.5,
    near_pips=8.0,
    big_mult=1.3,
    atr_mult=1.0,
    sl_mult=1.0,
    tp_mult=1.5,
    cost_pips=0.0,
):
    df = df.copy()
    df["s21"] = smma(df["close"], 21)
    df["atr"] = atr(df, 14)
    o = df["open"].to_numpy()
    c = df["close"].to_numpy()
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    s21 = df["s21"].to_numpy()
    a = df["atr"].to_numpy()
    rng = h - lo
    n = len(df)
    slope_min = slope_min_pips * PIP
    near = near_pips * PIP
    cost = cost_pips * PIP

    trades = []
    i = max(21, slope_lb) + 1
    while i < n - 1:
        if np.isnan(s21[i]) or np.isnan(a[i]) or i - slope_lb < 0 or np.isnan(s21[i - slope_lb]):
            i += 1
            continue
        slope = s21[i] - s21[i - slope_lb]
        near_ok = abs(c[i] - s21[i]) <= near
        big_ok = rng[i] >= big_mult * rng[i - 1] and rng[i] >= atr_mult * a[i]
        bear = c[i] < o[i]
        bull = c[i] > o[i]

        sig = None
        if c[i] < s21[i] and slope < -slope_min and near_ok and big_ok and bear:
            sig = "short"
        elif c[i] > s21[i] and slope > slope_min and near_ok and big_ok and bull:
            sig = "long"

        if sig is None:
            i += 1
            continue

        entry = o[i + 1]
        atr_e = a[i]
        if sig == "short":
            stop = entry + sl_mult * atr_e
            tp = entry - tp_mult * atr_e
        else:
            stop = entry - sl_mult * atr_e
            tp = entry + tp_mult * atr_e

        # walk forward from entry bar i+1 to resolution
        outcome = None
        exitp = None
        j = i + 1
        while j < n:
            if sig == "short":
                # stop above, tp below; assume stop checked first if both hit (conservative)
                if h[j] >= stop:
                    outcome = "stop"
                    exitp = stop
                    break
                if lo[j] <= tp:
                    outcome = "tp"
                    exitp = tp
                    break
            else:
                if lo[j] <= stop:
                    outcome = "stop"
                    exitp = stop
                    break
                if h[j] >= tp:
                    outcome = "tp"
                    exitp = tp
                    break
            j += 1
        if outcome is None:
            j = n - 1
            exitp = c[j]
            outcome = "eod"

        if sig == "short":
            pnl = entry - exitp
        else:
            pnl = exitp - entry
        pnl -= cost  # round-turn cost in price units
        trades.append(
            {
                "dir": sig,
                "entry_i": i + 1,
                "exit_i": j,
                "outcome": outcome,
                "pnl_pips": pnl / PIP,
                "atr_pips": atr_e / PIP,
            }
        )
        i = j + 1  # one trade at a time; resume after exit

    return pd.DataFrame(trades)


def summarize(tr, label):
    if len(tr) == 0:
        return f"{label}: 0 trades"
    n = len(tr)
    wins = (tr["pnl_pips"] > 0).sum()
    wr = wins / n * 100
    tot = tr["pnl_pips"].sum()
    exp = tr["pnl_pips"].mean()
    gp = tr.loc[tr["pnl_pips"] > 0, "pnl_pips"].sum()
    gl = -tr.loc[tr["pnl_pips"] < 0, "pnl_pips"].sum()
    pf = gp / gl if gl > 0 else float("inf")
    return (
        f"{label}: n={n} wr={wr:.1f}% exp={exp:+.2f}p tot={tot:+.0f}p PF={pf:.2f} avgATR={tr['atr_pips'].mean():.1f}p"
    )


VARIANTS = [(1.0, 1.5), (1.0, 1.0), (1.0, 2.0), (1.5, 2.0), (2.0, 3.0)]


def run_symbol(name, df):
    print(f"\n===== {name}  ({df.index[0].date()} -> {df.index[-1].date()}, {len(df)} H1 bars) =====")
    for sl, tp in VARIANTS:
        for cost in (0.0, 1.0, 2.0):
            tr = backtest(df, sl_mult=sl, tp_mult=tp, cost_pips=cost)
            print(summarize(tr, f"  SL{sl}/TP{tp} cost{cost}p"))
        print()


if __name__ == "__main__":
    run_symbol("EURUSD H1", load_eurusd_h1())
    try:
        run_symbol("GBPUSD H1", load_gbpusd_h1())
    except Exception as e:
        print(f"\nGBPUSD skipped: {e}")
