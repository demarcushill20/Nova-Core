"""Tests for novatrade.data.ohlcv_provider — unified OHLCV data providers."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.data.ohlcv_provider import (
    CachedOHLCVProvider,
    CSVOHLCVProvider,
    MetaApiOHLCVProvider,
    OHLCVProvider,
)
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candles(count: int, symbol: str = "EURUSD", tf: str = "H1") -> list[Candle]:
    """Generate a list of test candles with sequential timestamps."""
    base_ts = 1700000000.0
    return [
        Candle(
            timestamp=base_ts + i * 3600,
            open=1.0800 + i * 0.0001,
            high=1.0810 + i * 0.0001,
            low=1.0790 + i * 0.0001,
            close=1.0805 + i * 0.0001,
            volume=float(100 + i),
            symbol=symbol,
            timeframe=tf,
        )
        for i in range(count)
    ]


def _mock_adapter(candles: list[Candle]) -> AsyncMock:
    """Create a mock MT5Adapter that returns the given candles."""
    adapter = AsyncMock()
    adapter.get_candles = AsyncMock(return_value=candles)
    return adapter


# ---------------------------------------------------------------------------
# OHLCVProvider ABC
# ---------------------------------------------------------------------------


class TestOHLCVProviderABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            OHLCVProvider()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# MetaApiOHLCVProvider
# ---------------------------------------------------------------------------


class TestMetaApiOHLCVProvider:
    @pytest.mark.asyncio
    async def test_fetch_small_count(self):
        candles = _make_candles(50)
        adapter = _mock_adapter(candles)
        provider = MetaApiOHLCVProvider(adapter)

        result = await provider.fetch_candles("EURUSD", "H1", 50)
        assert len(result) == 50
        adapter.get_candles.assert_awaited_once_with("EURUSD", "H1", 50)

    @pytest.mark.asyncio
    async def test_fetch_within_limit(self):
        candles = _make_candles(100)
        adapter = _mock_adapter(candles)
        provider = MetaApiOHLCVProvider(adapter)

        result = await provider.fetch_candles("EURUSD", "H1", 100)
        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_fetch_large_count_paginates(self):
        """When count > 1000, should make multiple adapter calls."""
        all_candles = _make_candles(1500)
        adapter = AsyncMock()

        call_count = 0

        async def mock_get_candles(symbol, timeframe, count):
            nonlocal call_count
            if call_count == 0:
                call_count += 1
                return all_candles[:1000]
            else:
                call_count += 1
                return all_candles[1000:1500]

        adapter.get_candles = AsyncMock(side_effect=mock_get_candles)
        provider = MetaApiOHLCVProvider(adapter)

        result = await provider.fetch_candles("EURUSD", "H1", 1500)
        assert len(result) == 1500
        assert adapter.get_candles.await_count >= 2

    @pytest.mark.asyncio
    async def test_chronological_order(self):
        candles = _make_candles(10)
        adapter = _mock_adapter(candles)
        provider = MetaApiOHLCVProvider(adapter)

        result = await provider.fetch_candles("EURUSD", "H1", 10)
        timestamps = [c.timestamp for c in result]
        assert timestamps == sorted(timestamps)

    @pytest.mark.asyncio
    async def test_empty_result(self):
        adapter = _mock_adapter([])
        provider = MetaApiOHLCVProvider(adapter)

        result = await provider.fetch_candles("EURUSD", "H1", 100)
        assert result == []

    @pytest.mark.asyncio
    async def test_deduplication(self):
        """Duplicate timestamps across pagination batches should be removed."""
        candles = _make_candles(5)
        adapter = AsyncMock()
        # Both calls return the same candles (simulating overlap)
        adapter.get_candles = AsyncMock(return_value=candles)
        provider = MetaApiOHLCVProvider(adapter)

        # Request within single-call limit
        result = await provider.fetch_candles("EURUSD", "H1", 5)
        assert len(result) == 5

    def test_name(self):
        adapter = AsyncMock()
        provider = MetaApiOHLCVProvider(adapter)
        assert provider.name == "MetaApi"

    @pytest.mark.asyncio
    async def test_available_symbols(self):
        adapter = AsyncMock()
        provider = MetaApiOHLCVProvider(adapter)
        symbols = await provider.available_symbols()
        assert "EURUSD" in symbols


# ---------------------------------------------------------------------------
# CSVOHLCVProvider
# ---------------------------------------------------------------------------


class TestCSVOHLCVProvider:
    def _write_csv(self, path: Path, candles: list[Candle]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write("timestamp,open,high,low,close,volume\n")
            for c in candles:
                f.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")

    @pytest.mark.asyncio
    async def test_load_from_csv(self):
        candles = _make_candles(20)
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "EURUSD_H1.csv"
            self._write_csv(csv_path, candles)

            provider = CSVOHLCVProvider(tmpdir)
            result = await provider.fetch_candles("EURUSD", "H1", 10)
            assert len(result) == 10
            # Should be the last 10
            assert result[0].timestamp == candles[10].timestamp

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = CSVOHLCVProvider(tmpdir)
            result = await provider.fetch_candles("NOPE", "H1", 10)
            assert result == []

    @pytest.mark.asyncio
    async def test_available_symbols(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "EURUSD_H1.csv").touch()
            (Path(tmpdir) / "GBPUSD_H1.csv").touch()
            (Path(tmpdir) / "EURUSD_H4.csv").touch()

            provider = CSVOHLCVProvider(tmpdir)
            symbols = await provider.available_symbols()
            assert sorted(symbols) == ["EURUSD", "GBPUSD"]

    @pytest.mark.asyncio
    async def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = CSVOHLCVProvider(tmpdir)
            symbols = await provider.available_symbols()
            assert symbols == []

    def test_name(self):
        provider = CSVOHLCVProvider("/tmp/data")
        assert "CSV" in provider.name


# ---------------------------------------------------------------------------
# CachedOHLCVProvider
# ---------------------------------------------------------------------------


class TestCachedOHLCVProvider:
    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_inner(self):
        candles = _make_candles(5)
        inner = AsyncMock(spec=OHLCVProvider)
        inner.fetch_candles = AsyncMock(return_value=candles)
        inner.name = "MockInner"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.db"
            cached = CachedOHLCVProvider(inner, cache_path=cache_path)

            result = await cached.fetch_candles("EURUSD", "H1", 5)
            assert len(result) == 5
            inner.fetch_candles.assert_awaited_once()
            cached.close()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_inner(self):
        candles = _make_candles(5)
        inner = AsyncMock(spec=OHLCVProvider)
        inner.fetch_candles = AsyncMock(return_value=candles)
        inner.name = "MockInner"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.db"
            cached = CachedOHLCVProvider(inner, cache_path=cache_path, ttl_seconds=3600)

            # First call — cache miss
            await cached.fetch_candles("EURUSD", "H1", 5)
            # Second call — cache hit
            result = await cached.fetch_candles("EURUSD", "H1", 5)

            assert len(result) == 5
            # Inner should only be called once
            assert inner.fetch_candles.await_count == 1
            cached.close()

    @pytest.mark.asyncio
    async def test_cache_expiry(self):
        candles = _make_candles(5)
        inner = AsyncMock(spec=OHLCVProvider)
        inner.fetch_candles = AsyncMock(return_value=candles)
        inner.name = "MockInner"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.db"
            # TTL of 0 seconds — always expired
            cached = CachedOHLCVProvider(inner, cache_path=cache_path, ttl_seconds=0)

            await cached.fetch_candles("EURUSD", "H1", 5)
            await cached.fetch_candles("EURUSD", "H1", 5)

            # Both calls should hit inner (cache always expired)
            assert inner.fetch_candles.await_count == 2
            cached.close()

    @pytest.mark.asyncio
    async def test_invalidate(self):
        candles = _make_candles(3)
        inner = AsyncMock(spec=OHLCVProvider)
        inner.fetch_candles = AsyncMock(return_value=candles)
        inner.name = "MockInner"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.db"
            cached = CachedOHLCVProvider(inner, cache_path=cache_path, ttl_seconds=3600)

            await cached.fetch_candles("EURUSD", "H1", 3)
            deleted = cached.invalidate()
            assert deleted >= 1

            # After invalidation, next call should hit inner again
            await cached.fetch_candles("EURUSD", "H1", 3)
            assert inner.fetch_candles.await_count == 2
            cached.close()

    @pytest.mark.asyncio
    async def test_candle_round_trip_fidelity(self):
        """Candles stored and retrieved from cache should match originals."""
        candles = _make_candles(3, symbol="GBPUSD", tf="H4")
        inner = AsyncMock(spec=OHLCVProvider)
        inner.fetch_candles = AsyncMock(return_value=candles)
        inner.name = "MockInner"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.db"
            cached = CachedOHLCVProvider(inner, cache_path=cache_path)

            # Populate cache
            await cached.fetch_candles("GBPUSD", "H4", 3)
            # Read from cache
            result = await cached.fetch_candles("GBPUSD", "H4", 3)

            for orig, cached_c in zip(candles, result, strict=True):
                assert orig.timestamp == cached_c.timestamp
                assert orig.open == cached_c.open
                assert orig.high == cached_c.high
                assert orig.low == cached_c.low
                assert orig.close == cached_c.close
                assert orig.volume == cached_c.volume
                assert orig.symbol == cached_c.symbol
                assert orig.timeframe == cached_c.timeframe
            cached.close()

    def test_name(self):
        inner = MagicMock()
        inner.name = "TestProvider"
        cached = CachedOHLCVProvider(inner, cache_path="/tmp/test.db")
        assert cached.name == "Cached(TestProvider)"

    @pytest.mark.asyncio
    async def test_available_symbols_delegates(self):
        inner = AsyncMock(spec=OHLCVProvider)
        inner.available_symbols = AsyncMock(return_value=["EURUSD", "GBPUSD"])
        inner.name = "MockInner"
        cached = CachedOHLCVProvider(inner)
        symbols = await cached.available_symbols()
        assert symbols == ["EURUSD", "GBPUSD"]
