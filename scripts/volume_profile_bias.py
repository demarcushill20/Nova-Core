#!/usr/bin/env python3
"""Volume-profile bias strategy — port of a market-profile / POC-bias video.

Source idea (task 1110, trading-video transcript): the trader builds a *daily
directional bias* from how the volume profile's Point of Control (POC) and Value
Area migrate day-over-day ("accumulation / manipulation / distribution"):

  * If yesterday's cash-session POC closed *below* the prior day's POC, value is
    migrating down -> BEARISH bias for the next day.
  * If it closed *above*, value is migrating up -> BULLISH bias.

Then it trades back to the prior day's value using fixed "key levels":

  * the previous CASH session's Value Area High / Low / POC, and
  * the OVERNIGHT session's Value Area (18:00 prev -> 09:30 ET) High / Low.

The pitch (paraphrased): on a bearish day, wait for an early rally up into the
*untapped* previous-day Value Area High, expect rejection, and short back toward
the POC. On a bullish day, mirror it off the previous Value Area Low. One A+
setup per day.

This module ports that to EURUSD (our deepest M1 tape). FX has no real exchange
volume, so the profile is a TPO / time-at-price profile (the original market-
profile construction): each minute bar contributes one tick to every price bin
it spans. POC = most-visited price; Value Area = the 70% of TPO around the POC.

Anti-overfit spine matches the house gauntlet (forex_bloc_momentum.py /
calendar_flow_gauntlet.py):
  - net-of-cost in return space, charged on entry and exit
  - sealed hold-out: last SEALED_YEARS withheld, scored exactly once
  - Deflated Sharpe on the in-sample tape (Lopez de Prado), trials counted honestly
  - a positive control: randomised bias must collapse expectancy to ~0

Two fixed, economically-motivated configs are tested (no grid search):
  "edge-rejection" (the video's literal setup) and "value-breakout" (the inverse
  read, in case migration is continuation not mean-reversion). n_trials is set to
  the number of configs * directions actually evaluated, stated honestly.

Run:  python3 scripts/volume_profile_bias.py
      python3 scripts/volume_profile_bias.py --json
      python3 scripts/volume_profile_bias.py --rebuild   # ignore the level cache
Deps: pandas, numpy, scipy (already in the research venv).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# allow `python3 scripts/volume_profile_bias.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.research.walkforward import deflated_sharpe_ratio

M1_CSV = Path("data/candles/EURUSD_M1.csv")
CACHE = Path("data/candles/_vp_cache")
SYMBOL = "EURUSD"
PIP = 1e-4
COST_PIPS = 0.5  # round-turn ECN-ish; charged per side (entry + exit)
SEALED_YEARS = 2  # last N calendar years withheld, scored once
TRADING_DAYS = 252
VALUE_AREA_PCT = 0.70  # standard market-profile value area
BIN_PIPS = 1.0  # profile resolution: 1 pip per bin
TZ = "America/New_York"  # the video's "cash" / "overnight" sessions are ET


# --------------------------------------------------------------------------- #
# Profile construction
# --------------------------------------------------------------------------- #
def _session_profile(lo: np.ndarray, hi: np.ndarray) -> tuple[float, float, float] | None:
    """TPO profile from minute (low, high) arrays -> (poc, val, vah) in price.

    Each bar adds one tick to every 1-pip bin it spans. POC = busiest bin;
    Value Area = smallest contiguous band around the POC holding >=70% of TPO,
    grown one bin at a time toward whichever side is busier (standard rule).
    """
    if lo.size == 0:
        return None
    lo_b = np.floor(lo / (BIN_PIPS * PIP)).astype(np.int64)
    hi_b = np.floor(hi / (BIN_PIPS * PIP)).astype(np.int64)
    base = int(lo_b.min())
    width = int(hi_b.max()) - base + 1
    if width <= 0:
        return None
    counts = np.zeros(width, dtype=np.int64)
    # accumulate each bar across its bin span; np.add.at handles overlaps
    spans = (hi_b - lo_b) + 1
    idx = np.repeat(lo_b - base, spans) + (np.arange(spans.sum()) - np.repeat(np.cumsum(spans) - spans, spans))
    np.add.at(counts, idx, 1)

    total = counts.sum()
    poc_i = int(counts.argmax())
    target = VALUE_AREA_PCT * total
    acc = counts[poc_i]
    lo_i = hi_i = poc_i
    while acc < target and (lo_i > 0 or hi_i < width - 1):
        up = counts[hi_i + 1] if hi_i < width - 1 else -1
        dn = counts[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1
            acc += counts[hi_i]
        else:
            lo_i -= 1
            acc += counts[lo_i]

    def to_price(b: int) -> float:
        return (base + b + 0.5) * BIN_PIPS * PIP

    return to_price(poc_i), to_price(lo_i), to_price(hi_i)


def build_levels(rebuild: bool = False) -> pd.DataFrame:
    """One row per ET trading day with cash + overnight profile levels.

    Cash session   = 09:30..16:00 ET of the day.
    Overnight       = 18:00 (prev day) .. 09:30 ET, labelled by the trading day
                      it precedes (the 09:30 end).
    Cached to parquet keyed by the source mtime so reruns are instant.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{SYMBOL}_levels.parquet"
    if cache.exists() and not rebuild and cache.stat().st_mtime >= M1_CSV.stat().st_mtime:
        return pd.read_parquet(cache)

    df = pd.read_csv(M1_CSV, parse_dates=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(TZ)
    mins = df.index.hour * 60 + df.index.minute

    cash_mask = (mins >= 9 * 60 + 30) & (mins < 16 * 60)
    # overnight 18:00..23:59 belongs to the *next* calendar day's session;
    # 00:00..09:30 belongs to the same calendar day's session.
    on_eve = mins >= 18 * 60
    on_morn = mins < 9 * 60 + 30
    day = df.index.normalize()

    cash = df[cash_mask].copy()
    cash["day"] = day[cash_mask]
    cash["close"] = df["close"][cash_mask]

    # overnight session day-label: evening (>=18:00) bars belong to the NEXT
    # day's session; morning (<09:30) bars to the same day. Keep the labels
    # tz-aware (assign the Series, never .values — that would strip the tz and
    # silently de-align from the cash keys).
    on_day = pd.Series(day, index=df.index)
    on_day[on_eve] = (df.index[on_eve] + pd.Timedelta(days=1)).normalize()
    on = df[on_eve | on_morn].copy()
    on["day"] = on_day[on_eve | on_morn]

    rows = []
    cash_g = {d: g for d, g in cash.groupby("day")}
    on_g = {d: g for d, g in on.groupby("day")}
    for d in sorted(cash_g.keys()):
        cg = cash_g[d]
        cp = _session_profile(cg["low"].to_numpy(), cg["high"].to_numpy())
        if cp is None:
            continue
        og = on_g.get(d)
        op = _session_profile(og["low"].to_numpy(), og["high"].to_numpy()) if og is not None else None
        rows.append(
            {
                "day": d,
                "cash_poc": cp[0],
                "cash_val": cp[1],
                "cash_vah": cp[2],
                "cash_close": float(cg["close"].iloc[-1]),
                "on_poc": op[0] if op else np.nan,
                "on_val": op[1] if op else np.nan,
                "on_vah": op[2] if op else np.nan,
            }
        )
    lv = pd.DataFrame(rows).set_index("day").sort_index()
    lv.to_parquet(cache)
    return lv


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    name: str
    mode: str  # "rejection" (fade back to POC) | "breakout" (follow through)
    stop_frac: float  # stop = level +/- stop_frac * (VAH-VAL) band width


def _cash_bars(rebuild: bool = False) -> pd.DataFrame:
    """Cached minute bars inside the cash session, ET-indexed with a day key."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{SYMBOL}_cashbars.parquet"
    if cache.exists() and not rebuild and cache.stat().st_mtime >= M1_CSV.stat().st_mtime:
        return pd.read_parquet(cache)
    df = pd.read_csv(M1_CSV, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(TZ)
    mins = df.index.hour * 60 + df.index.minute
    cash = df[(mins >= 9 * 60 + 30) & (mins < 16 * 60)].copy()
    cash["day"] = cash.index.normalize()
    cash.to_parquet(cache)
    return cash


def run_config(lv: pd.DataFrame, bars: pd.DataFrame, cfg: Config, randomize: int | None = None) -> pd.DataFrame:  # noqa: C901 — research backtest; complexity is inherent
    """Simulate one trade/day. Returns a per-trade frame with net returns.

    Bias from yesterday's POC migration; entry on the first cash-session tag of
    the previous day's value-area edge; target = previous-day POC (rejection) or
    the opposite edge (breakout); hard stop beyond the edge; flat at the close.
    """
    idx = list(lv.index)
    pos = {d: i for i, d in enumerate(idx)}
    rng = np.random.default_rng(randomize) if randomize is not None else None
    by_day = {d: g for d, g in bars.groupby("day")}
    out = []

    for d in idx:
        i = pos[d]
        if i < 2:
            continue
        prev = lv.iloc[i - 1]
        prev2 = lv.iloc[i - 2]
        if not np.isfinite(prev[["cash_poc", "cash_val", "cash_vah"]]).all():
            continue
        band = prev["cash_vah"] - prev["cash_val"]
        if band <= 0:
            continue

        # ---- bias from POC migration (or randomised, for the control) ----
        if rng is not None:
            bias = 1 if rng.random() < 0.5 else -1
        else:
            if prev["cash_poc"] < prev2["cash_poc"]:
                bias = -1
            elif prev["cash_poc"] > prev2["cash_poc"]:
                bias = 1
            else:
                continue

        g = by_day.get(d)
        if g is None or g.empty:
            continue
        highs = g["high"].to_numpy()
        lows = g["low"].to_numpy()
        closes = g["close"].to_numpy()

        # ---- entry + bracket geometry depend on mode AND bias ----
        # entry_dir: -1 short, +1 long.  All stops are placed on the ADVERSE
        # side of entry; targets on the favourable side (asserted below).
        if cfg.mode == "rejection":
            # fade the *untapped* far value edge back to the POC
            if bias < 0:  # bearish: rally up into prior VAH, short
                entry_dir, level = -1, prev["cash_vah"]
                hit = np.flatnonzero(highs >= level)
                target = prev["cash_poc"]  # below entry
                stop = level + cfg.stop_frac * band  # above entry (adverse)
            else:  # bullish: dip into prior VAL, long
                entry_dir, level = 1, prev["cash_val"]
                hit = np.flatnonzero(lows <= level)
                target = prev["cash_poc"]  # above entry
                stop = level - cfg.stop_frac * band  # below entry (adverse)
        else:  # breakout: continuation through the *near* value edge
            if bias < 0:  # bearish: break DOWN through prior VAL
                entry_dir, level = -1, prev["cash_val"]
                hit = np.flatnonzero(lows <= level)
                target = level - band  # extension below
                stop = prev["cash_poc"]  # back inside, above (adverse)
            else:  # bullish: break UP through prior VAH
                entry_dir, level = 1, prev["cash_vah"]
                hit = np.flatnonzero(highs >= level)
                target = level + band  # extension above
                stop = prev["cash_poc"]  # back inside, below (adverse)

        if hit.size == 0:
            continue
        # sanity: target favourable, stop adverse relative to entry direction
        if entry_dir * (target - level) <= 0 or entry_dir * (stop - level) >= 0:
            continue
        k = int(hit[0])
        entry = level

        # ---- walk remaining bars to the first of {stop, target, close} ----
        # start AFTER the entry bar: no intrabar look-ahead on the tag bar.
        exit_px, reason = closes[-1], "close"
        for j in range(k + 1, len(g)):
            hi, lo = highs[j], lows[j]
            if entry_dir < 0:  # short
                if hi >= stop:
                    exit_px, reason = stop, "stop"
                    break
                if lo <= target:
                    exit_px, reason = target, "target"
                    break
            else:  # long
                if lo <= stop:
                    exit_px, reason = stop, "stop"
                    break
                if hi >= target:
                    exit_px, reason = target, "target"
                    break

        gross = entry_dir * (exit_px - entry)
        cost = 2 * COST_PIPS * PIP
        net = gross - cost
        out.append(
            {
                "day": d,
                "bias": bias,
                "entry": entry,
                "exit": exit_px,
                "reason": reason,
                "gross_pips": gross / PIP,
                "net_pips": net / PIP,
                "net_ret": net / entry,
            }
        )
    return pd.DataFrame(out)


def stats(trades: pd.DataFrame, n_trials: int) -> dict:
    if trades.empty:
        return {"n": 0}
    r = trades["net_ret"].to_numpy()
    daily = trades.groupby("day")["net_ret"].sum()
    mu, sd = daily.mean(), daily.std(ddof=1)
    sharpe = (mu / sd) * np.sqrt(TRADING_DAYS) if sd > 0 else 0.0
    dsr = deflated_sharpe_ratio(
        sharpe / np.sqrt(TRADING_DAYS),
        len(daily),
        n_trials,
        skew=float(pd.Series(daily).skew()),
        kurt=float(pd.Series(daily).kurt() + 3.0),
    )
    return {
        "n": len(trades),
        "win_rate": float((r > 0).mean()),
        "avg_net_pips": float(trades["net_pips"].mean()),
        "total_net_pips": float(trades["net_pips"].sum()),
        "expectancy_ret": float(r.mean()),
        "sharpe_annual": float(sharpe),
        "deflated_sharpe_prob": float(dsr),
        "target_rate": float((trades["reason"] == "target").mean()),
        "stop_rate": float((trades["reason"] == "stop").mean()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--rebuild", action="store_true", help="ignore caches")
    args = ap.parse_args()

    lv = build_levels(rebuild=args.rebuild)
    bars = _cash_bars(rebuild=args.rebuild)

    # sealed hold-out split
    last_year = lv.index.max().year
    seal_from = pd.Timestamp(year=last_year - SEALED_YEARS + 1, month=1, day=1, tz=lv.index.tz)
    is_lv = lv[lv.index < seal_from]
    oos_lv = lv[lv.index >= seal_from]

    configs = [
        Config("edge-rejection", "rejection", stop_frac=0.5),
        Config("value-breakout", "breakout", stop_frac=0.0),
    ]
    # honest trial count: configs evaluated in-sample (both bias directions live
    # inside each config, so they are not separate trials)
    n_trials = len(configs)

    report: dict[str, Any] = {
        "symbol": SYMBOL,
        "is_span": [str(is_lv.index.min().date()), str(is_lv.index.max().date())],
        "oos_span": [str(oos_lv.index.min().date()), str(oos_lv.index.max().date())],
        "cost_pips_round_turn": 2 * COST_PIPS,
        "n_trials": n_trials,
        "configs": {},
    }

    best = None
    for cfg in configs:
        t_is = run_config(is_lv, bars, cfg)
        s_is = stats(t_is, n_trials)
        report["configs"][cfg.name] = {"in_sample": s_is}
        if best is None or s_is.get("sharpe_annual", -9) > report["configs"][best]["in_sample"].get(
            "sharpe_annual", -9
        ):
            best = cfg.name

    # positive control on the best config: randomised bias must collapse
    ctrl_cfg = next(c for c in configs if c.name == best)
    t_ctrl = run_config(is_lv, bars, ctrl_cfg, randomize=7)
    report["control_random_bias"] = stats(t_ctrl, n_trials)

    # sealed hold-out: score the best config exactly once
    t_oos = run_config(oos_lv, bars, ctrl_cfg)
    report["configs"][best]["sealed_oos"] = stats(t_oos, n_trials)
    report["best_config"] = best

    if args.json:
        print(json.dumps(report, indent=2, default=float))
        return 0

    # ---- human summary ----
    print(f"\nVolume-Profile Bias — {SYMBOL}  (TPO profiles, ET sessions)")
    print(f"  in-sample : {report['is_span'][0]} .. {report['is_span'][1]}")
    print(f"  sealed OOS: {report['oos_span'][0]} .. {report['oos_span'][1]}")
    print(f"  cost/RT   : {report['cost_pips_round_turn']:.1f} pips   trials: {n_trials}\n")
    for name, blk in report["configs"].items():
        s = blk["in_sample"]
        if not s.get("n"):
            print(f"  [{name}] no trades")
            continue
        tag = "  <-- best" if name == best else ""
        print(f"  [{name}]{tag}")
        print(
            f"      IS : n={s['n']:>4}  win={s['win_rate']:.1%}  "
            f"avg={s['avg_net_pips']:+.2f}p  Sharpe={s['sharpe_annual']:+.2f}  "
            f"DSR={s['deflated_sharpe_prob']:.2f}  tgt={s['target_rate']:.0%}/stop={s['stop_rate']:.0%}"
        )
        if "sealed_oos" in blk:
            o = blk["sealed_oos"]
            print(
                f"      OOS: n={o['n']:>4}  win={o['win_rate']:.1%}  "
                f"avg={o['avg_net_pips']:+.2f}p  Sharpe={o['sharpe_annual']:+.2f}"
            )
    c = report["control_random_bias"]
    print(
        f"\n  control (random bias, best cfg): n={c['n']}  avg={c['avg_net_pips']:+.2f}p  "
        f"Sharpe={c['sharpe_annual']:+.2f}   (should be ~0)"
    )

    o = report["configs"][best].get("sealed_oos", {})
    ctrl_sharpe = c.get("sharpe_annual", 0.0)
    is_sharpe = report["configs"][best]["in_sample"].get("sharpe_annual", -9)
    # a config only earns "viable" if it (a) survives net of cost OOS AND
    # (b) beats its own random-bias control by a clear margin — otherwise the
    # apparent edge is bracket geometry, not the POC-migration signal.
    beats_control = is_sharpe > ctrl_sharpe + 1.0
    if o.get("sharpe_annual", -9) > 0.5 and o.get("avg_net_pips", -9) > 0 and beats_control:
        verdict = "VIABLE candidate"
    elif not beats_control:
        verdict = (
            "NEGATIVE — bias signal adds nothing over a random-direction "
            "control (apparent profit is bracket geometry, not the POC signal)"
        )
    else:
        verdict = "NEGATIVE (no edge net of cost)"
    print(
        f"\n  best-cfg IS Sharpe {is_sharpe:+.2f} vs random-bias control {ctrl_sharpe:+.2f} "
        f"-> signal {'beats' if beats_control else 'does NOT beat'} control"
    )
    print(f"\n  VERDICT: {verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
