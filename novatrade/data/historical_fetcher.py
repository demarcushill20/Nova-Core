"""Historical candle data fetcher via MetaApi.

Paginates backwards through the MetaApi historical candle API to fetch
arbitrarily long date ranges. Produces NovaTrade Candle objects ready
for the backtest engine.

Usage:
    fetcher = HistoricalFetcher.from_env()
    await fetcher.connect()
    h1 = await fetcher.fetch_range("EURUSD", "H1", start, end)
    await fetcher.disconnect()
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metaapi_cloud_sdk import MetaApi

from novatrade.cli.commands.data import (
    CANDLE_DIR,
    SNAPSHOT_DIR,
    aggregate_h1_to_h4,
    save_candles_csv,
)
from novatrade.models import Candle
from novatrade.storage.data_snapshot import save_snapshot

log = logging.getLogger("novatrade.data.historical_fetcher")

# MetaApi timeframe mapping (NovaTrade -> MetaApi)
_TF_MAP = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
    "W1": "1w",
}

# Max candles per MetaApi request
_PAGE_LIMIT = 1000

# Seconds between API requests to avoid rate limiting
_REQUEST_DELAY = 0.5

# Broker symbol suffixes — MetaApi/FTMO often uses ".sim", ".pro", etc.
# We try the canonical name first, then these suffixes.
_SYMBOL_SUFFIXES = ["", ".sim", ".pro", "m", ".i", ".e"]


def _env_file_to_environ(env_path: str = "/etc/novacore/novatrade.env") -> None:
    """Load KEY=VALUE lines from env file into os.environ (skip comments/blanks)."""
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _to_candle(raw: dict, symbol: str, timeframe: str) -> Candle:
    """Convert MetaApi candle dict to NovaTrade Candle."""
    ts = 0.0
    if "time" in raw and isinstance(raw["time"], datetime):
        ts = raw["time"].timestamp()
    return Candle(
        timestamp=ts,
        open=raw.get("open", 0.0),
        high=raw.get("high", 0.0),
        low=raw.get("low", 0.0),
        close=raw.get("close", 0.0),
        volume=raw.get("tickVolume", raw.get("volume", 0.0)),
        symbol=symbol,
        timeframe=timeframe,
    )


class HistoricalFetcher:
    """Fetches historical candles from MetaApi with automatic pagination."""

    def __init__(self, token: str, account_id: str, region: str = "new-york") -> None:
        self._token = token
        self._account_id = account_id
        self._region = region
        self._api: MetaApi | None = None
        self._account: Any = None
        self._connection: Any = None

    @classmethod
    def from_env(cls, env_path: str = "/etc/novacore/novatrade.env") -> HistoricalFetcher:
        """Create fetcher from environment variables."""
        _env_file_to_environ(env_path)
        return cls(
            token=os.environ["METAAPI_TOKEN"],
            account_id=os.environ["METAAPI_ACCOUNT_ID"],
            region=os.environ.get("METAAPI_REGION", "new-york"),
        )

    async def connect(self) -> None:
        """Initialize MetaApi SDK and deploy account."""
        log.info("Connecting to MetaApi (account=%s)...", self._account_id[:8])
        self._api = MetaApi(self._token, {"region": self._region})
        self._account = await self._api.metatrader_account_api.get_account(
            self._account_id,
        )
        if self._account.state != "DEPLOYED":
            log.info("Deploying account (state=%s)", self._account.state)
            await self._account.deploy()
            await self._account.wait_deployed()
        await self._account.wait_connected()

        # Open RPC connection for symbol resolution
        self._connection = self._account.get_rpc_connection()
        await self._connection.connect()
        await self._connection.wait_synchronized()
        log.info("MetaApi connected and ready")

    async def disconnect(self) -> None:
        """Clean up MetaApi resources."""
        if self._api:
            try:
                await self._api.close()
            except Exception:  # noqa: S110
                pass
            self._api = None
            self._account = None
            self._connection = None

    async def resolve_symbol(self, symbol: str) -> str:
        """Find the broker's actual symbol name (e.g. EURUSD -> EURUSD.sim).

        Tries the canonical name first, then common broker suffixes.
        """
        if self._connection is None:
            raise RuntimeError("Not connected — call connect() first")

        available = await self._connection.get_symbols()
        available_set = set(available) if isinstance(available[0], str) else set()

        for suffix in _SYMBOL_SUFFIXES:
            candidate = symbol + suffix
            if candidate in available_set:
                if suffix:
                    log.info("Resolved symbol %s -> %s", symbol, candidate)
                return candidate

        raise ValueError(
            f"Symbol {symbol} not found on broker. "
            f"Tried: {[symbol + s for s in _SYMBOL_SUFFIXES]}. "
            f"Available EUR/USD: {[s for s in available_set if 'EUR' in s and 'USD' in s]}"
        )

    async def fetch_range(
        self,
        symbol: str,
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
        on_progress: Callable[..., Any] | None = None,
    ) -> list[Candle]:
        """Fetch candles for a date range by paginating backwards.

        MetaApi loads candles backwards from start_time (latest), so we
        start at end_date and page backwards until we pass start_date.

        Args:
            symbol: Instrument (e.g. "EURUSD").
            timeframe: NovaTrade timeframe (e.g. "H1", "H4").
            start_date: Earliest date to fetch (UTC).
            end_date: Latest date to fetch (UTC).
            on_progress: Optional callback(fetched_count, oldest_ts) for progress.

        Returns:
            Candles sorted chronologically (oldest first).
        """
        if self._account is None:
            raise RuntimeError("Not connected — call connect() first")

        # Resolve broker symbol (e.g. EURUSD -> EURUSD.sim)
        broker_symbol = await self.resolve_symbol(symbol)
        ma_tf = _TF_MAP.get(timeframe, timeframe)
        start_ts = start_date.timestamp()
        cursor = end_date  # pagination cursor (moves backwards)
        all_candles: list[Candle] = []
        page = 0
        seen_timestamps: set[float] = set()

        log.info(
            "Fetching %s %s from %s to %s",
            symbol,
            timeframe,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
        )

        while True:
            page += 1
            log.debug("Page %d: requesting %d candles before %s", page, _PAGE_LIMIT, cursor.isoformat())

            raw_candles = await self._account.get_historical_candles(
                broker_symbol,
                ma_tf,
                start_time=cursor,
                limit=_PAGE_LIMIT,
            )

            if not raw_candles:
                log.info("No more candles returned at page %d", page)
                break

            new_count = 0
            oldest_in_page = None
            for raw in raw_candles:
                candle = _to_candle(raw, symbol, timeframe)
                # Skip duplicates and candles before start_date
                if candle.timestamp in seen_timestamps:
                    continue
                if candle.timestamp < start_ts:
                    continue
                seen_timestamps.add(candle.timestamp)
                all_candles.append(candle)
                new_count += 1
                if oldest_in_page is None or candle.timestamp < oldest_in_page:
                    oldest_in_page = candle.timestamp

            if on_progress:
                on_progress(len(all_candles), oldest_in_page)

            log.info(
                "Page %d: +%d new candles (total: %d, oldest: %s)",
                page,
                new_count,
                len(all_candles),
                datetime.fromtimestamp(oldest_in_page, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                if oldest_in_page
                else "N/A",
            )

            # Check if we've reached the start date
            if oldest_in_page and oldest_in_page <= start_ts:
                log.info("Reached start date boundary")
                break

            # If we got fewer candles than requested, we've hit the data boundary
            if len(raw_candles) < _PAGE_LIMIT:
                log.info("Data boundary reached (got %d < %d)", len(raw_candles), _PAGE_LIMIT)
                break

            # Move cursor backwards to oldest candle in this page
            if oldest_in_page:
                cursor = datetime.fromtimestamp(oldest_in_page, tz=timezone.utc)
            else:
                break

            # Rate limit
            await asyncio.sleep(_REQUEST_DELAY)

        # Sort chronologically
        all_candles.sort(key=lambda c: c.timestamp)
        log.info(
            "Fetch complete: %d %s candles from %s to %s",
            len(all_candles),
            timeframe,
            datetime.fromtimestamp(all_candles[0].timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            if all_candles
            else "N/A",
            datetime.fromtimestamp(all_candles[-1].timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            if all_candles
            else "N/A",
        )
        return all_candles


async def fetch_and_save(
    symbol: str = "EURUSD",
    days: int = 730,
    output_dir: Path | None = None,
    snapshot: bool = True,
    verbose: bool = True,
) -> dict:
    """High-level: fetch real data, save CSVs, optionally create snapshot.

    Args:
        symbol: Instrument symbol.
        days: Number of days of history to fetch.
        output_dir: Directory for CSV files (default: data/candles/).
        snapshot: Whether to create a frozen Parquet snapshot.
        verbose: Print progress to stdout.

    Returns:
        Dict with h1_count, h4_count, h1_path, h4_path, snapshot_id (if created).
    """
    output_dir = output_dir or CANDLE_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    end_date = datetime.now(timezone.utc)
    start_date = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        tzinfo=timezone.utc,
    )
    # Go back N days
    from datetime import timedelta

    start_date = start_date - timedelta(days=days)

    def _progress(count, oldest_ts):
        if verbose and oldest_ts:
            dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
            print(f"  ... {count:,} candles fetched (back to {dt:%Y-%m-%d %H:%M})")

    fetcher = HistoricalFetcher.from_env()
    await fetcher.connect()

    try:
        # Fetch H1 candles
        if verbose:
            print(f"Fetching {symbol} H1 candles ({days} days)...")
        h1_candles = await fetcher.fetch_range(
            symbol,
            "H1",
            start_date,
            end_date,
            on_progress=_progress,
        )

        # Derive H4 from H1 for exact consistency
        if verbose:
            print(f"Deriving H4 candles from {len(h1_candles)} H1 bars...")
        h4_candles = aggregate_h1_to_h4(h1_candles)

        # Save CSVs
        h1_path = output_dir / f"{symbol}_H1.csv"
        h4_path = output_dir / f"{symbol}_H4.csv"
        save_candles_csv(h1_candles, h1_path)
        save_candles_csv(h4_candles, h4_path)

        if verbose:
            print(f"Saved: {h1_path} ({len(h1_candles):,} bars)")
            print(f"Saved: {h4_path} ({len(h4_candles):,} bars)")

        result = {
            "h1_count": len(h1_candles),
            "h4_count": len(h4_candles),
            "h1_path": str(h1_path),
            "h4_path": str(h4_path),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

        # Create reproducible snapshot
        if snapshot:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            snap = save_snapshot(h1_candles, h4_candles, symbol, SNAPSHOT_DIR)
            result["snapshot_id"] = snap.snapshot_id
            if verbose:
                print(f"Snapshot: {snap.snapshot_id}")

        return result

    finally:
        await fetcher.disconnect()
