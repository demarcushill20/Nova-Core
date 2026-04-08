"""Tests for the Trade Stall / Runtime Health Watchdog.

Covers:
  - Evidence collection for all signal types
  - Level classification (NONE, SOFT, MAJOR, CRITICAL)
  - False-positive suppression (weekends, no-signal regime, market closed)
  - Market session awareness
  - Diagnosis and action generation
  - Escalation tracking
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from novatrade.monitor.watchdog import (
    AnomalyLevel,
    EvidenceSignal,
    RuntimeHealthSnapshot,
    TradeStallWatchdog,
    WatchdogConfig,
    WatchdogEvidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snap(**overrides) -> RuntimeHealthSnapshot:
    """Build a healthy default snapshot, then apply overrides."""
    defaults = dict(
        adapter_connected=True,
        readiness_ok=True,
        webhook_server_up=True,
        last_alert_time=time.time() - 300,  # 5 minutes ago
        last_trade_time=time.time() - 600,  # 10 minutes ago
        last_quote_time=time.time() - 30,   # 30 seconds ago
        alerts_received_total=10,
        alerts_rejected_total=0,
        trades_executed_today=3,
        monitor_cycles_failed=0,
        consecutive_startup_failures=0,
        risk_halted=False,
        uptime_seconds=7200,  # 2 hours
    )
    defaults.update(overrides)
    return RuntimeHealthSnapshot(**defaults)


def _tuesday_10am_utc() -> float:
    """A known Tuesday 10:00 UTC timestamp (London session active)."""
    return dt.datetime(2026, 4, 7, 10, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _saturday_noon_utc() -> float:
    """A known Saturday 12:00 UTC (market closed)."""
    return dt.datetime(2026, 4, 11, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _friday_22_utc() -> float:
    """Friday 22:00 UTC (after market close)."""
    return dt.datetime(2026, 4, 10, 22, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _sunday_20_utc() -> float:
    """Sunday 20:00 UTC (before market open)."""
    return dt.datetime(2026, 4, 12, 20, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _sunday_22_utc() -> float:
    """Sunday 22:00 UTC (after market open)."""
    return dt.datetime(2026, 4, 12, 22, 0, 0, tzinfo=dt.timezone.utc).timestamp()


# ===========================================================================
# 1. Healthy system — no anomalies
# ===========================================================================


class TestHealthySystem:
    def test_no_anomaly_when_healthy(self):
        wd = TradeStallWatchdog()
        snap = _snap()
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.level == AnomalyLevel.NONE
        assert verdict.evidence.signal_count == 0

    def test_no_anomaly_resets_escalation(self):
        wd = TradeStallWatchdog()
        # Force an escalation first
        bad_snap = _snap(adapter_connected=False)
        wd.evaluate(bad_snap, now=_tuesday_10am_utc())
        assert wd._escalation_count == 1
        # Now healthy
        good_snap = _snap()
        wd.evaluate(good_snap, now=_tuesday_10am_utc())
        assert wd._escalation_count == 0


# ===========================================================================
# 2. Evidence collection
# ===========================================================================


class TestEvidenceCollection:
    def test_adapter_disconnected(self):
        wd = TradeStallWatchdog()
        snap = _snap(adapter_connected=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.ADAPTER_DISCONNECTED)

    def test_no_alerts_for_long_time(self):
        wd = TradeStallWatchdog(WatchdogConfig(soft_no_alert_minutes=30))
        now = _tuesday_10am_utc()
        snap = _snap(last_alert_time=now - 3600)  # 60 min ago
        verdict = wd.evaluate(snap, now=now)
        assert verdict.evidence.has(EvidenceSignal.NO_ALERTS_RECEIVED)

    def test_no_trades_for_long_time(self):
        wd = TradeStallWatchdog(WatchdogConfig(major_no_trade_minutes=60))
        now = _tuesday_10am_utc()
        snap = _snap(last_trade_time=now - 7200)  # 2 hours ago
        verdict = wd.evaluate(snap, now=now)
        assert verdict.evidence.has(EvidenceSignal.NO_TRADES_EXECUTED)

    def test_stale_quotes(self):
        wd = TradeStallWatchdog(WatchdogConfig(soft_stale_quote_minutes=5))
        now = _tuesday_10am_utc()
        snap = _snap(last_quote_time=now - 600)  # 10 min ago
        verdict = wd.evaluate(snap, now=now)
        assert verdict.evidence.has(EvidenceSignal.QUOTES_STALE)

    def test_webhook_inactive(self):
        wd = TradeStallWatchdog()
        snap = _snap(webhook_server_up=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.WEBHOOK_INACTIVE)

    def test_repeated_rejections(self):
        wd = TradeStallWatchdog()
        snap = _snap(alerts_received_total=10, alerts_rejected_total=8)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.REPEATED_REJECTIONS)

    def test_readiness_degraded(self):
        wd = TradeStallWatchdog()
        snap = _snap(readiness_ok=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.READINESS_DEGRADED)

    def test_risk_halted_signals_readiness(self):
        wd = TradeStallWatchdog()
        snap = _snap(risk_halted=True)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.READINESS_DEGRADED)

    def test_monitor_cycles_failing(self):
        wd = TradeStallWatchdog()
        snap = _snap(monitor_cycles_failed=5)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.MONITOR_CYCLE_FAILING)

    def test_startup_crash_loop(self):
        wd = TradeStallWatchdog()
        snap = _snap(consecutive_startup_failures=3)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.STARTUP_CRASH_LOOP)

    def test_no_alerts_ever_with_long_uptime(self):
        wd = TradeStallWatchdog(WatchdogConfig(soft_no_alert_minutes=30))
        snap = _snap(
            last_alert_time=0.0,  # never received
            uptime_seconds=3600,  # 60 min uptime
        )
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.NO_ALERTS_RECEIVED)

    def test_no_alerts_short_uptime_no_signal(self):
        """If uptime is short, no-alert is not flagged."""
        wd = TradeStallWatchdog(WatchdogConfig(soft_no_alert_minutes=30))
        snap = _snap(
            last_alert_time=0.0,
            uptime_seconds=600,  # 10 min uptime, threshold is 30
        )
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert not verdict.evidence.has(EvidenceSignal.NO_ALERTS_RECEIVED)


# ===========================================================================
# 3. Level classification
# ===========================================================================


class TestLevelClassification:
    def test_single_signal_is_soft(self):
        wd = TradeStallWatchdog()
        snap = _snap(adapter_connected=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.level == AnomalyLevel.SOFT

    def test_no_trades_with_corroborating_signals_is_major(self):
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=2,
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,   # 2h ago → no trades
            last_alert_time=now - 3600,   # 1h ago → no alerts
            adapter_connected=False,       # adapter down
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.level == AnomalyLevel.MAJOR

    def test_no_trades_without_enough_corroboration_is_soft(self):
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            major_min_corroborating=2,
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,  # only no-trades signal
            last_alert_time=now - 300,   # recent alert
            adapter_connected=True,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.level == AnomalyLevel.SOFT

    def test_crash_loop_is_critical(self):
        wd = TradeStallWatchdog()
        snap = _snap(consecutive_startup_failures=3)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.level == AnomalyLevel.CRITICAL

    def test_adapter_down_plus_webhook_down_is_critical(self):
        wd = TradeStallWatchdog()
        snap = _snap(adapter_connected=False, webhook_server_up=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.level == AnomalyLevel.CRITICAL

    def test_adapter_down_plus_readiness_degraded_escalates_to_critical(self):
        """After 3 consecutive escalations, adapter+readiness becomes critical."""
        wd = TradeStallWatchdog()
        snap = _snap(adapter_connected=False, readiness_ok=False)
        # Run 3 times to build escalation count
        for _ in range(3):
            wd.evaluate(snap, now=_tuesday_10am_utc())
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.level == AnomalyLevel.CRITICAL


# ===========================================================================
# 4. False-positive suppression
# ===========================================================================


class TestFalsePositiveSuppression:
    def test_weekend_suppresses_soft(self):
        wd = TradeStallWatchdog()
        now = _saturday_noon_utc()
        snap = _snap(
            adapter_connected=False,
            last_alert_time=now - 300,
            last_trade_time=now - 600,
            last_quote_time=now - 30,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is True
        assert verdict.level == AnomalyLevel.NONE
        assert "market closed" in verdict.suppression_reason

    def test_weekend_suppresses_major(self):
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=2,
        ))
        now = _saturday_noon_utc()
        snap = _snap(
            last_trade_time=now - 7200,
            last_alert_time=now - 3600,
            last_quote_time=now - 30,
            adapter_connected=False,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is True

    def test_weekend_does_not_suppress_critical(self):
        wd = TradeStallWatchdog()
        now = _saturday_noon_utc()
        snap = _snap(
            consecutive_startup_failures=3,
            last_alert_time=now - 300,
            last_trade_time=now - 600,
            last_quote_time=now - 30,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is False
        assert verdict.level == AnomalyLevel.CRITICAL

    def test_no_signal_regime_downgrades_major_to_soft(self):
        """No trades + no alerts + no system issues → no-signal regime, not outage.

        When corroboration threshold is low enough to classify as MAJOR,
        the false-positive guard downgrades to SOFT because no alerts were
        ever received (implying strategy simply had no setups).
        """
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=1,  # only need 1 corroboration → MAJOR
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,
            last_alert_time=0.0,  # never received
            uptime_seconds=14400,
            alerts_received_total=0,  # ZERO alerts ever
            trades_executed_today=0,
            adapter_connected=True,
            readiness_ok=True,
            webhook_server_up=True,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is True
        assert verdict.level == AnomalyLevel.SOFT
        assert "no strategy signals" in verdict.suppression_reason

    def test_no_signal_stays_soft_if_not_major(self):
        """With high corroboration threshold, no-trades+no-alerts stays SOFT
        without needing the no-signal suppression path."""
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=3,  # need 3 → won't reach MAJOR
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,
            last_alert_time=0.0,
            uptime_seconds=14400,
            alerts_received_total=0,
            trades_executed_today=0,
            adapter_connected=True,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.level == AnomalyLevel.SOFT
        assert verdict.suppressed is False  # already soft, no suppression needed

    def test_friday_after_close_suppressed(self):
        wd = TradeStallWatchdog()
        now = _friday_22_utc()
        snap = _snap(
            adapter_connected=False,
            last_alert_time=now - 300,
            last_trade_time=now - 600,
            last_quote_time=now - 30,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is True

    def test_sunday_before_open_suppressed(self):
        wd = TradeStallWatchdog()
        now = _sunday_20_utc()
        snap = _snap(
            adapter_connected=False,
            last_alert_time=now - 300,
            last_trade_time=now - 600,
            last_quote_time=now - 30,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is True

    def test_sunday_after_open_not_suppressed(self):
        wd = TradeStallWatchdog()
        now = _sunday_22_utc()
        snap = _snap(
            adapter_connected=False,
            last_alert_time=now - 300,
            last_trade_time=now - 600,
            last_quote_time=now - 30,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.suppressed is False
        assert verdict.level == AnomalyLevel.SOFT


# ===========================================================================
# 5. Market session awareness
# ===========================================================================


class TestMarketSessions:
    def test_tuesday_10am_is_active(self):
        wd = TradeStallWatchdog()
        assert wd._is_market_active(_tuesday_10am_utc()) is True

    def test_saturday_is_inactive(self):
        wd = TradeStallWatchdog()
        assert wd._is_market_active(_saturday_noon_utc()) is False

    def test_friday_after_close_inactive(self):
        wd = TradeStallWatchdog()
        assert wd._is_market_active(_friday_22_utc()) is False

    def test_sunday_before_open_inactive(self):
        wd = TradeStallWatchdog()
        assert wd._is_market_active(_sunday_20_utc()) is False

    def test_sunday_after_open_active(self):
        wd = TradeStallWatchdog()
        # Sunday 22:00 UTC falls in the Sydney session (21-6)
        assert wd._is_market_active(_sunday_22_utc()) is True

    def test_wednesday_3am_active(self):
        """03:00 UTC Wednesday — Sydney and Tokyo sessions active."""
        ts = dt.datetime(2026, 4, 8, 3, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        wd = TradeStallWatchdog()
        assert wd._is_market_active(ts) is True


# ===========================================================================
# 6. Diagnosis and actions
# ===========================================================================


class TestDiagnosisAndActions:
    def test_healthy_diagnosis(self):
        wd = TradeStallWatchdog()
        snap = _snap()
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert "healthy" in verdict.diagnosis.lower()

    def test_critical_diagnosis_mentions_critical(self):
        wd = TradeStallWatchdog()
        snap = _snap(consecutive_startup_failures=3)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert "CRITICAL" in verdict.diagnosis

    def test_critical_actions_include_escalation(self):
        wd = TradeStallWatchdog()
        snap = _snap(consecutive_startup_failures=3)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert any("INCIDENT" in a for a in verdict.recommended_actions)

    def test_soft_actions_include_logging(self):
        wd = TradeStallWatchdog()
        snap = _snap(adapter_connected=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert any("Log" in a for a in verdict.recommended_actions)

    def test_major_actions_include_diagnostic(self):
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=2,
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,
            last_alert_time=now - 3600,
            adapter_connected=False,
        )
        verdict = wd.evaluate(snap, now=now)
        assert any("diagnostic" in a.lower() for a in verdict.recommended_actions)


# ===========================================================================
# 7. WatchdogEvidence unit tests
# ===========================================================================


class TestWatchdogEvidence:
    def test_add_signal(self):
        ev = WatchdogEvidence()
        ev.add(EvidenceSignal.ADAPTER_DISCONNECTED, "test detail")
        assert ev.has(EvidenceSignal.ADAPTER_DISCONNECTED)
        assert ev.signal_count == 1
        assert ev.details["adapter_disconnected"] == "test detail"

    def test_duplicate_signal_not_added_twice(self):
        ev = WatchdogEvidence()
        ev.add(EvidenceSignal.ADAPTER_DISCONNECTED)
        ev.add(EvidenceSignal.ADAPTER_DISCONNECTED)
        assert ev.signal_count == 1

    def test_to_dict(self):
        ev = WatchdogEvidence()
        ev.add(EvidenceSignal.QUOTES_STALE, "5 min ago")
        d = ev.to_dict()
        assert "quotes_stale" in d["signals"]
        assert d["signal_count"] == 1


# ===========================================================================
# 8. Escalation tracking
# ===========================================================================


class TestEscalation:
    def test_escalation_increments(self):
        wd = TradeStallWatchdog()
        snap = _snap(adapter_connected=False)
        wd.evaluate(snap, now=_tuesday_10am_utc())
        assert wd._escalation_count == 1
        wd.evaluate(snap, now=_tuesday_10am_utc())
        assert wd._escalation_count == 2

    def test_escalation_resets_on_healthy(self):
        wd = TradeStallWatchdog()
        bad = _snap(adapter_connected=False)
        good = _snap()
        wd.evaluate(bad, now=_tuesday_10am_utc())
        wd.evaluate(bad, now=_tuesday_10am_utc())
        assert wd._escalation_count == 2
        wd.evaluate(good, now=_tuesday_10am_utc())
        assert wd._escalation_count == 0

    def test_last_verdict_stored(self):
        wd = TradeStallWatchdog()
        assert wd.last_verdict is None
        snap = _snap()
        wd.evaluate(snap, now=_tuesday_10am_utc())
        assert wd.last_verdict is not None
        assert wd.last_verdict.level == AnomalyLevel.NONE
