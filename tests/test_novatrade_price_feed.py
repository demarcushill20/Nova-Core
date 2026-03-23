"""Tests for novatrade.data.price_feed — TickBatchPoller.

Phase 2.1 tests: async tick polling, deduplication, zero-price filtering,
error handling, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from novatrade.data.price_feed import TickBatchPoller
from novatrade.data.tick import Tick
from novatrade.models import SymbolPrice

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class MockAdapter:
    """Minimal mock of MT5Adapter for testing the poller."""

    def __init__(self, prices: list[SymbolPrice] | None = None) -> None:
        self._prices = list(prices or [])
        self._call_count = 0
        self._call_log: list[str] = []

    async def get_symbol_price(self, symbol: str) -> SymbolPrice:
        self._call_log.append(symbol)
        if self._call_count < len(self._prices):
            price = self._prices[self._call_count]
            self._call_count += 1
            return price
        # Return last price if we've run out
        if self._prices:
            return self._prices[-1]
        return SymbolPrice(symbol=symbol, bid=1.10000, ask=1.10020, timestamp=time.time())


class ErrorAdapter:
    """Adapter that raises on specific calls."""

    def __init__(self, error_on: set[int] | None = None) -> None:
        self._error_on = error_on or set()
        self._call_count = 0

    async def get_symbol_price(self, symbol: str) -> SymbolPrice:
        self._call_count += 1
        if self._call_count in self._error_on:
            raise ConnectionError(f"Simulated error on call {self._call_count}")
        return SymbolPrice(
            symbol=symbol,
            bid=1.10000 + self._call_count * 0.00010,
            ask=1.10020 + self._call_count * 0.00010,
            timestamp=1000.0 + self._call_count,
        )


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------


class TestTickBatchPollerInit:
    def test_valid_construction(self):
        adapter = MockAdapter()
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=1.0)
        assert poller.symbols == ["EURUSD"]
        assert poller.interval == 1.0
        assert not poller.running
        assert poller.ticks_yielded == 0

    def test_multi_symbol(self):
        adapter = MockAdapter()
        poller = TickBatchPoller(adapter, ["EURUSD", "GBPUSD"], interval=0.5)
        assert poller.symbols == ["EURUSD", "GBPUSD"]

    def test_invalid_interval_zero(self):
        with pytest.raises(ValueError, match="interval must be > 0"):
            TickBatchPoller(MockAdapter(), ["EURUSD"], interval=0)

    def test_invalid_interval_negative(self):
        with pytest.raises(ValueError, match="interval must be > 0"):
            TickBatchPoller(MockAdapter(), ["EURUSD"], interval=-1.0)

    def test_empty_symbols(self):
        with pytest.raises(ValueError, match="symbols list must not be empty"):
            TickBatchPoller(MockAdapter(), [], interval=0.5)


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestTickBatchPollerStream:
    @pytest.mark.asyncio
    async def test_yields_ticks(self):
        """Poller yields Tick objects from adapter prices."""
        prices = [
            SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, timestamp=1000.0),
            SymbolPrice(symbol="EURUSD", bid=1.10010, ask=1.10030, timestamp=1001.0),
            SymbolPrice(symbol="EURUSD", bid=1.10020, ask=1.10040, timestamp=1002.0),
        ]
        adapter = MockAdapter(prices)
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if len(ticks) >= 3:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        assert len(ticks) == 3
        assert ticks[0].bid == 1.10000
        assert ticks[1].bid == 1.10010
        assert ticks[2].bid == 1.10020
        assert poller.ticks_yielded == 3

    @pytest.mark.asyncio
    async def test_deduplication(self):
        """Poller skips ticks where bid/ask haven't changed."""
        prices = [
            SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, timestamp=1000.0),
            SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, timestamp=1001.0),  # dup
            SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, timestamp=1002.0),  # dup
            SymbolPrice(symbol="EURUSD", bid=1.10010, ask=1.10030, timestamp=1003.0),  # new
        ]
        adapter = MockAdapter(prices)
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if adapter._call_count >= 4:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        assert len(ticks) == 2  # first + last (2 dups skipped)
        assert ticks[0].bid == 1.10000
        assert ticks[1].bid == 1.10010
        assert poller.ticks_yielded == 2

    @pytest.mark.asyncio
    async def test_zero_price_filtered(self):
        """Poller filters out zero/negative prices."""
        prices = [
            SymbolPrice(symbol="EURUSD", bid=0.0, ask=0.0, timestamp=1000.0),
            SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, timestamp=1001.0),
            SymbolPrice(symbol="EURUSD", bid=-1.0, ask=1.10020, timestamp=1002.0),
            # 4th call: valid price so the loop yields and we can stop
            SymbolPrice(symbol="EURUSD", bid=1.10030, ask=1.10050, timestamp=1003.0),
        ]
        adapter = MockAdapter(prices)
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if adapter._call_count >= 4:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        # Price 0 (zero) and price 2 (negative) filtered; prices 1 and 3 pass
        assert len(ticks) == 2
        assert ticks[0].bid == 1.10000
        assert ticks[1].bid == 1.10030

    @pytest.mark.asyncio
    async def test_stop_terminates(self):
        """Calling stop() terminates the stream gracefully."""
        adapter = MockAdapter(
            [
                SymbolPrice(symbol="EURUSD", bid=1.10000 + i * 0.00010, ask=1.10020 + i * 0.00010, timestamp=1000.0 + i)
                for i in range(100)
            ]
        )
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if len(ticks) >= 2:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        assert len(ticks) >= 2
        assert not poller.running

    @pytest.mark.asyncio
    async def test_error_does_not_kill_stream(self):
        """Adapter exceptions are caught, logged, and the stream continues."""
        adapter = ErrorAdapter(error_on={2})  # error on second call
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if len(ticks) >= 3:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        assert len(ticks) == 3  # got 3 good ticks despite 1 error
        assert poller.errors == 1

    @pytest.mark.asyncio
    async def test_multi_symbol_polling(self):
        """Poller iterates through all symbols each loop."""
        # The adapter returns different prices based on call count
        adapter = MockAdapter()

        # Override to return symbol-specific prices
        call_count = 0

        async def mock_price(symbol):
            nonlocal call_count
            call_count += 1
            if symbol == "EURUSD":
                return SymbolPrice(
                    symbol="EURUSD",
                    bid=1.10000 + call_count * 0.00010,
                    ask=1.10020 + call_count * 0.00010,
                    timestamp=1000.0 + call_count,
                )
            else:
                return SymbolPrice(
                    symbol="GBPUSD",
                    bid=1.27000 + call_count * 0.00010,
                    ask=1.27020 + call_count * 0.00010,
                    timestamp=1000.0 + call_count,
                )

        adapter.get_symbol_price = mock_price
        poller = TickBatchPoller(adapter, ["EURUSD", "GBPUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if len(ticks) >= 4:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        symbols_seen = {t.symbol for t in ticks}
        assert "EURUSD" in symbols_seen
        assert "GBPUSD" in symbols_seen


# ---------------------------------------------------------------------------
# Stats tests
# ---------------------------------------------------------------------------


class TestTickBatchPollerStats:
    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        """Stats counters are accurate after streaming."""
        prices = [
            SymbolPrice(symbol="EURUSD", bid=1.10000 + i * 0.00010, ask=1.10020 + i * 0.00010, timestamp=1000.0 + i)
            for i in range(5)
        ]
        adapter = MockAdapter(prices)
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        ticks: list[Tick] = []

        async def collect():
            async for tick in poller.stream():
                ticks.append(tick)
                if len(ticks) >= 5:
                    poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        stats = poller.stats
        assert stats["ticks_yielded"] == 5
        assert stats["polls"] >= 5
        assert stats["errors"] == 0
        assert stats["running"] is False
        assert stats["symbols"] == ["EURUSD"]

    def test_stats_initial(self):
        """Stats show zero counters on fresh poller."""
        poller = TickBatchPoller(MockAdapter(), ["EURUSD"], interval=1.0)
        stats = poller.stats
        assert stats["ticks_yielded"] == 0
        assert stats["polls"] == 0
        assert stats["errors"] == 0


# ---------------------------------------------------------------------------
# Tick properties
# ---------------------------------------------------------------------------


class TestTickFromPoller:
    @pytest.mark.asyncio
    async def test_tick_properties(self):
        """Ticks yielded have correct symbol, bid, ask, timestamp."""
        prices = [
            SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, timestamp=1234567890.0),
        ]
        adapter = MockAdapter(prices)
        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.01)

        tick = None

        async def collect():
            nonlocal tick
            async for t in poller.stream():
                tick = t
                poller.stop()

        await asyncio.wait_for(collect(), timeout=5.0)

        assert tick is not None
        assert tick.symbol == "EURUSD"
        assert tick.bid == 1.10000
        assert tick.ask == 1.10020
        assert tick.timestamp == 1234567890.0
        assert tick.is_valid()
