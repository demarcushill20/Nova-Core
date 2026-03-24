"""Tests for FTMO compliance S6: WeekendAutoCloser, NewsCalendarBlocker,
GapTradingPreventer, and BestDayRuleTracker.

Covers all 4 new compliance components added to novatrade.risk.ftmo_compliance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from novatrade.risk.ftmo_compliance import (
    BestDayRuleTracker,
    GapTradingPreventer,
    NewsCalendarBlocker,
    WeekendAutoCloser,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    """Build a UTC timestamp from components."""
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# WeekendAutoCloser
# ---------------------------------------------------------------------------


class TestWeekendAutoCloser:
    """Tests for WeekendAutoCloser.

    March 2026 dates are in US EDT (DST started March 8), so
    forex market close is at 21:00 UTC (NY 5:00 PM EDT).
    January 2026 dates are in US EST, so close is at 22:00 UTC.
    """

    def test_friday_before_close_window_edt(self):
        """20:54 UTC on Friday (EDT) — not yet in the 5-min close buffer."""
        closer = WeekendAutoCloser()
        # 2026-03-27 is a Friday, EDT → close at 21:00 UTC
        ts = _ts(2026, 3, 27, 20, 54)
        assert closer.should_close_positions(ts) is False

    def test_friday_in_close_window_edt(self):
        """20:55 UTC on Friday (EDT) — in the 5-min close buffer (21:00 - 5 = 20:55)."""
        closer = WeekendAutoCloser()
        ts = _ts(2026, 3, 27, 20, 55)
        assert closer.should_close_positions(ts) is True

    def test_friday_before_close_window_est(self):
        """21:54 UTC on Friday (EST) — not yet in the 5-min close buffer."""
        closer = WeekendAutoCloser()
        # 2026-01-09 is a Friday, EST → close at 22:00 UTC
        ts = _ts(2026, 1, 9, 21, 54)
        assert closer.should_close_positions(ts) is False

    def test_friday_in_close_window_est(self):
        """21:55 UTC on Friday (EST) — in the 5-min close buffer (22:00 - 5 = 21:55)."""
        closer = WeekendAutoCloser()
        ts = _ts(2026, 1, 9, 21, 55)
        assert closer.should_close_positions(ts) is True

    def test_saturday(self):
        """All day Saturday — is_weekend should be True."""
        closer = WeekendAutoCloser()
        # 2026-03-28 is a Saturday
        ts = _ts(2026, 3, 28, 12, 0)
        assert closer.is_weekend(ts) is True

    def test_sunday_before_open_edt(self):
        """20:59 UTC on Sunday (EDT) — still weekend (market opens 21:00)."""
        closer = WeekendAutoCloser()
        # 2026-03-29 is a Sunday, EDT → open at 21:00 UTC
        ts = _ts(2026, 3, 29, 20, 59)
        assert closer.is_weekend(ts) is True

    def test_sunday_after_open_edt(self):
        """21:01 UTC on Sunday (EDT) — market is open, not weekend."""
        closer = WeekendAutoCloser()
        ts = _ts(2026, 3, 29, 21, 1)
        assert closer.is_weekend(ts) is False

    def test_sunday_before_open_est(self):
        """21:59 UTC on Sunday (EST) — still weekend (market opens 22:00)."""
        closer = WeekendAutoCloser()
        # 2026-01-11 is a Sunday, EST → open at 22:00 UTC
        ts = _ts(2026, 1, 11, 21, 59)
        assert closer.is_weekend(ts) is True

    def test_sunday_after_open_est(self):
        """22:01 UTC on Sunday (EST) — market is open, not weekend."""
        closer = WeekendAutoCloser()
        ts = _ts(2026, 1, 11, 22, 1)
        assert closer.is_weekend(ts) is False

    def test_weekday(self):
        """Tuesday noon — neither should_close nor is_weekend."""
        closer = WeekendAutoCloser()
        # 2026-03-24 is a Tuesday
        ts = _ts(2026, 3, 24, 12, 0)
        assert closer.should_close_positions(ts) is False
        assert closer.is_weekend(ts) is False

    def test_friday_exactly_at_market_close_edt(self):
        """21:00 UTC Friday (EDT) — is_weekend is True."""
        closer = WeekendAutoCloser()
        ts = _ts(2026, 3, 27, 21, 0)
        assert closer.is_weekend(ts) is True
        assert closer.should_close_positions(ts) is True

    def test_friday_exactly_at_market_close_est(self):
        """22:00 UTC Friday (EST) — is_weekend is True."""
        closer = WeekendAutoCloser()
        ts = _ts(2026, 1, 9, 22, 0)
        assert closer.is_weekend(ts) is True
        assert closer.should_close_positions(ts) is True

    def test_record_close_attempt(self):
        """Verify close attempt timestamp tracking."""
        closer = WeekendAutoCloser()
        assert closer.last_close_attempt_ts == 0.0
        ts = _ts(2026, 3, 27, 20, 56)
        closer.record_close_attempt(ts)
        assert closer.last_close_attempt_ts == ts

    def test_market_close_hour_utc_edt(self):
        """During EDT, market close is 21:00 UTC."""
        # 2026-06-15 is well within EDT
        ts = _ts(2026, 6, 15, 12, 0)
        assert WeekendAutoCloser._market_close_hour_utc(ts) == 21

    def test_market_close_hour_utc_est(self):
        """During EST, market close is 22:00 UTC."""
        # 2026-01-15 is well within EST
        ts = _ts(2026, 1, 15, 12, 0)
        assert WeekendAutoCloser._market_close_hour_utc(ts) == 22


# ---------------------------------------------------------------------------
# NewsCalendarBlocker
# ---------------------------------------------------------------------------


class TestNewsCalendarBlocker:
    """Tests for NewsCalendarBlocker."""

    def test_nfp_blocked(self):
        """13:20 UTC on NFP date — within 15-min blackout of 13:30."""
        blocker = NewsCalendarBlocker()
        # 2026-03-06 is an NFP date
        ts = _ts(2026, 3, 6, 13, 20)
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        assert blocked is True
        assert "NFP" in reason
        assert "10 minutes" in reason

    def test_nfp_not_blocked_outside_window(self):
        """14:00 UTC on NFP date — 30 minutes after, outside 15-min window."""
        blocker = NewsCalendarBlocker()
        ts = _ts(2026, 3, 6, 14, 0)
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        assert blocked is False

    def test_fomc_blocked(self):
        """18:50 UTC on FOMC date — within 15-min blackout of 19:00."""
        blocker = NewsCalendarBlocker()
        # 2026-03-18 is an FOMC date
        ts = _ts(2026, 3, 18, 18, 50)
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        assert blocked is True
        assert "FOMC" in reason
        assert "10 minutes" in reason

    def test_non_event_day(self):
        """Random weekday with no events — not blocked."""
        blocker = NewsCalendarBlocker()
        # 2026-03-24 is a Tuesday with no high-impact events
        ts = _ts(2026, 3, 24, 14, 0)
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        assert blocked is False
        assert reason == ""

    def test_currency_mismatch(self):
        """NFP (USD event) should NOT block EURJPY (no USD in symbol)."""
        blocker = NewsCalendarBlocker()
        ts = _ts(2026, 3, 6, 13, 25)
        blocked, reason = blocker.is_blocked(ts, "EURJPY", blackout_minutes=15)
        assert blocked is False

    def test_nfp_blocks_gbpusd(self):
        """NFP (USD event) blocks GBPUSD since it contains USD."""
        blocker = NewsCalendarBlocker()
        ts = _ts(2026, 3, 6, 13, 25)
        blocked, reason = blocker.is_blocked(ts, "GBPUSD", blackout_minutes=15)
        assert blocked is True
        assert "NFP" in reason

    def test_ecb_blocks_eurusd(self):
        """ECB (EUR event) blocks EURUSD since it contains EUR."""
        blocker = NewsCalendarBlocker()
        # 2026-03-05 is an ECB date, 13:15 UTC
        ts = _ts(2026, 3, 5, 13, 10)
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        assert blocked is True
        assert "ECB" in reason

    def test_ecb_does_not_block_usdjpy(self):
        """ECB (EUR event) does NOT block USDJPY (no EUR in symbol)."""
        blocker = NewsCalendarBlocker()
        ts = _ts(2026, 3, 5, 13, 10)
        blocked, reason = blocker.is_blocked(ts, "USDJPY", blackout_minutes=15)
        assert blocked is False

    def test_custom_blackout_window(self):
        """Wider blackout window catches events further out."""
        blocker = NewsCalendarBlocker()
        # 25 minutes before NFP at 13:30 — outside 15-min window, inside 30-min
        ts = _ts(2026, 3, 6, 13, 5)
        blocked_15, _ = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        blocked_30, _ = blocker.is_blocked(ts, "EURUSD", blackout_minutes=30)
        assert blocked_15 is False
        assert blocked_30 is True

    def test_after_event_within_window(self):
        """5 minutes after NFP — still within the 15-min post-event window."""
        blocker = NewsCalendarBlocker()
        ts = _ts(2026, 3, 6, 13, 35)
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        assert blocked is True
        assert "5 minutes ago" in reason


# ---------------------------------------------------------------------------
# GapTradingPreventer
# ---------------------------------------------------------------------------


class TestGapTradingPreventer:
    """Tests for GapTradingPreventer."""

    def test_normal_spread_allowed(self):
        """1.0 pip vs 0.8 avg — ratio 1.25x, well under 3.0x limit."""
        preventer = GapTradingPreventer()
        allowed, reason = preventer.check(1.0, 0.8)
        assert allowed is True
        assert "1.25x" in reason

    def test_gap_spread_blocked(self):
        """3.0 pip vs 0.8 avg — ratio 3.75x, exceeds 3.0x limit."""
        preventer = GapTradingPreventer()
        allowed, reason = preventer.check(3.0, 0.8)
        assert allowed is False
        assert "3.75x" in reason

    def test_exactly_at_threshold(self):
        """2.4 pip vs 0.8 avg — ratio exactly 3.0x, should be allowed."""
        preventer = GapTradingPreventer()
        allowed, reason = preventer.check(2.4, 0.8)
        assert allowed is True

    def test_zero_avg_spread_skips(self):
        """Zero average spread — check is skipped."""
        preventer = GapTradingPreventer()
        allowed, reason = preventer.check(1.0, 0.0)
        assert allowed is True
        assert "skipped" in reason

    def test_custom_multiplier(self):
        """Custom multiplier of 2.0x."""
        preventer = GapTradingPreventer()
        # 1.5 / 0.8 = 1.875 — under 2.0x
        allowed, _ = preventer.check(1.5, 0.8, multiplier=2.0)
        assert allowed is True
        # 1.7 / 0.8 = 2.125 — over 2.0x
        allowed, _ = preventer.check(1.7, 0.8, multiplier=2.0)
        assert allowed is False


# ---------------------------------------------------------------------------
# BestDayRuleTracker
# ---------------------------------------------------------------------------


class TestBestDayRuleTracker:
    """Tests for BestDayRuleTracker."""

    def test_no_trades_is_ok(self):
        """Empty tracker — no trades means ok."""
        tracker = BestDayRuleTracker()
        ok, reason = tracker.check()
        assert ok is True
        assert "no trades" in reason

    def test_balanced_days(self):
        """5 days with similar P&L — each is ~20% of total, under 30% limit."""
        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-20", 100.0)
        tracker.record_daily_pnl("2026-03-21", 110.0)
        tracker.record_daily_pnl("2026-03-22", 90.0)
        tracker.record_daily_pnl("2026-03-23", 105.0)
        tracker.record_daily_pnl("2026-03-24", 95.0)
        ok, reason = tracker.check(max_pct=0.30)
        assert ok is True

    def test_single_dominant_day(self):
        """One day is ~50% of total profit — exceeds 30% limit."""
        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-20", 500.0)
        tracker.record_daily_pnl("2026-03-21", 100.0)
        tracker.record_daily_pnl("2026-03-22", 100.0)
        tracker.record_daily_pnl("2026-03-23", 100.0)
        tracker.record_daily_pnl("2026-03-24", 200.0)
        ok, reason = tracker.check(max_pct=0.30)
        assert ok is False
        assert "2026-03-20" in reason
        assert "50.0%" in reason

    def test_negative_days_excluded(self):
        """Negative P&L days don't count toward total profit."""
        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-20", 300.0)
        tracker.record_daily_pnl("2026-03-21", -200.0)
        tracker.record_daily_pnl("2026-03-22", 100.0)
        tracker.record_daily_pnl("2026-03-23", 100.0)
        # Total profit = 300 + 100 + 100 = 500 (ignoring -200)
        # Best day = 300/500 = 60% > 30%
        ok, reason = tracker.check(max_pct=0.30)
        assert ok is False

    def test_get_stats(self):
        """Verify stats dict contents."""
        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-20", 200.0)
        tracker.record_daily_pnl("2026-03-21", 100.0)
        tracker.record_daily_pnl("2026-03-22", -50.0)

        stats = tracker.get_stats()
        assert stats["best_day_pnl"] == 200.0
        assert stats["total_profit"] == 300.0  # 200 + 100 (excluding -50)
        assert abs(stats["best_day_pct"] - 200.0 / 300.0) < 0.001
        assert stats["best_day_date"] == "2026-03-20"
        assert stats["days_traded"] == 3
        assert "2026-03-22" in stats["daily_breakdown"]
        assert stats["daily_breakdown"]["2026-03-22"] == -50.0

    def test_get_stats_empty(self):
        """Stats with no trades."""
        tracker = BestDayRuleTracker()
        stats = tracker.get_stats()
        assert stats["best_day_pnl"] == 0.0
        assert stats["total_profit"] == 0.0
        assert stats["best_day_pct"] == 0.0
        assert stats["best_day_date"] == ""
        assert stats["days_traded"] == 0

    def test_get_stats_all_negative(self):
        """Stats when all days are negative."""
        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-20", -100.0)
        tracker.record_daily_pnl("2026-03-21", -50.0)
        stats = tracker.get_stats()
        assert stats["best_day_pnl"] == 0.0
        assert stats["total_profit"] == 0.0
        ok, reason = tracker.check()
        assert ok is True
        assert "no profitable days" in reason

    def test_record_adds_to_existing_day(self):
        """Multiple records on the same day accumulate."""
        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-20", 50.0)
        tracker.record_daily_pnl("2026-03-20", 75.0)
        stats = tracker.get_stats()
        assert stats["daily_breakdown"]["2026-03-20"] == 125.0
