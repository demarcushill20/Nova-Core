#!/usr/bin/env python3
"""Volume-profile RECLAIM confirmation — task 1111 (same video as task 1110).

Task 1110 ported the market-profile / POC-bias video and returned a clean
NEGATIVE: the literal edge-rejection setup loses, and the value-breakout
variant's apparent profit is matched by a *random-direction* control, so the
"edge" is bracket geometry, not the POC-migration signal.

This task (1111) is the NEXT segment of the same transcript. The trader adds two
mechanics the 1110 backtest did not model:

  1. BREAK-AND-RECLAIM confirmation (the headline of this segment):
       "we did break below this level but immediately we reclaimed this level ...
        aggressive buyers not getting a follow-through ... aggressive sellers
        stepping in that got a follow through."
     i.e. don't enter on the first blind tag of the value edge. Wait for price
     to POKE THROUGH the edge and then RECLAIM it (close back on the origin
     side) within a short window -> a failed breakout / liquidity sweep, then
     enter the fade back toward the POC.

  2. INSIDE-DAY MERGE filter:
       "when we have ... the next day within the value of the previous day I can
        merge the volume because we pretty much just consolidated and it's not
        giving me much information."
     i.e. if the prior day's value area sits entirely inside the day-before's
     value area (an inside / consolidation day), the profile is uninformative
     -> skip the signal that day.

The economic question 1111 actually adds over 1110: does requiring a *confirmed
reclaim* (a structural sweep-and-reclaim, not a naive touch) finally separate the
signal from the random-direction control? If a random bias with the SAME reclaim
geometry earns the same expectancy, the reclaim is still just bracket geometry.

Anti-overfit spine is inherited from scripts/volume_profile_bias.py:
  - net-of-cost in return space (entry + exit), sealed 2-year hold-out,
  - Deflated Sharpe with an honest trial count,
  - a randomised-bias positive control that MUST collapse the edge.

Run:  python3 scripts/volume_profile_reclaim.py
      python3 scripts/volume_profile_reclaim.py --json
Deps: pandas, numpy, scipy (research venv). Reuses the 1110 level/bar caches.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.research.walkforward import deflated_sharpe_ratio
from scripts.volume_profile_bias import (  # reuse the vetted 1110 machinery
    COST_PIPS,
    PIP,
    SEALED_YEARS,
    SYMBOL,
    TRADING_DAYS,
    _cash_bars,
    build_levels,
)


@dataclass
class Config:
    name: str
    # entry trigger: "touch" = first tag of the edge (1110 baseline);
    #                "reclaim" = poke through then reclaim within RECLAIM_WIN bars
    trigger: str
    stop_frac: float
    inside_day_merge: bool  # skip days whose prior profile is an inside day


RECLAIM_WIN = 15  # bars (minutes) allowed between the poke and the reclaim
RECLAIM_PIPS = 1.0  # how far beyond the edge the poke must reach (>= 1 bin)


def _is_inside_day(prev: pd.Series, prev2: pd.Series) -> bool:
    """True if prev day's value area sits entirely inside prev2's value area."""
    return bool(prev["cash_val"] >= prev2["cash_val"] and prev["cash_vah"] <= prev2["cash_vah"])


def run_config(  # noqa: C901 — research backtest; branching is inherent
    lv: pd.DataFrame, bars: pd.DataFrame, cfg: Config, randomize: int | None = None
) -> pd.DataFrame:
    """One trade/day. Fade the untapped far value edge back toward the POC.

    Bias = prior-day POC migration (or randomised for the control). Entry is
    either the first touch of the edge (baseline) or a confirmed break-and-
    reclaim of it (this task's addition). Stop beyond the edge, target = POC,
    flat at the cash close.
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
        if not np.isfinite(prev2[["cash_val", "cash_vah"]]).all():
            continue
        band = prev["cash_vah"] - prev["cash_val"]
        if band <= 0:
            continue

        if cfg.inside_day_merge and _is_inside_day(prev, prev2):
            continue  # consolidation day -> profile uninformative, skip

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

        # rejection geometry: bearish -> fade prior VAH down to POC; bullish ->
        # fade prior VAL up to POC.
        if bias < 0:
            entry_dir, level = -1, prev["cash_vah"]
            target = prev["cash_poc"]
            stop = level + cfg.stop_frac * band
        else:
            entry_dir, level = 1, prev["cash_val"]
            target = prev["cash_poc"]
            stop = level - cfg.stop_frac * band

        # sanity: target favourable, stop adverse
        if entry_dir * (target - level) <= 0 or entry_dir * (stop - level) >= 0:
            continue

        # ---- find the entry bar k per the trigger ----
        poke = RECLAIM_PIPS * PIP
        k = None
        if cfg.trigger == "touch":
            if entry_dir < 0:
                hit = np.flatnonzero(highs >= level)
            else:
                hit = np.flatnonzero(lows <= level)
            if hit.size:
                k = int(hit[0])
        else:  # reclaim: poke beyond the edge, then close back on the origin side
            if entry_dir < 0:  # short at VAH: spike ABOVE VAH+poke, then close < VAH
                pokes = np.flatnonzero(highs >= level + poke)
                for p in pokes:
                    end = min(p + 1 + RECLAIM_WIN, len(g))
                    recl = np.flatnonzero(closes[p + 1 : end] < level)
                    if recl.size:
                        k = int(p + 1 + recl[0])
                        break
            else:  # long at VAL: spike BELOW VAL-poke, then close > VAL
                pokes = np.flatnonzero(lows <= level - poke)
                for p in pokes:
                    end = min(p + 1 + RECLAIM_WIN, len(g))
                    recl = np.flatnonzero(closes[p + 1 : end] > level)
                    if recl.size:
                        k = int(p + 1 + recl[0])
                        break

        if k is None or k >= len(g) - 1:
            continue
        # enter at the confirmed level (reclaim => fill at the reclaimed edge)
        entry = level

        # ---- walk to first of {stop, target, close}; start AFTER entry bar ----
        exit_px, reason = closes[-1], "close"
        for j in range(k + 1, len(g)):
            hi, lo = highs[j], lows[j]
            if entry_dir < 0:
                if hi >= stop:
                    exit_px, reason = stop, "stop"
                    break
                if lo <= target:
                    exit_px, reason = target, "target"
                    break
            else:
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
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    lv = build_levels(rebuild=args.rebuild)
    bars = _cash_bars(rebuild=args.rebuild)

    last_year = lv.index.max().year
    seal_from = pd.Timestamp(year=last_year - SEALED_YEARS + 1, month=1, day=1, tz=lv.index.tz)
    is_lv = lv[lv.index < seal_from]
    oos_lv = lv[lv.index >= seal_from]

    configs = [
        Config("touch-baseline", "touch", stop_frac=0.5, inside_day_merge=False),
        Config("reclaim", "reclaim", stop_frac=0.5, inside_day_merge=False),
        Config("reclaim+inside-merge", "reclaim", stop_frac=0.5, inside_day_merge=True),
    ]
    n_trials = len(configs)

    report: dict[str, Any] = {
        "symbol": SYMBOL,
        "task": "1111-reclaim-confirmation",
        "is_span": [str(is_lv.index.min().date()), str(is_lv.index.max().date())],
        "oos_span": [str(oos_lv.index.min().date()), str(oos_lv.index.max().date())],
        "cost_pips_round_turn": 2 * COST_PIPS,
        "reclaim_window_bars": RECLAIM_WIN,
        "n_trials": n_trials,
        "configs": {},
    }

    best = None
    for cfg in configs:
        s_is = stats(run_config(is_lv, bars, cfg), n_trials)
        report["configs"][cfg.name] = {"in_sample": s_is}
        if best is None or s_is.get("sharpe_annual", -9) > report["configs"][best]["in_sample"].get(
            "sharpe_annual", -9
        ):
            best = cfg.name

    ctrl_cfg = next(c for c in configs if c.name == best)
    report["control_random_bias"] = stats(run_config(is_lv, bars, ctrl_cfg, randomize=7), n_trials)
    report["configs"][best]["sealed_oos"] = stats(run_config(oos_lv, bars, ctrl_cfg), n_trials)
    report["best_config"] = best

    if args.json:
        print(json.dumps(report, indent=2, default=float))
        return 0

    print(f"\nVolume-Profile RECLAIM (task 1111) — {SYMBOL}  (TPO profiles, ET sessions)")
    print(f"  in-sample : {report['is_span'][0]} .. {report['is_span'][1]}")
    print(f"  sealed OOS: {report['oos_span'][0]} .. {report['oos_span'][1]}")
    print(
        f"  cost/RT   : {report['cost_pips_round_turn']:.1f}p   "
        f"reclaim window: {RECLAIM_WIN} bars   trials: {n_trials}\n"
    )
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
    beats_control = is_sharpe > ctrl_sharpe + 1.0
    if o.get("sharpe_annual", -9) > 0.5 and o.get("avg_net_pips", -9) > 0 and beats_control:
        verdict = "VIABLE candidate"
    elif not beats_control:
        verdict = (
            "NEGATIVE — reclaim confirmation adds nothing over a random-direction "
            "control (apparent profit is bracket geometry, not the sweep-reclaim signal)"
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
