"""Tests for P4: Enhanced Session-Aware Risk Management

Tests the session-aware filtering functionality that replaces basic weekend
checking with sophisticated session quality assessment and London-NY overlap focus.

Test coverage:
- Session quality assessment for all sessions
- Trading permission logic based on configuration
- Integration with session calendar
- Edge cases (weekends, transitions, invalid times)
- Configuration-driven behavior
"""

import datetime as dt

import pytest

from novatrade.evaluation.session_calendar import Session
from novatrade.risk.session_aware_filter import (
    SessionAwareConfig,
    SessionAwareFilter,
    SessionQuality,
    create_london_ny_focus_filter,
    create_overlap_only_filter,
)


class TestSessionAwareFilter:
    """Test session-aware filtering functionality."""

    def test_weekend_detection(self):
        """Test that weekends are properly detected and blocked."""
        filter_obj = SessionAwareFilter()

        # Saturday 12:00 UTC - should be blocked (Jan 4, 2025 is a Saturday)
        saturday = dt.datetime(2025, 1, 4, 12, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(saturday)

        assert info.session is None
        assert info.quality == SessionQuality.CLOSED
        assert "weekend" in info.reason.lower()
        assert not filter_obj.is_trading_allowed(saturday)

    def test_london_ny_overlap_premium_quality(self):
        """Test that London-NY overlap is rated as premium quality."""
        filter_obj = SessionAwareFilter()

        # Wednesday 14:00 UTC - in the overlap (13-16 UTC)
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(overlap_time)

        assert info.session == Session.OVERLAP_LON_NY
        assert info.quality == SessionQuality.PREMIUM
        assert "overlap" in info.reason.lower()
        assert "peak liquidity" in info.reason.lower()
        assert filter_obj.is_trading_allowed(overlap_time)

    def test_london_session_good_quality(self):
        """Test that London session is rated as good quality."""
        filter_obj = SessionAwareFilter()

        # Wednesday 10:00 UTC - London session (8-13 UTC, before NY opens)
        london_time = dt.datetime(2026, 4, 2, 10, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(london_time)

        assert info.session == Session.LONDON
        assert info.quality == SessionQuality.GOOD
        assert "london" in info.reason.lower()
        assert "good liquidity" in info.reason.lower()
        assert filter_obj.is_trading_allowed(london_time)

    def test_new_york_session_good_quality(self):
        """Test that New York session is rated as good quality."""
        filter_obj = SessionAwareFilter()

        # Wednesday 18:00 UTC - NY session (16-21 UTC, after London closes)
        ny_time = dt.datetime(2026, 4, 2, 18, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(ny_time)

        assert info.session == Session.NEW_YORK
        assert info.quality == SessionQuality.GOOD
        assert "new york" in info.reason.lower()
        assert "good liquidity" in info.reason.lower()
        assert filter_obj.is_trading_allowed(ny_time)

    def test_asian_session_acceptable_quality(self):
        """Test that Asian session is rated as acceptable quality."""
        filter_obj = SessionAwareFilter()

        # Wednesday 04:00 UTC - Asian session (0-8 UTC)
        asian_time = dt.datetime(2026, 4, 2, 4, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(asian_time)

        assert info.session == Session.ASIAN
        assert info.quality == SessionQuality.ACCEPTABLE
        assert "asian" in info.reason.lower()
        assert "lower volume" in info.reason.lower()
        assert filter_obj.is_trading_allowed(asian_time)

    def test_overlap_only_configuration(self):
        """Test overlap-only configuration blocks non-overlap sessions."""
        config = SessionAwareConfig(allow_overlap_only=True)
        filter_obj = SessionAwareFilter(config)

        # London session should be blocked
        london_time = dt.datetime(2026, 4, 2, 10, 0, tzinfo=dt.timezone.utc)
        assert not filter_obj.is_trading_allowed(london_time)

        # Overlap should be allowed
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        assert filter_obj.is_trading_allowed(overlap_time)

    def test_minimum_quality_threshold(self):
        """Test that minimum quality threshold is enforced."""
        # Require good quality or better
        config = SessionAwareConfig(minimum_quality=SessionQuality.GOOD)
        filter_obj = SessionAwareFilter(config)

        # Asian session (acceptable) should be blocked
        asian_time = dt.datetime(2026, 4, 2, 4, 0, tzinfo=dt.timezone.utc)
        assert not filter_obj.is_trading_allowed(asian_time)

        # London session (good) should be allowed
        london_time = dt.datetime(2026, 4, 2, 10, 0, tzinfo=dt.timezone.utc)
        assert filter_obj.is_trading_allowed(london_time)

        # Overlap (premium) should be allowed
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        assert filter_obj.is_trading_allowed(overlap_time)

    def test_asian_session_disabled(self):
        """Test that Asian session can be disabled via configuration."""
        config = SessionAwareConfig(allow_asian_session=False)
        filter_obj = SessionAwareFilter(config)

        asian_time = dt.datetime(2026, 4, 2, 4, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(asian_time)

        assert info.session == Session.ASIAN
        assert info.quality == SessionQuality.POOR  # Disabled = poor quality
        assert "disabled by config" in info.reason
        assert not filter_obj.is_trading_allowed(asian_time)

    def test_get_next_optimal_session(self):
        """Test finding the next optimal trading session."""
        filter_obj = SessionAwareFilter()

        # If currently in a poor session, should find next good session
        poor_time = dt.datetime(2026, 4, 2, 23, 0, tzinfo=dt.timezone.utc)  # Late night
        next_info = filter_obj.get_next_optimal_session(poor_time)

        assert next_info.quality in (SessionQuality.PREMIUM, SessionQuality.GOOD)
        assert "hours" in next_info.reason  # Should indicate time until next session

    def test_factory_functions(self):
        """Test the factory functions for common configurations."""
        # London-NY focus filter
        london_ny_filter = create_london_ny_focus_filter()
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        assert london_ny_filter.is_trading_allowed(overlap_time)

        # Overlap-only filter
        overlap_only_filter = create_overlap_only_filter()
        london_time = dt.datetime(2026, 4, 2, 10, 0, tzinfo=dt.timezone.utc)
        assert not overlap_only_filter.is_trading_allowed(london_time)  # London blocked
        assert overlap_only_filter.is_trading_allowed(overlap_time)  # Overlap allowed

    @pytest.mark.parametrize(
        "hour,expected_session,expected_quality",
        [
            (2, Session.ASIAN, SessionQuality.ACCEPTABLE),
            (10, Session.LONDON, SessionQuality.GOOD),
            (14, Session.OVERLAP_LON_NY, SessionQuality.PREMIUM),
            (18, Session.NEW_YORK, SessionQuality.GOOD),
        ],
    )
    def test_session_mapping(self, hour, expected_session, expected_quality):
        """Test session mapping for various hours."""
        filter_obj = SessionAwareFilter()
        test_time = dt.datetime(2026, 4, 2, hour, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(test_time)

        assert info.session == expected_session
        assert info.quality == expected_quality
        assert info.hour_utc == hour

    def test_explicit_timestamp_handling(self):
        """Test that explicit timestamps are handled correctly."""
        filter_obj = SessionAwareFilter()

        # Test with explicit timestamp during overlap
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        info = filter_obj.check_session(overlap_time)
        allowed = filter_obj.is_trading_allowed(overlap_time)

        assert info.hour_utc == 14
        assert info.quality == SessionQuality.PREMIUM
        assert allowed

        # Test with None (current time) - just verify it doesn't crash
        try:
            info_current = filter_obj.check_session(None)
            allowed_current = filter_obj.is_trading_allowed(None)
            # Should return valid results without crashing
            assert isinstance(info_current.hour_utc, int)
            assert isinstance(allowed_current, bool)
        except Exception as e:
            pytest.fail(f"Should not crash with None timestamp: {e}")

    def test_edge_case_session_boundaries(self):
        """Test behavior at session boundaries."""
        filter_obj = SessionAwareFilter()

        # Just before overlap starts (12:59 UTC)
        before_overlap = dt.datetime(2026, 4, 2, 12, 59, tzinfo=dt.timezone.utc)
        info_before = filter_obj.check_session(before_overlap)
        assert info_before.session == Session.LONDON

        # Start of overlap (13:00 UTC)
        overlap_start = dt.datetime(2026, 4, 2, 13, 0, tzinfo=dt.timezone.utc)
        info_start = filter_obj.check_session(overlap_start)
        assert info_start.session == Session.OVERLAP_LON_NY

        # End of overlap (15:59 UTC)
        overlap_end = dt.datetime(2026, 4, 2, 15, 59, tzinfo=dt.timezone.utc)
        info_end = filter_obj.check_session(overlap_end)
        assert info_end.session == Session.OVERLAP_LON_NY

        # Just after overlap ends (16:00 UTC)
        after_overlap = dt.datetime(2026, 4, 2, 16, 0, tzinfo=dt.timezone.utc)
        info_after = filter_obj.check_session(after_overlap)
        assert info_after.session == Session.NEW_YORK


class TestSessionAwareConfig:
    """Test session-aware configuration validation."""

    def test_default_config(self):
        """Test default configuration values."""
        config = SessionAwareConfig()

        assert not config.allow_overlap_only
        assert config.allow_premium_sessions
        assert config.allow_asian_session
        assert not config.block_transition_periods
        assert config.minimum_quality == SessionQuality.ACCEPTABLE

    def test_restrictive_config(self):
        """Test highly restrictive configuration."""
        config = SessionAwareConfig(
            allow_overlap_only=True,
            minimum_quality=SessionQuality.PREMIUM,
            allow_asian_session=False,
        )

        filter_obj = SessionAwareFilter(config)

        # Only overlap should be allowed
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        assert filter_obj.is_trading_allowed(overlap_time)

        # London should be blocked (not premium)
        london_time = dt.datetime(2026, 4, 2, 10, 0, tzinfo=dt.timezone.utc)
        assert not filter_obj.is_trading_allowed(london_time)


class TestIntegrationWithSessionCalendar:
    """Test integration with the session calendar module."""

    def test_consistency_with_calendar(self):
        """Test that filter results are consistent with session calendar."""
        filter_obj = SessionAwareFilter()

        # Test several times to ensure consistency
        test_times = [dt.datetime(2026, 4, 2, hour, 0, tzinfo=dt.timezone.utc) for hour in range(24)]

        for test_time in test_times:
            info = filter_obj.check_session(test_time)
            calendar_session = filter_obj._calendar.get_session(test_time.hour)

            if not filter_obj._calendar.is_weekend(test_time.timestamp()):
                # On trading days, sessions should match
                assert info.session == calendar_session
            else:
                # On weekends, session should be None and quality should be CLOSED
                assert info.session is None
                assert info.quality == SessionQuality.CLOSED

    def test_weekend_consistency(self):
        """Test that weekend detection is consistent between filter and calendar."""
        filter_obj = SessionAwareFilter()

        # Use a known Saturday (Jan 4, 2025 is a Saturday)
        saturday = dt.datetime(2025, 1, 4, 12, 0, tzinfo=dt.timezone.utc)
        # Sunday is Jan 5, 2025
        sunday = dt.datetime(2025, 1, 5, 12, 0, tzinfo=dt.timezone.utc)

        for weekend_time in [saturday, sunday]:
            filter_info = filter_obj.check_session(weekend_time)
            calendar_weekend = filter_obj._calendar.is_weekend(weekend_time.timestamp())

            assert calendar_weekend  # Calendar should detect weekend
            assert filter_info.quality == SessionQuality.CLOSED  # Filter should block
            assert not filter_obj.is_trading_allowed(weekend_time)  # Trading blocked
