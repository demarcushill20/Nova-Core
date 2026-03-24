"""DST simulation tests for NovaTrade FTMO compliance and bar aggregation.

Verifies that all time-sensitive NovaTrade components handle EU DST transitions
correctly using Europe/Prague (CET/CEST) timezone:

    EU Spring Forward 2026: March 29, 02:00 CET -> 03:00 CEST (01:00 UTC)
    EU Fall Back 2026:      October 25, 03:00 CEST -> 02:00 CET (01:00 UTC)

Components under test:
    - FtmoDailyLossTracker: daily loss day boundary uses Prague midnight
    - ServerRequestCounter: request counter resets at Prague midnight
    - TradingDaysTracker: unique trading dates use Prague calendar day
    - BarAggregator / bar_boundary(): UTC-epoch-aligned bar boundaries
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from novatrade.data.bar_aggregator import BarAggregator, bar_boundary
from novatrade.data.tick import Tick
from novatrade.risk.ftmo_compliance import (
    FtmoDailyLossTracker,
    ServerRequestCounter,
    TradingDaysTracker,
)

PRAGUE = ZoneInfo("Europe/Prague")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build a timezone-aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _prague_date(dt_utc: datetime) -> str:
    """Convert UTC datetime to Prague calendar date string."""
    return dt_utc.astimezone(PRAGUE).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# TestDailyLossTrackerDST
# ---------------------------------------------------------------------------


class TestDailyLossTrackerDST:
    """FtmoDailyLossTracker across EU DST transitions (Europe/Prague)."""

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_spring_forward_same_day_no_reset(self, mock_dt):
        """Trades at 01:59 UTC and 02:01 UTC on March 29 are both Prague Mar 29.

        Spring-forward fires at 01:00 UTC (02:00 CET -> 03:00 CEST).
        01:59 UTC = 03:59 CEST (Prague) = March 29 (already in CEST)
        02:01 UTC = 04:01 CEST (Prague) = March 29

        The Prague calendar date does NOT change, so daily loss must NOT reset.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        t = FtmoDailyLossTracker(initial_account_size=100_000.0)

        # Initialize at 01:59 UTC Mar 29 (Prague 02:59 CET, still Mar 29)
        mock_dt.now.return_value = _utc(2026, 3, 29, 1, 59)
        t.initialize(100_000.0, 100_000.0)
        ref_before = t.day_reference
        day_before = t._day_key

        # Record a loss
        result1 = t.check(99_000.0, 98_000.0)
        assert result1.passed  # $2,000 loss < $4,000 buffer

        # Advance to 02:01 UTC Mar 29 (Prague 04:01 CEST, still Mar 29)
        mock_dt.now.return_value = _utc(2026, 3, 29, 2, 1)
        result2 = t.check(99_000.0, 97_500.0)

        # Day should NOT have changed — same Prague date
        assert t._day_key == "2026-03-29"
        assert t._day_key == day_before
        assert t.day_reference == ref_before
        # Loss should accumulate, not reset
        assert result2.passed  # $2,500 loss < $4,000 buffer

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_spring_forward_day_boundary(self, mock_dt):
        """Day resets correctly at the new CEST midnight after spring-forward.

        Last CET midnight:  23:00 UTC Mar 28 = Prague 00:00 Mar 29 (CET, UTC+1)
        First CEST midnight: 22:00 UTC Mar 29 = Prague 00:00 Mar 30 (CEST, UTC+2)

        The daily loss tracker should reset when crossing from Mar 29 to Mar 30.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        t = FtmoDailyLossTracker(initial_account_size=100_000.0)

        # Initialize at 23:00 UTC Mar 28 (Prague 00:00 Mar 29, CET)
        mock_dt.now.return_value = _utc(2026, 3, 28, 23, 0)
        t.initialize(100_000.0, 100_000.0)
        assert t._day_key == "2026-03-29"

        # Accumulate some loss during Mar 29
        mock_dt.now.return_value = _utc(2026, 3, 29, 12, 0)
        result = t.check(98_000.0, 97_000.0)
        assert result.passed  # $3,000 loss
        assert t._day_key == "2026-03-29"

        # Cross into Mar 30 at 22:00 UTC Mar 29 (Prague 00:00 Mar 30, CEST)
        mock_dt.now.return_value = _utc(2026, 3, 29, 22, 0)
        result2 = t.check(97_000.0, 97_000.0)
        assert t._day_key == "2026-03-30"
        # Reference should have reset to new day's max(balance, equity)
        assert t.day_reference == 97_000.0
        assert result2.passed  # no loss relative to new reference

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_fall_back_same_day_no_reset(self, mock_dt):
        """October 25 fall-back: trades before and after are same Prague day.

        00:30 UTC Oct 25 = 02:30 CEST (Prague) = Oct 25 (before clocks go back)
        01:30 UTC Oct 25 = 02:30 CET (Prague) = Oct 25 (after clocks go back)

        Both are Prague October 25 — no reset should occur.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        t = FtmoDailyLossTracker(initial_account_size=100_000.0)

        # Initialize at 00:30 UTC Oct 25 (Prague 02:30 CEST, Oct 25)
        mock_dt.now.return_value = _utc(2026, 10, 25, 0, 30)
        t.initialize(100_000.0, 100_000.0)
        day_before = t._day_key
        assert day_before == "2026-10-25"

        # Record a loss
        t.check(99_000.0, 98_500.0)

        # After fall-back: 01:30 UTC Oct 25 (Prague 02:30 CET, still Oct 25)
        mock_dt.now.return_value = _utc(2026, 10, 25, 1, 30)
        result = t.check(99_000.0, 98_000.0)

        assert t._day_key == "2026-10-25"
        assert t._day_key == day_before  # still Oct 25
        assert result.passed  # $2,000 loss < $4,000 buffer

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_fall_back_day_boundary(self, mock_dt):
        """Day resets correctly at Prague midnight during fall-back.

        After fall-back, Prague midnight = 23:00 UTC (CET = UTC+1).
        Last CEST midnight: 22:00 UTC Oct 24 = Prague 00:00 Oct 25
        First CET midnight: 23:00 UTC Oct 25 = Prague 00:00 Oct 26

        The daily loss tracker should reset when crossing from Oct 25 to Oct 26.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        t = FtmoDailyLossTracker(initial_account_size=100_000.0)

        # Initialize during Oct 25 (Prague time)
        mock_dt.now.return_value = _utc(2026, 10, 25, 12, 0)
        t.initialize(100_000.0, 100_000.0)
        assert t._day_key == "2026-10-25"

        # Accumulate loss
        mock_dt.now.return_value = _utc(2026, 10, 25, 20, 0)
        t.check(98_000.0, 97_000.0)

        # Cross into Oct 26 at 23:00 UTC Oct 25 (Prague 00:00 Oct 26, CET)
        mock_dt.now.return_value = _utc(2026, 10, 25, 23, 0)
        result = t.check(97_000.0, 97_000.0)
        assert t._day_key == "2026-10-26"
        assert t.day_reference == 97_000.0  # reset to new day reference
        assert result.passed

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_cumulative_loss_through_dst_transition(self, mock_dt):
        """Losses accumulate correctly through DST spring-forward without spurious reset.

        Three trades on Prague March 29:
        - 01:00 UTC (03:00 CEST, post-transition): -$1,000
        - 02:30 UTC (04:30 CEST): -$1,500 more (cumulative -$2,500)
        - 10:00 UTC (12:00 CEST): -$500 more (cumulative -$3,000)

        All on the same Prague day, so losses must accumulate from the same reference.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        t = FtmoDailyLossTracker(initial_account_size=100_000.0)

        # Initialize early on Mar 29 (Prague time)
        mock_dt.now.return_value = _utc(2026, 3, 29, 1, 0)
        t.initialize(100_000.0, 100_000.0)
        ref = t.day_reference
        assert ref == 100_000.0

        # Trade 1: $1,000 loss (before spring-forward)
        result1 = t.check(99_500.0, 99_000.0)
        assert result1.passed
        assert t.day_reference == ref  # unchanged

        # Trade 2: cumulative $2,500 loss (after spring-forward, same Prague day)
        mock_dt.now.return_value = _utc(2026, 3, 29, 2, 30)
        result2 = t.check(98_000.0, 97_500.0)
        assert result2.passed  # $2,500 < $4,000 buffer
        assert t.day_reference == ref  # still same reference

        # Trade 3: cumulative $3,000 loss (well into CEST, same Prague day)
        mock_dt.now.return_value = _utc(2026, 3, 29, 10, 0)
        result3 = t.check(97_500.0, 97_000.0)
        assert result3.passed  # $3,000 < $4,000 buffer
        assert t.day_reference == ref  # reference never reset


# ---------------------------------------------------------------------------
# TestServerRequestCounterDST
# ---------------------------------------------------------------------------


class TestServerRequestCounterDST:
    """ServerRequestCounter across EU DST transitions (Europe/Prague)."""

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_spring_forward_counter_reset(self, mock_dt):
        """Counter at 21:59 UTC Mar 29 is Prague 23:59 Mar 29 (same day).
        Counter at 22:00 UTC Mar 29 is Prague 00:00 Mar 30 (new day -> resets).

        After spring-forward, Prague midnight = 22:00 UTC (CEST = UTC+2).
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        counter = ServerRequestCounter()

        # Record requests at 21:59 UTC Mar 29 (Prague 23:59 Mar 29 CEST)
        mock_dt.now.return_value = _utc(2026, 3, 29, 21, 59)
        counter.record("order_open")
        counter.record("modify_sl")
        counter.record("close_position")
        assert counter._count == 3
        assert counter._day_key == "2026-03-29"

        # Cross to 22:00 UTC Mar 29 (Prague 00:00 Mar 30 CEST) -> new day
        mock_dt.now.return_value = _utc(2026, 3, 29, 22, 0)
        counter.record("order_open")
        assert counter._day_key == "2026-03-30"
        assert counter._count == 1  # reset to 0, then incremented by record()

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_fall_back_counter_no_double_reset(self, mock_dt):
        """Counter does not reset twice during the fall-back ambiguous hour.

        On Oct 25, clocks go back at 03:00 CEST -> 02:00 CET (01:00 UTC).
        The period 00:00-01:00 UTC is Prague 02:00-03:00 CEST (first pass).
        The period 01:00-02:00 UTC is Prague 02:00-03:00 CET (second pass).

        Both map to Prague Oct 25, so the counter should stay on the same day
        and never reset during this ambiguous hour.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        counter = ServerRequestCounter()

        # Start at 00:30 UTC Oct 25 (Prague 02:30 CEST, Oct 25)
        mock_dt.now.return_value = _utc(2026, 10, 25, 0, 30)
        counter.record("order_open")
        counter.record("modify_sl")
        assert counter._count == 2
        day_key = counter._day_key
        assert day_key == "2026-10-25"

        # 00:59 UTC (Prague 02:59 CEST, still Oct 25 first pass)
        mock_dt.now.return_value = _utc(2026, 10, 25, 0, 59)
        counter.record("close_position")
        assert counter._count == 3
        assert counter._day_key == day_key

        # 01:30 UTC (Prague 02:30 CET, Oct 25 second pass of ambiguous hour)
        mock_dt.now.return_value = _utc(2026, 10, 25, 1, 30)
        counter.record("pending_create")
        assert counter._count == 4  # accumulated, no reset
        assert counter._day_key == day_key

        # 02:00 UTC (Prague 03:00 CET, still Oct 25 — no new day)
        mock_dt.now.return_value = _utc(2026, 10, 25, 2, 0)
        counter.record("order_open")
        assert counter._count == 5
        assert counter._day_key == day_key


# ---------------------------------------------------------------------------
# TestTradingDaysTrackerDST
# ---------------------------------------------------------------------------


class TestTradingDaysTrackerDST:
    """TradingDaysTracker across EU DST transitions (Europe/Prague)."""

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_spring_forward_distinct_days(self, mock_dt):
        """Trades at 23:00 UTC Mar 28 and 22:00 UTC Mar 29 record 2 distinct days.

        23:00 UTC Mar 28 = Prague 00:00 Mar 29 (CET, UTC+1)
        22:00 UTC Mar 29 = Prague 00:00 Mar 30 (CEST, UTC+2)

        These are two different Prague calendar days.
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        tracker = TradingDaysTracker()

        # Trade at 23:00 UTC Mar 28 (Prague Mar 29)
        mock_dt.now.return_value = _utc(2026, 3, 28, 23, 0)
        tracker.record_trade_day()
        assert tracker.days_traded == 1
        assert "2026-03-29" in tracker.trading_dates

        # Trade at 22:00 UTC Mar 29 (Prague Mar 30)
        mock_dt.now.return_value = _utc(2026, 3, 29, 22, 0)
        tracker.record_trade_day()
        assert tracker.days_traded == 2
        assert "2026-03-30" in tracker.trading_dates

    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_fall_back_no_duplicate_day(self, mock_dt):
        """Trades during the repeated Prague 02:00-03:00 hour don't create duplicates.

        Oct 25 fall-back: 03:00 CEST -> 02:00 CET at 01:00 UTC.
        - 00:30 UTC = Prague 02:30 CEST Oct 25 (first pass)
        - 01:30 UTC = Prague 02:30 CET Oct 25 (second pass, after fold)

        Both resolve to the same Prague date (2026-10-25).
        """
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        tracker = TradingDaysTracker()

        # First pass: 00:30 UTC (Prague 02:30 CEST Oct 25)
        mock_dt.now.return_value = _utc(2026, 10, 25, 0, 30)
        tracker.record_trade_day()
        assert tracker.days_traded == 1

        # Second pass (ambiguous hour): 01:30 UTC (Prague 02:30 CET Oct 25)
        mock_dt.now.return_value = _utc(2026, 10, 25, 1, 30)
        tracker.record_trade_day()
        assert tracker.days_traded == 1  # still just 1 day

        # Verify it is the correct date
        assert tracker.trading_dates == ["2026-10-25"]


# ---------------------------------------------------------------------------
# TestBarAggregatorDST
# ---------------------------------------------------------------------------


class TestBarAggregatorDST:
    """BarAggregator and bar_boundary() across EU DST transitions.

    Bar boundaries are UTC-epoch-aligned (floor(ts / period) * period) and
    must NOT shift during DST transitions.
    """

    def test_h1_bar_boundary_invariant_across_dst(self):
        """H1 bar boundaries are UTC-based and should NOT shift during DST.

        Spring-forward moment: 01:00 UTC on March 29, 2026.
        Bar boundaries for H1 (3600s) should be the same whether or not DST
        is in effect, because they are pure UTC epoch arithmetic.
        """
        h1 = 3600

        # Just before spring-forward: 00:59 UTC Mar 29
        ts_before = _utc(2026, 3, 29, 0, 59).timestamp()
        boundary_before = bar_boundary(ts_before, h1)

        # Just after spring-forward: 01:01 UTC Mar 29
        ts_after = _utc(2026, 3, 29, 1, 1).timestamp()
        boundary_after = bar_boundary(ts_after, h1)

        # 00:59 UTC belongs to the 00:00 UTC bar
        assert boundary_before == _utc(2026, 3, 29, 0, 0).timestamp()
        # 01:01 UTC belongs to the 01:00 UTC bar
        assert boundary_after == _utc(2026, 3, 29, 1, 0).timestamp()

        # Boundaries are exactly 1 hour apart
        assert boundary_after - boundary_before == h1

        # Fall-back: same test at 01:00 UTC Oct 25
        ts_fb_before = _utc(2026, 10, 25, 0, 59).timestamp()
        ts_fb_after = _utc(2026, 10, 25, 1, 1).timestamp()
        fb_boundary_before = bar_boundary(ts_fb_before, h1)
        fb_boundary_after = bar_boundary(ts_fb_after, h1)

        assert fb_boundary_before == _utc(2026, 10, 25, 0, 0).timestamp()
        assert fb_boundary_after == _utc(2026, 10, 25, 1, 0).timestamp()
        assert fb_boundary_after - fb_boundary_before == h1

    def test_tick_continuity_through_dst(self):
        """Ticks flowing through DST transition are properly aggregated into H1 bars.

        Sends ticks across the spring-forward boundary (01:00 UTC Mar 29) and
        verifies that bars are emitted without gaps — the 00:00 UTC bar closes
        and the 01:00 UTC bar opens normally.
        """
        agg = BarAggregator(timeframes=["H1"])

        # Base timestamp: 00:00 UTC Mar 29 2026
        base_ts = _utc(2026, 3, 29, 0, 0).timestamp()

        # Send ticks for the 00:00 UTC bar (before DST transition at 01:00 UTC)
        for i in range(5):
            ts = base_ts + i * 600  # every 10 minutes: 00:00, 00:10, ... 00:40
            tick = Tick(
                symbol="EURUSD",
                timestamp=ts,
                bid=1.1000 + i * 0.0001,
                ask=1.1002 + i * 0.0001,
            )
            completed = agg.on_tick(tick)
            assert completed == []  # bar not yet closed

        # Send first tick in the 01:00 UTC bar (DST transition happened at 01:00 UTC)
        ts_cross = base_ts + 3600  # exactly 01:00:00 UTC
        tick_cross = Tick(
            symbol="EURUSD",
            timestamp=ts_cross,
            bid=1.1010,
            ask=1.1012,
        )
        completed = agg.on_tick(tick_cross)

        # The 00:00 UTC bar should have been emitted
        assert len(completed) == 1
        tf, candle = completed[0]
        assert tf == "H1"
        assert candle.timestamp == base_ts  # bar open at 00:00 UTC
        assert candle.symbol == "EURUSD"

        # Continue sending ticks in the 01:00 UTC bar
        for i in range(1, 4):
            ts = base_ts + 3600 + i * 600  # 01:10, 01:20, 01:30
            tick = Tick(
                symbol="EURUSD",
                timestamp=ts,
                bid=1.1010 + i * 0.0001,
                ask=1.1012 + i * 0.0001,
            )
            completed = agg.on_tick(tick)
            assert completed == []

        # Send first tick in 02:00 UTC bar to close the 01:00 bar
        ts_close2 = base_ts + 7200  # 02:00:00 UTC
        tick_close2 = Tick(
            symbol="EURUSD",
            timestamp=ts_close2,
            bid=1.1020,
            ask=1.1022,
        )
        completed = agg.on_tick(tick_close2)

        # The 01:00 UTC bar should have been emitted
        assert len(completed) == 1
        tf2, candle2 = completed[0]
        assert tf2 == "H1"
        assert candle2.timestamp == base_ts + 3600  # bar open at 01:00 UTC

        # Verify no gaps: the two bars are exactly 1 hour apart
        assert candle2.timestamp - candle.timestamp == 3600

        # Verify total bars emitted
        assert agg.bars_emitted == 2
