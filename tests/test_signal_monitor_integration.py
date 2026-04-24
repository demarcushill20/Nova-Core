"""Integration tests for signal monitoring with IRB strategy."""

from datetime import datetime, timedelta, timezone

from novatrade.monitor.signal_monitor import SignalRateMonitor, get_current_stats, record_signal

# Fixed weekday timestamp for tests (Wednesday) to avoid weekend MARKET_CLOSED
_WEEKDAY = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)


class TestSignalMonitorIntegration:
    """Test signal monitor integration with actual strategy."""

    def test_engine_level_signal_recording(self):
        """Test that bar evaluations are recorded at the engine level."""
        import novatrade.monitor.signal_monitor as sm

        sm._global_monitor = SignalRateMonitor()

        # Simulate what LiveStrategyEngine.on_bar does: call record_signal("any")
        # for each bar evaluation, regardless of engine state.
        for _ in range(16):
            record_signal("any")

        stats = get_current_stats()

        assert stats.signals_1h == 16, f"Expected 16 evaluations, got {stats.signals_1h}"
        assert stats.last_signal_at != "", "Should have timestamp of last signal"

    def test_trade_signal_vs_any_signal_distinction(self):
        """Test that trade signals are tracked separately from general evaluations."""
        import novatrade.monitor.signal_monitor as sm

        sm._global_monitor = SignalRateMonitor()

        # Simulate bar evaluations (engine level)
        for _ in range(10):
            record_signal("any")

        # Simulate a trade signal (strategy level, from IRBStrategy.check_entry)
        record_signal("trade")

        stats = get_current_stats()

        assert stats.signals_1h == 11, "Should count both 'any' and 'trade' signals"
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
