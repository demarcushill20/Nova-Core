#!/usr/bin/env python3
"""Backtest task-1086: a GEX/dealer-gamma-INSPIRED regime-switch strategy.

Context
-------
Tasks 1081-1085 evaluated four YouTube transcripts on dealer gamma exposure
(GEX). Verdict there: the *options-data* signal (net GEX flips, 0DTE pinning,
strike magnets) is REAL but out of scope for NovaCore -- we trade spot FX / gold
/ crypto on MetaApi with no options-chain feed, and the literal edge is crowded.

This task asks the next question: *build a strategy that may work using whatever
is TRUE in the video.* What is true and PORTABLE -- independent of having an
options chain -- is the **mechanism**, not the data:

  - When dealers are net LONG gamma they hedge AGAINST moves (sell rallies, buy
    dips). Volatility is suppressed; price mean-reverts and "pins" toward a
    magnet level. The video calls this "the map".
  - When dealers are net SHORT gamma they hedge WITH moves (buy as it rises,
    sell as it falls). Volatility is amplified; price trends and accelerates.
    The video calls this "a heavier foot on the gas pedal".

GEX is one *cause* of this regime split, but the *effect* -- regime-dependent
behaviour (revert vs. trend) -- is observable from price alone. So we proxy the
gamma regime with a realized-volatility compression/expansion detector and run
the matching playbook:

  LONG-GAMMA proxy  (vol compressed)  -> MEAN-REVERT toward a rolling "magnet".
  SHORT-GAMMA proxy (vol expanding)   -> MOMENTUM breakout in the break dir'n.

This is NOT a claim that we can see dealer positioning. It is a falsifiable bet
that the *regime-conditioning* idea -- "pick revert vs. trend by the vol state"
-- adds value over running either playbook unconditionally.

Discipline (same as the rest of the alpha sweep)
------------------------------------------------
A single number proves nothing, so every run prints the strategy alongside
controls designed to kill it:

  NET  vs GROSS            -- is any edge just cost-fragile?
  CONTROL revert-always    -- mean-revert with NO regime gate.
  CONTROL trend-always     -- momentum with NO regime gate.
  CONTROL inverted-gate    -- run the WRONG playbook per regime. If this does as
                              well as the real gate, the gate is noise.

The honest test: does the regime-gated strategy beat BOTH unconditional
controls AND clearly beat its own inverted twin, net of cost? If not -> NO-GO.

No-lookahead: indicators use bars [.. i]; the decision made at the close of bar
i is filled at the OPEN of bar i+1; stops/targets are checked intrabar on the
high/low of subsequent bars. Single position, fixed-fraction risk, expectancy
reported in R (R = entry-to-initial-stop distance), matching the repo metric.

Examples
--------
    # Gold 15m
    python scripts/backtest_1086_gamma_regime.py \
        --data data/candles/xauusd_15m.parquet --symbol XAUUSD \
        --cost-pips 2.5 --pip 0.1

    # BTC 1h
    python scripts/backtest_1086_gamma_regime.py \
        --data data/candles/btcusd_1h.parquet --symbol BTCUSD \
        --cost-pips 5.0 --pip 1.0

    # EURUSD 15m (ECN-ish cost)
    python scripts/backtest_1086_gamma_regime.py \
        --data data/candles/eurusd_15m.parquet --symbol EURUSD \
        --cost-pips 0.7 --pip 0.0001
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# data loading
# --------------------------------------------------------------------------- #
def load_candles(path: str) -> pd.DataFrame:
    """Load OHLC candles from parquet or CSV into a time-indexed frame."""
    p = Path(path)
    if p.suffix == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)
        tcol = next((c for c in df.columns if c.lower() in ("time", "timestamp", "date", "datetime")), None)
        if tcol is not None:
            df[tcol] = pd.to_datetime(df[tcol])
            df = df.set_index(tcol)
    df.columns = [c.lower() for c in df.columns]
    need = {"open", "high", "low", "close"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing OHLC columns {missing}")
    df = df[["open", "high", "low", "close"]].dropna()
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


# --------------------------------------------------------------------------- #
# indicators (all causal: value at i uses bars [.. i] only)
# --------------------------------------------------------------------------- #
def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, lo, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - lo), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def realized_vol(df: pd.DataFrame, n: int) -> pd.Series:
    """Rolling std of log returns -- the raw vol measure we compress/expand on."""
    r = np.log(df["close"]).diff()
    return r.rolling(n, min_periods=n).std()


# --------------------------------------------------------------------------- #
# strategy parameters
# --------------------------------------------------------------------------- #
@dataclass
class Params:
    # regime detector (the gamma-regime proxy)
    vol_fast: int = 20  # short realized-vol window
    vol_slow: int = 100  # baseline realized-vol window
    compress_pctl: float = 0.40  # vol_ratio below this pctl => long-gamma proxy
    expand_pctl: float = 0.60  # vol_ratio above this pctl => short-gamma proxy
    pctl_window: int = 500  # rolling window for the percentile ranks

    # mean-revert leg ("the map")
    anchor_n: int = 50  # rolling-mean magnet
    z_entry: float = 2.0  # enter when |close-anchor| in stdevs exceeds this
    mr_stop_atr: float = 1.5  # stop = z_entry extension + this many ATR beyond

    # momentum leg ("gas pedal")
    donchian_n: int = 20  # breakout lookback
    mo_stop_atr: float = 2.0  # initial / trailing ATR stop
    mo_trail: bool = True

    atr_n: int = 14
    max_hold: int = 96  # force-exit after this many bars (regime can flip)


# --------------------------------------------------------------------------- #
# signal generation
#   mode: 'gate'      -> revert in compression, trend in expansion (the thesis)
#         'inverted'  -> trend in compression, revert in expansion (control)
#         'revert'    -> always mean-revert, no gate (control)
#         'trend'     -> always momentum, no gate (control)
# returns per-bar desired entry: +1 long, -1 short, 0 flat, computed at close i.
# --------------------------------------------------------------------------- #
def gen_signals(df: pd.DataFrame, p: Params, mode: str) -> pd.DataFrame:
    close = df["close"]
    a = atr(df, p.atr_n)

    # --- regime proxy ---
    vf = realized_vol(df, p.vol_fast)
    vs = realized_vol(df, p.vol_slow)
    vol_ratio = vf / vs
    # rolling percentile rank of the ratio (causal): fraction of the trailing
    # window below the current value.
    rank = vol_ratio.rolling(p.pctl_window, min_periods=p.pctl_window).apply(
        lambda x: (x[:-1] < x[-1]).mean(), raw=True
    )
    compressed = rank <= p.compress_pctl  # long-gamma proxy
    expanding = rank >= p.expand_pctl  # short-gamma proxy

    # --- mean-revert leg ---
    anchor = close.rolling(p.anchor_n, min_periods=p.anchor_n).mean()
    sd = close.rolling(p.anchor_n, min_periods=p.anchor_n).std()
    z = (close - anchor) / sd
    mr_sig = pd.Series(0, index=df.index)
    mr_sig[z >= p.z_entry] = -1  # stretched above magnet -> fade short
    mr_sig[z <= -p.z_entry] = 1  # stretched below magnet -> fade long

    # --- momentum leg ---
    hh = df["high"].rolling(p.donchian_n, min_periods=p.donchian_n).max()
    ll = df["low"].rolling(p.donchian_n, min_periods=p.donchian_n).min()
    mo_sig = pd.Series(0, index=df.index)
    mo_sig[close >= hh] = 1  # new high -> ride up
    mo_sig[close <= ll] = -1  # new low  -> ride down

    # --- combine per mode ---
    sig = pd.Series(0, index=df.index)
    if mode == "revert":
        sig = mr_sig
    elif mode == "trend":
        sig = mo_sig
    elif mode == "gate":
        sig = sig.where(~compressed, mr_sig).where(~expanding, mo_sig)
        sig = pd.Series(np.where(compressed, mr_sig, np.where(expanding, mo_sig, 0)), index=df.index)
    elif mode == "inverted":
        sig = pd.Series(np.where(compressed, mo_sig, np.where(expanding, mr_sig, 0)), index=df.index)
    else:
        raise ValueError(f"unknown mode {mode}")

    out = pd.DataFrame(
        {
            "signal": sig.fillna(0).astype(int),
            "atr": a,
            "anchor": anchor,
            "z": z,
            "compressed": compressed.fillna(False),
            "expanding": expanding.fillna(False),
        }
    )
    return out


# --------------------------------------------------------------------------- #
# event-driven backtest (single position, no lookahead)
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    direction: int
    entry: float
    stop: float
    risk: float  # |entry - stop| in price = 1R
    bars: int
    is_revert: bool


def backtest(df: pd.DataFrame, sigdf: pd.DataFrame, p: Params, cost_price: float, mode: str) -> dict:
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    c = df["close"].to_numpy()
    sig = sigdf["signal"].to_numpy()
    a = sigdf["atr"].to_numpy()
    anchor = sigdf["anchor"].to_numpy()
    comp = sigdf["compressed"].to_numpy()

    n = len(df)
    pos: Trade | None = None
    results: list[float] = []  # PnL in R, net of cost
    holds: list[int] = []

    i = 1
    while i < n:
        if pos is None:
            s = sig[i - 1]  # decision at close of i-1
            if s != 0 and not np.isnan(a[i - 1]) and a[i - 1] > 0 and not np.isnan(anchor[i - 1]):
                entry = o[i]  # fill at open of i
                if mode == "gate":
                    is_rev = bool(comp[i - 1])
                else:
                    is_rev = mode == "revert" or (mode == "inverted" and not comp[i - 1])
                stop_atr = p.mr_stop_atr if is_rev else p.mo_stop_atr
                risk = stop_atr * a[i - 1]
                if risk <= 0:
                    i += 1
                    continue
                stop = entry - s * risk
                pos = Trade(direction=s, entry=entry, stop=stop, risk=risk, bars=0, is_revert=is_rev)
                # fall through: manage from bar i onward
            else:
                i += 1
                continue

        # ---- manage open position on bar i ----
        d = pos.direction
        pos.bars += 1
        exit_price = None

        # stop check (intrabar, conservative: stop can hit same bar as entry)
        if (d == 1 and lo[i] <= pos.stop) or (d == -1 and h[i] >= pos.stop):
            exit_price = pos.stop

        # target / mean-revert exit: revert leg exits at the magnet (anchor)
        if (
            exit_price is None
            and pos.is_revert
            and not np.isnan(anchor[i])
            and ((d == 1 and h[i] >= anchor[i]) or (d == -1 and lo[i] <= anchor[i]))
        ):
            exit_price = anchor[i]

        # momentum trailing stop
        if exit_price is None and not pos.is_revert and p.mo_trail and not np.isnan(a[i]):
            new_stop = c[i] - d * p.mo_stop_atr * a[i]
            if d == 1:
                pos.stop = max(pos.stop, new_stop)
            else:
                pos.stop = min(pos.stop, new_stop)

        # max-hold force exit
        if exit_price is None and pos.bars >= p.max_hold:
            exit_price = c[i]

        if exit_price is not None:
            gross = d * (exit_price - pos.entry)
            net = gross - cost_price  # round-trip cost in price terms
            results.append(net / pos.risk)
            holds.append(pos.bars)
            pos = None
        i += 1

    arr = np.array(results)
    if len(arr) == 0:
        return {
            "trades": 0,
            "expectancy_r": 0.0,
            "total_r": 0.0,
            "win_rate": 0.0,
            "avg_hold": 0.0,
            "sharpe_per_trade": 0.0,
        }
    return {
        "trades": len(arr),
        "expectancy_r": float(arr.mean()),
        "total_r": float(arr.sum()),
        "win_rate": float((arr > 0).mean()),
        "avg_hold": float(np.mean(holds)),
        "sharpe_per_trade": float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0,
    }


# --------------------------------------------------------------------------- #
def _run_mode(df, p, cost_price, mode):
    sigdf = gen_signals(df, p, mode)
    return backtest(df, sigdf, p, cost_price, mode)


def _fmt(tag: str, m: dict) -> str:
    return (
        f"  {tag:<34} trades={m['trades']:>5}  "
        f"exp={m['expectancy_r']:+.4f}R  total={m['total_r']:+8.1f}R  "
        f"win={m['win_rate']:.2%}  shrp/t={m['sharpe_per_trade']:+.3f}  "
        f"hold={m['avg_hold']:.0f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbol", default="?")
    ap.add_argument("--pip", type=float, default=0.0001, help="price value of 1 pip")
    ap.add_argument("--cost-pips", type=float, default=1.0, help="round-trip cost in pips (spread+slip+comm)")
    # optional param overrides
    ap.add_argument("--z-entry", type=float, default=Params.z_entry)
    ap.add_argument("--compress-pctl", type=float, default=Params.compress_pctl)
    ap.add_argument("--expand-pctl", type=float, default=Params.expand_pctl)
    args = ap.parse_args()

    df = load_candles(args.data)
    p = Params(z_entry=args.z_entry, compress_pctl=args.compress_pctl, expand_pctl=args.expand_pctl)
    cost_price = args.cost_pips * args.pip

    print(f"\n=== GEX-inspired regime-switch :: {args.symbol} ===")
    print(f"data={args.data}  bars={len(df):,}  {df.index.min()} -> {df.index.max()}")
    print(
        f"cost={args.cost_pips}p ({cost_price:g} price)  "
        f"z={p.z_entry}  compress<=p{p.compress_pctl:.0%}  expand>=p{p.expand_pctl:.0%}\n"
    )

    gate = _run_mode(df, p, cost_price, "gate")
    inv = _run_mode(df, p, cost_price, "inverted")
    rev = _run_mode(df, p, cost_price, "revert")
    tr = _run_mode(df, p, cost_price, "trend")
    # gross version of the thesis (cost = 0) to see if any edge is cost-fragile
    gate_gross = _run_mode(df, p, 0.0, "gate")

    print(_fmt("THESIS  regime-gate (NET)", gate))
    print(_fmt("thesis  regime-gate (GROSS)", gate_gross))
    print(_fmt("control inverted-gate (NET)", inv))
    print(_fmt("control revert-always (NET)", rev))
    print(_fmt("control trend-always  (NET)", tr))

    # ---- honest verdict ----
    print("\n--- verdict ---")
    beats_uncond = gate["expectancy_r"] > max(rev["expectancy_r"], tr["expectancy_r"])
    beats_inverted = gate["expectancy_r"] > inv["expectancy_r"] + 0.01
    gross_edge = gate_gross["expectancy_r"] > 0 and gate_gross["trades"] >= 30
    pos_net = gate["expectancy_r"] > 0 and gate["trades"] >= 30
    # breakeven cost in R terms: how much cost/trade the gross edge can absorb.
    be_r = gate_gross["expectancy_r"]
    if pos_net and beats_uncond and beats_inverted:
        print("  CANDIDATE: gate is net-positive, beats both unconditional controls AND its inverted twin.")
    elif not gross_edge:
        print("  NO-GO: no GROSS edge even before cost -- the regime gate adds no raw signal.")
    elif gross_edge and beats_uncond and beats_inverted and not pos_net:
        print(
            f"  REAL-BUT-COST-KILLED: the regime gate IS a genuine conditioner -- GROSS edge "
            f"{be_r:+.4f}R and it beats every unconditional control AND its inverted twin "
            f"(the regime split carries real information, not noise). But the per-trade move is "
            f"too small: cost wipes the edge (net {gate['expectancy_r']:+.4f}R). Same cost wall as "
            f"the rest of the price-action sweep -- not tradeable as-is on this instrument/cost."
        )
    elif not beats_inverted:
        print("  NO-GO: gate does not clearly beat its INVERTED twin -> the regime split is noise.")
    elif not beats_uncond:
        print("  NO-GO: gate does not beat running one playbook unconditionally -> the gate adds nothing.")
    else:
        print("  NO-GO: net expectancy non-positive / too few trades after cost.")


if __name__ == "__main__":
    main()
