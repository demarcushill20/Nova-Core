"""Dukascopy free historical tick data fetcher.

Downloads LZMA-compressed binary tick data from Dukascopy's public data feed,
caches locally, and converts to NovaTrade Tick / Candle objects.

URL pattern:
    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM-1}/{DD}/{HH}h_ticks.bi5

Binary format per tick (20 bytes, big-endian):
    int32  ms_offset    — milliseconds since hour start
    int32  ask_raw      — ask price * point_scale
    int32  bid_raw      — bid price * point_scale
    float32 ask_vol     — ask volume
    float32 bid_vol     — bid volume

Dukascopy months are 0-indexed (January = 00).

Public API:
    - DukascopyFetcher: download, cache, and parse tick data
    - fetch_ticks: convenience for a date range
    - ticks_to_candles: aggregate ticks into OHLCV Candle objects
"""

from __future__ import annotations

import logging
import lzma
import struct
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novatrade.data.tick import Tick
from novatrade.models import Candle

log = logging.getLogger("novatrade.data.dukascopy_fetcher")

_BASE_URL = "https://datafeed.dukascopy.com/datafeed"

# Point scale: how many units per pip in the raw integer encoding.
# Most pairs use 5-digit pricing (100_000), JPY pairs use 3-digit (1_000),
# metals use different scales.
_POINT_SCALE: dict[str, int] = {
    "EURUSD": 100_000,
    "GBPUSD": 100_000,
    "USDCHF": 100_000,
    "AUDUSD": 100_000,
    "USDCAD": 100_000,
    "NZDUSD": 100_000,
    "EURGBP": 100_000,
    "EURJPY": 1_000,
    "GBPJPY": 1_000,
    "USDJPY": 1_000,
    "AUDJPY": 1_000,
    "XAUUSD": 1_000,
    "XAGUSD": 100_000,
}

# Tick struct: ms_offset(i4) + ask(i4) + bid(i4) + ask_vol(f4) + bid_vol(f4)
_TICK_STRUCT = struct.Struct(">iiiff")
_TICK_SIZE = _TICK_STRUCT.size  # 20 bytes


class DukascopyFetcher:
    """Download and cache Dukascopy tick data.

    Args:
        cache_dir: Local directory for caching downloaded .bi5 files.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        cache_dir: str | Path = "data/dukascopy",
        timeout: float = 30.0,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._timeout = timeout

    def _cache_path(self, symbol: str, dt: datetime) -> Path:
        return (
            self._cache_dir
            / symbol
            / f"{dt.year:04d}"
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / f"{dt.hour:02d}h_ticks.bi5"
        )

    def _url(self, symbol: str, dt: datetime) -> str:
        # Dukascopy months are 0-indexed
        month_0 = dt.month - 1
        return f"{_BASE_URL}/{symbol}/{dt.year:04d}/{month_0:02d}/{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"

    def _download_hour(self, symbol: str, dt: datetime) -> bytes | None:
        """Download a single hour .bi5 file. Returns raw LZMA bytes or None."""
        cache = self._cache_path(symbol, dt)
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_bytes()

        url = self._url(symbol, dt)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NovaTrade/1.0"})  # noqa: S310
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                data = resp.read()
            if not data:
                return None
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            return data
        except Exception as exc:
            log.debug("Dukascopy download failed %s: %s", url, exc)
            return None

    def _parse_hour(self, symbol: str, hour_start: datetime, raw: bytes) -> list[Tick]:
        """Decompress and parse a single hour of tick data."""
        scale = _POINT_SCALE.get(symbol, 100_000)
        try:
            decompressed = lzma.decompress(raw)
        except lzma.LZMAError:
            log.warning("LZMA decompression failed for %s %s", symbol, hour_start)
            return []

        n_ticks = len(decompressed) // _TICK_SIZE
        if n_ticks == 0:
            return []

        hour_ts = hour_start.timestamp()
        ticks: list[Tick] = []

        for i in range(n_ticks):
            offset = i * _TICK_SIZE
            ms_off, ask_raw, bid_raw, ask_vol, bid_vol = _TICK_STRUCT.unpack_from(decompressed, offset)
            ts = hour_ts + ms_off / 1000.0
            ask = ask_raw / scale
            bid = bid_raw / scale
            ticks.append(
                Tick(
                    symbol=symbol,
                    timestamp=ts,
                    bid=bid,
                    ask=ask,
                    volume=ask_vol + bid_vol,
                )
            )

        return ticks

    def fetch_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Tick]:
        """Fetch tick data for a symbol over a date range (UTC).

        Args:
            symbol: Instrument symbol (e.g. "EURUSD").
            start: Start datetime (UTC, inclusive).
            end: End datetime (UTC, exclusive).

        Returns:
            List of Tick objects sorted by timestamp.
        """
        symbol = symbol.upper()
        if symbol not in _POINT_SCALE:
            raise ValueError(f"Unknown symbol '{symbol}'. Supported: {sorted(_POINT_SCALE)}")

        # Normalize to hour boundaries
        current = start.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        end_utc = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end

        all_ticks: list[Tick] = []
        hours_fetched = 0

        while current < end_utc:
            raw = self._download_hour(symbol, current)
            if raw:
                ticks = self._parse_hour(symbol, current, raw)
                # Filter to exact range
                for t in ticks:
                    if start.timestamp() <= t.timestamp < end_utc.timestamp():
                        all_ticks.append(t)
            hours_fetched += 1
            if hours_fetched % 100 == 0:
                log.info(
                    "Dukascopy: %s %d hours fetched, %d ticks so far",
                    symbol,
                    hours_fetched,
                    len(all_ticks),
                )
            current += timedelta(hours=1)

        log.info(
            "Dukascopy: %s %s→%s: %d hours, %d ticks",
            symbol,
            start.date(),
            end.date(),
            hours_fetched,
            len(all_ticks),
        )
        return all_ticks

    @property
    def supported_symbols(self) -> list[str]:
        return sorted(_POINT_SCALE.keys())


def ticks_to_candles(
    ticks: list[Tick],
    timeframe_seconds: int = 3600,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
) -> list[Candle]:
    """Aggregate ticks into OHLCV candles.

    Args:
        ticks: Sorted list of Tick objects.
        timeframe_seconds: Bar period in seconds (3600 = H1).
        symbol: Symbol label for Candle objects.
        timeframe: Timeframe label for Candle objects.

    Returns:
        List of Candle objects sorted by timestamp.
    """
    if not ticks:
        return []

    candles: list[Candle] = []
    bar_start = (int(ticks[0].timestamp) // timeframe_seconds) * timeframe_seconds
    o = h = lo = c = ticks[0].mid
    vol = 0.0

    for tick in ticks:
        ts = tick.timestamp
        tick_bar = (int(ts) // timeframe_seconds) * timeframe_seconds

        if tick_bar != bar_start:
            # Emit completed bar
            candles.append(
                Candle(
                    timestamp=float(bar_start),
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    volume=vol,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            )
            bar_start = tick_bar
            o = h = lo = c = tick.mid
            vol = 0.0

        mid = tick.mid
        if mid > h:
            h = mid
        if mid < lo:
            lo = mid
        c = mid
        vol += tick.volume

    # Emit final bar
    candles.append(
        Candle(
            timestamp=float(bar_start),
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=vol,
            symbol=symbol,
            timeframe=timeframe,
        )
    )

    return candles
