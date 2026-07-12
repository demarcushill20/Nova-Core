"""NFP jump-momentum event study on EURUSD ticks (Iteration 3 of 30%-target loop).

PRE-REGISTERED (2026-07-12, before first run):
  Hypothesis : post-announcement drift — after the NFP release (first Friday, 08:30 ET),
               EURUSD continues in the direction of the initial jump (Evans-Lyons style
               order-flow persistence). Event-conditioned, NOT time-of-day seasonality
               (the calendar sweep that died was unconditional).
  Event      : first Friday of month, 08:30 America/New_York (handles DST correctly).
               Validity check: post-window 5-min tick range >= 2x pre-window range,
               else release was moved -> skip month (no calendar data needed).
  Jump       : mid(08:35 ET) - mid(08:29 ET). Trade only if |jump| >= 3x median |5-min
               move| of the same morning 07:30-08:25 (adaptive threshold, no fitted pips).
  Entry      : 08:35 ET in jump direction, long at ask / short at bid (spread paid).
  Exits      : +1h, +2h, +4h at bid (long) / ask (short). Report each horizon.
  Controls   : (a) placebo = identical procedure on 2nd/3rd/4th Fridays (no NFP),
               (b) split halves 2016-2020 vs 2021-2026.
  Bar        : net expectancy > 0 with t >= 2 at some horizon AND placebo ~0 AND both
               halves same sign. Also report the fade (mean-reversion) direction honestly.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

TICK_DIR = "data/ticks/EURUSD"
ET = "America/New_York"
PIP = 1e-4


def month_files() -> list[str]:
    return sorted(glob.glob(os.path.join(TICK_DIR, "*.parquet")))


def fridays_of_month(year: int, month: int) -> list[pd.Timestamp]:
    days = pd.date_range(f"{year}-{month:02d}-01", periods=31, freq="D")
    days = days[days.month == month]
    return [d for d in days if d.weekday() == 4]


def asof_mid(df: pd.DataFrame, ts_et: pd.Timestamp) -> tuple[float, float, float] | None:
    """(mid, bid, ask) at-or-before ts (tolerance 120s)."""
    ts = ts_et.tz_convert("UTC")
    i = df["time"].searchsorted(ts, side="right") - 1
    if i < 0:
        return None
    row = df.iloc[i]
    if (ts - row["time"]).total_seconds() > 120:
        return None
    return (row["bid"] + row["ask"]) / 2.0, row["bid"], row["ask"]


def range_pips(df: pd.DataFrame, t0: pd.Timestamp, t1: pd.Timestamp) -> float:
    lo = df["time"].searchsorted(t0.tz_convert("UTC"), side="left")
    hi = df["time"].searchsorted(t1.tz_convert("UTC"), side="left")
    if hi - lo < 5:
        return 0.0
    mid = (df["bid"].values[lo:hi] + df["ask"].values[lo:hi]) / 2.0
    return (mid.max() - mid.min()) / PIP


def study_day(df: pd.DataFrame, day: pd.Timestamp, horizons: list[int]) -> dict | None:
    """Run the event procedure for one Friday. Returns trade record or None."""
    d = day.tz_localize(ET) if day.tzinfo is None else day
    t_pre = d.replace(hour=8, minute=29)
    t_ent = d.replace(hour=8, minute=35)

    pre = asof_mid(df, t_pre)
    ent = asof_mid(df, t_ent)
    if pre is None or ent is None:
        return None

    # release-validity: post 5-min range must dwarf pre 5-min range
    r_pre = range_pips(df, d.replace(hour=8, minute=24), d.replace(hour=8, minute=29))
    r_post = range_pips(df, d.replace(hour=8, minute=30), d.replace(hour=8, minute=35))
    valid_release = r_post >= 2.0 * max(r_pre, 1.0)

    # adaptive jump threshold: 3x median |5-min move| over the same morning
    moves = []
    t = d.replace(hour=7, minute=30)
    while t <= d.replace(hour=8, minute=20):
        a, b = asof_mid(df, t), asof_mid(df, t + pd.Timedelta(minutes=5))
        if a and b:
            moves.append(abs(b[0] - a[0]) / PIP)
        t += pd.Timedelta(minutes=5)
    thresh = 3.0 * np.median(moves) if moves else np.inf

    jump = (ent[0] - pre[0]) / PIP
    if not np.isfinite(thresh) or abs(jump) < thresh:
        return {"traded": False, "valid_release": valid_release, "jump": jump, "thresh": thresh}

    side = 1 if jump > 0 else -1
    entry_px = ent[2] if side > 0 else ent[1]  # ask for long, bid for short
    f_side = -side
    f_entry_px = ent[2] if f_side > 0 else ent[1]
    rec = {
        "traded": True,
        "valid_release": valid_release,
        "jump": jump,
        "thresh": thresh,
        "side": side,
        "date": d.date(),
        "spread_at_entry": (ent[2] - ent[1]) / PIP,
    }
    for h in horizons:
        x = asof_mid(df, t_ent + pd.Timedelta(hours=h))
        if x is None:
            rec[f"pnl_{h}h"] = np.nan
            rec[f"fade_{h}h"] = np.nan
            continue
        exit_px = x[1] if side > 0 else x[2]  # bid for long, ask for short
        rec[f"pnl_{h}h"] = side * (exit_px - entry_px) / PIP
        f_exit_px = x[1] if f_side > 0 else x[2]
        rec[f"fade_{h}h"] = f_side * (f_exit_px - f_entry_px) / PIP
    return rec


def summarize(trades: list[dict], horizons: list[int], label: str, key: str = "pnl"):
    tr = [t for t in trades if t.get("traded")]
    print(f"\n{label}: {len(tr)} trades (of {len(trades)} event-days)")
    for h in horizons:
        p = np.array([t[f"{key}_{h}h"] for t in tr if np.isfinite(t.get(f"{key}_{h}h", np.nan))])
        if len(p) < 8:
            print(f"  +{h}h: insufficient ({len(p)})")
            continue
        t_stat = p.mean() / (p.std(ddof=1) / np.sqrt(len(p))) if p.std() else 0.0
        print(
            f"  +{h}h: mean {p.mean():+6.1f}p  med {np.median(p):+6.1f}p  win {(p > 0).mean() * 100:4.0f}%  "
            f"t={t_stat:+.2f}  n={len(p)}  worst {p.min():+.0f}p  best {p.max():+.0f}p"
        )
    if key == "fade" and tr:
        sp = np.array([t["spread_at_entry"] for t in tr if np.isfinite(t.get("spread_at_entry", np.nan))])
        if len(sp):
            p90 = np.percentile(sp, 90)
            print(f"  actual spread at entry: mean {sp.mean():.1f}p  med {np.median(sp):.1f}p  p90 {p90:.1f}p")


def main():
    horizons = [1, 2, 4]
    files = month_files()
    print(
        f"{'=' * 100}\nNFP JUMP-MOMENTUM — EURUSD ticks, {len(files)} months, pre-registered event study\n{'=' * 100}"
    )

    nfp_trades, placebo_trades = [], []
    skipped_invalid = 0
    for f in files:
        base = os.path.basename(f).replace(".parquet", "")
        year, month = int(base[:4]), int(base[5:7])
        df = pd.read_parquet(f)
        df = df.sort_values("time").reset_index(drop=True)
        fridays = fridays_of_month(year, month)
        if not fridays:
            continue
        for idx, fri in enumerate(fridays):
            rec = study_day(df, fri, horizons)
            if rec is None:
                continue
            if idx == 0:  # NFP Friday
                if rec["valid_release"]:
                    nfp_trades.append(rec)
                else:
                    skipped_invalid += 1
            else:  # placebo Fridays (regardless of "validity" — same mechanical rule)
                placebo_trades.append(rec)

    print(
        f"\nNFP first-Fridays with confirmed release spike: {len(nfp_trades)} (skipped {skipped_invalid} moved/quiet)"
    )
    summarize(nfp_trades, horizons, "NFP jump-momentum (net of spread)")

    # split halves
    half1 = [t for t in nfp_trades if t.get("traded") and str(t["date"]) < "2021-01-01"]
    half2 = [t for t in nfp_trades if t.get("traded") and str(t["date"]) >= "2021-01-01"]
    summarize(half1, horizons, "  half 1 (2016-2020)")
    summarize(half2, horizons, "  half 2 (2021-2026)")

    summarize(placebo_trades, horizons, "PLACEBO Fridays (no NFP)")

    # honest post-hoc: the FADE direction (inverts pre-registered hypothesis — treat as
    # hypothesis-GENERATING only; real bid/ask at news time already embedded)
    summarize(nfp_trades, horizons, "POST-HOC: NFP fade (real news-time spreads)", key="fade")
    summarize(half1, horizons, "  fade half 1 (2016-2020)", key="fade")
    summarize(half2, horizons, "  fade half 2 (2021-2026)", key="fade")
    summarize(placebo_trades, horizons, "  fade PLACEBO", key="fade")

    print(f"\n{'#' * 100}")
    print("BAR: net > 0 with t>=2 at some horizon, placebo ~0, both halves same sign.")
    print("#" * 100)


if __name__ == "__main__":
    main()
