#!/usr/bin/env python3
"""Outcome-parity report: live FTMO trades vs. backtest trades.

Matches live deals against backtest trades by (direction, entry-time ± tolerance)
and classifies each row:
  - MATCH           : live ↔ backtest trade found; outcome signs agree
  - OUTCOME_FLIP    : live ↔ backtest trade found; outcome signs DISAGREE
  - MISSING_IN_LIVE : backtest had a trade; live did not open one
  - EXTRA_IN_LIVE   : live opened a trade; backtest did not

Partial-exit legs in the backtest (two trades with the same entry_bar) are
collapsed into a single "first-exit" entry for alignment, then the overall
outcome = sum of all legs with that entry_bar.

Usage:
    python3 scripts/parity_report.py \
        --live OUTPUT/parity/live_trades_20260423_125000.csv \
        --backtest OUTPUT/parity/backtest_trades_20260423_125529.csv \
        --window-start 2026-04-21 --window-end 2026-04-24 \
        --tolerance-min 15
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f%z")
        except ValueError:
            return None


def _side_live_to_canon(s: str) -> str:
    s = (s or "").upper()
    return "SHORT" if s == "SELL" else "LONG" if s == "BUY" else s


def _side_bt_to_canon(s: str) -> str:
    s = (s or "").upper()
    if s in ("LONG", "SHORT"):
        return s
    if "LONG" in s:
        return "LONG"
    if "SHORT" in s:
        return "SHORT"
    return s


@dataclass
class LiveTrade:
    position_id: str
    side: str  # LONG / SHORT
    entry_time: datetime
    entry_price: float
    sl: float | None
    net_pnl: float
    outcome: str  # WIN/LOSS/FLAT/OPEN
    raw: dict = field(default_factory=dict)


@dataclass
class BTEntry:
    """One backtest entry, possibly spanning multiple legs (partial exit)."""

    entry_bar: int
    side: str
    entry_time: datetime
    entry_price: float
    stop_loss: float
    total_pnl_usd: float = 0.0
    total_pnl_pips: float = 0.0
    legs: int = 0
    outcome: str = "FLAT"
    raw: list = field(default_factory=list)


def _load_live(path: Path) -> list[LiveTrade]:
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            et = _parse_iso(row.get("entry_time", ""))
            if et is None:
                continue
            try:
                sl = float(row["sl"]) if row.get("sl") else None
            except ValueError:
                sl = None
            try:
                net = float(row.get("net_pnl") or 0)
            except ValueError:
                net = 0.0
            out.append(
                LiveTrade(
                    position_id=row.get("position_id", ""),
                    side=_side_live_to_canon(row.get("side", "")),
                    entry_time=et.astimezone(timezone.utc),
                    entry_price=float(row.get("entry_price") or 0),
                    sl=sl,
                    net_pnl=net,
                    outcome=row.get("outcome", "").upper() or ("WIN" if net > 0 else "LOSS" if net < 0 else "FLAT"),
                    raw=row,
                )
            )
    return sorted(out, key=lambda t: t.entry_time)


def _load_backtest(path: Path) -> list[BTEntry]:
    """Collapse partial-exit legs sharing entry_bar into one BTEntry."""
    by_bar: dict[int, BTEntry] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            bar = int(row["entry_bar"])
            et = _parse_iso(row.get("entry_iso", ""))
            if et is None:
                continue
            side = _side_bt_to_canon(row.get("side", ""))
            pnl_usd = float(row.get("pnl_usd") or 0)
            pnl_pips = float(row.get("pnl_pips") or 0)
            if bar not in by_bar:
                by_bar[bar] = BTEntry(
                    entry_bar=bar,
                    side=side,
                    entry_time=et.astimezone(timezone.utc),
                    entry_price=float(row.get("entry_price") or 0),
                    stop_loss=float(row.get("stop_loss") or 0),
                )
            e = by_bar[bar]
            e.total_pnl_usd += pnl_usd
            e.total_pnl_pips += pnl_pips
            e.legs += 1
            e.raw.append(row)
    for e in by_bar.values():
        e.outcome = "WIN" if e.total_pnl_pips > 0 else "LOSS" if e.total_pnl_pips < 0 else "FLAT"
    return sorted(by_bar.values(), key=lambda e: e.entry_time)


def _align(live: list[LiveTrade], bt: list[BTEntry], tol: timedelta, price_tol_pips: float = 5.0):
    """Greedy match, primary by direction+time, fallback by price+SL similarity.

    Live uses broker fill time; backtest uses signal-bar time. IRB pending stops
    can hold up to trigger_window_bars × primary_tf before filling (~60 min for
    v5 M5), so a fill at 00:02 may correspond to a backtest signal at 23:15. We
    widen the time tolerance and add a price/SL fingerprint as a stronger match
    criterion (same entry ±price_tol AND same SL ±price_tol regardless of time).
    """
    used_bt: set[int] = set()
    matches = []  # list of (live, bt, dt)
    live_only = []
    price_tol = price_tol_pips / 10000.0  # pips -> price units for EURUSD

    def _score(lt: LiveTrade, be: BTEntry) -> tuple[bool, float]:
        if be.side != lt.side:
            return False, float("inf")
        dt_sec = abs((be.entry_time - lt.entry_time).total_seconds())
        price_close = abs(be.entry_price - lt.entry_price) <= price_tol
        sl_close = lt.sl is not None and be.stop_loss and abs(be.stop_loss - lt.sl) <= price_tol
        time_ok = abs(be.entry_time - lt.entry_time) <= tol
        # Accept if time-close OR (price+SL fingerprint close within 90 minutes)
        if time_ok or (price_close and sl_close and dt_sec <= 5400):
            # Composite score: lower is better. Price/SL proximity weighted more than time.
            ppips = abs(be.entry_price - lt.entry_price) * 10000
            slpips = abs((be.stop_loss or 0) - (lt.sl or 0)) * 10000 if lt.sl else 0
            return True, ppips + slpips + dt_sec / 600
        return False, float("inf")

    for lt in live:
        best_i = -1
        best_score = float("inf")
        best_dt = None
        for i, be in enumerate(bt):
            if i in used_bt:
                continue
            ok, score = _score(lt, be)
            if ok and score < best_score:
                best_score = score
                best_i = i
                best_dt = abs(be.entry_time - lt.entry_time)
        if best_i >= 0:
            matches.append((lt, bt[best_i], best_dt))
            used_bt.add(best_i)
        else:
            live_only.append(lt)

    bt_only = [be for i, be in enumerate(bt) if i not in used_bt]
    return matches, live_only, bt_only


def _in_window(ts: datetime, start: datetime, end: datetime) -> bool:
    return start <= ts < end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", required=True)
    ap.add_argument("--backtest", required=True)
    ap.add_argument("--window-start", required=True, help="YYYY-MM-DD (UTC inclusive)")
    ap.add_argument("--window-end", required=True, help="YYYY-MM-DD (UTC exclusive)")
    ap.add_argument(
        "--tolerance-min", type=int, default=15, help="Max minutes between live and backtest entry to consider a match"
    )
    ap.add_argument("--out-dir", default="OUTPUT/parity")
    args = ap.parse_args()

    window_start = datetime.strptime(args.window_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_end = datetime.strptime(args.window_end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    tol = timedelta(minutes=args.tolerance_min)

    live_all = _load_live(Path(args.live))
    bt_all = _load_backtest(Path(args.backtest))
    live = [t for t in live_all if _in_window(t.entry_time, window_start, window_end)]
    bt = [t for t in bt_all if _in_window(t.entry_time, window_start, window_end)]

    print(f"[parity] live trades in window : {len(live)}")
    print(f"[parity] backtest entries in window : {len(bt)}  (legs collapsed by entry_bar)")
    print(f"[parity] tolerance: ±{args.tolerance_min} min, same direction required")

    matches, live_only, bt_only = _align(live, bt, tol)

    outcome_match = [m for m in matches if m[0].outcome == m[1].outcome]
    outcome_flip = [m for m in matches if m[0].outcome != m[1].outcome]

    # ----- Report -----
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    report_csv = out_dir / f"parity_report_{stamp}.csv"
    with report_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "status",
                "side",
                "live_entry_iso",
                "live_entry_price",
                "live_sl",
                "live_outcome",
                "live_net_pnl_usd",
                "bt_entry_iso",
                "bt_entry_price",
                "bt_stop_loss",
                "bt_outcome",
                "bt_pnl_usd",
                "bt_pnl_pips",
                "entry_time_diff_sec",
                "entry_price_diff_pips",
                "sl_diff_pips",
            ]
        )
        for lt, be, dt in matches:
            status = "MATCH" if lt.outcome == be.outcome else "OUTCOME_FLIP"
            ep_diff_pips = round((lt.entry_price - be.entry_price) * 10000, 2)
            sl_diff = None
            if lt.sl is not None and be.stop_loss:
                sl_diff = round((lt.sl - be.stop_loss) * 10000, 2)
            w.writerow(
                [
                    status,
                    lt.side,
                    lt.entry_time.isoformat(),
                    lt.entry_price,
                    lt.sl,
                    lt.outcome,
                    lt.net_pnl,
                    be.entry_time.isoformat(),
                    be.entry_price,
                    be.stop_loss,
                    be.outcome,
                    round(be.total_pnl_usd, 2),
                    round(be.total_pnl_pips, 2),
                    int(dt.total_seconds()),
                    ep_diff_pips,
                    sl_diff,
                ]
            )
        for lt in live_only:
            w.writerow(
                [
                    "EXTRA_IN_LIVE",
                    lt.side,
                    lt.entry_time.isoformat(),
                    lt.entry_price,
                    lt.sl,
                    lt.outcome,
                    lt.net_pnl,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
        for be in bt_only:
            w.writerow(
                [
                    "MISSING_IN_LIVE",
                    be.side,
                    "",
                    "",
                    "",
                    "",
                    "",
                    be.entry_time.isoformat(),
                    be.entry_price,
                    be.stop_loss,
                    be.outcome,
                    round(be.total_pnl_usd, 2),
                    round(be.total_pnl_pips, 2),
                    "",
                    "",
                    "",
                ]
            )
    print(f"[parity] wrote {report_csv}")

    summary = {
        "window_start": args.window_start,
        "window_end": args.window_end,
        "tolerance_minutes": args.tolerance_min,
        "live_count": len(live),
        "backtest_count": len(bt),
        "matched": len(matches),
        "outcome_match": len(outcome_match),
        "outcome_flip": len(outcome_flip),
        "extra_in_live": len(live_only),
        "missing_in_live": len(bt_only),
        "outcome_match_rate": round(len(outcome_match) / len(matches), 3) if matches else None,
        "report_csv": str(report_csv),
    }
    summary_path = out_dir / f"parity_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
