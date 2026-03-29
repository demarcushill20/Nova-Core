"""Integration tests for signal monitoring with IRB strategy."""

from datetime import datetime, timedelta, timezone

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.monitor.signal_monitor import SignalRateMonitor, get_current_stats
from novatrade.strategies.irb import IRBStrategy

# Fixed weekday timestamp for tests (Wednesday) to avoid weekend MARKET_CLOSED
_WEEKDAY = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)


class TestSignalMonitorIntegration:
    """Test signal monitor integration with actual strategy."""

    def test_irb_strategy_signal_recording(self):
        """Test that IRB strategy records signals during evaluation."""
        # Create a fresh monitor (resets singleton state)
        SignalRateMonitor()

        # Create IRB strategy
        env = BacktestEnvironment()
        strategy = IRBStrategy(env)

        # Create some test candles (simple uptrend scenario)
        base_time = _WEEKDAY
        candles = []

        # Create a pattern that should generate some signals
        for i in range(50):  # Need enough for warmup
            candle = Candle(
                timestamp=base_time + timedelta(minutes=i * 5),
                open=1.1000 + i * 0.0001,
                high=1.1005 + i * 0.0001,
                low=1.0995 + i * 0.0001,
                close=1.1003 + i * 0.0001,
                volume=1000.0,
                symbol="EURUSD",
                timeframe="M5",
            )
            candles.append(candle)

        # Generate indicators
        indicators = strategy.compute_indicators(candles)

        # Generate signals (this should record signal activity)
        strategy.generate_signals(candles, indicators)

        # Get current stats
        stats = get_current_stats()

        # Should have recorded some signal evaluations
        assert stats.signals_1h > 0, "Should have recorded signal evaluations"
        assert stats.last_signal_at != "", "Should have timestamp of last signal"

        # The number of evaluations should be related to candle count (after warmup)
        expected_evaluations = len(candles) - env.warmup_bars
        # Allow some tolerance since not all bars may meet basic criteria
        assert stats.signals_1h >= expected_evaluations * 0.5, (
            f"Expected at least {expected_evaluations * 0.5} evaluations, got {stats.signals_1h}"
        )

    def test_trade_signal_vs_any_signal_distinction(self):
        """Test that trade signals are tracked separately from general evaluations."""
        # Create a fresh monitor (resets singleton state)
        SignalRateMonitor()

        # Create IRB strategy
        env = BacktestEnvironment()
        strategy = IRBStrategy(env)

        # Create candles designed to trigger a trade signal
        base_time = _WEEKDAY
        candles = []

        # Create IRB pattern: bar with open/close in lower portion (uptrend reversal)
        for i in range(35):  # Enough for warmup (34) + 1 signal bar
            if i < 34:
                # Build trend
                candle = Candle(
                    timestamp=base_time + timedelta(minutes=i * 5),
                    open=1.1000 + i * 0.0002,
                    high=1.1010 + i * 0.0002,
                    low=1.0990 + i * 0.0002,
                    close=1.1008 + i * 0.0002,
                    volume=1000.0,
                    symbol="EURUSD",
                    timeframe="M5",
                )
            else:
                # IRB reversal bar: large range with open/close in lower portion
                candle = Candle(
                    timestamp=base_time + timedelta(minutes=i * 5),
                    open=1.1070,  # Open near top
                    high=1.1080,  # High
                    low=1.1020,  # Low (60 pip range)
                    close=1.1030,  # Close in lower 30% (should trigger uptrend IRB)
                    volume=1000.0,
                    symbol="EURUSD",
                    timeframe="M5",
                )
            candles.append(candle)

        # Generate indicators and signals
        indicators = strategy.compute_indicators(candles)
        signals = strategy.generate_signals(candles, indicators)

        # Get stats
        stats = get_current_stats()

        # Should have recorded signal evaluations
        assert stats.signals_1h > 0, "Should have recorded some signal evaluations"

        # If we generated any actual trade signals, they should be tracked separately
        if signals:
            assert stats.last_trade_signal_at != "", "Should have recorded trade signal timestamp"

    def test_signal_rate_health_assessment(self):
        """Test signal rate health assessment with realistic scenario."""
        monitor = SignalRateMonitor()

        # Use fixed weekday time
        now = _WEEKDAY
        for i in range(4):
            # Backdate signals by 15 minutes each
            monitor.signal_timestamps.append(now - timedelta(minutes=i * 15))

        stats = monitor.get_stats(now=now)

        assert stats.status == "OK"
        assert stats.concern_level == "GREEN"
        assert stats.signals_1h == 4

    def test_signal_monitoring_persistence(self):
        """Test that signal stats persist to disk for heartbeat visibility."""
        from novatrade.monitor.signal_monitor import SIGNAL_STATS_FILE

        # Generate some signals
        monitor = SignalRateMonitor()
        monitor.record_signal("trade")
        monitor.record_signal("any")

        # Get stats (this triggers persistence)
        monitor.get_stats()

        # Check that file was created
        assert SIGNAL_STATS_FILE.exists(), "Signal stats file should be created"

        # Check that it contains valid JSON
        import json

        data = json.loads(SIGNAL_STATS_FILE.read_text())
        assert "signals_1h" in data
        assert "status" in data
        assert data["signals_1h"] > 0

    def test_regime_and_session_in_persisted_stats(self):
        """Test that regime and session fields are included in persisted stats."""
        from novatrade.monitor.signal_monitor import SIGNAL_STATS_FILE

        monitor = SignalRateMonitor()
        monitor.record_signal("any")

        # Use a weekday time
        monitor.get_stats(now=_WEEKDAY)

        import json

        data = json.loads(SIGNAL_STATS_FILE.read_text())
        assert "regime" in data
        assert "session" in data
        assert data["session"] == "london"  # _WEEKDAY is 10:00 UTC
