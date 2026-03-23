"""Tests for novatrade.data.bar_aggregator — BarAggregator.

Phase 2.2 tests: bar boundary alignment, multi-timeframe emission,
OHLCV correctness, gap handling, force_close, and backtest parity.
"""

from __future__ import annotations

import pytest

from novatrade.data.bar_aggregator import (
    TIMEFRAME_SECONDS,
    BarAggregator,
    bar_boundary,
)
from novatrade.data.tick import Tick

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tick(
    symbol: str = "EURUSD",
    timestamp: float = 0.0,
    bid: float = 1.10000,
    ask: float = 1.10020,
    volume: float = 0.0,
) -> Tick:
    return Tick(symbol=symbol, timestamp=timestamp, bid=bid, ask=ask, volume=volume)


# ---------------------------------------------------------------------------
# bar_boundary() tests
# ---------------------------------------------------------------------------


class TestBarBoundary:
    def test_h1_exact_boundary(self):
        """Timestamp exactly at hour boundary returns that boundary."""
        assert bar_boundary(3600.0, 3600) == 3600.0

    def test_h1_mid_bar(self):
        """Timestamp mid-bar rounds down to bar open."""
        assert bar_boundary(3601.0, 3600) == 3600.0
        assert bar_boundary(7199.0, 3600) == 3600.0

    def test_h1_just_before_next(self):
        """Timestamp at 3599.9 belongs to the 0-3600 bar."""
        assert bar_boundary(3599.9, 3600) == 0.0

    def test_h1_at_next_boundary(self):
        """Timestamp at 3600.0 belongs to the 3600-7200 bar."""
        assert bar_boundary(3600.0, 3600) == 3600.0

    def test_h4_boundaries(self):
        """H4 bars close at 0, 14400, 28800, ..."""
        assert bar_boundary(0.0, 14400) == 0.0
        assert bar_boundary(14399.0, 14400) == 0.0
        assert bar_boundary(14400.0, 14400) == 14400.0
        assert bar_boundary(28800.0, 14400) == 28800.0

    def test_m15_boundaries(self):
        assert bar_boundary(900.0, 900) == 900.0
        assert bar_boundary(899.0, 900) == 0.0

    def test_d1_boundaries(self):
        assert bar_boundary(86400.0, 86400) == 86400.0
        assert bar_boundary(86399.0, 86400) == 0.0


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------


class TestBarAggregatorInit:
    def test_default_h1(self):
        agg = BarAggregator()
        assert agg.timeframes == ["H1"]

    def test_multi_timeframe(self):
        agg = BarAggregator(timeframes=["H1", "H4"])
        assert agg.timeframes == ["H1", "H4"]

    def test_invalid_timeframe(self):
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            BarAggregator(timeframes=["H1", "H3"])

    def test_initial_stats(self):
        agg = BarAggregator()
        assert agg.ticks_processed == 0
        assert agg.bars_emitted == 0
        assert agg.active_bars == 0


# ---------------------------------------------------------------------------
# Single-timeframe H1 tests
# ---------------------------------------------------------------------------


class TestBarAggregatorH1:
    def test_no_bar_within_same_period(self):
        """Ticks within the same H1 bar don't emit."""
        agg = BarAggregator(timeframes=["H1"])
        # All ticks within first H1 bar (0-3600)
        for ts in [100.0, 200.0, 500.0, 3599.0]:
            result = agg.on_tick(make_tick(timestamp=ts))
            assert result == []

        assert agg.active_bars == 1
        assert agg.ticks_processed == 4

    def test_bar_emitted_on_boundary_cross(self):
        """Tick crossing H1 boundary emits the previous bar."""
        agg = BarAggregator(timeframes=["H1"])
        # Build a bar in period 0-3600
        agg.on_tick(make_tick(timestamp=100.0, bid=1.10000, ask=1.10020))
        agg.on_tick(make_tick(timestamp=200.0, bid=1.10050, ask=1.10070))  # higher
        agg.on_tick(make_tick(timestamp=300.0, bid=1.09950, ask=1.09970))  # lower
        agg.on_tick(make_tick(timestamp=3500.0, bid=1.10020, ask=1.10040))  # close

        # Cross into next bar
        result = agg.on_tick(make_tick(timestamp=3600.0, bid=1.10030, ask=1.10050))
        assert len(result) == 1

        tf, candle = result[0]
        assert tf == "H1"
        assert candle.timestamp == 0.0  # bar open boundary
        assert candle.symbol == "EURUSD"

    def test_ohlcv_correctness(self):
        """OHLCV values computed correctly from tick midpoints."""
        agg = BarAggregator(timeframes=["H1"])

        # Build bar with known midpoints
        # mid = (bid + ask) / 2
        agg.on_tick(make_tick(timestamp=100.0, bid=1.10000, ask=1.10020))  # mid=1.10010
        agg.on_tick(make_tick(timestamp=200.0, bid=1.10080, ask=1.10100))  # mid=1.10090 (high)
        agg.on_tick(make_tick(timestamp=300.0, bid=1.09900, ask=1.09920))  # mid=1.09910 (low)
        agg.on_tick(make_tick(timestamp=400.0, bid=1.10040, ask=1.10060))  # mid=1.10050 (close)

        # Emit by crossing boundary
        result = agg.on_tick(make_tick(timestamp=3600.0, bid=1.10050, ask=1.10070))
        assert len(result) == 1
        _, candle = result[0]

        assert candle.open == pytest.approx(1.10010, abs=1e-6)
        assert candle.high == pytest.approx(1.10090, abs=1e-6)
        assert candle.low == pytest.approx(1.09910, abs=1e-6)
        assert candle.close == pytest.approx(1.10050, abs=1e-6)

    def test_volume_accumulation(self):
        """Volume sums across all ticks in the bar."""
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(timestamp=100.0, volume=100.0))
        agg.on_tick(make_tick(timestamp=200.0, volume=200.0))
        agg.on_tick(make_tick(timestamp=300.0, volume=150.0))

        result = agg.on_tick(make_tick(timestamp=3600.0))
        assert len(result) == 1
        _, candle = result[0]
        assert candle.volume == pytest.approx(450.0)

    def test_consecutive_bars(self):
        """Multiple bars emitted in sequence."""
        agg = BarAggregator(timeframes=["H1"])

        # Bar 1: 0-3600
        agg.on_tick(make_tick(timestamp=100.0, bid=1.10000, ask=1.10020))
        result1 = agg.on_tick(make_tick(timestamp=3600.0, bid=1.10010, ask=1.10030))
        assert len(result1) == 1
        assert result1[0][1].timestamp == 0.0

        # Bar 2: 3600-7200
        result2 = agg.on_tick(make_tick(timestamp=7200.0, bid=1.10020, ask=1.10040))
        assert len(result2) == 1
        assert result2[0][1].timestamp == 3600.0

        assert agg.bars_emitted == 2

    def test_gap_handling(self):
        """Ticks that skip a bar period only emit the bar being built."""
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(timestamp=100.0))

        # Skip to 3 hours later (gap of 2 bars)
        result = agg.on_tick(make_tick(timestamp=10800.0))
        assert len(result) == 1  # only the bar we were building
        assert result[0][1].timestamp == 0.0  # bar from period 0


# ---------------------------------------------------------------------------
# Multi-timeframe tests (H1 + H4)
# ---------------------------------------------------------------------------


class TestBarAggregatorMultiTF:
    def test_h4_emits_alongside_h1(self):
        """H4 bar emits when H4 boundary crossed (every 4th hour)."""
        agg = BarAggregator(timeframes=["H1", "H4"])

        # Build ticks in period 0
        agg.on_tick(make_tick(timestamp=100.0))

        # Cross H1 boundary at 3600 — only H1 emits (H4 period is 0-14400)
        result = agg.on_tick(make_tick(timestamp=3600.0))
        tfs = [tf for tf, _ in result]
        assert "H1" in tfs
        assert "H4" not in tfs

        # Continue building...
        agg.on_tick(make_tick(timestamp=7200.0))  # H1 #2
        agg.on_tick(make_tick(timestamp=10800.0))  # H1 #3

        # Cross H4 boundary at 14400 — both H1 AND H4 emit
        result = agg.on_tick(make_tick(timestamp=14400.0))
        tfs = [tf for tf, _ in result]
        assert "H1" in tfs
        assert "H4" in tfs

    def test_h4_boundary_alignment(self):
        """H4 bars close at 0:00, 4:00, 8:00, 12:00, 16:00, 20:00 UTC."""
        agg = BarAggregator(timeframes=["H4"])

        boundaries = [0, 14400, 28800, 43200, 57600, 72000]

        # Feed one tick per period, crossing each boundary
        agg.on_tick(make_tick(timestamp=100.0))  # period 0

        emitted_boundaries = []
        for boundary in boundaries[1:]:
            result = agg.on_tick(make_tick(timestamp=float(boundary)))
            if result:
                for _, candle in result:
                    emitted_boundaries.append(candle.timestamp)

        # Each boundary tick emits the previous period's bar
        assert emitted_boundaries == [0.0, 14400.0, 28800.0, 43200.0, 57600.0]

    def test_multi_symbol_independence(self):
        """Each symbol has its own independent bar accumulators."""
        agg = BarAggregator(timeframes=["H1"])

        # EURUSD tick at t=100
        agg.on_tick(make_tick(symbol="EURUSD", timestamp=100.0, bid=1.10000, ask=1.10020))
        # GBPUSD tick at t=200
        agg.on_tick(make_tick(symbol="GBPUSD", timestamp=200.0, bid=1.27000, ask=1.27020))

        assert agg.active_bars == 2

        # Cross boundary for both
        result = agg.on_tick(make_tick(symbol="EURUSD", timestamp=3600.0, bid=1.10010, ask=1.10030))
        assert len(result) == 1
        assert result[0][1].symbol == "EURUSD"

        result = agg.on_tick(make_tick(symbol="GBPUSD", timestamp=3600.0, bid=1.27010, ask=1.27030))
        assert len(result) == 1
        assert result[0][1].symbol == "GBPUSD"


# ---------------------------------------------------------------------------
# force_close tests
# ---------------------------------------------------------------------------


class TestBarAggregatorForceClose:
    def test_force_close_returns_partial_bar(self):
        """force_close emits bars with at least one tick."""
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(timestamp=100.0, bid=1.10000, ask=1.10020))
        agg.on_tick(make_tick(timestamp=200.0, bid=1.10010, ask=1.10030))

        result = agg.force_close()
        assert len(result) == 1
        _, candle = result[0]
        assert candle.timestamp == 0.0  # bar open
        assert candle.open == pytest.approx(1.10010, abs=1e-6)  # mid of first tick
        assert agg.active_bars == 0

    def test_force_close_specific_symbol(self):
        """force_close(symbol) only closes that symbol's bars."""
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(symbol="EURUSD", timestamp=100.0))
        agg.on_tick(make_tick(symbol="GBPUSD", timestamp=100.0, bid=1.27000, ask=1.27020))

        result = agg.force_close("EURUSD")
        assert len(result) == 1
        assert result[0][1].symbol == "EURUSD"
        assert agg.active_bars == 1  # GBPUSD still building

    def test_force_close_empty(self):
        """force_close on empty aggregator returns nothing."""
        agg = BarAggregator(timeframes=["H1"])
        result = agg.force_close()
        assert result == []

    def test_force_close_multi_tf(self):
        """force_close emits partial bars for all timeframes."""
        agg = BarAggregator(timeframes=["H1", "H4"])
        agg.on_tick(make_tick(timestamp=100.0))

        result = agg.force_close()
        tfs = [tf for tf, _ in result]
        assert "H1" in tfs
        assert "H4" in tfs
        assert agg.active_bars == 0


# ---------------------------------------------------------------------------
# Candle properties
# ---------------------------------------------------------------------------


class TestCandleOutput:
    def test_candle_has_correct_timeframe(self):
        agg = BarAggregator(timeframes=["M15"])
        agg.on_tick(make_tick(timestamp=100.0))
        result = agg.on_tick(make_tick(timestamp=900.0))
        assert len(result) == 1
        _, candle = result[0]
        assert candle.timeframe == "M15"

    def test_candle_has_correct_symbol(self):
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(symbol="GBPUSD", timestamp=100.0, bid=1.27000, ask=1.27020))
        result = agg.on_tick(make_tick(symbol="GBPUSD", timestamp=3600.0, bid=1.27010, ask=1.27030))
        assert len(result) == 1
        _, candle = result[0]
        assert candle.symbol == "GBPUSD"

    def test_single_tick_bar(self):
        """A bar with only one tick has O=H=L=C."""
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(timestamp=100.0, bid=1.10000, ask=1.10020))  # mid=1.10010
        result = agg.on_tick(make_tick(timestamp=3600.0))
        assert len(result) == 1
        _, candle = result[0]
        mid = 1.10010
        assert candle.open == pytest.approx(mid, abs=1e-6)
        assert candle.high == pytest.approx(mid, abs=1e-6)
        assert candle.low == pytest.approx(mid, abs=1e-6)
        assert candle.close == pytest.approx(mid, abs=1e-6)


# ---------------------------------------------------------------------------
# Stats & introspection
# ---------------------------------------------------------------------------


class TestBarAggregatorStats:
    def test_stats_tracking(self):
        agg = BarAggregator(timeframes=["H1", "H4"])
        agg.on_tick(make_tick(timestamp=100.0))
        agg.on_tick(make_tick(timestamp=200.0))
        agg.on_tick(make_tick(timestamp=3600.0))  # emit H1

        stats = agg.stats
        assert stats["ticks_processed"] == 3
        assert stats["bars_emitted"] == 1  # H1 only
        assert stats["timeframes"] == ["H1", "H4"]

    def test_building_bar_introspection(self):
        agg = BarAggregator(timeframes=["H1"])
        agg.on_tick(make_tick(timestamp=100.0))

        bar = agg.building_bar("EURUSD", "H1")
        assert bar is not None
        assert bar.tick_count == 1
        assert bar.bar_open_ts == 0.0

    def test_building_bar_none_for_unknown(self):
        agg = BarAggregator(timeframes=["H1"])
        assert agg.building_bar("EURUSD", "H1") is None


# ---------------------------------------------------------------------------
# All timeframes
# ---------------------------------------------------------------------------


class TestAllTimeframes:
    @pytest.mark.parametrize("tf", ["M1", "M5", "M15", "M30", "H1", "H4", "D1"])
    def test_timeframe_supported(self, tf):
        """All documented timeframes can be used."""
        agg = BarAggregator(timeframes=[tf])
        period = TIMEFRAME_SECONDS[tf]

        agg.on_tick(make_tick(timestamp=1.0))
        result = agg.on_tick(make_tick(timestamp=float(period)))
        assert len(result) == 1
        assert result[0][0] == tf
        assert result[0][1].timestamp == 0.0


# ---------------------------------------------------------------------------
# Backtest parity
# ---------------------------------------------------------------------------


class TestBacktestParity:
    def test_h1_bar_alignment_matches_epoch(self):
        """H1 bars use floor(ts/3600)*3600, same as backtest engine."""
        agg = BarAggregator(timeframes=["H1"])

        # Feed ticks at known timestamps
        # 2024-01-01 00:00:00 UTC = 1704067200
        base_ts = 1704067200.0
        agg.on_tick(make_tick(timestamp=base_ts + 100))
        agg.on_tick(make_tick(timestamp=base_ts + 1800))
        result = agg.on_tick(make_tick(timestamp=base_ts + 3600))  # next hour

        assert len(result) == 1
        _, candle = result[0]
        assert candle.timestamp == base_ts  # bar aligned to hour

    def test_h4_bar_alignment_matches_epoch(self):
        """H4 bars use floor(ts/14400)*14400."""
        agg = BarAggregator(timeframes=["H4"])

        base_ts = 1704067200.0  # 2024-01-01 00:00 UTC
        agg.on_tick(make_tick(timestamp=base_ts + 100))
        result = agg.on_tick(make_tick(timestamp=base_ts + 14400))

        assert len(result) == 1
        _, candle = result[0]
        assert candle.timestamp == base_ts
