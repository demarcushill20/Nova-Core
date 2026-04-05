"""Test feed health enhancement for improved market closure handling.

Ensures that market closure is properly distinguished from truly unhealthy states
in the feed health statistics and reporting.
"""

import time
from unittest.mock import patch

import pytest

from novatrade.data.tick import Tick
from novatrade.monitor.feed_health import FeedHealthConfig, FeedHealthSupervisor


class TestFeedHealthMarketClosureEnhancement:
    """Test the enhanced feed health statistics for market closure."""

    def test_stats_distinguish_market_closed_from_unhealthy(self):
        """Market closure should be tracked separately from truly unhealthy states."""
        config = FeedHealthConfig(max_stale_seconds=5.0, max_clock_drift_seconds=1.0)
        supervisor = FeedHealthSupervisor(config)

        # Mock market closed condition
        with patch("novatrade.monitor.feed_health._is_forex_market_closed", return_value=True):
            # Simulate receiving a tick during market closure
            tick = Tick(symbol="EURUSD", timestamp=time.time(), bid=1.2000, ask=1.2003)
            supervisor.on_tick(tick)

            stats = supervisor.stats

            # Verify market closure is tracked separately
            assert stats["tracked_symbols"] == 1
            assert stats["healthy"] == 0
            assert stats["unhealthy"] == 0  # Market closed should NOT count as unhealthy
            assert stats["market_closed"] == 1
            assert "EURUSD" in stats["symbols"]
            assert stats["symbols"]["EURUSD"]["state"] == "MARKET_CLOSED"

    def test_stats_count_truly_unhealthy_states(self):
        """Truly problematic states should still count as unhealthy."""
        config = FeedHealthConfig(max_stale_seconds=5.0, max_clock_drift_seconds=1.0)
        supervisor = FeedHealthSupervisor(config)

        # Mock market open condition
        with patch("novatrade.monitor.feed_health._is_forex_market_closed", return_value=False):
            # Simulate receiving a tick with significant clock drift
            tick = Tick(
                symbol="EURUSD",
                timestamp=time.time() - 10.0,  # 10 seconds in the past = clock drift
                bid=1.2000,
                ask=1.2003,
            )
            supervisor.on_tick(tick)

            stats = supervisor.stats

            # Verify unhealthy state is properly categorized
            assert stats["tracked_symbols"] == 1
            assert stats["healthy"] == 0
            assert stats["unhealthy"] == 1  # Clock drift should count as unhealthy
            assert stats["market_closed"] == 0
            assert stats["symbols"]["EURUSD"]["state"] == "CLOCK_DRIFT"

    def test_stats_healthy_during_market_hours(self):
        """Normal operation during market hours should count as healthy."""
        config = FeedHealthConfig(max_stale_seconds=5.0, max_clock_drift_seconds=1.0)
        supervisor = FeedHealthSupervisor(config)

        # Mock market open condition
        with patch("novatrade.monitor.feed_health._is_forex_market_closed", return_value=False):
            # Simulate receiving a good tick during market hours
            tick = Tick(
                symbol="EURUSD",
                timestamp=time.time(),  # Current time = no drift
                bid=1.2000,
                ask=1.2003,
            )
            supervisor.on_tick(tick)

            stats = supervisor.stats

            # Verify healthy state is properly categorized
            assert stats["tracked_symbols"] == 1
            assert stats["healthy"] == 1
            assert stats["unhealthy"] == 0
            assert stats["market_closed"] == 0
            assert stats["symbols"]["EURUSD"]["state"] == "HEALTHY"

    def test_stats_mixed_states_multiple_symbols(self):
        """Multiple symbols with different states should be categorized correctly."""
        config = FeedHealthConfig(max_stale_seconds=5.0, max_clock_drift_seconds=1.0)
        supervisor = FeedHealthSupervisor(config)

        # Mock market closed for consistent behavior
        with patch("novatrade.monitor.feed_health._is_forex_market_closed", return_value=True):
            # Add multiple symbols
            for symbol in ["EURUSD", "GBPUSD", "USDJPY"]:
                tick = Tick(symbol=symbol, timestamp=time.time(), bid=1.2000, ask=1.2003)
                supervisor.on_tick(tick)

            stats = supervisor.stats

            # All should be market closed
            assert stats["tracked_symbols"] == 3
            assert stats["healthy"] == 0
            assert stats["unhealthy"] == 0
            assert stats["market_closed"] == 3

    def test_backwards_compatibility_with_existing_fields(self):
        """Ensure existing fields are maintained for backwards compatibility."""
        config = FeedHealthConfig()
        supervisor = FeedHealthSupervisor(config)

        with patch("novatrade.monitor.feed_health._is_forex_market_closed", return_value=False):
            tick = Tick(symbol="EURUSD", timestamp=time.time(), bid=1.2000, ask=1.2003)
            supervisor.on_tick(tick)

            stats = supervisor.stats

            # Verify all expected fields are present
            expected_fields = {
                "tracked_symbols",
                "healthy",
                "unhealthy",
                "market_closed",  # New field
                "disconnected",
                "symbols",
            }
            assert set(stats.keys()) == expected_fields

    def test_market_closed_field_always_present(self):
        """The market_closed field should always be present, even when zero."""
        config = FeedHealthConfig()
        supervisor = FeedHealthSupervisor(config)

        # Test with no symbols
        stats = supervisor.stats
        assert "market_closed" in stats
        assert stats["market_closed"] == 0

        # Test with healthy symbol
        with patch("novatrade.monitor.feed_health._is_forex_market_closed", return_value=False):
            tick = Tick(symbol="EURUSD", timestamp=time.time(), bid=1.2000, ask=1.2003)
            supervisor.on_tick(tick)

            stats = supervisor.stats
            assert "market_closed" in stats
            assert stats["market_closed"] == 0  # Should be 0 when market is open


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
