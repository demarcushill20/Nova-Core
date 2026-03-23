"""Tests for novatrade.data.tick — Tick model and TickBuffer."""

import pytest

from novatrade.data.tick import Tick, TickBuffer

# ---------------------------------------------------------------------------
# Tick dataclass
# ---------------------------------------------------------------------------


class TestTick:
    """Tick immutability, properties, and validation."""

    def _make_tick(self, **overrides: object) -> Tick:
        defaults: dict[str, object] = {
            "symbol": "EURUSD",
            "timestamp": 1700000000.0,
            "bid": 1.08500,
            "ask": 1.08520,
            "last": 1.08510,
            "volume": 10.0,
        }
        defaults.update(overrides)
        return Tick(**defaults)  # type: ignore[arg-type]

    def test_basic_construction(self):
        t = self._make_tick()
        assert t.symbol == "EURUSD"
        assert t.bid == 1.08500
        assert t.ask == 1.08520

    def test_frozen_immutability(self):
        t = self._make_tick()
        with pytest.raises(AttributeError):
            t.bid = 1.09  # type: ignore[misc]

    def test_mid_property(self):
        t = self._make_tick(bid=1.08500, ask=1.08520)
        assert t.mid == pytest.approx(1.08510, abs=1e-6)

    def test_spread_property(self):
        t = self._make_tick(bid=1.08500, ask=1.08520)
        assert t.spread == pytest.approx(0.00020, abs=1e-6)

    def test_spread_pips_forex(self):
        t = self._make_tick(symbol="EURUSD", bid=1.08500, ask=1.08520)
        # spread = 0.00020, pip_size = 0.0001 → 2.0 pips
        assert t.spread_pips == pytest.approx(2.0, abs=0.01)

    def test_spread_pips_jpy_pair(self):
        t = self._make_tick(symbol="USDJPY", bid=149.500, ask=149.530)
        # spread = 0.030, pip_size = 0.01 → 3.0 pips
        assert t.spread_pips == pytest.approx(3.0, abs=0.01)

    def test_is_valid_good_tick(self):
        t = self._make_tick()
        assert t.is_valid() is True

    def test_is_valid_zero_bid(self):
        t = self._make_tick(bid=0.0)
        assert t.is_valid() is False

    def test_is_valid_bid_gt_ask(self):
        t = self._make_tick(bid=1.09, ask=1.08)
        assert t.is_valid() is False

    def test_is_valid_zero_timestamp(self):
        t = self._make_tick(timestamp=0.0)
        assert t.is_valid() is False

    def test_default_volume(self):
        t = Tick(symbol="EURUSD", timestamp=1.0, bid=1.0, ask=1.1)
        assert t.volume == 0.0
        assert t.last == 0.0

    def test_equality(self):
        t1 = self._make_tick()
        t2 = self._make_tick()
        assert t1 == t2

    def test_hash_for_sets(self):
        t1 = self._make_tick(timestamp=1.0)
        t2 = self._make_tick(timestamp=2.0)
        assert len({t1, t2}) == 2


# ---------------------------------------------------------------------------
# TickBuffer
# ---------------------------------------------------------------------------


class TestTickBuffer:
    """TickBuffer FIFO ordering, capacity limits, and multi-symbol."""

    def _tick(self, symbol: str = "EURUSD", ts: float = 1.0) -> Tick:
        return Tick(symbol=symbol, timestamp=ts, bid=1.08, ask=1.09, last=1.085)

    def test_push_and_latest(self):
        buf = TickBuffer(max_size=10)
        t = self._tick(ts=100.0)
        buf.push(t)
        assert buf.latest("EURUSD") is t

    def test_latest_returns_none_for_unknown_symbol(self):
        buf = TickBuffer()
        assert buf.latest("GBPUSD") is None

    def test_fifo_ordering(self):
        buf = TickBuffer(max_size=100)
        for i in range(5):
            buf.push(self._tick(ts=float(i)))
        ticks = buf.all_ticks("EURUSD")
        assert len(ticks) == 5
        assert [t.timestamp for t in ticks] == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_capacity_eviction(self):
        buf = TickBuffer(max_size=3)
        for i in range(5):
            buf.push(self._tick(ts=float(i)))
        ticks = buf.all_ticks("EURUSD")
        assert len(ticks) == 3
        # Oldest (0, 1) should be evicted
        assert ticks[0].timestamp == 2.0
        assert ticks[2].timestamp == 4.0

    def test_since_filtering(self):
        buf = TickBuffer(max_size=100)
        for i in range(10):
            buf.push(self._tick(ts=float(i)))
        recent = buf.since("EURUSD", 7.0)
        assert len(recent) == 3
        assert recent[0].timestamp == 7.0

    def test_since_empty_symbol(self):
        buf = TickBuffer()
        assert buf.since("NOPE", 0.0) == []

    def test_multi_symbol(self):
        buf = TickBuffer(max_size=100)
        buf.push(self._tick(symbol="EURUSD", ts=1.0))
        buf.push(self._tick(symbol="GBPUSD", ts=2.0))
        buf.push(self._tick(symbol="EURUSD", ts=3.0))

        assert buf.count("EURUSD") == 2
        assert buf.count("GBPUSD") == 1
        assert set(buf.symbols) == {"EURUSD", "GBPUSD"}

    def test_clear_single_symbol(self):
        buf = TickBuffer(max_size=100)
        buf.push(self._tick(symbol="EURUSD"))
        buf.push(self._tick(symbol="GBPUSD"))
        buf.clear("EURUSD")
        assert buf.count("EURUSD") == 0
        assert buf.count("GBPUSD") == 1

    def test_clear_all(self):
        buf = TickBuffer(max_size=100)
        buf.push(self._tick(symbol="EURUSD"))
        buf.push(self._tick(symbol="GBPUSD"))
        buf.clear()
        assert buf.symbols == []

    def test_count_unknown_symbol(self):
        buf = TickBuffer()
        assert buf.count("NOPE") == 0

    def test_invalid_max_size(self):
        with pytest.raises(ValueError, match="max_size must be >= 1"):
            TickBuffer(max_size=0)

    def test_stats(self):
        buf = TickBuffer(max_size=50)
        buf.push(self._tick(symbol="EURUSD", ts=10.0))
        buf.push(self._tick(symbol="EURUSD", ts=20.0))
        stats = buf.stats
        assert stats["max_size"] == 50
        assert stats["symbols"]["EURUSD"]["count"] == 2
        assert stats["symbols"]["EURUSD"]["oldest_ts"] == 10.0
        assert stats["symbols"]["EURUSD"]["newest_ts"] == 20.0

    def test_all_ticks_empty(self):
        buf = TickBuffer()
        assert buf.all_ticks("EURUSD") == []

    def test_max_size_property(self):
        buf = TickBuffer(max_size=42)
        assert buf.max_size == 42
