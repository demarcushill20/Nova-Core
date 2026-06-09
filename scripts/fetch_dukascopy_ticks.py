#!/usr/bin/env python3
"""Fetch real bid/ask tick data from Dukascopy (free, auth-free) for the fidelity
backtester. Parallel, per-month chunked, resumable.

Dukascopy serves one LZMA-compressed ``.bi5`` per hour; each tick is 20 bytes,
big-endian ``(ms_since_hour, ask*1e5, bid*1e5, ask_vol, bid_vol)``. The month in
the URL path is 0-indexed (00=Jan). FX trades Sun 22:00 → Fri 22:00 UTC.

Output (resumable — skips months already written):
    data/ticks/<SYMBOL>/<YYYY-MM>.parquet   raw ticks (time, bid, ask)
    data/candles/<SYMBOL>_M1.csv            mid-price M1 OHLC (rebuilt each run)

    python3 scripts/fetch_dukascopy_ticks.py --symbol EURUSD \
        --start 2016-01 --end 2026-04 --workers 24
"""

from __future__ import annotations

import argparse
import lzma
import random
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://datafeed.dukascopy.com/datafeed"
POINT = 100_000.0  # EURUSD: 5 decimal places


def _market_open(dt: datetime) -> bool:
    wd = dt.weekday()  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 6 and dt.hour < 22:
        return False
    return not (wd == 4 and dt.hour >= 22)


def fetch_hour(symbol: str, dt: datetime, retries: int = 8) -> list[tuple[float, float, float]]:
    """Return [(epoch_seconds, bid, ask), ...] for one hour. Retries transient
    failures (503/timeouts) with exponential backoff + jitter so rate-limiting
    never drops data; 404 means the hour is genuinely absent."""
    url = f"{BASE}/{symbol}/{dt.year}/{dt.month - 1:02d}/{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"
    data = b""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (fixed host)
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            if attempt == retries - 1:
                return []
            time.sleep(min(10.0, 0.5 * (2**attempt)) + random.uniform(0, 0.4))  # noqa: S311
        except Exception:
            if attempt == retries - 1:
                return []
            time.sleep(min(10.0, 0.5 * (2**attempt)) + random.uniform(0, 0.4))  # noqa: S311
    if not data:
        return []
    try:
        raw = lzma.decompress(data)
    except Exception:
        return []
    hour0 = dt.replace(minute=0, second=0, microsecond=0).timestamp()
    out: list[tuple[float, float, float]] = []
    for i in range(0, len(raw) - 19, 20):
        ms, ask, bid, _av, _bv = struct.unpack(">iiiff", raw[i : i + 20])
        out.append((hour0 + ms / 1000.0, bid / POINT, ask / POINT))
    return out


def _month_hours(year: int, month: int) -> list[datetime]:
    dt = datetime(year, month, 1, tzinfo=timezone.utc)
    nxt = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    hours = []
    while dt < nxt:
        if _market_open(dt):
            hours.append(dt)
        dt += timedelta(hours=1)
    return hours


def fetch_month(symbol: str, year: int, month: int, workers: int) -> pd.DataFrame:
    hours = _month_hours(year, month)
    rows: list[tuple[float, float, float]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk in ex.map(lambda d: fetch_hour(symbol, d), hours):
            rows.extend(chunk)
    rows.sort()
    df = pd.DataFrame(rows, columns=["ts", "bid", "ask"])
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def _months(start: str, end: str):
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    y, m = sy, sm
    while (y, m) < (ey, em):
        yield y, m
        y, m = (y + (m == 12), (m % 12) + 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--start", required=True, help="UTC month, inclusive (YYYY-MM)")
    ap.add_argument("--end", required=True, help="UTC month, exclusive (YYYY-MM)")
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args()

    tick_dir = ROOT / "data" / "ticks" / args.symbol
    tick_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    total_ticks = 0
    months = list(_months(args.start, args.end))
    print(
        f"[fetch] {args.symbol} {args.start} → {args.end}  ({len(months)} months, {args.workers} workers)", flush=True
    )

    for y, m in months:
        out = tick_dir / f"{y}-{m:02d}.parquet"
        if out.exists():
            print(f"  {y}-{m:02d}  (exists, skip)", flush=True)
            continue
        ms0 = time.time()
        df = fetch_month(args.symbol, y, m, args.workers)
        if len(df):
            df[["time", "bid", "ask"]].to_parquet(out, index=False)
        total_ticks += len(df)
        sp = ((df["ask"] - df["bid"]) / 0.0001).median() if len(df) else float("nan")
        print(
            f"  {y}-{m:02d}  ticks={len(df):>9,}  med_spread={sp:.2f}p  "
            f"({time.time() - ms0:.0f}s, total {total_ticks:,}, elapsed {time.time() - t0:.0f}s)",
            flush=True,
        )

    print(f"\n[fetch] done: {total_ticks:,} new ticks in {time.time() - t0:.0f}s", flush=True)
    _rebuild_m1(args.symbol, tick_dir)


def _rebuild_m1(symbol: str, tick_dir: Path) -> None:
    """Concatenate all monthly tick parquets → mid-price M1 OHLC for the engine."""
    files = sorted(tick_dir.glob("*.parquet"))
    if not files:
        return
    print(f"[m1] aggregating {len(files)} monthly files → M1 ...", flush=True)
    parts = []
    for f in files:
        d = pd.read_parquet(f, columns=["time", "bid", "ask"])
        mid = (d["bid"] + d["ask"]) / 2.0
        bars = mid.set_axis(d["time"]).resample("1min", label="right", closed="right").ohlc().dropna()
        parts.append(bars)
    m1 = pd.concat(parts).reset_index().rename(columns={"time": "timestamp"})
    m1_csv = ROOT / "data" / "candles" / f"{symbol}_M1.csv"
    m1.to_csv(m1_csv, index=False)
    print(f"[m1] wrote {m1_csv}  ({len(m1):,} bars, {m1_csv.stat().st_size / 1e6:.0f} MB)", flush=True)


if __name__ == "__main__":
    main()
