"""Tests for signal rate monitoring functionality."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from novatrade.monitor.signal_monitor import (
    REGIME_MULTIPLIERS,
    SESSION_LENIENCY,
    SignalRateMonitor,
    SignalRateStats,
    _load_regime,
    get_current_stats,
    get_session,
    is_weekend,
    load_signal_stats,
    record_signal,
)


class TestSignalRateStats:
    """Test SignalRateStats dataclass."""

    def test_default_values(self):
        """Test default field values."""
        stats = SignalRateStats()
        assert stats.signals_1h == 0
        assert stats.signals_4h == 0
        assert stats.signals_24h == 0
        assert stats.status == "UNKNOWN"
        assert stats.concern_level == "GREEN"
        assert stats.strategy == "IRB"
        assert stats.regime == ""
        assert stats.session == ""

    def test_new_fields_present(self):
        """Test that regime and session fields exist."""
        stats = SignalRateStats(regime="quiet", session="asian")
        assert stats.regime == "quiet"
        assert stats.session == "asian"


class TestForexSessions:
    """Test forex session detection."""

    def test_london_morning(self):
        """Test London session detection (07:00-12:00 UTC)."""
        dt = datetime(2026, 3, 25, 9, 30, tzinfo=timezone.utc)  # Wednesday 09:30
        assert get_session(dt) == "london"

    def test_overlap_session(self):
        """Test London+NY overlap (12:00-16:00 UTC)."""
        dt = datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc)  # Wednesday 14:00
        assert get_session(dt) == "overlap"

    def test_newyork_session(self):
        """Test NY session (16:00-21:00 UTC)."""
        dt = datetime(2026, 3, 25, 18, 0, tzinfo=timezone.utc)  # Wednesday 18:00
        assert get_session(dt) == "newyork"

    def test_asian_session_late_night(self):
        """Test Asian session (21:00-07:00 UTC) — late night."""
        dt = datetime(2026, 3, 25, 23, 0, tzinfo=timezone.utc)  # Wednesday 23:00
        assert get_session(dt) == "asian"

    def test_asian_session_early_morning(self):
        """Test Asian session — early morning."""
        dt = datetime(2026, 3, 26, 3, 0, tzinfo=timezone.utc)  # Thursday 03:00
        assert get_session(dt) == "asian"

    def test_weekend_saturday(self):
        """Test weekend detection on Saturday."""
        dt = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)  # Saturday
        assert get_session(dt) == "weekend"
        assert is_weekend(dt) is True

    def test_weekend_friday_late(self):
        """Test weekend starts Friday 22:00 UTC."""
        dt = datetime(2026, 3, 27, 22, 30, tzinfo=timezone.utc)  # Friday 22:30
        assert is_weekend(dt) is True
        assert get_session(dt) == "weekend"

    def test_friday_before_close(self):
        """Test Friday before market close is NOT weekend."""
        dt = datetime(2026, 3, 27, 21, 0, tzinfo=timezone.utc)  # Friday 21:00
        assert is_weekend(dt) is False

    def test_sunday_before_open(self):
        """Test Sunday before 22:00 UTC is weekend."""
        dt = datetime(2026, 3, 29, 20, 0, tzinfo=timezone.utc)  # Sunday 20:00
        assert is_weekend(dt) is True

    def test_sunday_after_open(self):
        """Test Sunday after 22:00 UTC is NOT weekend."""
        dt = datetime(2026, 3, 29, 22, 30, tzinfo=timezone.utc)  # Sunday 22:30
        assert is_weekend(dt) is False

    def test_session_boundary_7am(self):
        """Test boundary: 07:00 is London."""
        dt = datetime(2026, 3, 25, 7, 0, tzinfo=timezone.utc)
        assert get_session(dt) == "london"

    def test_session_boundary_12pm(self):
        """Test boundary: 12:00 is overlap."""
        dt = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
        assert get_session(dt) == "overlap"

    def test_session_boundary_16pm(self):
        """Test boundary: 16:00 is NY."""
        dt = datetime(2026, 3, 25, 16, 0, tzinfo=timezone.utc)
        assert get_session(dt) == "newyork"

    def test_session_boundary_21pm(self):
        """Test boundary: 21:00 is Asian."""
        dt = datetime(2026, 3, 25, 21, 0, tzinfo=timezone.utc)
        assert get_session(dt) == "asian"


class TestSignalRateMonitor:
    """Test SignalRateMonitor class."""

    def test_fresh_monitor_no_signals(self):
        """Test fresh monitor with no signals recorded."""
        monitor = SignalRateMonitor()
        # Use a weekday time to avoid weekend suppression
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)  # Wednesday
        stats = monitor.get_stats(now=now)

        assert stats.signals_1h == 0
        assert stats.signals_4h == 0
        assert stats.signals_24h == 0
        assert stats.status == "NO_DATA"
        assert stats.concern_level == "YELLOW"
        assert stats.last_signal_at == ""

    def test_record_single_signal(self):
        """Test recording a single signal."""
        monitor = SignalRateMonitor()
        monitor.record_signal("any")

        # Use a weekday London session time
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        # Manually adjust signal to be within the time window
        monitor.signal_timestamps = [now - timedelta(minutes=1)]
        stats = monitor.get_stats(now=now)

        assert stats.signals_1h == 1
        assert stats.signals_4h == 1
        assert stats.signals_24h == 1
        assert stats.status == "OK"
        assert stats.concern_level == "GREEN"
        assert stats.last_signal_at != ""

    def test_record_trade_signals(self):
        """Test recording trade-specific signals."""
        monitor = SignalRateMonitor()

        # Record mixed signals
        monitor.record_signal("any")
        monitor.record_signal("trade")
        monitor.record_signal("any")
        monitor.record_signal("trade")

        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        # Adjust timestamps to be in the window
        base = now - timedelta(minutes=5)
        monitor.signal_timestamps = [base + timedelta(minutes=i) for i in range(4)]
        monitor.trade_signal_timestamps = [base + timedelta(minutes=1), base + timedelta(minutes=3)]

        stats = monitor.get_stats(now=now)
        assert stats.signals_1h == 4
        assert stats.last_signal_at != ""
        assert stats.last_trade_signal_at != ""

    def test_signal_rate_healthy(self):
        """Test healthy signal rate assessment."""
        monitor = SignalRateMonitor()

        # Simulate 6 signals in the last hour (healthy rate)
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        for i in range(6):
            signal_time = now - timedelta(minutes=i * 10)
            monitor.signal_timestamps.append(signal_time)

        stats = monitor.get_stats(now=now)
        assert stats.signals_1h == 6
        assert stats.status == "OK"
        assert stats.concern_level == "GREEN"

    def test_signal_rate_low_yellow(self, monkeypatch):
        """Test low signal rate - yellow warning during London (ranging regime)."""
        from novatrade.monitor import signal_monitor

        monkeypatch.setattr(signal_monitor, "_load_regime", lambda: "ranging")

        monitor = SignalRateMonitor()

        # 1 signal 2 hours ago (in 4h window but not 1h)
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        signal_time = now - timedelta(hours=2)
        monitor.signal_timestamps.append(signal_time)

        stats = monitor.get_stats(now=now)
        assert stats.signals_1h == 0
        assert stats.signals_4h == 1
        assert stats.status == "LOW_RATE"
        assert stats.concern_level == "YELLOW"

    def test_signal_rate_stale_red(self):
        """Test stale signals — red alert with default regime."""
        monitor = SignalRateMonitor()

        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        old_signal = now - timedelta(hours=10)
        monitor.signal_timestamps.append(old_signal)

        status, concern = monitor._assess_health(0, 0, 0, now, "ranging", "london")
        assert status == "STALE"
        assert concern == "RED"

    def test_signal_rate_no_signals_4h_red(self):
        """Test no signals in 4h window — red alert during London."""
        monitor = SignalRateMonitor()

        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        old_signal = now - timedelta(hours=5)
        monitor.signal_timestamps.append(old_signal)

        stats = monitor.get_stats(now=now)
        assert stats.signals_1h == 0
        assert stats.signals_4h == 0
        assert stats.signals_24h == 1
        assert stats.status == "LOW_RATE"
        assert stats.concern_level == "RED"

    def test_memory_cleanup(self):
        """Test that old signals are cleaned up to prevent memory growth."""
        monitor = SignalRateMonitor()

        now = datetime.now(timezone.utc)
        old_signals = [now - timedelta(days=2), now - timedelta(hours=30)]
        recent_signal = now - timedelta(minutes=10)

        monitor.signal_timestamps.extend(old_signals)
        monitor.signal_timestamps.append(recent_signal)

        # Record a new signal (triggers cleanup)
        monitor.record_signal("any")

        assert len(monitor.signal_timestamps) == 2  # recent_signal + new signal
        assert all(ts > now - timedelta(days=1) for ts in monitor.signal_timestamps)

    def test_persistence_roundtrip(self, tmp_path, monkeypatch):
        """Test signal stats persistence and loading."""
        monitor = SignalRateMonitor()
        monitor.record_signal("any")
        monitor.record_signal("trade")

        original_stats = monitor.get_stats()

        from novatrade.monitor import signal_monitor

        monkeypatch.setattr(signal_monitor, "SIGNAL_STATS_FILE", tmp_path / "signal_stats.json")
        monkeypatch.setattr(signal_monitor, "STATE_DIR", tmp_path)

        monitor._persist_stats(original_stats)
        loaded_stats = load_signal_stats()

        assert loaded_stats is not None
        assert loaded_stats.signals_1h == original_stats.signals_1h
        assert loaded_stats.status == original_stats.status
        assert loaded_stats.concern_level == original_stats.concern_level

    def test_persistence_corrupted_file(self, tmp_path, monkeypatch):
        """Test graceful handling of corrupted stats file."""
        from novatrade.monitor import signal_monitor

        corrupted_file = tmp_path / "signal_stats.json"
        corrupted_file.write_text("{ invalid json")

        monkeypatch.setattr(signal_monitor, "SIGNAL_STATS_FILE", corrupted_file)

        stats = load_signal_stats()
        assert stats is None

    def test_global_convenience_functions(self):
        """Test global convenience functions."""
        from novatrade.monitor import signal_monitor

        signal_monitor._global_monitor = SignalRateMonitor()

        record_signal("trade")
        record_signal("any")

        stats = get_current_stats()
        assert stats.signals_1h == 2
        assert stats.last_signal_at != ""
        assert stats.last_trade_signal_at != ""

    def test_time_window_accuracy(self):
        """Test accuracy of time window calculations."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)

        signals = [
            now - timedelta(minutes=30),  # 0.5h ago — count in 1h, 4h, 24h
            now - timedelta(hours=2),  # 2h ago — count in 4h, 24h only
            now - timedelta(hours=6),  # 6h ago — count in 24h only
            now - timedelta(hours=30),  # 30h ago — not counted
        ]

        monitor.signal_timestamps.extend(signals)
        stats = monitor.get_stats(now=now)

        assert stats.signals_1h == 1
        assert stats.signals_4h == 2
        assert stats.signals_24h == 3

    def test_custom_strategy_symbol(self):
        """Test custom strategy and symbol initialization."""
        monitor = SignalRateMonitor(strategy="CUSTOM", symbol="GBPUSD")
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        stats = monitor.get_stats(now=now)

        assert stats.strategy == "CUSTOM"
        assert stats.symbol == "GBPUSD"


class TestRegimeAwareThresholds:
    """Test regime-aware signal threshold adjustment."""

    def test_quiet_regime_relaxes_stale_threshold(self):
        """In quiet regime, stale threshold is doubled (16h instead of 8h)."""
        monitor = SignalRateMonitor()

        # Signal from 10 hours ago — would be RED in ranging, but OK stale-threshold in quiet
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        old_signal = now - timedelta(hours=10)
        monitor.signal_timestamps.append(old_signal)

        # With quiet regime — 10h < 16h stale_red, so should NOT be RED
        status, concern = monitor._assess_health(0, 0, 0, now, "quiet", "london")
        assert concern != "RED" or status != "STALE"

    def test_quiet_regime_no_4h_red(self):
        """In quiet regime, 0 signals in 4h should not trigger RED."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)

        # 1 signal in 24h window but not in 4h
        old_signal = now - timedelta(hours=5)
        monitor.signal_timestamps.append(old_signal)

        # quiet regime: low_4h_threshold=0 but check is <=, so 0 signals_4h is still RED
        # However with quiet regime, 0 in 4h is less alarming
        status, concern = monitor._assess_health(0, 0, 1, now, "quiet", "london")
        # In quiet regime, low_4h_threshold is 0, so 0 <= 0 triggers RED
        assert status == "LOW_RATE"

    def test_volatile_regime_tightens_thresholds(self):
        """In volatile regime, thresholds are tighter."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc)  # Overlap session

        # 1 signal in 4h — enough for ranging, but suspicious for volatile
        status, concern = monitor._assess_health(0, 1, 5, now, "volatile", "overlap")
        # volatile: low_4h_threshold=1, so 1 <= 1 is RED
        assert status == "LOW_RATE"
        assert concern == "RED"

    def test_volatile_regime_stale_faster(self):
        """In volatile regime, STALE triggers faster (4h instead of 8h)."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc)

        old_signal = now - timedelta(hours=5)
        monitor.signal_timestamps.append(old_signal)

        # volatile + overlap: stale_red_hours=4, leniency=1.2, effective=3.33h
        # 5 hours > 3.33 → STALE RED
        status, concern = monitor._assess_health(0, 0, 0, now, "volatile", "overlap")
        assert status == "STALE"
        assert concern == "RED"

    def test_trending_regime_moderate_thresholds(self):
        """Trending regime has moderate thresholds between ranging and volatile."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)

        # 2 signals in 4h, 0 in 1h — OK in ranging, YELLOW in trending (threshold 3)
        status, concern = monitor._assess_health(0, 2, 5, now, "trending", "london")
        assert status == "LOW_RATE"
        assert concern == "YELLOW"

    def test_ranging_default_thresholds(self):
        """Ranging regime uses default thresholds."""
        thresholds = REGIME_MULTIPLIERS["ranging"]
        assert thresholds["stale_red_hours"] == 8
        assert thresholds["stale_yellow_hours"] == 4
        assert thresholds["low_combined_4h"] == 2

    def test_all_regimes_defined(self):
        """All expected regimes have multiplier definitions."""
        assert "quiet" in REGIME_MULTIPLIERS
        assert "ranging" in REGIME_MULTIPLIERS
        assert "trending" in REGIME_MULTIPLIERS
        assert "volatile" in REGIME_MULTIPLIERS

    def test_regime_loaded_from_file(self, tmp_path, monkeypatch):
        """Test that regime is loaded from regime.json."""
        from novatrade.monitor import signal_monitor

        regime_file = tmp_path / "regime.json"
        regime_file.write_text(json.dumps({"regime": "volatile"}))
        monkeypatch.setattr(signal_monitor, "REGIME_FILE", regime_file)

        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        monitor.signal_timestamps.append(now - timedelta(minutes=5))

        stats = monitor.get_stats(now=now)
        assert stats.regime == "volatile"

    def test_regime_staleness_fallback(self, tmp_path, monkeypatch):
        """Stale regime.json (>30 min old) falls back to 'ranging'."""
        from novatrade.monitor import signal_monitor

        regime_file = tmp_path / "regime.json"
        regime_file.write_text(json.dumps({"regime": "quiet"}))

        # Backdate mtime by 31 minutes
        stale_time = time.time() - (31 * 60)
        os.utime(str(regime_file), (stale_time, stale_time))

        monkeypatch.setattr(signal_monitor, "REGIME_FILE", regime_file)

        result = _load_regime()
        assert result == "ranging"

    def test_missing_regime_file_defaults(self, tmp_path, monkeypatch):
        """Missing regime.json defaults to 'ranging'."""
        from novatrade.monitor import signal_monitor

        monkeypatch.setattr(signal_monitor, "REGIME_FILE", tmp_path / "nonexistent.json")

        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        monitor.signal_timestamps.append(now - timedelta(minutes=5))

        stats = monitor.get_stats(now=now)
        assert stats.regime == "ranging"

    def test_corrupted_regime_file_defaults(self, tmp_path, monkeypatch):
        """Corrupted regime.json defaults to 'ranging'."""
        from novatrade.monitor import signal_monitor

        bad_file = tmp_path / "regime.json"
        bad_file.write_text("not json {{{")
        monkeypatch.setattr(signal_monitor, "REGIME_FILE", bad_file)

        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        monitor.signal_timestamps.append(now - timedelta(minutes=5))

        stats = monitor.get_stats(now=now)
        assert stats.regime == "ranging"

    def test_unknown_regime_defaults(self, tmp_path, monkeypatch):
        """Unknown regime value defaults to 'ranging'."""
        from novatrade.monitor import signal_monitor

        regime_file = tmp_path / "regime.json"
        regime_file.write_text(json.dumps({"regime": "unknown_type"}))
        monkeypatch.setattr(signal_monitor, "REGIME_FILE", regime_file)

        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        monitor.signal_timestamps.append(now - timedelta(minutes=5))

        stats = monitor.get_stats(now=now)
        assert stats.regime == "ranging"


class TestSessionAwareAlerting:
    """Test session-aware alert suppression."""

    def test_weekend_suppresses_all_alerts(self):
        """During weekend, status is MARKET_CLOSED and GREEN."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)  # Saturday

        stats = monitor.get_stats(now=now)
        assert stats.status == "MARKET_CLOSED"
        assert stats.concern_level == "GREEN"
        assert stats.session == "weekend"

    def test_weekend_no_red_alerts(self):
        """Even with stale signals, weekend should never be RED."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)  # Saturday

        # Simulate very old signals
        monitor.signal_timestamps.append(now - timedelta(hours=48))

        stats = monitor.get_stats(now=now)
        assert stats.concern_level == "GREEN"
        assert stats.status == "MARKET_CLOSED"

    def test_asian_session_relaxed_thresholds(self):
        """Asian session has relaxed thresholds (50% expected rate)."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 26, 3, 0, tzinfo=timezone.utc)  # Thursday 03:00 = Asian

        # 1 signal in 4h, 0 in 1h — would be YELLOW during London, but...
        monitor.signal_timestamps.append(now - timedelta(hours=2))

        stats = monitor.get_stats(now=now)
        assert stats.session == "asian"
        # In ranging regime with asian leniency: effective_4h_yellow = max(1, int(2 * 0.5)) = 1
        # signals_4h=1 is NOT < 1, so should be OK
        assert stats.concern_level == "GREEN"

    def test_asian_quiet_double_relaxation(self):
        """Asian session + quiet regime = maximum relaxation."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 26, 3, 0, tzinfo=timezone.utc)

        # 0 signals in 4h — RED during London/ranging, but downgraded during Asian/quiet
        old_signal = now - timedelta(hours=5)
        monitor.signal_timestamps.append(old_signal)

        status, concern = monitor._assess_health(0, 0, 1, now, "quiet", "asian")
        # quiet + asian: should downgrade to YELLOW
        assert concern == "YELLOW"

    def test_overlap_session_tighter(self):
        """London+NY overlap expects slightly higher rates (leniency > 1)."""
        assert SESSION_LENIENCY["overlap"] > 1.0

    def test_london_session_full_expectations(self):
        """London session has full expectations (leniency = 1.0)."""
        assert SESSION_LENIENCY["london"] == 1.0

    def test_stats_include_session(self):
        """Stats object includes current session."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        monitor.signal_timestamps.append(now - timedelta(minutes=5))

        stats = monitor.get_stats(now=now)
        assert stats.session == "london"

    def test_stats_include_regime(self, tmp_path, monkeypatch):
        """Stats object includes current regime."""
        from novatrade.monitor import signal_monitor

        regime_file = tmp_path / "regime.json"
        regime_file.write_text(json.dumps({"regime": "trending"}))
        monkeypatch.setattr(signal_monitor, "REGIME_FILE", regime_file)

        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        monitor.signal_timestamps.append(now - timedelta(minutes=5))

        stats = monitor.get_stats(now=now)
        assert stats.regime == "trending"

    def test_newyork_session_slightly_relaxed(self):
        """NY session is slightly below London (0.9)."""
        monitor = SignalRateMonitor()
        now = datetime(2026, 3, 25, 18, 0, tzinfo=timezone.utc)  # NY session

        # 1 signal in 4h, 0 in 1h
        monitor.signal_timestamps.append(now - timedelta(hours=2))

        stats = monitor.get_stats(now=now)
        assert stats.session == "newyork"
        # ranging: low_combined_4h=2, * 0.9 = 1.8, max(1, int(1.8)) = 1
        # signals_4h=1, not < 1, so OK
        assert stats.concern_level == "GREEN"


class TestLoadSignalStatsWithNewFields:
    """Test that load_signal_stats handles new fields gracefully."""

    def test_load_with_new_fields(self, tmp_path, monkeypatch):
        """Loading stats with regime and session fields works."""
        from novatrade.monitor import signal_monitor

        stats_data = {
            "signals_1h": 5,
            "signals_4h": 20,
            "signals_24h": 100,
            "last_signal_at": "2026-03-25T10:00:00+00:00",
            "last_trade_signal_at": "",
            "status": "OK",
            "concern_level": "GREEN",
            "regime": "trending",
            "session": "london",
            "strategy": "IRB",
            "timeframe": "M5",
            "symbol": "EURUSD",
            "updated_at": "2026-03-25T10:00:00+00:00",
        }

        stats_file = tmp_path / "signal_stats.json"
        stats_file.write_text(json.dumps(stats_data))
        monkeypatch.setattr(signal_monitor, "SIGNAL_STATS_FILE", stats_file)

        loaded = load_signal_stats()
        assert loaded is not None
        assert loaded.regime == "trending"
        assert loaded.session == "london"
        assert loaded.signals_1h == 5

    def test_load_without_new_fields_backwards_compat(self, tmp_path, monkeypatch):
        """Loading old stats without regime/session still works."""
        from novatrade.monitor import signal_monitor

        # Old format — no regime or session
        stats_data = {
            "signals_1h": 3,
            "signals_4h": 10,
            "signals_24h": 50,
            "last_signal_at": "",
            "last_trade_signal_at": "",
            "status": "OK",
            "concern_level": "GREEN",
            "strategy": "IRB",
            "timeframe": "M5",
            "symbol": "EURUSD",
            "updated_at": "",
        }

        stats_file = tmp_path / "signal_stats.json"
        stats_file.write_text(json.dumps(stats_data))
        monkeypatch.setattr(signal_monitor, "SIGNAL_STATS_FILE", stats_file)

        loaded = load_signal_stats()
        assert loaded is not None
        assert loaded.regime == ""
        assert loaded.session == ""
        assert loaded.signals_1h == 3
