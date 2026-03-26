"""Tests for FeedHealthSupervisor — Phase 3 of Full-Python NovaTrade.

Covers all 8 failure domains:
    1. Staleness
    2. Clock drift
    3. Spread widening (absolute + relative)
    4. Disconnection
    5. Duplicate signal suppression
    6. Market closed (weekend detection)
    7. Not subscribed
    8. Stale timestamps
"""

from __future__ import annotations

import pytest

from novatrade.data.tick import Tick
from novatrade.monitor.feed_health import (
    FeedHealthConfig,
    FeedHealthSnapshot,
    FeedHealthSupervisor,
    FeedState,
    _is_forex_market_closed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tick(
    symbol: str = "EURUSD",
    bid: float = 1.1000,
    ask: float = 1.1002,
    ts: float = 1_700_000_000.0,
) -> Tick:
    """Build a valid Tick with sensible defaults."""
    return Tick(symbol=symbol, timestamp=ts, bid=bid, ask=ask)


def _make_supervisor(
    config: FeedHealthConfig | None = None,
    clock_time: float = 1_700_000_000.0,
) -> tuple[FeedHealthSupervisor, list[float]]:
    """Create a supervisor with a controllable clock.

    Returns (supervisor, clock_container) where clock_container[0] is the
    current time — mutate it to advance the clock.
    """
    clock_val = [clock_time]
    sup = FeedHealthSupervisor(config=config, clock=lambda: clock_val[0])
    return sup, clock_val


# ---------------------------------------------------------------------------
# FeedState enum
# ---------------------------------------------------------------------------


class TestFeedState:
    def test_all_states_exist(self) -> None:
        expected = {
            "HEALTHY",
            "STALE",
            "CLOCK_DRIFT",
            "SPREAD_WIDE",
            "DISCONNECTED",
            "MARKET_CLOSED",
            "NOT_SUBSCRIBED",
            "STALE_TIMESTAMP",
        }
        assert {s.value for s in FeedState} == expected

    def test_string_values_match_names(self) -> None:
        for state in FeedState:
            assert state.value == state.name


# ---------------------------------------------------------------------------
# FeedHealthConfig
# ---------------------------------------------------------------------------


class TestFeedHealthConfig:
    def test_defaults(self) -> None:
        cfg = FeedHealthConfig()
        assert cfg.max_stale_seconds == 30.0
        assert cfg.max_clock_drift_seconds == 5.0
        assert cfg.max_spread_pips == 5.0
        assert cfg.spread_window == 20
        assert cfg.spread_spike_ratio == 3.0
        assert cfg.signal_dedup_window == 60.0
        assert cfg.max_signals_in_window == 2
        assert cfg.stale_timestamp_threshold == 10.0

    def test_frozen(self) -> None:
        cfg = FeedHealthConfig()
        with pytest.raises(AttributeError):
            cfg.max_stale_seconds = 99.0  # type: ignore[misc]

    def test_custom_values(self) -> None:
        cfg = FeedHealthConfig(max_stale_seconds=10.0, max_spread_pips=3.0)
        assert cfg.max_stale_seconds == 10.0
        assert cfg.max_spread_pips == 3.0


# ---------------------------------------------------------------------------
# FeedHealthSnapshot
# ---------------------------------------------------------------------------


class TestFeedHealthSnapshot:
    def test_to_dict(self) -> None:
        snap = FeedHealthSnapshot(
            symbol="EURUSD",
            state=FeedState.HEALTHY,
            last_tick_age=1.5,
            clock_drift=0.3,
            current_spread_pips=1.234,
            avg_spread_pips=1.1,
            tick_count=42,
            timestamp=1_700_000_000.0,
        )
        d = snap.to_dict()
        assert d["symbol"] == "EURUSD"
        assert d["state"] == "HEALTHY"
        assert d["last_tick_age"] == 1.5
        assert d["tick_count"] == 42

    def test_default_timestamp(self) -> None:
        snap = FeedHealthSnapshot(symbol="X", state=FeedState.STALE)
        assert snap.timestamp > 0


# ---------------------------------------------------------------------------
# Domain 7: NOT_SUBSCRIBED — symbol never received data
# ---------------------------------------------------------------------------


class TestNotSubscribed:
    def test_unknown_symbol_not_tradeable(self) -> None:
        sup, _ = _make_supervisor()
        assert not sup.is_tradeable("GBPUSD")

    def test_unknown_symbol_snapshot_state(self) -> None:
        sup, _ = _make_supervisor()
        snap = sup.get_snapshot("GBPUSD")
        assert snap.state == FeedState.NOT_SUBSCRIBED

    def test_becomes_healthy_after_tick(self) -> None:
        sup, clock = _make_supervisor()
        tick = _tick(ts=clock[0])
        state = sup.on_tick(tick)
        assert state == FeedState.HEALTHY
        assert sup.is_tradeable("EURUSD")


# ---------------------------------------------------------------------------
# Domain 1: STALENESS — no tick within threshold
# ---------------------------------------------------------------------------


class TestStaleness:
    def test_healthy_within_threshold(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 29.0  # Still under 30s
        assert sup.is_tradeable("EURUSD")

    def test_stale_after_threshold(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 31.0  # Over 30s
        snap = sup.get_snapshot("EURUSD")
        assert snap.state == FeedState.STALE
        assert not sup.is_tradeable("EURUSD")

    def test_recovery_on_new_tick(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 31.0
        assert not sup.is_tradeable("EURUSD")
        # New tick arrives
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.HEALTHY
        assert sup.is_tradeable("EURUSD")

    def test_check_staleness_periodic(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.on_tick(_tick(symbol="GBPUSD", ts=clock[0]))
        clock[0] += 31.0
        result = sup.check_staleness()
        assert result["EURUSD"] == FeedState.STALE
        assert result["GBPUSD"] == FeedState.STALE

    def test_custom_stale_threshold(self) -> None:
        cfg = FeedHealthConfig(max_stale_seconds=5.0)
        sup, clock = _make_supervisor(config=cfg)
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 6.0
        assert not sup.is_tradeable("EURUSD")


# ---------------------------------------------------------------------------
# Domain 2: CLOCK_DRIFT — broker vs system time divergence
# ---------------------------------------------------------------------------


class TestClockDrift:
    def test_no_drift(self) -> None:
        sup, clock = _make_supervisor()
        state = sup.on_tick(_tick(ts=clock[0]))  # broker time == system time
        assert state == FeedState.HEALTHY

    def test_drift_detected(self) -> None:
        sup, clock = _make_supervisor()
        # Broker timestamp 6s behind system clock
        state = sup.on_tick(_tick(ts=clock[0] - 6.0))
        assert state == FeedState.CLOCK_DRIFT

    def test_drift_within_tolerance(self) -> None:
        sup, clock = _make_supervisor()
        state = sup.on_tick(_tick(ts=clock[0] - 4.0))
        assert state == FeedState.HEALTHY

    def test_drift_recovery(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0] - 6.0))  # Drifted
        state = sup.on_tick(_tick(ts=clock[0]))  # Normal
        assert state == FeedState.HEALTHY


# ---------------------------------------------------------------------------
# Domain 3: SPREAD_WIDE — absolute or relative threshold
# ---------------------------------------------------------------------------


class TestSpreadWide:
    def test_normal_spread_healthy(self) -> None:
        sup, clock = _make_supervisor()
        # 2 pip spread
        state = sup.on_tick(_tick(bid=1.1000, ask=1.1002, ts=clock[0]))
        assert state == FeedState.HEALTHY

    def test_absolute_spread_exceeds(self) -> None:
        sup, clock = _make_supervisor()
        # 6 pip spread (> default 5.0)
        state = sup.on_tick(_tick(bid=1.1000, ask=1.1006, ts=clock[0]))
        assert state == FeedState.SPREAD_WIDE

    def test_relative_spread_spike(self) -> None:
        cfg = FeedHealthConfig(max_spread_pips=50.0, spread_spike_ratio=3.0)
        sup, clock = _make_supervisor(config=cfg)
        # Feed 10 normal ticks to build average
        for _i in range(10):
            sup.on_tick(_tick(bid=1.1000, ask=1.1001, ts=clock[0]))
            clock[0] += 0.5
        # Spike: 4x the average (1 pip avg, now 4 pips)
        state = sup.on_tick(_tick(bid=1.1000, ask=1.1004, ts=clock[0]))
        assert state == FeedState.SPREAD_WIDE

    def test_spread_within_relative_tolerance(self) -> None:
        cfg = FeedHealthConfig(max_spread_pips=50.0, spread_spike_ratio=3.0)
        sup, clock = _make_supervisor(config=cfg)
        for _i in range(10):
            sup.on_tick(_tick(bid=1.1000, ask=1.1001, ts=clock[0]))
            clock[0] += 0.5
        # 2.5x average — within 3x tolerance
        state = sup.on_tick(_tick(bid=1.1000, ask=1.10025, ts=clock[0]))
        assert state == FeedState.HEALTHY

    def test_jpy_pair_spread_pips(self) -> None:
        sup, clock = _make_supervisor()
        # USDJPY: pip = 0.01, spread = 0.02 → 2 pips (normal)
        state = sup.on_tick(_tick(symbol="USDJPY", bid=150.00, ask=150.02, ts=clock[0]))
        assert state == FeedState.HEALTHY

    def test_jpy_pair_wide_spread(self) -> None:
        sup, clock = _make_supervisor()
        # USDJPY: spread = 0.06 → 6 pips (wide)
        state = sup.on_tick(_tick(symbol="USDJPY", bid=150.00, ask=150.06, ts=clock[0]))
        assert state == FeedState.SPREAD_WIDE


# ---------------------------------------------------------------------------
# Domain 4: DISCONNECTION — adapter signals down
# ---------------------------------------------------------------------------


class TestDisconnection:
    def test_mark_disconnected(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        sup.mark_disconnected("EURUSD")
        assert not sup.is_tradeable("EURUSD")
        snap = sup.get_snapshot("EURUSD")
        assert snap.state == FeedState.DISCONNECTED

    def test_reconnect_restores(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        sup.mark_disconnected("EURUSD")
        sup.mark_reconnected("EURUSD")
        assert sup.is_tradeable("EURUSD")

    def test_tick_clears_disconnected(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        sup.mark_disconnected("EURUSD")
        # New tick arrives — reconnected
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.HEALTHY

    def test_check_staleness_shows_disconnected(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        sup.mark_disconnected("EURUSD")
        result = sup.check_staleness()
        assert result["EURUSD"] == FeedState.DISCONNECTED


# ---------------------------------------------------------------------------
# Domain 5: DUPLICATE SIGNAL SUPPRESSION
# ---------------------------------------------------------------------------


class TestDuplicateSignal:
    def test_first_signal_allowed(self) -> None:
        sup, _ = _make_supervisor()
        assert not sup.is_duplicate_signal("EURUSD", "BUY")

    def test_second_signal_allowed(self) -> None:
        sup, _ = _make_supervisor()
        sup.is_duplicate_signal("EURUSD", "BUY")
        assert not sup.is_duplicate_signal("EURUSD", "BUY")

    def test_third_signal_blocked(self) -> None:
        sup, _ = _make_supervisor()
        sup.is_duplicate_signal("EURUSD", "BUY")
        sup.is_duplicate_signal("EURUSD", "BUY")
        assert sup.is_duplicate_signal("EURUSD", "BUY")

    def test_different_direction_independent(self) -> None:
        sup, _ = _make_supervisor()
        sup.is_duplicate_signal("EURUSD", "BUY")
        sup.is_duplicate_signal("EURUSD", "BUY")
        # SELL is a separate key — not blocked
        assert not sup.is_duplicate_signal("EURUSD", "SELL")

    def test_different_symbol_independent(self) -> None:
        sup, _ = _make_supervisor()
        sup.is_duplicate_signal("EURUSD", "BUY")
        sup.is_duplicate_signal("EURUSD", "BUY")
        assert not sup.is_duplicate_signal("GBPUSD", "BUY")

    def test_window_expiry_resets(self) -> None:
        cfg = FeedHealthConfig(signal_dedup_window=10.0)
        sup, clock = _make_supervisor(config=cfg)
        sup.is_duplicate_signal("EURUSD", "BUY")
        sup.is_duplicate_signal("EURUSD", "BUY")
        assert sup.is_duplicate_signal("EURUSD", "BUY")  # Blocked
        # Advance past window
        clock[0] += 11.0
        assert not sup.is_duplicate_signal("EURUSD", "BUY")  # Allowed again

    def test_custom_max_signals(self) -> None:
        cfg = FeedHealthConfig(max_signals_in_window=5)
        sup, _ = _make_supervisor(config=cfg)
        for _ in range(5):
            assert not sup.is_duplicate_signal("EURUSD", "BUY")
        assert sup.is_duplicate_signal("EURUSD", "BUY")  # 6th blocked


# ---------------------------------------------------------------------------
# Domain 6: MARKET CLOSED — weekend detection
# ---------------------------------------------------------------------------


class TestMarketClosed:
    def test_weekday_open(self) -> None:
        # 2024-11-14 (Thursday) 12:00 UTC
        import datetime

        thu_noon = datetime.datetime(2024, 11, 14, 12, 0, tzinfo=datetime.timezone.utc)
        assert not _is_forex_market_closed(thu_noon.timestamp())

    def test_friday_before_close(self) -> None:
        import datetime

        fri_21 = datetime.datetime(2024, 11, 15, 21, 0, tzinfo=datetime.timezone.utc)
        assert not _is_forex_market_closed(fri_21.timestamp())

    def test_friday_after_close(self) -> None:
        import datetime

        fri_22 = datetime.datetime(2024, 11, 15, 22, 0, tzinfo=datetime.timezone.utc)
        assert _is_forex_market_closed(fri_22.timestamp())

    def test_saturday(self) -> None:
        import datetime

        sat = datetime.datetime(2024, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
        assert _is_forex_market_closed(sat.timestamp())

    def test_sunday_before_open(self) -> None:
        import datetime

        sun_21 = datetime.datetime(2024, 11, 17, 21, 0, tzinfo=datetime.timezone.utc)
        assert _is_forex_market_closed(sun_21.timestamp())

    def test_sunday_after_open(self) -> None:
        import datetime

        sun_22 = datetime.datetime(2024, 11, 17, 22, 0, tzinfo=datetime.timezone.utc)
        assert not _is_forex_market_closed(sun_22.timestamp())

    def test_market_closed_blocks_trading(self) -> None:
        import datetime

        sat_noon = datetime.datetime(2024, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
        sup, clock = _make_supervisor(clock_time=sat_noon.timestamp())
        # Even though we get a tick, market is closed
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.MARKET_CLOSED
        assert not sup.is_tradeable("EURUSD")

    def test_check_staleness_market_closed(self) -> None:
        import datetime

        sat_noon = datetime.datetime(2024, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
        sup, clock = _make_supervisor(clock_time=sat_noon.timestamp())
        sup.on_tick(_tick(ts=clock[0]))
        result = sup.check_staleness()
        assert result["EURUSD"] == FeedState.MARKET_CLOSED


# ---------------------------------------------------------------------------
# Domain 8: STALE_TIMESTAMP — broker time not advancing
# ---------------------------------------------------------------------------


class TestStaleTimestamp:
    def test_normal_timestamps_healthy(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 1.0
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.HEALTHY

    def test_stale_broker_timestamp_via_snapshot(self) -> None:
        """Broker timestamp is old but tick arrived on time (drift was low).

        After time passes, get_snapshot checks broker_age = now - last_broker_ts.
        If >threshold and tick_count > 1 and measured drift was low, STALE_TIMESTAMP fires.
        """
        cfg = FeedHealthConfig(
            stale_timestamp_threshold=10.0,
            max_clock_drift_seconds=5.0,
        )
        sup, clock = _make_supervisor(config=cfg)
        # Two ticks arrive normally (need tick_count > 1)
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 1.0
        sup.on_tick(_tick(ts=clock[0]))
        # Time passes — broker timestamp is now old
        clock[0] += 15.0
        # get_snapshot: age=15 < stale=30. drift=0 (measured at arrival).
        # broker_age = now - last_broker_ts = 15 > threshold=10 → STALE_TIMESTAMP
        snap = sup.get_snapshot("EURUSD")
        assert snap.state == FeedState.STALE_TIMESTAMP

    def test_clock_drift_beats_stale_timestamp(self) -> None:
        """When measured drift at arrival exceeds threshold, CLOCK_DRIFT fires first."""
        cfg = FeedHealthConfig(max_clock_drift_seconds=5.0, stale_timestamp_threshold=10.0)
        sup, clock = _make_supervisor(config=cfg)
        # Tick arrives with 6s drift (broker timestamp 6s behind)
        state = sup.on_tick(_tick(ts=clock[0] - 6.0))
        assert state == FeedState.CLOCK_DRIFT


# ---------------------------------------------------------------------------
# Snapshot & Stats
# ---------------------------------------------------------------------------


class TestSnapshotAndStats:
    def test_snapshot_healthy(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        snap = sup.get_snapshot("EURUSD")
        assert snap.state == FeedState.HEALTHY
        assert snap.symbol == "EURUSD"
        assert snap.tick_count == 1

    def test_snapshot_tick_age(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        clock[0] += 5.0
        snap = sup.get_snapshot("EURUSD")
        assert snap.last_tick_age == pytest.approx(5.0)

    def test_all_snapshots(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.on_tick(_tick(symbol="GBPUSD", ts=clock[0]))
        snaps = sup.get_all_snapshots()
        assert "EURUSD" in snaps
        assert "GBPUSD" in snaps
        assert all(s.state == FeedState.HEALTHY for s in snaps.values())

    def test_stats_aggregate(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.on_tick(_tick(symbol="GBPUSD", ts=clock[0]))
        clock[0] += 31.0  # STALE both
        stats = sup.stats
        assert stats["tracked_symbols"] == 2
        assert stats["unhealthy"] == 2

    def test_tracked_symbols(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.on_tick(_tick(symbol="GBPUSD", ts=clock[0]))
        assert set(sup.tracked_symbols) == {"EURUSD", "GBPUSD"}

    def test_tracked_symbols_excludes_dedup_keys(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.is_duplicate_signal("EURUSD", "BUY")
        # Dedup key "EURUSD:BUY" should not appear
        assert "EURUSD:BUY" not in sup.tracked_symbols
        assert "EURUSD" in sup.tracked_symbols


# ---------------------------------------------------------------------------
# Multi-symbol independence
# ---------------------------------------------------------------------------


class TestMultiSymbol:
    def test_independent_health(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.on_tick(_tick(symbol="GBPUSD", ts=clock[0]))
        clock[0] += 31.0
        # Update only EURUSD
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        assert sup.is_tradeable("EURUSD")
        assert not sup.is_tradeable("GBPUSD")

    def test_disconnect_isolated(self) -> None:
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(symbol="EURUSD", ts=clock[0]))
        sup.on_tick(_tick(symbol="GBPUSD", ts=clock[0]))
        sup.mark_disconnected("EURUSD")
        assert not sup.is_tradeable("EURUSD")
        assert sup.is_tradeable("GBPUSD")


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    def test_on_tick_exception_returns_disconnected(self) -> None:
        """Fail-closed: any internal error → DISCONNECTED."""
        sup, clock = _make_supervisor()
        # Create a tick that will cause an error in spread_pips
        # by monkey-patching _assess_tick to raise
        original = sup._assess_tick

        def broken_assess(tick: Tick) -> FeedState:
            raise RuntimeError("test boom")

        sup._assess_tick = broken_assess  # type: ignore[method-assign]
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.DISCONNECTED
        sup._assess_tick = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Integration: full lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_full_lifecycle(self) -> None:
        """Simulate: start → receive ticks → go stale → recover → disconnect → recover."""
        sup, clock = _make_supervisor()

        # 1. Not subscribed
        assert not sup.is_tradeable("EURUSD")

        # 2. First tick → healthy
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.HEALTHY
        assert sup.is_tradeable("EURUSD")

        # 3. Go stale
        clock[0] += 31.0
        assert not sup.is_tradeable("EURUSD")

        # 4. Recover
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.HEALTHY

        # 5. Disconnect
        sup.mark_disconnected("EURUSD")
        assert not sup.is_tradeable("EURUSD")

        # 6. Recover via new tick
        state = sup.on_tick(_tick(ts=clock[0]))
        assert state == FeedState.HEALTHY
        assert sup.is_tradeable("EURUSD")

    def test_rapid_ticks(self) -> None:
        """100 ticks in quick succession — all healthy."""
        sup, clock = _make_supervisor()
        for _i in range(100):
            clock[0] += 0.5
            state = sup.on_tick(_tick(ts=clock[0]))
            assert state == FeedState.HEALTHY
        assert sup.get_snapshot("EURUSD").tick_count == 100

    def test_spread_averaging(self) -> None:
        """Spread average computed over window."""
        cfg = FeedHealthConfig(spread_window=5, max_spread_pips=50.0)
        sup, clock = _make_supervisor(config=cfg)
        spreads_pips = []
        for i in range(5):
            ask = 1.1000 + (i + 1) * 0.0001  # 1-5 pip spreads
            sup.on_tick(_tick(bid=1.1000, ask=ask, ts=clock[0]))
            spreads_pips.append((ask - 1.1000) / 0.0001)
            clock[0] += 0.5
        snap = sup.get_snapshot("EURUSD")
        expected_avg = sum(spreads_pips) / len(spreads_pips)
        assert snap.avg_spread_pips == pytest.approx(expected_avg, rel=0.01)


# ---------------------------------------------------------------------------
# check_staleness: dedup key exclusion
# ---------------------------------------------------------------------------


class TestCheckStalenessExcludesDedup:
    def test_dedup_keys_excluded_from_staleness(self) -> None:
        """Dedup keys (e.g. 'EURUSD:SHORT') should not appear in check_staleness results."""
        sup, clock = _make_supervisor()
        # Feed a tick so EURUSD is tracked
        sup.on_tick(_tick(ts=clock[0]))
        # Trigger dedup tracking by calling is_duplicate_signal
        sup.is_duplicate_signal("EURUSD", "SHORT")

        staleness = sup.check_staleness()

        assert "EURUSD" in staleness
        assert "EURUSD:SHORT" not in staleness

    def test_dedup_keys_do_not_pollute_staleness_after_multiple_signals(self) -> None:
        """Multiple dedup keys should all be excluded from staleness."""
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        sup.is_duplicate_signal("EURUSD", "LONG")
        sup.is_duplicate_signal("EURUSD", "SHORT")
        sup.is_duplicate_signal("GBPUSD", "LONG")

        staleness = sup.check_staleness()

        # Only real symbols should appear
        assert "EURUSD" in staleness
        for key in staleness:
            assert ":" not in key, f"Dedup key {key!r} leaked into staleness"

    def test_tracked_symbols_excludes_dedup_keys(self) -> None:
        """tracked_symbols property should not include dedup keys."""
        sup, clock = _make_supervisor()
        sup.on_tick(_tick(ts=clock[0]))
        sup.is_duplicate_signal("EURUSD", "SHORT")

        symbols = sup.tracked_symbols

        assert "EURUSD" in symbols
        assert "EURUSD:SHORT" not in symbols
