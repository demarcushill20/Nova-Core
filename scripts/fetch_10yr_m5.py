#!/usr/bin/env python3
"""Fetch 10 years of EURUSD M5 candles from Dukascopy tick data.

Downloads tick data hour-by-hour (cached locally), aggregates to M5 candles
on-the-fly to keep memory bounded, then derives H1 for the higher timeframe.

Fully resumable — uses DukascopyFetcher's local cache so already-downloaded
hours are instant on re-run.

Usage:
    python3 scripts/fetch_10yr_m5.py                    # full 10yr fetch
    python3 scripts/fetch_10yr_m5.py --start 2020-01-01 # custom start
    python3 scripts/fetch_10yr_m5.py --dry-run           # show plan only
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure nova-core is on the import path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from novatrade.data.dukascopy_fetcher import DukascopyFetcher, ticks_to_candles  # noqa: E402
from novatrade.models import Candle  # noqa: E402

log = logging.getLogger("fetch_10yr_m5")

# Output paths
DATA_DIR = BASE / "data"
CANDLE_DIR = DATA_DIR / "candles"
M5_CSV = CANDLE_DIR / "EURUSD_M5_10yr.csv"
H1_CSV = CANDLE_DIR / "EURUSD_H1_10yr.csv"

# Defaults
DEFAULT_START = datetime(2016, 1, 4, tzinfo=timezone.utc)  # First Monday of 2016
DEFAULT_END = datetime(2026, 3, 28, tzinfo=timezone.utc)  # Recent Friday
SYMBOL = "EURUSD"


def is_forex_trading_hour(dt: datetime) -> bool:
    """Check if this hour falls within forex trading hours.

    Forex: Sunday 21:00 UTC to Friday 21:00 UTC.
    Skip Saturday entirely and Sunday before 21:00.
    """
    weekday = dt.weekday()  # 0=Mon ... 6=Sun
    if weekday == 5:  # Saturday — always closed
        return False
    if weekday == 6 and dt.hour < 21:  # Sunday before market open
        return False
    return not (weekday == 4 and dt.hour >= 22)  # Friday after market close


def aggregate_m5_to_h1(m5_candles: list[Candle]) -> list[Candle]:
    """Aggregate M5 candles into H1 candles."""
    if not m5_candles:
        return []

    h1_candles: list[Candle] = []
    h1_period = 3600  # 1 hour in seconds

    current_h1_start = int(m5_candles[0].timestamp // h1_period) * h1_period
    o = m5_candles[0].open
    h = m5_candles[0].high
    lo = m5_candles[0].low
    c = m5_candles[0].close
    vol = m5_candles[0].volume

    for candle in m5_candles[1:]:
        bar_h1 = int(candle.timestamp // h1_period) * h1_period
        if bar_h1 != current_h1_start:
            # Emit completed H1 bar
            h1_candles.append(
                Candle(
                    timestamp=float(current_h1_start),
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    volume=vol,
                    symbol=SYMBOL,
                    timeframe="H1",
                )
            )
            current_h1_start = bar_h1
            o = candle.open
            h = candle.high
            lo = candle.low
            c = candle.close
            vol = candle.volume
        else:
            h = max(h, candle.high)
            lo = min(lo, candle.low)
            c = candle.close
            vol += candle.volume

    # Emit final bar
    h1_candles.append(
        Candle(
            timestamp=float(current_h1_start),
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=vol,
            symbol=SYMBOL,
            timeframe="H1",
        )
    )
    return h1_candles


def save_candles_csv(candles: list[Candle], path: Path) -> None:
    """Save candles to CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            dt = datetime.fromtimestamp(c.timestamp, tz=timezone.utc)
            writer.writerow(
                [
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{c.open:.5f}",
                    f"{c.high:.5f}",
                    f"{c.low:.5f}",
                    f"{c.close:.5f}",
                    f"{c.volume:.2f}",
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch 10yr EURUSD M5 from Dukascopy")
    parser.add_argument("--start", type=str, default="2016-01-04", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="2026-03-28", help="End date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    total_days = (end - start).days

    # Count trading hours
    trading_hours = 0
    current = start
    while current < end:
        if is_forex_trading_hour(current):
            trading_hours += 1
        current += timedelta(hours=1)

    print(f"Plan: {SYMBOL} M5 candles from {args.start} to {args.end}")
    print(f"  Total calendar days: {total_days}")
    print(f"  Trading hours to fetch: {trading_hours:,}")
    print(f"  Expected M5 candles: ~{trading_hours * 12:,}")
    print(f"  Expected H1 candles: ~{trading_hours:,}")
    print(f"  Cache dir: {DATA_DIR / 'dukascopy' / SYMBOL}")
    print(f"  Output M5: {M5_CSV}")
    print(f"  Output H1: {H1_CSV}")

    if args.dry_run:
        print("\nDry run — exiting.")
        return 0

    print("\nStarting download (cached hours are instant)...\n")

    fetcher = DukascopyFetcher(cache_dir=DATA_DIR / "dukascopy")
    all_m5: list[Candle] = []

    # Process day by day
    day = start
    days_processed = 0
    hours_downloaded = 0
    hours_cached = 0
    ticks_total = 0
    t0 = time.monotonic()

    while day < end:
        # Process 24 hours of this day
        day_ticks = []
        for hour_offset in range(24):
            hour_dt = day + timedelta(hours=hour_offset)
            if hour_dt >= end:
                break
            if not is_forex_trading_hour(hour_dt):
                continue

            # Check if cached (for stats)
            cache_path = fetcher._cache_path(SYMBOL, hour_dt)
            was_cached = cache_path.exists()

            raw = fetcher._download_hour(SYMBOL, hour_dt)
            if raw:
                ticks = fetcher._parse_hour(SYMBOL, hour_dt, raw)
                day_ticks.extend(ticks)
                if was_cached:
                    hours_cached += 1
                else:
                    hours_downloaded += 1
                    # Small delay on fresh downloads to be polite
                    time.sleep(0.05)

        # Aggregate day's ticks to M5
        if day_ticks:
            m5_candles = ticks_to_candles(
                day_ticks,
                timeframe_seconds=300,
                symbol=SYMBOL,
                timeframe="M5",
            )
            all_m5.extend(m5_candles)
            ticks_total += len(day_ticks)

        days_processed += 1

        # Progress every 30 days
        if days_processed % 30 == 0:
            elapsed = time.monotonic() - t0
            pct = days_processed / total_days * 100
            rate = days_processed / elapsed if elapsed > 0 else 0
            eta = (total_days - days_processed) / rate if rate > 0 else 0
            print(
                f"  [{pct:5.1f}%] Day {days_processed}/{total_days} "
                f"| {len(all_m5):,} M5 bars | {ticks_total:,} ticks "
                f"| {hours_cached} cached + {hours_downloaded} new "
                f"| ETA {eta / 60:.0f}min"
            )

        day += timedelta(days=1)

    elapsed = time.monotonic() - t0
    print(f"\nDownload complete in {elapsed:.1f}s")
    print(f"  Days processed: {days_processed}")
    print(f"  Hours: {hours_cached} cached + {hours_downloaded} fresh downloads")
    print(f"  Total ticks: {ticks_total:,}")
    print(f"  M5 candles: {len(all_m5):,}")

    if not all_m5:
        print("ERROR: No candles produced!")
        return 1

    # Sort by timestamp (should already be sorted, but ensure)
    all_m5.sort(key=lambda c: c.timestamp)

    # Derive H1 from M5
    print("Deriving H1 from M5 candles...")
    all_h1 = aggregate_m5_to_h1(all_m5)
    print(f"  H1 candles: {len(all_h1):,}")

    # Date range summary
    first_dt = datetime.fromtimestamp(all_m5[0].timestamp, tz=timezone.utc)
    last_dt = datetime.fromtimestamp(all_m5[-1].timestamp, tz=timezone.utc)
    print(f"  Date range: {first_dt:%Y-%m-%d %H:%M} → {last_dt:%Y-%m-%d %H:%M}")

    # Save CSVs
    print(f"Saving M5 to {M5_CSV}...")
    save_candles_csv(all_m5, M5_CSV)
    print(f"Saving H1 to {H1_CSV}...")
    save_candles_csv(all_h1, H1_CSV)

    m5_size = M5_CSV.stat().st_size / (1024 * 1024)
    h1_size = H1_CSV.stat().st_size / (1024 * 1024)
    print("\nDone!")
    print(f"  {M5_CSV.name}: {len(all_m5):,} bars ({m5_size:.1f} MB)")
    print(f"  {H1_CSV.name}: {len(all_h1):,} bars ({h1_size:.1f} MB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
