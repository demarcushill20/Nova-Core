"""Unified OHLCV data provider interface for NovaTrade.

Phase 1.2 of the Full-Python pipeline. Provides a provider-neutral abstraction
for fetching historical candle data, replacing TradingView as the default source.

Public API:
    - OHLCVProvider: Abstract base for candle data sources
    - MetaApiOHLCVProvider: Fetches from live broker via MT5Adapter
    - CSVOHLCVProvider: Loads from local CSV files via existing loader
    - CachedOHLCVProvider: SQLite disk cache wrapping any provider
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from novatrade.data.loader import load_candles
from novatrade.models import Candle

if TYPE_CHECKING:
    from novatrade.adapter.base import MT5Adapter


class OHLCVProvider(ABC):
    """Abstract interface for historical OHLCV candle data.

    All implementations return ``list[Candle]`` sorted chronologically
    (oldest first). Timestamps are unix epoch seconds.
    """

    @abstractmethod
    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        """Fetch the most recent ``count`` candles for ``symbol`` at ``timeframe``.

        Returns:
            List of Candle objects sorted oldest-first.
        """

    @abstractmethod
    async def available_symbols(self) -> list[str]:
        """Return list of symbols this provider can serve."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging."""


class MetaApiOHLCVProvider(OHLCVProvider):
    """Fetches candles from live broker via the MT5Adapter interface.

    Wraps ``adapter.get_candles()`` with pagination for large requests
    (MetaApi returns max ~1000 candles per call).
    """

    MAX_PER_REQUEST = 1000

    def __init__(self, adapter: MT5Adapter) -> None:
        self._adapter = adapter

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        if count <= self.MAX_PER_REQUEST:
            return await self._adapter.get_candles(symbol, timeframe, count)

        # Paginate: fetch in chunks, deduplicate by timestamp
        all_candles: list[Candle] = []
        remaining = count
        seen_ts: set[float] = set()

        while remaining > 0:
            batch_size = min(remaining, self.MAX_PER_REQUEST)
            batch = await self._adapter.get_candles(symbol, timeframe, batch_size)
            if not batch:
                break

            new_candles = [c for c in batch if c.timestamp not in seen_ts]
            if not new_candles:
                break  # no new data available

            for c in new_candles:
                seen_ts.add(c.timestamp)
            all_candles.extend(new_candles)
            remaining -= len(new_candles)

        # Sort chronologically and trim to requested count
        all_candles.sort(key=lambda c: c.timestamp)
        return all_candles[-count:] if len(all_candles) > count else all_candles

    async def available_symbols(self) -> list[str]:
        # MetaApi doesn't have a simple symbol list endpoint;
        # return the symbols we know we trade
        return ["EURUSD"]

    @property
    def name(self) -> str:
        return "MetaApi"


class CSVOHLCVProvider(OHLCVProvider):
    """Loads candles from local CSV files using the existing multi-format loader.

    File naming convention: ``{data_dir}/{symbol}_{timeframe}.csv``
    (e.g., ``data/EURUSD_H1.csv``).
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def _file_path(self, symbol: str, timeframe: str) -> Path:
        return self._data_dir / f"{symbol}_{timeframe}.csv"

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        path = self._file_path(symbol, timeframe)
        if not path.exists():
            return []

        candles = load_candles(str(path), symbol=symbol, timeframe=timeframe)
        # Return the last `count` candles (most recent)
        return candles[-count:] if len(candles) > count else candles

    async def available_symbols(self) -> list[str]:
        if not self._data_dir.exists():
            return []
        symbols: set[str] = set()
        for f in self._data_dir.glob("*_*.csv"):
            # Parse symbol from filename: EURUSD_H1.csv → EURUSD
            parts = f.stem.rsplit("_", 1)
            if len(parts) == 2:
                symbols.add(parts[0])
        return sorted(symbols)

    @property
    def name(self) -> str:
        return f"CSV({self._data_dir})"


class CachedOHLCVProvider(OHLCVProvider):
    """SQLite disk cache wrapping any OHLCVProvider.

    Caches fetched candles to avoid redundant broker API calls.
    Cache entries expire after ``ttl_seconds`` (default 1 hour).
    """

    def __init__(
        self,
        inner: OHLCVProvider,
        cache_path: str | Path = "data/ohlcv_cache.db",
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._inner = inner
        self._cache_path = Path(cache_path)
        self._ttl = ttl_seconds
        self._db: sqlite3.Connection | None = None

    def _ensure_db(self) -> sqlite3.Connection:
        if self._db is None:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self._cache_path))
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS candle_cache (
                    cache_key TEXT PRIMARY KEY,
                    candles_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )"""
            )
            self._db.commit()
        return self._db

    @staticmethod
    def _cache_key(symbol: str, timeframe: str, count: int) -> str:
        raw = f"{symbol}:{timeframe}:{count}"
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()

    def _candles_to_json(self, candles: list[Candle]) -> str:
        return json.dumps(
            [
                {
                    "timestamp": c.timestamp,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                    "symbol": c.symbol,
                    "timeframe": c.timeframe,
                }
                for c in candles
            ]
        )

    @staticmethod
    def _json_to_candles(data: str) -> list[Candle]:
        rows = json.loads(data)
        return [
            Candle(
                timestamp=r["timestamp"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r.get("volume", 0.0),
                symbol=r.get("symbol", ""),
                timeframe=r.get("timeframe", ""),
            )
            for r in rows
        ]

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        db = self._ensure_db()
        key = self._cache_key(symbol, timeframe, count)
        now = time.time()

        # Check cache
        row = db.execute(
            "SELECT candles_json, fetched_at FROM candle_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()

        if row is not None:
            candles_json, fetched_at = row
            if now - fetched_at < self._ttl:
                return self._json_to_candles(candles_json)

        # Cache miss or expired — fetch from inner provider
        candles = await self._inner.fetch_candles(symbol, timeframe, count)

        # Store in cache
        db.execute(
            "INSERT OR REPLACE INTO candle_cache (cache_key, candles_json, fetched_at) VALUES (?, ?, ?)",
            (key, self._candles_to_json(candles), now),
        )
        db.commit()
        return candles

    async def available_symbols(self) -> list[str]:
        return await self._inner.available_symbols()

    @property
    def name(self) -> str:
        return f"Cached({self._inner.name})"

    def invalidate(self, symbol: str | None = None) -> int:
        """Clear cache entries. Returns count of entries removed.

        If ``symbol`` is given, only clears entries for that symbol.
        Otherwise clears the entire cache.
        """
        db = self._ensure_db()
        if symbol is None:
            cursor = db.execute("DELETE FROM candle_cache")
        else:
            # Cache keys are hashes — we need to delete all and refetch
            # For targeted invalidation, we'd need to store symbol in the table
            # For now, clear all (simple and correct)
            cursor = db.execute("DELETE FROM candle_cache")
        db.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._db is not None:
            self._db.close()
            self._db = None
