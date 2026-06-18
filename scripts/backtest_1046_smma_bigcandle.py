#!/usr/bin/env python3
"""
Task 1046 — SMMA-stack trend-pullback "Big Fat Candle" continuation system.

Spec (from TASKS/1046...):
  TREND  : SMMA_21 < SMMA_50 < SMMA_200 (down) or > > (up), AND last N closes
           all on the trend side of all three SMMAs.            [N=3 default]
  SLOPE  : SMMA_21[i] - SMMA_21[i-slope_lb] beyond +/- min_slope_pips. [lb=3]
  PULLBACK: |close - SMMA_21| <= near_21_pips                    [5 pips]
  TRIGGER: a "big fat candle" in the trend direction whose full range
             (a) >= big_mult x previous candle range, AND          [2.0x]
             (b) >= atr_mult x ATR(14)                              [0.8x]
           (green for longs, red for shorts).
  ENTRY  : next bar open.
  STOP   : opposite extreme of the trigger candle (spec-silent -> natural stop).
  TARGET : R-multiple of stop distance (tested 1.0 / 1.5 / 2.0).

Symmetric long+short. Costs applied as round-turn pips.
"""

import numpy as np
import pandas as pd

PIP = 0.0001  # non-JPY


def smma(series, period):
    # Wilder smoothing (SMMA / RMA): seed with SMA, then recursive.
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
    out = pd.concat([o, h, lo, c], axis=1, keys=["open", "high", "low", "close"]).dropna()
    return out


def backtest(  # noqa: C901  # research backtest; complexity is inherent
    df,
    N=3,
    slope_lb=3,
    slope_min_pips=1.0,
    near_pips=5.0,
    big_mult=2.0,
    atr_mult=0.8,
    target_R=1.5,
    cost_pips=0.0,
    stop_buffer_pips=0.0,
):
    df = df.copy()
    df["s21"] = smma(df["close"], 21)
    df["s50"] = smma(df["close"], 50)
    df["s200"] = smma(df["close"], 200)
    df["atr"] = atr(df, 14)

    c = df["close"].to_numpy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    s21 = df["s21"].to_numpy()
    s50 = df["s50"].to_numpy()
    s200 = df["s200"].to_numpy()
    a = df["atr"].to_numpy()
    rng = h - lo
    n = len(df)

    slope_min = slope_min_pips * PIP
    near = near_pips * PIP
    buf = stop_buffer_pips * PIP

    trades = []
    i = 200 + slope_lb + 1
    in_pos = False
    while i < n - 1:
        if in_pos:
            i += 1
            continue
        if np.isnan(s200[i]) or np.isnan(a[i]):
            i += 1
            continue

        # trend stack + last N closes on trend side of all three SMMAs
        up_stack = s21[i] > s50[i] > s200[i]
        dn_stack = s21[i] < s50[i] < s200[i]
        up_closes = all(c[i - k] > s21[i - k] and c[i - k] > s50[i - k] and c[i - k] > s200[i - k] for k in range(N))
        dn_closes = all(c[i - k] < s21[i - k] and c[i - k] < s50[i - k] and c[i - k] < s200[i - k] for k in range(N))

        slope = s21[i] - s21[i - slope_lb]
        near_ok = abs(c[i] - s21[i]) <= near
        big = rng[i] >= big_mult * rng[i - 1] and rng[i] >= atr_mult * a[i]
        green = c[i] > o[i]
        red = c[i] < o[i]

        direction = 0
        if up_stack and up_closes and slope > slope_min and near_ok and big and green:
            direction = 1
        elif dn_stack and dn_closes and slope < -slope_min and near_ok and big and red:
            direction = -1

        if direction == 0:
            i += 1
            continue

        entry = o[i + 1]
        if direction == 1:
            stop = lo[i] - buf
            risk = entry - stop
            if risk <= 0:
                i += 1
                continue
            tp = entry + target_R * risk
        else:
            stop = h[i] + buf
            risk = stop - entry
            if risk <= 0:
                i += 1
                continue
            tp = entry - target_R * risk

        # walk forward bar-by-bar; stop checked before target (conservative)
        outcome = None
        j = i + 1
        while j < n:
            if direction == 1:
                if lo[j] <= stop:
                    outcome = -1.0
                    break
                if h[j] >= tp:
                    outcome = target_R
                    break
            else:
                if h[j] >= stop:
                    outcome = -1.0
                    break
                if lo[j] <= tp:
                    outcome = target_R
                    break
            j += 1
        if outcome is None:
            # close at last bar
            last = c[-1]
            pnl_price = (last - entry) if direction == 1 else (entry - last)
            outcome = pnl_price / risk

        cost_R = (cost_pips * PIP) / risk
        net_R = outcome - cost_R
        trades.append(
            {
                "time": df.index[i],
                "dir": direction,
                "risk_pips": risk / PIP,
                "gross_R": outcome,
                "net_R": net_R,
                "year": df.index[i].year,
            }
        )
        i = j + 1 if j < n else n
        in_pos = False

    return pd.DataFrame(trades)


def stats(tr, label):
    if len(tr) == 0:
        return f"{label}: NO TRADES"
    n = len(tr)
    wr = (tr["gross_R"] > 0).mean()
    g = tr["gross_R"].sum()
    gn = tr["net_R"].sum()
    exp_g = tr["gross_R"].mean()
    exp_n = tr["net_R"].mean()
    wins = tr.loc[tr["net_R"] > 0, "net_R"].sum()
    losses = -tr.loc[tr["net_R"] <= 0, "net_R"].sum()
    pf = wins / losses if losses > 0 else float("inf")
    return (
        f"{label}: n={n} WR={wr * 100:.1f}% | gross sumR={g:+.1f} exp={exp_g:+.3f}R | "
        f"net sumR={gn:+.1f} exp={exp_n:+.3f}R PF={pf:.2f} | medRisk={tr['risk_pips'].median():.1f}p"
    )


def main():
    out = []

    def p(x):
        print(x)
        out.append(x)

    datasets = {"EURUSD_H1": load_eurusd_h1(), "GBPUSD_H1": load_gbpusd_h1()}
    for name, df in datasets.items():
        p(f"\n{'=' * 70}\n{name}  rows={len(df)}  {df.index[0]} -> {df.index[-1]}\n{'=' * 70}")
        # faithful spec config across targets, gross + at realistic costs
        for tR in (1.0, 1.5, 2.0):
            tr = backtest(df, target_R=tR, cost_pips=0.0)
            p(stats(tr, f"  [faithful N3 slope1p near5p] tgt={tR}R gross "))
            for cost in (1.0, 2.0):
                trn = backtest(df, target_R=tR, cost_pips=cost)
                p(stats(trn, f"      net@{cost:.1f}p tgt={tR}R "))
            if len(tr):
                yr = tr.groupby("year")["net_R"].sum()
                p("      by-year net@0: " + " ".join(f"{y}:{v:+.0f}" for y, v in yr.items()))
        # robustness: loosen pullback, drop slope min
        p("  -- robustness sweep (tgt=1.5R, net@1.5p) --")
        for near in (5, 10, 20):
            for sl in (0.0, 1.0):
                trn = backtest(df, near_pips=near, slope_min_pips=sl, target_R=1.5, cost_pips=1.5)
                p(stats(trn, f"     near={near}p slopeMin={sl}p "))
    with open("WORK/1046_run.txt", "w") as f:
        f.write("\n".join(out))
    print("\n[written WORK/1046_run.txt]")


if __name__ == "__main__":
    main()
