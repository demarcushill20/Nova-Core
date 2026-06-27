"""Crypto OHLCV fetcher (Binance public REST, no API key).

Downloads spot klines from Binance's public market-data endpoint, caches them
on disk as CSV, and converts to NovaTrade ``Candle`` objects so they drop into
the existing backtest / Monte-Carlo / gauntlet stack unchanged.

This is the one true gap identified in the task-1093 analysis: every other
fetcher in this package targets FX/macro (MetaApi, Dukascopy, HistData,
TradingView). Crypto (e.g. SOL) had no source.

Endpoint (no key, public):
    https://api.binance.com/api/v3/klines?symbol=SOLUSDT&interval=1h&limit=1000

Each kline row is a JSON array:
    [ openTime(ms), open, high, low, close, volume, closeTime(ms), ... ]

Public API:
    - CryptoFetcher: fetch + cache + parse klines
    - fetch_ohlcv: convenience for (symbol, timeframe, since, limit) -> [Candle]

Design notes:
    - stdlib ``urllib`` only — mirrors ``dukascopy_fetcher`` and adds no
      dependency. Swap in ccxt/httpx later if multi-exchange support is needed.
    - On-disk cache lives at ``data/crypto/{SYMBOL}_{INTERVAL}.csv`` mirroring
      the Dukascopy/HistData cache layout.
    - Returns the canonical ``Candle`` dataclass so downstream code is untouched.
"""

from __future__ import annotations

import csv
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

from novatrade.models import Candle

log = logging.getLogger("novatrade.data.crypto_fetcher")

_BASE_URL = "https://api.binance.com/api/v3/klines"
_MAX_LIMIT = 1000  # Binance per-request kline cap

# How many milliseconds one bar of each interval spans — used for pagination.
_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "crypto"


def _normalise_symbol(symbol: str) -> str:
    """Accept ``SOL/USDT`` or ``SOLUSDT`` -> Binance form ``SOLUSDT``."""
    return symbol.replace("/", "").replace("-", "").upper()


class CryptoFetcher:
    """Fetch, cache, and parse Binance public spot klines into ``Candle``s."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.retries = retries

    # ------------------------------------------------------------------ HTTP
    def _get(self, params: dict) -> list:
        url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(  # noqa: S310 — url is a fixed https Binance endpoint
                    url, headers={"User-Agent": "novacore-crypto-fetcher/1.0"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — fixed https endpoint
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_err = exc
                log.warning("klines fetch attempt %d/%d failed: %s", attempt, self.retries, exc)
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"Binance klines fetch failed after {self.retries} tries: {last_err}")

    # --------------------------------------------------------------- fetching
    def fetch_ohlcv(
        self,
        symbol: str = "SOL/USDT",
        timeframe: str = "1h",
        since: int | None = None,
        limit: int | None = None,
        use_cache: bool = True,
    ) -> list[Candle]:
        """Return chronologically-sorted ``Candle``s.

        Args:
            symbol: ``SOL/USDT`` or ``SOLUSDT``.
            timeframe: Binance interval (``1m``..``1d``); see ``_INTERVAL_MS``.
            since: start time in **ms** epoch. None => Binance default window.
            limit: total bars wanted (paginated past the 1000/req cap). None =>
                a single ``_MAX_LIMIT`` page.
            use_cache: read/write the on-disk CSV cache.
        """
        sym = _normalise_symbol(symbol)
        if timeframe not in _INTERVAL_MS:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; known: {sorted(_INTERVAL_MS)}")

        cache_path = self.cache_dir / f"{sym}_{timeframe}.csv"
        if use_cache and limit and cache_path.exists():
            cached = self._read_cache(cache_path)
            if len(cached) >= limit:
                return cached[-limit:]

        candles = self._download(sym, timeframe, since=since, limit=limit or _MAX_LIMIT)

        if use_cache and candles:
            self._write_cache(cache_path, candles)
        return candles

    def _download(self, sym: str, timeframe: str, since: int | None, limit: int) -> list[Candle]:
        step_ms = _INTERVAL_MS[timeframe]
        out: list[Candle] = []
        start = since
        while len(out) < limit:
            page = min(_MAX_LIMIT, limit - len(out))
            params: dict = {"symbol": sym, "interval": timeframe, "limit": page}
            if start is not None:
                params["startTime"] = int(start)
            rows = self._get(params)
            if not rows:
                break
            for r in rows:
                out.append(
                    Candle(
                        timestamp=float(r[0]) / 1000.0,  # ms -> seconds, matches Candle
                        open=float(r[1]),
                        high=float(r[2]),
                        low=float(r[3]),
                        close=float(r[4]),
                        volume=float(r[5]),
                        symbol=sym,
                        timeframe=timeframe,
                    )
                )
            # Advance just past the last bar's open time to page forward.
            start = int(rows[-1][0]) + step_ms
            if len(rows) < page:
                break  # exchange returned fewer than asked => reached the head
        return out[:limit]

    # ------------------------------------------------------------------ cache
    @staticmethod
    def _read_cache(path: Path) -> list[Candle]:
        candles: list[Candle] = []
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                candles.append(
                    Candle(
                        timestamp=float(row["timestamp"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        symbol=row.get("symbol", ""),
                        timeframe=row.get("timeframe", ""),
                    )
                )
        candles.sort(key=lambda c: c.timestamp)
        return candles

    @staticmethod
    def _write_cache(path: Path, candles: list[Candle]) -> None:
        # Merge with any existing cache, dedupe by timestamp, keep sorted.
        existing = CryptoFetcher._read_cache(path) if path.exists() else []
        merged: dict[float, Candle] = {c.timestamp: c for c in existing}
        for c in candles:
            merged[c.timestamp] = c
        rows = sorted(merged.values(), key=lambda c: c.timestamp)
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "symbol", "timeframe"])
            for c in rows:
                writer.writerow([c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.symbol, c.timeframe])


def fetch_ohlcv(
    symbol: str = "SOL/USDT",
    timeframe: str = "1h",
    since: int | None = None,
    limit: int | None = None,
    cache_dir: Path | str | None = None,
    use_cache: bool = True,
) -> list[Candle]:
    """Module-level convenience wrapper around :class:`CryptoFetcher`."""
    return CryptoFetcher(cache_dir=cache_dir).fetch_ohlcv(
        symbol=symbol, timeframe=timeframe, since=since, limit=limit, use_cache=use_cache
    )
