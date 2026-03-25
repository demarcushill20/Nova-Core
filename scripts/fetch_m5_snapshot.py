#!/usr/bin/env python3
"""Fetch EURUSD M5 data from Dukascopy and create frozen M5+H1 snapshots.

Downloads tick data from Dukascopy's public data feed (hour-by-hour, LZMA
compressed), aggregates into M5 candles, derives H1 candles, and saves both
as a frozen Parquet snapshot pair with SHA-256 fingerprint.

Usage:
    python scripts/fetch_m5_snapshot.py                        # full 2-year fetch
    python scripts/fetch_m5_snapshot.py --quick                # 1-month quick test
    python scripts/fetch_m5_snapshot.py --start 2025-01-01 --end 2025-07-01
    python scripts/fetch_m5_snapshot.py --symbol GBPUSD --start 2024-06-01 --end 2025-06-01
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure novatrade is importable
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from novatrade.data.dukascopy_fetcher import DukascopyFetcher, ticks_to_candles  # noqa: E402
from novatrade.data.timeframe import derive_timeframe  # noqa: E402
from novatrade.storage.data_snapshot import save_snapshot  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fetch_m5_snapshot")

# ---------------------------------------------------------------------------
# Graceful interrupt
# ---------------------------------------------------------------------------

_interrupted = False


def _handle_sigint(sig: int, frame: object) -> None:
    global _interrupted
    if _interrupted:
        log.warning("Second interrupt — aborting immediately.")
        sys.exit(1)
    _interrupted = True
    log.warning("Keyboard interrupt received. Will finish current hour and save partial data.")


signal.signal(signal.SIGINT, _handle_sigint)

# ---------------------------------------------------------------------------
# Enhanced fetcher with day-level progress
# ---------------------------------------------------------------------------


class ProgressFetcher(DukascopyFetcher):
    """DukascopyFetcher subclass that prints day-by-day progress and respects interrupt."""

    def fetch_ticks_with_progress(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list:
        """Fetch ticks with detailed progress reporting and interrupt support."""
        from novatrade.data.tick import Tick

        symbol = symbol.upper()
        # Let the parent validate the symbol
        if symbol not in self.supported_symbols:
            raise ValueError(f"Unknown symbol '{symbol}'. Supported: {self.supported_symbols}")

        # Normalize to hour boundaries
        current = start.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        end_utc = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        start_utc = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start

        # Calculate total hours for progress
        total_hours = int((end_utc - current).total_seconds() / 3600)
        hours_fetched = 0
        hours_cached = 0
        hours_downloaded = 0

        all_ticks: list[Tick] = []
        start_ts = start_utc.timestamp()
        end_ts = end_utc.timestamp()

        current_day: str | None = None
        day_ticks = 0
        day_start_time = time.time()
        fetch_start_time = time.time()

        log.info(
            "Starting fetch: %s %s -> %s (%d hours to process)",
            symbol,
            start.date(),
            end.date(),
            total_hours,
        )

        while current < end_utc:
            if _interrupted:
                log.warning(
                    "Interrupted at %s — collected %d ticks from %d hours so far.",
                    current.isoformat(),
                    len(all_ticks),
                    hours_fetched,
                )
                break

            # Day-level progress reporting
            day_str = current.strftime("%Y-%m-%d")
            if day_str != current_day:
                if current_day is not None:
                    elapsed = time.time() - day_start_time
                    log.info(
                        "  Day %s: %d ticks (%.1fs)",
                        current_day,
                        day_ticks,
                        elapsed,
                    )
                current_day = day_str
                day_ticks = 0
                day_start_time = time.time()

                # Overall progress
                pct = (hours_fetched / total_hours * 100) if total_hours > 0 else 0
                elapsed_total = time.time() - fetch_start_time
                if hours_fetched > 0:
                    rate = hours_fetched / elapsed_total
                    eta_seconds = (total_hours - hours_fetched) / rate if rate > 0 else 0
                    eta_str = _format_duration(eta_seconds)
                else:
                    eta_str = "calculating..."

                log.info(
                    "Progress: %d/%d hours (%.1f%%) | %d ticks | cached: %d, downloaded: %d | ETA: %s",
                    hours_fetched,
                    total_hours,
                    pct,
                    len(all_ticks),
                    hours_cached,
                    hours_downloaded,
                    eta_str,
                )

            # Check if this hour is cached
            cache_path = self._cache_path(symbol, current)
            was_cached = cache_path.exists() and cache_path.stat().st_size > 0

            raw = self._download_hour(symbol, current)
            if raw:
                ticks = self._parse_hour(symbol, current, raw)
                for t in ticks:
                    if start_ts <= t.timestamp < end_ts:
                        all_ticks.append(t)
                        day_ticks += 1

            if was_cached:
                hours_cached += 1
            else:
                hours_downloaded += 1

            hours_fetched += 1
            current += timedelta(hours=1)

        # Final day log
        if current_day is not None and day_ticks > 0:
            elapsed = time.time() - day_start_time
            log.info("  Day %s: %d ticks (%.1fs)", current_day, day_ticks, elapsed)

        total_elapsed = time.time() - fetch_start_time
        log.info(
            "Fetch complete: %s %s->%s: %d hours (%d cached, %d downloaded), %d ticks in %s",
            symbol,
            start.date(),
            end.date(),
            hours_fetched,
            hours_cached,
            hours_downloaded,
            len(all_ticks),
            _format_duration(total_elapsed),
        )

        return all_ticks


def _format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m:02d}m"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch M5 data from Dukascopy and create frozen M5+H1 snapshots.",
    )
    parser.add_argument(
        "--symbol",
        default="EURUSD",
        help="Instrument symbol (default: EURUSD)",
    )
    parser.add_argument(
        "--start",
        default="2024-01-01",
        help="Start date YYYY-MM-DD (default: 2024-01-01)",
    )
    parser.add_argument(
        "--end",
        default="2026-01-01",
        help="End date YYYY-MM-DD (default: 2026-01-01)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick test mode: fetch only 1 month (2025-01-01 to 2025-02-01)",
    )
    parser.add_argument(
        "--cache-dir",
        default="data/dukascopy",
        help="Dukascopy tick cache directory (default: data/dukascopy)",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="data/snapshots",
        help="Output snapshot directory (default: data/snapshots)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds (default: 30)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Override dates for quick mode
    if args.quick:
        args.start = "2025-01-01"
        args.end = "2025-02-01"
        log.info("=== QUICK TEST MODE: 1 month only (2025-01-01 to 2025-02-01) ===")

    # Parse dates
    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        log.error("Invalid date format: %s (use YYYY-MM-DD)", e)
        return 1

    if end_dt <= start_dt:
        log.error("End date must be after start date.")
        return 1

    symbol = args.symbol.upper()

    # Resolve paths relative to project root
    cache_dir = _project_root / args.cache_dir
    snapshot_dir = _project_root / args.snapshot_dir

    log.info("=" * 60)
    log.info("NovaTrade M5 Snapshot Fetcher")
    log.info("=" * 60)
    log.info("Symbol:       %s", symbol)
    log.info("Date range:   %s to %s", start_dt.date(), end_dt.date())
    days = (end_dt - start_dt).days
    total_hours = days * 24
    log.info("Duration:     %d days (%d hours to fetch)", days, total_hours)
    log.info("Cache dir:    %s", cache_dir)
    log.info("Snapshot dir: %s", snapshot_dir)
    log.info("=" * 60)

    # -----------------------------------------------------------------------
    # Step 1: Fetch ticks
    # -----------------------------------------------------------------------
    log.info("")
    log.info("STEP 1/4: Fetching tick data from Dukascopy...")
    log.info("")

    fetcher = ProgressFetcher(cache_dir=cache_dir, timeout=args.timeout)
    ticks = fetcher.fetch_ticks_with_progress(symbol, start_dt, end_dt)

    if not ticks:
        log.error("No ticks fetched. Cannot proceed.")
        return 1

    if _interrupted and len(ticks) < 1000:
        log.error("Too few ticks after interrupt (%d). Aborting.", len(ticks))
        return 1

    log.info("Total ticks collected: %d", len(ticks))

    # -----------------------------------------------------------------------
    # Step 2: Aggregate ticks to M5 candles
    # -----------------------------------------------------------------------
    log.info("")
    log.info("STEP 2/4: Aggregating ticks into M5 candles (300s bars)...")

    m5_candles = ticks_to_candles(
        ticks,
        timeframe_seconds=300,
        symbol=symbol,
        timeframe="M5",
    )
    log.info("M5 candles: %d bars", len(m5_candles))

    if not m5_candles:
        log.error("No M5 candles produced. Aborting.")
        return 1

    # Report M5 date range
    m5_first = datetime.fromtimestamp(m5_candles[0].timestamp, tz=timezone.utc)
    m5_last = datetime.fromtimestamp(m5_candles[-1].timestamp, tz=timezone.utc)
    log.info("M5 range:   %s to %s", m5_first.isoformat(), m5_last.isoformat())

    # -----------------------------------------------------------------------
    # Step 3: Derive H1 candles from M5
    # -----------------------------------------------------------------------
    log.info("")
    log.info("STEP 3/4: Deriving H1 candles from M5 data...")

    h1_candles = derive_timeframe(m5_candles, "H1")
    log.info("H1 candles: %d bars", len(h1_candles))

    if not h1_candles:
        log.error("No H1 candles derived. Aborting.")
        return 1

    h1_first = datetime.fromtimestamp(h1_candles[0].timestamp, tz=timezone.utc)
    h1_last = datetime.fromtimestamp(h1_candles[-1].timestamp, tz=timezone.utc)
    log.info("H1 range:   %s to %s", h1_first.isoformat(), h1_last.isoformat())

    # Sanity check: M5 bars should be ~12x H1 bars
    ratio = len(m5_candles) / len(h1_candles) if len(h1_candles) > 0 else 0
    log.info("M5:H1 ratio: %.1f (expected ~12.0)", ratio)

    # -----------------------------------------------------------------------
    # Step 4: Save snapshot
    # -----------------------------------------------------------------------
    log.info("")
    log.info("STEP 4/4: Saving frozen snapshot...")

    # save_snapshot expects (primary_candles, higher_candles, symbol, dir)
    # The param names are h1_candles/h4_candles but they're really primary/higher
    snapshot = save_snapshot(
        h1_candles=m5_candles,  # primary = M5
        h4_candles=h1_candles,  # higher = H1
        symbol=symbol,
        snapshot_dir=snapshot_dir,
    )

    log.info("Snapshot saved!")
    log.info("  Snapshot ID:  %s", snapshot.snapshot_id)
    log.info("  Primary TF:   %s (%d bars)", snapshot.primary_tf, snapshot.primary_bars)
    log.info("  Higher TF:    %s (%d bars)", snapshot.higher_tf, snapshot.higher_bars)
    log.info("  Date range:   %s to %s", snapshot.date_range_start, snapshot.date_range_end)

    # File sizes
    primary_path = snapshot_dir / f"{snapshot.snapshot_id}_h1.parquet"
    higher_path = snapshot_dir / f"{snapshot.snapshot_id}_h4.parquet"
    meta_path = snapshot_dir / f"{snapshot.snapshot_id}_meta.json"

    def _file_size(p: Path) -> str:
        if p.exists():
            size = p.stat().st_size
            if size < 1024:
                return f"{size} B"
            elif size < 1024 * 1024:
                return f"{size / 1024:.1f} KB"
            else:
                return f"{size / (1024 * 1024):.1f} MB"
        return "N/A"

    log.info("")
    log.info("Files created:")
    log.info("  %s  (%s)", primary_path, _file_size(primary_path))
    log.info("  %s  (%s)", higher_path, _file_size(higher_path))
    log.info("  %s  (%s)", meta_path, _file_size(meta_path))

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info("Symbol:       %s", symbol)
    log.info("Date range:   %s to %s", snapshot.date_range_start, snapshot.date_range_end)
    log.info("M5 bars:      %d", len(m5_candles))
    log.info("H1 bars:      %d", len(h1_candles))
    log.info("M5:H1 ratio:  %.1f", ratio)
    log.info("Snapshot ID:  %s", snapshot.snapshot_id)
    if _interrupted:
        log.warning("NOTE: Fetch was interrupted — snapshot contains partial data.")
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
