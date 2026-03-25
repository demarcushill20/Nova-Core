#!/usr/bin/env python3
"""Fetch 10 years (2016-2026) of EURUSD data from Dukascopy in yearly chunks.

Downloads tick data year-by-year to avoid RAM exhaustion, aggregates each
year into M5 candles (discarding ticks), then merges all years into a
single unified snapshot with M5 + H1 + H4 candle sets.

The Dukascopy tick cache is persistent — re-running skips already-downloaded
hours, so the script is fully resumable.

Usage:
    # Full 10-year fetch (default: 2016-01-01 to 2026-01-01)
    python scripts/fetch_10y_dukascopy.py

    # Custom range
    python scripts/fetch_10y_dukascopy.py --start 2018 --end 2026

    # Different symbol
    python scripts/fetch_10y_dukascopy.py --symbol GBPUSD

    # Quick test: single year
    python scripts/fetch_10y_dukascopy.py --quick

    # Skip snapshot save (just produce CSV)
    python scripts/fetch_10y_dukascopy.py --csv-only

Output:
    data/snapshots/{id}_h1.parquet   (M5 candles as primary)
    data/snapshots/{id}_h4.parquet   (H1 candles as higher)
    data/candles/{SYMBOL}_M5_10y.csv (optional CSV export)
    data/candles/{SYMBOL}_H1_10y.csv (optional CSV export)
    data/candles/{SYMBOL}_H4_10y.csv (optional CSV export)
"""

from __future__ import annotations

import argparse
import csv
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure novatrade is importable
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from novatrade.data.dukascopy_fetcher import DukascopyFetcher, ticks_to_candles  # noqa: E402
from novatrade.data.timeframe import derive_timeframe  # noqa: E402
from novatrade.models import Candle  # noqa: E402
from novatrade.storage.data_snapshot import save_snapshot  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fetch_10y")

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
    log.warning("Interrupt received. Will finish current year and save partial data.")


signal.signal(signal.SIGINT, _handle_sigint)

# ---------------------------------------------------------------------------
# Progress-reporting fetcher (reused from fetch_m5_snapshot)
# ---------------------------------------------------------------------------


class ProgressFetcher(DukascopyFetcher):
    """DukascopyFetcher with day-level progress and interrupt support."""

    def fetch_ticks_with_progress(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list:
        from datetime import timedelta

        from novatrade.data.tick import Tick

        symbol = symbol.upper()
        if symbol not in self.supported_symbols:
            raise ValueError(f"Unknown symbol '{symbol}'. Supported: {self.supported_symbols}")

        current = start.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        end_utc = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        start_utc = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start

        total_hours = int((end_utc - current).total_seconds() / 3600)
        hours_fetched = 0
        hours_cached = 0
        hours_downloaded = 0

        all_ticks: list[Tick] = []
        start_ts = start_utc.timestamp()
        end_ts = end_utc.timestamp()

        current_day: str | None = None
        day_ticks = 0
        fetch_start = time.time()

        while current < end_utc:
            if _interrupted:
                log.warning(
                    "Interrupted at %s — %d ticks from %d hours.",
                    current.isoformat(),
                    len(all_ticks),
                    hours_fetched,
                )
                break

            day_str = current.strftime("%Y-%m-%d")
            if day_str != current_day:
                if current_day is not None and day_ticks > 0:
                    pass  # suppress per-day logging for 10y (too noisy)
                current_day = day_str
                day_ticks = 0

                # Log progress every 7 days
                if hours_fetched > 0 and hours_fetched % 168 == 0:
                    pct = hours_fetched / total_hours * 100
                    elapsed = time.time() - fetch_start
                    rate = hours_fetched / elapsed if elapsed > 0 else 1
                    eta = (total_hours - hours_fetched) / rate if rate > 0 else 0
                    log.info(
                        "  %d/%d hours (%.1f%%) | %d ticks | cached=%d dl=%d | ETA %s",
                        hours_fetched,
                        total_hours,
                        pct,
                        len(all_ticks),
                        hours_cached,
                        hours_downloaded,
                        _fmt_duration(eta),
                    )

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

        elapsed = time.time() - fetch_start
        log.info(
            "  Year done: %d hours (%d cached, %d downloaded), %d ticks in %s",
            hours_fetched,
            hours_cached,
            hours_downloaded,
            len(all_ticks),
            _fmt_duration(elapsed),
        )

        return all_ticks


def _fmt_duration(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return f"{h}h{m:02d}m"


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def save_candles_csv(candles: list[Candle], path: Path) -> None:
    """Write candles to CSV with standard OHLCV columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "datetime", "open", "high", "low", "close", "volume"])
        for c in candles:
            dt = datetime.fromtimestamp(c.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([c.timestamp, dt, c.open, c.high, c.low, c.close, c.volume])
    log.info("CSV saved: %s (%d bars, %.1f MB)", path, len(candles), path.stat().st_size / 1e6)


# ---------------------------------------------------------------------------
# Data quality summary
# ---------------------------------------------------------------------------


def quality_summary(candles: list[Candle], tf: str) -> dict:
    """Quick quality check on merged candles."""
    if not candles:
        return {"bars": 0, "status": "EMPTY"}

    from novatrade.data.timeframe import TIMEFRAME_MINUTES

    tf_seconds = TIMEFRAME_MINUTES[tf] * 60

    timestamps = [c.timestamp for c in candles]
    first_dt = datetime.fromtimestamp(timestamps[0], tz=timezone.utc)
    last_dt = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc)

    # Count gaps (missing bars, excluding weekends)
    gaps = 0
    large_gaps = 0
    for i in range(1, len(timestamps)):
        delta = timestamps[i] - timestamps[i - 1]
        if delta > tf_seconds * 1.5:  # allow small jitter
            # If gap is > 2 days, it's a weekend or holiday — expected
            if delta > 172800:  # > 48h = weekend/holiday, expected
                continue
            gaps += 1
            if delta > tf_seconds * 10:
                large_gaps += 1

    # Price range
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    return {
        "bars": len(candles),
        "first": first_dt.strftime("%Y-%m-%d %H:%M"),
        "last": last_dt.strftime("%Y-%m-%d %H:%M"),
        "years": (last_dt - first_dt).days / 365.25,
        "price_min": min(lows),
        "price_max": max(highs),
        "price_range_pips": round((max(highs) - min(lows)) * 10000, 0),
        "intra_week_gaps": gaps,
        "large_gaps": large_gaps,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch 10-year Dukascopy data in yearly chunks, produce unified snapshot.",
    )
    p.add_argument("--symbol", default="EURUSD", help="Symbol (default: EURUSD)")
    p.add_argument("--start", type=int, default=2016, help="Start year (default: 2016)")
    p.add_argument("--end", type=int, default=2026, help="End year exclusive (default: 2026)")
    p.add_argument("--quick", action="store_true", help="Quick test: fetch 2025 only")
    p.add_argument("--cache-dir", default="data/dukascopy", help="Tick cache dir")
    p.add_argument("--snapshot-dir", default="data/snapshots", help="Snapshot output dir")
    p.add_argument("--candle-dir", default="data/candles", help="CSV output dir")
    p.add_argument("--csv-only", action="store_true", help="Save CSV only (skip Parquet snapshot)")
    p.add_argument("--no-csv", action="store_true", help="Skip CSV export")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout (default: 30)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.quick:
        args.start = 2025
        args.end = 2026
        log.info("=== QUICK MODE: 2025 only ===")

    symbol = args.symbol.upper()
    start_year = args.start
    end_year = args.end
    years = list(range(start_year, end_year))

    cache_dir = _project_root / args.cache_dir
    snapshot_dir = _project_root / args.snapshot_dir
    candle_dir = _project_root / args.candle_dir

    log.info("=" * 70)
    log.info("NovaTrade 10-Year Dukascopy Fetch")
    log.info("=" * 70)
    log.info("Symbol:     %s", symbol)
    log.info("Range:      %d → %d (%d years)", start_year, end_year, len(years))
    log.info("Years:      %s", ", ".join(str(y) for y in years))
    log.info("Cache:      %s", cache_dir)
    log.info("Snapshots:  %s", snapshot_dir)
    log.info("=" * 70)

    fetcher = ProgressFetcher(cache_dir=cache_dir, timeout=args.timeout)
    pipeline_start = time.time()

    # -----------------------------------------------------------------------
    # Step 1: Fetch each year → M5 candles (ticks discarded after aggregation)
    # -----------------------------------------------------------------------
    all_m5: list[Candle] = []
    years_completed = 0

    for year in years:
        if _interrupted:
            log.warning("Interrupted before year %d — saving partial data.", year)
            break

        year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
        year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)

        log.info("")
        log.info("─" * 50)
        log.info("YEAR %d/%d: %d", years_completed + 1, len(years), year)
        log.info("─" * 50)

        # Fetch ticks for this year
        ticks = fetcher.fetch_ticks_with_progress(symbol, year_start, year_end)

        if not ticks:
            log.warning("  No ticks for %d — skipping (holiday year or data gap).", year)
            years_completed += 1
            continue

        # Aggregate to M5 — then discard ticks to free RAM
        m5 = ticks_to_candles(ticks, timeframe_seconds=300, symbol=symbol, timeframe="M5")
        tick_count = len(ticks)
        del ticks  # free memory

        log.info("  %d: %d ticks → %d M5 bars", year, tick_count, len(m5))

        all_m5.extend(m5)
        years_completed += 1

    if not all_m5:
        log.error("No M5 candles produced. Aborting.")
        return 1

    # Ensure sorted (should already be, but defensive)
    all_m5.sort(key=lambda c: c.timestamp)

    # Deduplicate (if years overlap at boundaries)
    seen_ts: set[float] = set()
    deduped: list[Candle] = []
    for c in all_m5:
        if c.timestamp not in seen_ts:
            seen_ts.add(c.timestamp)
            deduped.append(c)
    if len(deduped) < len(all_m5):
        log.info(
            "Deduplication: %d → %d M5 bars (%d duplicates removed)",
            len(all_m5),
            len(deduped),
            len(all_m5) - len(deduped),
        )
    all_m5 = deduped

    log.info("")
    log.info("=" * 50)
    log.info("MERGE COMPLETE: %d M5 bars across %d years", len(all_m5), years_completed)
    log.info("=" * 50)

    # -----------------------------------------------------------------------
    # Step 2: Derive H1 and H4 from merged M5
    # -----------------------------------------------------------------------
    log.info("")
    log.info("Deriving H1 from M5...")
    h1 = derive_timeframe(all_m5, "H1")
    log.info("H1 candles: %d bars", len(h1))

    log.info("Deriving H4 from H1...")
    h4 = derive_timeframe(h1, "H4")
    log.info("H4 candles: %d bars", len(h4))

    # Ratios
    m5_h1_ratio = len(all_m5) / len(h1) if h1 else 0
    h1_h4_ratio = len(h1) / len(h4) if h4 else 0
    log.info("Ratios: M5:H1=%.1f (expect ~12), H1:H4=%.1f (expect ~4)", m5_h1_ratio, h1_h4_ratio)

    # -----------------------------------------------------------------------
    # Step 3: Quality summary
    # -----------------------------------------------------------------------
    log.info("")
    log.info("DATA QUALITY")
    log.info("-" * 40)

    for label, candles, tf in [("M5", all_m5, "M5"), ("H1", h1, "H1"), ("H4", h4, "H4")]:
        q = quality_summary(candles, tf)
        log.info(
            "  %s: %d bars, %s → %s (%.1f years), price %.4f–%.4f (%.0f pips), gaps=%d",
            label,
            q["bars"],
            q["first"],
            q["last"],
            q["years"],
            q["price_min"],
            q["price_max"],
            q["price_range_pips"],
            q["intra_week_gaps"],
        )

    # -----------------------------------------------------------------------
    # Step 4: Save snapshot (M5 primary, H1 higher)
    # -----------------------------------------------------------------------
    if not args.csv_only:
        log.info("")
        log.info("Saving frozen snapshot (M5 + H1)...")

        snapshot = save_snapshot(
            h1_candles=all_m5,  # primary = M5
            h4_candles=h1,  # higher = H1
            symbol=symbol,
            snapshot_dir=snapshot_dir,
        )

        log.info("Snapshot ID:  %s", snapshot.snapshot_id)
        log.info("Primary TF:   %s (%d bars)", snapshot.primary_tf, snapshot.primary_bars)
        log.info("Higher TF:    %s (%d bars)", snapshot.higher_tf, snapshot.higher_bars)
        log.info("Date range:   %s → %s", snapshot.date_range_start, snapshot.date_range_end)

        # Also save an H1+H4 snapshot for H1-based strategies
        log.info("")
        log.info("Saving frozen snapshot (H1 + H4)...")

        snapshot_h1h4 = save_snapshot(
            h1_candles=h1,
            h4_candles=h4,
            symbol=symbol,
            snapshot_dir=snapshot_dir,
        )

        log.info("Snapshot ID:  %s", snapshot_h1h4.snapshot_id)
        log.info("Primary TF:   %s (%d bars)", snapshot_h1h4.primary_tf, snapshot_h1h4.primary_bars)
        log.info("Higher TF:    %s (%d bars)", snapshot_h1h4.higher_tf, snapshot_h1h4.higher_bars)

    # -----------------------------------------------------------------------
    # Step 5: CSV export
    # -----------------------------------------------------------------------
    if not args.no_csv:
        log.info("")
        log.info("Exporting CSV files...")

        tag = f"{start_year}-{end_year}"
        save_candles_csv(all_m5, candle_dir / f"{symbol}_M5_{tag}.csv")
        save_candles_csv(h1, candle_dir / f"{symbol}_H1_{tag}.csv")
        save_candles_csv(h4, candle_dir / f"{symbol}_H4_{tag}.csv")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    elapsed = time.time() - pipeline_start
    log.info("")
    log.info("=" * 70)
    log.info("FINAL SUMMARY")
    log.info("=" * 70)
    log.info("Symbol:       %s", symbol)
    log.info(
        "Range:        %d → %d (%d years%s)", start_year, end_year, years_completed, ", PARTIAL" if _interrupted else ""
    )
    log.info("M5 bars:      %d", len(all_m5))
    log.info("H1 bars:      %d", len(h1))
    log.info("H4 bars:      %d", len(h4))
    if not args.csv_only:
        log.info("Snapshot M5:  %s", snapshot.snapshot_id)
        log.info("Snapshot H1:  %s", snapshot_h1h4.snapshot_id)
    log.info("Total time:   %s", _fmt_duration(elapsed))
    log.info("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
