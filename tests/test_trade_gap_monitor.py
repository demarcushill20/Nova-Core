"""Tests for trade gap monitor — session/regime-aware no-trade detection."""

from datetime import datetime, timezone

from novatrade.monitor.trade_gap_monitor import (
    _REGIME_MULTIPLIERS,
    _SESSION_MULTIPLIERS,
    DEFAULT_GAP_THRESHOLD_S,
    _get_session,
    check_trade_gap,
)


class TestGetSession:
    """Forex session detection."""

    def test_london_session(self):
        dt = datetime(2026, 4, 9, 9, 0, tzinfo=timezone.utc)  # Thursday 09:00 UTC
        assert _get_session(dt) == "london"

    def test_overlap_session(self):
        dt = datetime(2026, 4, 9, 14, 0, tzinfo=timezone.utc)  # Thursday 14:00 UTC
        assert _get_session(dt) == "overlap"

    def test_newyork_session(self):
        dt = datetime(2026, 4, 9, 18, 0, tzinfo=timezone.utc)  # Thursday 18:00 UTC
        assert _get_session(dt) == "newyork"

    def test_asian_session(self):
        dt = datetime(2026, 4, 9, 3, 0, tzinfo=timezone.utc)  # Thursday 03:00 UTC
        assert _get_session(dt) == "asian"

    def test_weekend_saturday(self):
        dt = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)  # Saturday
        assert _get_session(dt) == "weekend"

    def test_weekend_friday_late(self):
        dt = datetime(2026, 4, 10, 23, 0, tzinfo=timezone.utc)  # Friday 23:00 UTC
        assert _get_session(dt) == "weekend"

    def test_not_weekend_friday_early(self):
        dt = datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)  # Friday 14:00 UTC
        assert _get_session(dt) == "overlap"

    def test_weekend_sunday_early(self):
        dt = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)  # Sunday 15:00 UTC
        assert _get_session(dt) == "weekend"

    def test_not_weekend_sunday_late(self):
        dt = datetime(2026, 4, 12, 23, 0, tzinfo=timezone.utc)  # Sunday 23:00 UTC
        assert _get_session(dt) == "asian"  # Market reopened


class TestThresholdMultipliers:
    """Verify session and regime multiplier definitions."""

    def test_weekend_suppresses(self):
        assert _SESSION_MULTIPLIERS["weekend"] == 0.0

    def test_asian_doubles(self):
        assert _SESSION_MULTIPLIERS["asian"] == 2.0

    def test_quiet_regime_doubles(self):
        assert _REGIME_MULTIPLIERS["quiet"] == 2.0

    def test_volatile_tightens(self):
        assert _REGIME_MULTIPLIERS["volatile"] < 1.0

    def test_effective_threshold_quiet_asian(self):
        """Quiet regime + Asian session → 4× base = 12h threshold."""
        base = DEFAULT_GAP_THRESHOLD_S
        effective = base * _SESSION_MULTIPLIERS["asian"] * _REGIME_MULTIPLIERS["quiet"]
        assert effective == base * 4.0

    def test_effective_threshold_volatile_overlap(self):
        """Volatile regime + overlap session → 0.75× base = 2.25h."""
        base = DEFAULT_GAP_THRESHOLD_S
        effective = base * _SESSION_MULTIPLIERS["overlap"] * _REGIME_MULTIPLIERS["volatile"]
        assert effective == base * 0.75


class TestCheckTradeGap:
    """Integration tests for the check_trade_gap function."""

    def test_weekend_suppressed(self):
        """Weekend should always return suppressed, GREEN."""
        now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)  # Saturday
        status = check_trade_gap(now=now)
        assert status.suppressed is True
        assert status.alert_level == "GREEN"
        assert "weekend" in status.suppression_reason

    def test_low_uptime_suppressed(self):
        """Should not alert when uptime is very low."""
        now = datetime(2026, 4, 9, 9, 0, tzinfo=timezone.utc)
        # check_trade_gap reads uptime from live_metrics.json
        # In test environment, uptime will be 0 → suppressed
        status = check_trade_gap(now=now)
        assert status.suppressed is True
        assert "uptime" in status.suppression_reason
