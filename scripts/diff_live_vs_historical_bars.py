#!/usr/bin/env python3
"""Diff live-observed M5 bars (from journalctl) vs MetaApi historical fetch.

Parses `bar closed: EURUSD M5 O=... H=... L=... C=...` lines from the
daemon journal and compares against bars fetched retrospectively for
the same window. Any OHLC mismatch is flagged.

Usage:
    journalctl -u novacore-novatrade.service --since "YYYY-MM-DD HH:MM" \
        --until "YYYY-MM-DD HH:MM" --no-pager |
        python3 scripts/diff_live_vs_historical_bars.py \
            --start "2026-04-24 00:00" --end "2026-04-24 02:00"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.data.historical_fetcher import HistoricalFetcher


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_BAR_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*bar closed: EURUSD M5 "
    r"O=([\d.]+) H=([\d.]+) L=([\d.]+) C=([\d.]+)"
)


def _parse_live_bars(stdin_text: str) -> dict[str, tuple[float, float, float, float]]:
    """Return {bar_close_iso_minute: (O,H,L,C)} keyed to the 5-min boundary.

    Journal timestamps are the log emit time, which is ~15s after the bar
    close. We floor to the 5-min boundary to align with historical keys.
    """
    out: dict[str, tuple[float, float, float, float]] = {}
    for m in _BAR_RE.finditer(stdin_text):
        ts_s, o, h, lo, c = m.groups()
        ts = datetime.fromisoformat(ts_s).replace(tzinfo=timezone.utc)
        # Floor to 5-min boundary (bar close = start of current 5-min window)
        floored = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        out[floored.isoformat()] = (float(o), float(h), float(lo), float(c))
    return out


async def _fetch_hist(start: datetime, end: datetime) -> dict[str, tuple[float, float, float, float]]:
    fetcher = HistoricalFetcher(
        token=os.environ["METAAPI_TOKEN"],
        account_id=os.environ["METAAPI_ACCOUNT_ID"],
        region=os.environ.get("METAAPI_REGION", "new-york"),
    )
    await fetcher.connect()
    try:
        bars = await fetcher.fetch_range("EURUSD", "M5", start, end)
    finally:
        await fetcher.disconnect()
    return {
        datetime.fromtimestamp(b.timestamp, tz=timezone.utc).isoformat(): (b.open, b.high, b.low, b.close) for b in bars
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help='"YYYY-MM-DD HH:MM" UTC')
    ap.add_argument("--end", required=True, help='"YYYY-MM-DD HH:MM" UTC')
    ap.add_argument("--env-file", default="OUTPUT/novatrade.env.updated")
    ap.add_argument("--pip-threshold", type=float, default=0.5, help="Flag diffs larger than this (pips)")
    args = ap.parse_args()

    _load_env_file(Path(args.env_file))

    # Read journal lines from stdin
    stdin_text = sys.stdin.read()
    live = _parse_live_bars(stdin_text)
    if not live:
        print("[diff] ERROR: no live bar lines found on stdin")
        return 1
    print(f"[diff] parsed {len(live)} live bars from journal")

    start = datetime.strptime(args.start, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    hist = asyncio.run(_fetch_hist(start, end))
    print(f"[diff] fetched {len(hist)} historical bars")

    # Align + diff
    thresh = args.pip_threshold / 10000.0
    fmt = "{:19}  live O={:.5f} H={:.5f} L={:.5f} C={:.5f}  hist O={:.5f} H={:.5f} L={:.5f} C={:.5f}  Δ{}"
    exact = 0
    diffs = 0
    only_live = 0
    only_hist = 0
    for key in sorted(set(live) | set(hist)):
        if key in live and key in hist:
            lo, lh, lo_l, lc = live[key]
            ho, hh, hl, hc = hist[key]
            dO = abs(lo - ho)
            dH = abs(lh - hh)
            dL = abs(lo_l - hl)
            dC = abs(lc - hc)
            tag_parts = []
            if dO > thresh:
                tag_parts.append(f"O={dO * 10000:.1f}p")
            if dH > thresh:
                tag_parts.append(f"H={dH * 10000:.1f}p")
            if dL > thresh:
                tag_parts.append(f"L={dL * 10000:.1f}p")
            if dC > thresh:
                tag_parts.append(f"C={dC * 10000:.1f}p")
            if tag_parts:
                print(fmt.format(key, lo, lh, lo_l, lc, ho, hh, hl, hc, " ".join(tag_parts)))
                diffs += 1
            else:
                exact += 1
        elif key in live:
            only_live += 1
        else:
            only_hist += 1

    print(f"\n[diff] exact matches: {exact}")
    print(f"[diff] mismatches (>{args.pip_threshold} pips): {diffs}")
    print(f"[diff] only in live journal: {only_live}")
    print(f"[diff] only in historical: {only_hist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
