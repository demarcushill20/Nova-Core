"""Tests for session watchdog — tiered anomaly detection."""

from datetime import datetime, timezone

from novatrade.monitor.session_watchdog import (
    AnomalyAssessment,
    AnomalyLevel,
    SessionWatchdog,
    SymptomEvidence,
    WatchdogAction,
    WatchdogConfig,
    WatchdogState,
)


def _make_watchdog(now: float = 1712620800.0) -> tuple[SessionWatchdog, list[float]]:
    """Create a watchdog with a controllable clock and fresh state.

    Default now = 2024-04-09 00:00:00 UTC (Tuesday).
    """
    clock_values = [now]

    def clock() -> float:
        return clock_values[0]

    wd = SessionWatchdog(config=WatchdogConfig(), clock=clock)
    # Always start with clean state to prevent cross-test leakage
    wd._state = WatchdogState()
    return wd, clock_values


def _tuesday_london_time() -> float:
    """Return a timestamp for Tuesday 10:00 UTC (London session)."""
    return datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def _friday_late_time() -> float:
    """Return a timestamp for Friday 23:00 UTC (weekend)."""
    return datetime(2026, 4, 10, 23, 0, 0, tzinfo=timezone.utc).timestamp()


def _saturday_time() -> float:
    """Return a timestamp for Saturday 12:00 UTC (weekend)."""
    return datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def _tuesday_asian_time() -> float:
    """Return a timestamp for Tuesday 03:00 UTC (Asian session)."""
    return datetime(2026, 4, 7, 3, 0, 0, tzinfo=timezone.utc).timestamp()


def _tuesday_overlap_time() -> float:
    """Return a timestamp for Tuesday 14:00 UTC (London-NY overlap)."""
    return datetime(2026, 4, 7, 14, 0, 0, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Level 1 Tests
# ---------------------------------------------------------------------------


class TestLevel1Detection:
    """Test Level 1 (minor anomaly) detection."""

    def test_no_alerts_triggers_level1(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=44000,  # > 12h threshold (43200s)
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1
        assert any("no_alerts" in s for s in result.symptoms)

    def test_stale_quotes_triggers_level1(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            quotes_stale=True,
            quote_age_seconds=200,  # > 120s threshold
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1
        assert any("stale_quotes" in s for s in result.symptoms)

    def test_degraded_readiness_triggers_level1(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            readiness_ok=True,
            readiness_degraded=True,
            readiness_failures=3,
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1
        assert any("readiness_degraded" in s for s in result.symptoms)

    def test_adapter_disconnected_not_down_triggers_level1(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DEGRADED",
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1
        assert any("adapter_disconnected" in s for s in result.symptoms)

    def test_healthy_system_no_anomaly(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=300,  # 5 min — well within threshold
            quote_age_seconds=10,
            readiness_ok=True,
            readiness_degraded=False,
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.NONE
        assert len(result.symptoms) == 0

    def test_level1_actions_include_early_warning(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=44000,  # > 12h threshold (43200s)
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert WatchdogAction.LOG_ANOMALY in result.actions
        assert WatchdogAction.RUN_DIAGNOSTICS in result.actions
        assert WatchdogAction.SEND_EARLY_WARNING in result.actions


# ---------------------------------------------------------------------------
# Level 2 Tests
# ---------------------------------------------------------------------------


class TestLevel2Detection:
    """Test Level 2 (major anomaly) detection."""

    def test_no_trades_with_adapter_down(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DEGRADED",
            last_trade_age_seconds=44000,  # > 12h
            last_alert_age_seconds=44000,
            session="london",
            regime="ranging",
            signals_4h=0,
            signals_1h=0,
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_2
        assert any("no_trades" in s for s in result.symptoms)

    def test_no_trades_with_stale_quotes(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_trade_age_seconds=44000,  # > 12h
            quotes_stale=True,
            quote_age_seconds=300,
            session="london",
            regime="ranging",
            signals_4h=0,
            signals_1h=0,
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_2

    def test_no_trades_with_excessive_rejections(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_trade_age_seconds=44000,  # > 12h
            consecutive_rejections=6,  # > threshold of 5
            session="london",
            regime="ranging",
            signals_4h=0,
            signals_1h=0,
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_2
        assert any("repeated_rejections" in s for s in result.symptoms)

    def test_no_trades_legitimate_no_setup(self):
        """No trades but system is healthy — market gave no setup."""
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_trade_age_seconds=44000,  # > 12h
            readiness_ok=True,
            readiness_degraded=False,
            webhook_active=True,
            signals_4h=5,  # signals ARE flowing
            signals_1h=2,
            consecutive_rejections=0,
            session="london",
            regime="ranging",
            # Only corroborating symptom would be no_alerts, but
            # the system is otherwise healthy.
            last_alert_age_seconds=44000,
        )

        result = wd.evaluate(evidence)
        # Should NOT be Level 2 — legitimate no-trade period
        assert result.level != AnomalyLevel.LEVEL_2

    def test_no_trades_quiet_regime_suppressed(self):
        """No trades during quiet regime with connected adapter is legitimate."""
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_trade_age_seconds=44000,
            last_alert_age_seconds=44000,
            readiness_ok=True,
            webhook_active=True,
            signals_4h=0,
            signals_1h=0,
            session="london",
            regime="quiet",  # quiet regime
        )

        result = wd.evaluate(evidence)
        assert result.level != AnomalyLevel.LEVEL_2

    def test_level2_actions_include_investigation(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            last_trade_age_seconds=44000,
            last_alert_age_seconds=44000,
            session="london",
            regime="ranging",
            signals_4h=0,
        )

        result = wd.evaluate(evidence)
        assert result.level in (AnomalyLevel.LEVEL_2, AnomalyLevel.LEVEL_3)
        if result.level == AnomalyLevel.LEVEL_2:
            assert WatchdogAction.INVESTIGATE in result.actions
            assert WatchdogAction.GENERATE_REPAIR_PLAN in result.actions


# ---------------------------------------------------------------------------
# Level 3 Tests
# ---------------------------------------------------------------------------


class TestLevel3Detection:
    """Test Level 3 (critical outage) detection."""

    def test_crash_loop_detection(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        # Simulate crash loop
        wd._state.last_startup_failure_at = now - 60  # 1 minute ago

        evidence = SymptomEvidence(
            startup_failures=4,  # > threshold of 3
            adapter_connected=True,
            adapter_state="OK",
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_3
        assert any("crash_loop" in s for s in result.symptoms)

    def test_adapter_hard_failure(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_3
        assert any("adapter_hard_failure" in s for s in result.symptoms)

    def test_continuous_health_failure(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="DEGRADED",
            consecutive_health_failures=12,  # > threshold of 10
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_3
        assert any("continuous_health_failure" in s for s in result.symptoms)

    def test_total_pipeline_failure(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            webhook_active=False,
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_3
        assert any("total_pipeline_failure" in s or "adapter_hard_failure" in s for s in result.symptoms)

    def test_level3_actions_include_escalation(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert WatchdogAction.OPEN_INCIDENT in result.actions
        assert WatchdogAction.ESCALATE in result.actions

    def test_crash_loop_outside_window_no_trigger(self):
        """Crash loop with failures outside the time window should not trigger."""
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        # Failures happened 2 hours ago — outside 30-minute window
        wd._state.last_startup_failure_at = now - 7200

        evidence = SymptomEvidence(
            startup_failures=4,
            adapter_connected=True,
            adapter_state="OK",
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level != AnomalyLevel.LEVEL_3


# ---------------------------------------------------------------------------
# False Positive Safeguard Tests
# ---------------------------------------------------------------------------


class TestFalsePositiveSafeguards:
    """Test false positive suppression."""

    def test_weekend_suppresses_all_anomalies(self):
        now = _saturday_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            last_alert_age_seconds=50000,
            last_trade_age_seconds=50000,
            quotes_stale=True,
            market_closed=False,  # Let weekend detection handle it
            session="weekend",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.suppressed is True
        assert "weekend" in result.suppression_reason

    def test_market_closed_flag_suppresses(self):
        now = _tuesday_london_time()  # Doesn't matter — flag overrides
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            market_closed=True,
            session="weekend",
        )

        result = wd.evaluate(evidence)
        assert result.suppressed is True
        assert result.suppression_reason == "market_closed"

    def test_asian_quiet_regime_suppresses_level1(self):
        now = _tuesday_asian_time()
        wd, clock = _make_watchdog(now)

        # Asian (2x) * quiet (1.5x) = 3x multiplier → threshold = 129600s
        # Use value above that to trigger Level 1, which then gets suppressed
        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=130000,  # > 129600s (3x threshold of 43200s)
            session="asian",
            regime="quiet",
        )

        result = wd.evaluate(evidence)
        assert result.suppressed is True
        assert result.suppression_reason == "asian_quiet_regime"

    def test_asian_ranging_does_not_suppress_level1(self):
        now = _tuesday_asian_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=87000,  # With 2x multiplier, threshold = 86400s
            session="asian",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1
        assert result.suppressed is False


# ---------------------------------------------------------------------------
# Threshold Multiplier Tests
# ---------------------------------------------------------------------------


class TestThresholdMultipliers:
    """Test session/regime threshold adjustments."""

    def test_asian_session_doubles_thresholds(self):
        now = _tuesday_asian_time()
        wd, clock = _make_watchdog(now)

        # > 43200 (12h) but < 86400 (2x asian adjustment)
        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=50000,  # > 43200 but < 86400
            session="asian",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        # Should NOT trigger because asian doubles the threshold
        assert result.level == AnomalyLevel.NONE

    def test_overlap_session_tightens_thresholds(self):
        now = _tuesday_overlap_time()
        wd, clock = _make_watchdog(now)

        # During overlap, multiplier = 0.8, so threshold = 34560s (0.8 × 43200)
        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=35000,  # > 34560 (0.8 × 43200)
            session="overlap",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1

    def test_quiet_regime_widens_thresholds(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        # Quiet regime multiplier = 1.5, so threshold = 64800s (1.5 × 43200)
        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=50000,  # > 43200 but < 64800
            session="london",
            regime="quiet",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.NONE

    def test_volatile_regime_tightens_thresholds(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        # Volatile regime multiplier = 0.7, so threshold = 30240s (0.7 × 43200)
        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=31000,  # > 30240 (0.7 × 43200)
            session="london",
            regime="volatile",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1


# ---------------------------------------------------------------------------
# Cooldown Tests
# ---------------------------------------------------------------------------


class TestCooldowns:
    """Test alert cooldown mechanism."""

    def test_should_alert_initially_true(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        assert wd.should_alert(AnomalyLevel.LEVEL_1) is True
        assert wd.should_alert(AnomalyLevel.LEVEL_2) is True
        assert wd.should_alert(AnomalyLevel.LEVEL_3) is True

    def test_cooldown_blocks_rapid_alerts(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.mark_alerted(AnomalyLevel.LEVEL_1)

        # Immediately after — should be blocked
        assert wd.should_alert(AnomalyLevel.LEVEL_1) is False

    def test_cooldown_expires(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.mark_alerted(AnomalyLevel.LEVEL_1)

        # Advance past cooldown (4 hours)
        clock[0] = now + 14500
        assert wd.should_alert(AnomalyLevel.LEVEL_1) is True


# ---------------------------------------------------------------------------
# State Tracking Tests
# ---------------------------------------------------------------------------


class TestStateTracking:
    """Test persistent state tracking."""

    def test_record_alert_received(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.record_alert_received()
        assert wd.state.last_alert_at == now

    def test_record_trade_executed(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.record_trade_executed()
        assert wd.state.last_trade_at == now

    def test_record_health_failure_increments(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.record_health_failure()
        wd.record_health_failure()
        assert wd.state.consecutive_health_failures == 2

    def test_record_health_ok_resets_counter(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.record_health_failure()
        wd.record_health_failure()
        wd.record_health_ok()
        assert wd.state.consecutive_health_failures == 0

    def test_startup_failure_tracking(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.record_startup_failure()
        wd.record_startup_failure()
        assert wd.state.startup_failures == 2

    def test_startup_failure_resets_after_window(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        wd.record_startup_failure()

        # Advance past crash loop window (30 min)
        clock[0] = now + 2000
        wd.record_startup_failure()
        assert wd.state.startup_failures == 1  # Reset + 1 new

    def test_escalation_tracking(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=44000,  # > 12h threshold
            session="london",
            regime="ranging",
        )

        wd.evaluate(evidence)
        assert wd.state.consecutive_level1 == 1
        assert wd.state.current_level == "LEVEL_1"

        wd.evaluate(evidence)
        assert wd.state.consecutive_level1 == 2


# ---------------------------------------------------------------------------
# Assessment Serialization Tests
# ---------------------------------------------------------------------------


class TestAssessmentSerialization:
    """Test assessment serialization."""

    def test_to_dict(self):
        assessment = AnomalyAssessment(
            level=AnomalyLevel.LEVEL_1,
            symptoms=["test_symptom"],
            actions=[WatchdogAction.LOG_ANOMALY],
        )
        d = assessment.to_dict()
        assert d["level"] == "LEVEL_1"
        assert d["actions"] == ["LOG_ANOMALY"]
        assert d["symptoms"] == ["test_symptom"]

    def test_none_level_to_dict(self):
        assessment = AnomalyAssessment()
        d = assessment.to_dict()
        assert d["level"] == "NONE"
        assert d["actions"] == []


# ---------------------------------------------------------------------------
# Evidence Gathering Tests
# ---------------------------------------------------------------------------


class TestEvidenceGathering:
    """Test the gather_evidence convenience method."""

    def test_gather_evidence_returns_evidence(self):
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        evidence = wd.gather_evidence()
        assert isinstance(evidence, SymptomEvidence)
        assert evidence.session == "london"

    def test_gather_evidence_detects_weekend(self):
        now = _saturday_time()
        wd, clock = _make_watchdog(now)

        evidence = wd.gather_evidence()
        assert evidence.market_closed is True
        assert evidence.session == "weekend"


# ---------------------------------------------------------------------------
# Integration-style Tests
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration-style tests combining multiple features."""

    def test_level_escalation_path(self):
        """Test that system can escalate from Level 1 -> Level 2 -> Level 3."""
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        # Level 1: just stale alerts
        evidence1 = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=44000,  # > 12h threshold
            session="london",
            regime="ranging",
        )
        r1 = wd.evaluate(evidence1)
        assert r1.level == AnomalyLevel.LEVEL_1

        # Level 2: situation worsens — no trades + adapter issues
        evidence2 = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DEGRADED",
            last_trade_age_seconds=44000,
            last_alert_age_seconds=44000,
            session="london",
            regime="ranging",
            signals_4h=0,
        )
        r2 = wd.evaluate(evidence2)
        assert r2.level == AnomalyLevel.LEVEL_2

        # Level 3: adapter goes completely down
        evidence3 = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            session="london",
            regime="ranging",
        )
        r3 = wd.evaluate(evidence3)
        assert r3.level == AnomalyLevel.LEVEL_3

    def test_recovery_from_anomaly(self):
        """Test that system returns to NONE when evidence improves."""
        now = _tuesday_london_time()
        wd, clock = _make_watchdog(now)

        # Trigger Level 1
        bad = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=44000,  # > 12h threshold
            session="london",
            regime="ranging",
        )
        r1 = wd.evaluate(bad)
        assert r1.level == AnomalyLevel.LEVEL_1

        # System recovers
        good = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=300,
            quote_age_seconds=10,
            readiness_ok=True,
            session="london",
            regime="ranging",
        )
        r2 = wd.evaluate(good)
        assert r2.level == AnomalyLevel.NONE
        assert wd.state.current_level == "NONE"
