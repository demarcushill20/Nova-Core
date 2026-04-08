"""Trade Stall / Runtime Health Watchdog — Autonomous anomaly detection.

Layered detection protocol that distinguishes "market gave no setup" from
"system is broken" and escalates proportionally:

  Level 1 — Soft anomaly:  early warning, lightweight diagnostics
  Level 2 — Major anomaly: autonomous investigation + repair plan
  Level 3 — Critical:      incident record + containment + escalation

False-positive safeguards:
  - Respects market session hours / weekend closures
  - Respects strategy flat/no-signal regimes
  - Requires corroborating evidence before escalating
  - Uses configurable thresholds per anomaly level
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("novatrade.monitor.watchdog")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class WatchdogConfig:
    """Tunable thresholds for the trade stall watchdog."""

    # Level 1: soft anomaly
    soft_no_alert_minutes: float = 90.0  # no inbound alerts for this long
    soft_stale_quote_minutes: float = 10.0  # quotes older than this

    # Level 2: major anomaly
    major_no_trade_minutes: float = 180.0  # 3 hours no trades
    major_min_corroborating: int = 2  # need >=2 corroborating signals

    # Level 3: critical
    critical_consecutive_startup_failures: int = 3
    critical_readiness_fail_minutes: float = 15.0  # readiness failing continuously

    # Market session awareness
    forex_sessions_utc: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "sydney": (21, 6),    # 21:00–06:00 UTC
        "tokyo": (0, 9),      # 00:00–09:00 UTC
        "london": (7, 16),    # 07:00–16:00 UTC
        "new_york": (12, 21), # 12:00–21:00 UTC
    })
    # Weekend: no FX trading from Friday 21:00 UTC to Sunday 21:00 UTC
    weekend_close_day: int = 4   # Friday (0=Monday)
    weekend_close_hour: int = 21
    weekend_open_day: int = 6    # Sunday
    weekend_open_hour: int = 21

    # Check interval (how often the watchdog evaluates)
    check_interval_seconds: float = 120.0  # 2 minutes


# ---------------------------------------------------------------------------
# Anomaly levels and evidence
# ---------------------------------------------------------------------------


class AnomalyLevel(Enum):
    NONE = "NONE"
    SOFT = "SOFT"           # Level 1
    MAJOR = "MAJOR"         # Level 2
    CRITICAL = "CRITICAL"   # Level 3


class EvidenceSignal(Enum):
    """Individual signals that contribute to anomaly detection."""
    NO_ALERTS_RECEIVED = "no_alerts_received"
    NO_TRADES_EXECUTED = "no_trades_executed"
    ADAPTER_DISCONNECTED = "adapter_disconnected"
    READINESS_DEGRADED = "readiness_degraded"
    QUOTES_STALE = "quotes_stale"
    WEBHOOK_INACTIVE = "webhook_inactive"
    REPEATED_REJECTIONS = "repeated_rejections"
    STARTUP_CRASH_LOOP = "startup_crash_loop"
    MONITOR_CYCLE_FAILING = "monitor_cycle_failing"


@dataclass
class WatchdogEvidence:
    """Collected evidence for the current evaluation cycle."""
    signals: list[EvidenceSignal] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    def has(self, signal: EvidenceSignal) -> bool:
        return signal in self.signals

    def add(self, signal: EvidenceSignal, detail: str = "") -> None:
        if signal not in self.signals:
            self.signals.append(signal)
        if detail:
            self.details[signal.value] = detail

    def to_dict(self) -> dict:
        return {
            "signals": [s.value for s in self.signals],
            "details": self.details,
            "signal_count": self.signal_count,
            "timestamp": self.timestamp,
        }


@dataclass
class WatchdogVerdict:
    """Result of a single watchdog evaluation cycle."""
    level: AnomalyLevel
    evidence: WatchdogEvidence
    diagnosis: str = ""
    recommended_actions: list[str] = field(default_factory=list)
    is_market_active: bool = True
    suppressed: bool = False  # True if suppressed by false-positive guard
    suppression_reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "evidence": self.evidence.to_dict(),
            "diagnosis": self.diagnosis,
            "recommended_actions": self.recommended_actions,
            "is_market_active": self.is_market_active,
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Runtime state snapshot (injected by the caller each cycle)
# ---------------------------------------------------------------------------


@dataclass
class RuntimeHealthSnapshot:
    """Point-in-time system state provided to the watchdog each cycle.

    The watchdog does NOT reach into runtime internals; the caller
    gathers this snapshot and hands it over.
    """
    adapter_connected: bool = False
    readiness_ok: bool = True
    webhook_server_up: bool = True
    last_alert_time: float = 0.0
    last_trade_time: float = 0.0
    last_quote_time: float = 0.0
    alerts_received_total: int = 0
    alerts_rejected_total: int = 0
    trades_executed_today: int = 0
    monitor_cycles_failed: int = 0
    consecutive_startup_failures: int = 0
    risk_halted: bool = False
    uptime_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class TradeStallWatchdog:
    """Autonomous trade-stall and runtime health watchdog.

    Call ``evaluate()`` periodically (e.g. every 2 minutes from the
    monitor loop).  It returns a ``WatchdogVerdict`` with the anomaly
    level, collected evidence, and recommended actions.

    The watchdog never takes action directly — it only diagnoses and
    recommends.  The caller decides what to execute.
    """

    def __init__(self, config: WatchdogConfig | None = None) -> None:
        self._config = config or WatchdogConfig()
        self._last_verdict: WatchdogVerdict | None = None
        self._escalation_count: int = 0  # consecutive non-NONE verdicts
        self._started_at: float = time.time()

    @property
    def config(self) -> WatchdogConfig:
        return self._config

    @property
    def last_verdict(self) -> WatchdogVerdict | None:
        return self._last_verdict

    # --- Main evaluation -----------------------------------------------------

    def evaluate(
        self,
        snapshot: RuntimeHealthSnapshot,
        *,
        now: float | None = None,
    ) -> WatchdogVerdict:
        """Run a single watchdog evaluation cycle.

        Args:
            snapshot: Current runtime health state.
            now: Override current time (for testing). Defaults to time.time().

        Returns:
            WatchdogVerdict with anomaly level, evidence, and recommendations.
        """
        now = now or time.time()
        evidence = WatchdogEvidence(timestamp=now)

        # --- Collect evidence ------------------------------------------------
        self._check_adapter(snapshot, evidence)
        self._check_alerts(snapshot, evidence, now)
        self._check_trades(snapshot, evidence, now)
        self._check_quotes(snapshot, evidence, now)
        self._check_webhook(snapshot, evidence)
        self._check_rejections(snapshot, evidence)
        self._check_readiness(snapshot, evidence)
        self._check_monitor_health(snapshot, evidence)
        self._check_startup_crashes(snapshot, evidence)

        # --- Determine anomaly level -----------------------------------------
        level = self._classify_level(snapshot, evidence, now)

        # --- False-positive suppression --------------------------------------
        is_market_active = self._is_market_active(now)
        suppressed = False
        suppression_reason = ""

        if level != AnomalyLevel.NONE and not is_market_active:
            # Suppress soft/major during market-closed windows UNLESS
            # critical (crash loop, hard failure)
            if level in (AnomalyLevel.SOFT, AnomalyLevel.MAJOR):
                suppressed = True
                suppression_reason = "market closed — FX session inactive or weekend"
                level = AnomalyLevel.NONE

        if level == AnomalyLevel.MAJOR and snapshot.trades_executed_today == 0:
            # No trades today is NOT anomalous if also no alerts received
            # (strategy simply had no setups)
            if not evidence.has(EvidenceSignal.ADAPTER_DISCONNECTED) and \
               not evidence.has(EvidenceSignal.READINESS_DEGRADED) and \
               not evidence.has(EvidenceSignal.WEBHOOK_INACTIVE) and \
               snapshot.alerts_received_total == 0:
                suppressed = True
                suppression_reason = (
                    "no trades AND no alerts received — likely no strategy signals "
                    "(flat/no-setup regime), not a system failure"
                )
                level = AnomalyLevel.SOFT  # downgrade to soft

        # --- Build diagnosis and recommendations ----------------------------
        diagnosis = self._build_diagnosis(level, evidence, snapshot)
        actions = self._build_actions(level, evidence, snapshot)

        verdict = WatchdogVerdict(
            level=level,
            evidence=evidence,
            diagnosis=diagnosis,
            recommended_actions=actions,
            is_market_active=is_market_active,
            suppressed=suppressed,
            suppression_reason=suppression_reason,
        )

        # Track escalation
        if level != AnomalyLevel.NONE:
            self._escalation_count += 1
        else:
            self._escalation_count = 0

        self._last_verdict = verdict

        if level != AnomalyLevel.NONE:
            log.warning(
                "watchdog: %s anomaly detected (%d signals, escalation=%d)%s — %s",
                level.value,
                evidence.signal_count,
                self._escalation_count,
                " [SUPPRESSED]" if suppressed else "",
                diagnosis[:200],
            )
        else:
            log.debug("watchdog: no anomaly (market_active=%s)", is_market_active)

        return verdict

    # --- Evidence collectors -------------------------------------------------

    def _check_adapter(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence) -> None:
        if not snap.adapter_connected:
            ev.add(EvidenceSignal.ADAPTER_DISCONNECTED, "broker adapter reports disconnected")

    def _check_alerts(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence, now: float) -> None:
        if snap.last_alert_time > 0:
            minutes_since = (now - snap.last_alert_time) / 60
            if minutes_since > self._config.soft_no_alert_minutes:
                ev.add(
                    EvidenceSignal.NO_ALERTS_RECEIVED,
                    f"last alert {minutes_since:.0f}m ago (threshold: {self._config.soft_no_alert_minutes:.0f}m)",
                )
        elif snap.uptime_seconds > self._config.soft_no_alert_minutes * 60:
            # Never received any alert since startup, and uptime exceeds threshold
            ev.add(
                EvidenceSignal.NO_ALERTS_RECEIVED,
                f"no alerts ever received (uptime: {snap.uptime_seconds / 60:.0f}m)",
            )

    def _check_trades(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence, now: float) -> None:
        if snap.last_trade_time > 0:
            minutes_since = (now - snap.last_trade_time) / 60
            if minutes_since > self._config.major_no_trade_minutes:
                ev.add(
                    EvidenceSignal.NO_TRADES_EXECUTED,
                    f"last trade {minutes_since:.0f}m ago (threshold: {self._config.major_no_trade_minutes:.0f}m)",
                )
        elif snap.uptime_seconds > self._config.major_no_trade_minutes * 60:
            ev.add(
                EvidenceSignal.NO_TRADES_EXECUTED,
                f"no trades since startup (uptime: {snap.uptime_seconds / 60:.0f}m)",
            )

    def _check_quotes(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence, now: float) -> None:
        if snap.last_quote_time > 0:
            minutes_since = (now - snap.last_quote_time) / 60
            if minutes_since > self._config.soft_stale_quote_minutes:
                ev.add(
                    EvidenceSignal.QUOTES_STALE,
                    f"last quote {minutes_since:.1f}m ago (threshold: {self._config.soft_stale_quote_minutes:.0f}m)",
                )

    def _check_webhook(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence) -> None:
        if not snap.webhook_server_up:
            ev.add(EvidenceSignal.WEBHOOK_INACTIVE, "webhook server is not running")

    def _check_rejections(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence) -> None:
        if snap.alerts_received_total > 0:
            rejection_rate = snap.alerts_rejected_total / snap.alerts_received_total
            if rejection_rate > 0.5 and snap.alerts_rejected_total >= 3:
                ev.add(
                    EvidenceSignal.REPEATED_REJECTIONS,
                    f"rejection rate {rejection_rate:.0%} ({snap.alerts_rejected_total}/{snap.alerts_received_total})",
                )

    def _check_readiness(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence) -> None:
        if not snap.readiness_ok:
            ev.add(EvidenceSignal.READINESS_DEGRADED, "launch gate / readiness check failing")
        if snap.risk_halted:
            ev.add(EvidenceSignal.READINESS_DEGRADED, "risk engine is halted")

    def _check_monitor_health(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence) -> None:
        if snap.monitor_cycles_failed >= 3:
            ev.add(
                EvidenceSignal.MONITOR_CYCLE_FAILING,
                f"{snap.monitor_cycles_failed} monitor cycles have failed",
            )

    def _check_startup_crashes(self, snap: RuntimeHealthSnapshot, ev: WatchdogEvidence) -> None:
        if snap.consecutive_startup_failures >= self._config.critical_consecutive_startup_failures:
            ev.add(
                EvidenceSignal.STARTUP_CRASH_LOOP,
                f"{snap.consecutive_startup_failures} consecutive startup failures",
            )

    # --- Classification ------------------------------------------------------

    def _classify_level(
        self,
        snap: RuntimeHealthSnapshot,
        ev: WatchdogEvidence,
        now: float,
    ) -> AnomalyLevel:
        """Classify anomaly level from collected evidence.

        Level 3 (Critical): crash loop, or adapter hard failure preventing
        startup, or continuous readiness failure.

        Level 2 (Major): no trades for 3h during active session AND at least
        N corroborating signals (adapter down, no alerts, stale quotes, etc.).

        Level 1 (Soft): any single anomaly signal.
        """
        # --- Critical checks (always evaluated) ---
        if ev.has(EvidenceSignal.STARTUP_CRASH_LOOP):
            return AnomalyLevel.CRITICAL

        if ev.has(EvidenceSignal.ADAPTER_DISCONNECTED) and \
           ev.has(EvidenceSignal.WEBHOOK_INACTIVE):
            return AnomalyLevel.CRITICAL

        if ev.has(EvidenceSignal.ADAPTER_DISCONNECTED) and \
           ev.has(EvidenceSignal.READINESS_DEGRADED) and \
           self._escalation_count >= 3:
            return AnomalyLevel.CRITICAL

        # --- Major checks ---
        if ev.has(EvidenceSignal.NO_TRADES_EXECUTED):
            corroborating = sum(1 for s in [
                EvidenceSignal.NO_ALERTS_RECEIVED,
                EvidenceSignal.ADAPTER_DISCONNECTED,
                EvidenceSignal.READINESS_DEGRADED,
                EvidenceSignal.QUOTES_STALE,
                EvidenceSignal.WEBHOOK_INACTIVE,
                EvidenceSignal.REPEATED_REJECTIONS,
                EvidenceSignal.MONITOR_CYCLE_FAILING,
            ] if ev.has(s))

            if corroborating >= self._config.major_min_corroborating:
                return AnomalyLevel.MAJOR

        # --- Soft checks ---
        if ev.signal_count > 0:
            return AnomalyLevel.SOFT

        return AnomalyLevel.NONE

    # --- Market session awareness -------------------------------------------

    def _is_market_active(self, now: float) -> bool:
        """Check if current time falls within any forex trading session.

        Returns False during weekends (Friday 21:00 UTC – Sunday 21:00 UTC)
        and outside all configured session windows.
        """
        import datetime as dt

        utc_now = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
        weekday = utc_now.weekday()  # 0=Monday
        hour = utc_now.hour

        # Weekend check
        wc_day = self._config.weekend_close_day
        wc_hour = self._config.weekend_close_hour
        wo_day = self._config.weekend_open_day
        wo_hour = self._config.weekend_open_hour

        if weekday == wc_day and hour >= wc_hour:
            return False  # Friday after close
        if weekday in (5, 6):
            if weekday == 5:  # Saturday
                return False
            if weekday == wo_day and hour < wo_hour:
                return False  # Sunday before open
        if weekday == 0 and hour < 0:  # should not happen, but safety
            return False

        # Session check — at least one session must be active
        for _name, (start, end) in self._config.forex_sessions_utc.items():
            if start <= end:
                # Normal range (e.g., 7–16)
                if start <= hour < end:
                    return True
            else:
                # Overnight range (e.g., 21–6)
                if hour >= start or hour < end:
                    return True

        return False

    # --- Diagnosis and action builders --------------------------------------

    def _build_diagnosis(
        self,
        level: AnomalyLevel,
        ev: WatchdogEvidence,
        snap: RuntimeHealthSnapshot,
    ) -> str:
        if level == AnomalyLevel.NONE:
            return "System healthy — no anomalies detected."

        parts: list[str] = []
        if level == AnomalyLevel.CRITICAL:
            parts.append("CRITICAL OUTAGE DETECTED.")
        elif level == AnomalyLevel.MAJOR:
            parts.append("MAJOR ANOMALY: trading pipeline appears blocked.")
        else:
            parts.append("Soft anomaly: early warning.")

        for sig in ev.signals:
            detail = ev.details.get(sig.value, sig.value)
            parts.append(f"  - {detail}")

        if not snap.adapter_connected:
            parts.append("Root cause likely: broker adapter disconnected.")
        elif not snap.readiness_ok:
            parts.append("Root cause likely: readiness/launch gate degraded.")
        elif snap.risk_halted:
            parts.append("Root cause likely: risk engine halted all trading.")

        return " ".join(parts)

    def _build_actions(
        self,
        level: AnomalyLevel,
        ev: WatchdogEvidence,
        snap: RuntimeHealthSnapshot,
    ) -> list[str]:
        actions: list[str] = []

        if level == AnomalyLevel.SOFT:
            actions.append("Log anomaly and continue monitoring.")
            if ev.has(EvidenceSignal.ADAPTER_DISCONNECTED):
                actions.append("Attempt adapter auto-reconnect.")
            if ev.has(EvidenceSignal.QUOTES_STALE):
                actions.append("Re-subscribe to market data.")

        elif level == AnomalyLevel.MAJOR:
            actions.append("Run full diagnostic cycle.")
            if ev.has(EvidenceSignal.ADAPTER_DISCONNECTED):
                actions.append("Attempt adapter reconnection with exponential backoff.")
            if ev.has(EvidenceSignal.READINESS_DEGRADED):
                actions.append("Re-evaluate launch gate and report blockers.")
            if ev.has(EvidenceSignal.REPEATED_REJECTIONS):
                actions.append("Inspect rejection reasons — possible config or risk limit issue.")
            actions.append("Generate incident report for operator review.")

        elif level == AnomalyLevel.CRITICAL:
            actions.append("OPEN INCIDENT RECORD.")
            if ev.has(EvidenceSignal.STARTUP_CRASH_LOOP):
                actions.append("Inspect crash logs for root cause.")
                actions.append("If local code/config issue, attempt patch.")
            if ev.has(EvidenceSignal.ADAPTER_DISCONNECTED):
                actions.append("Verify MetaAPI credentials and account deployment status.")
                actions.append("Check external service status (MetaAPI, broker).")
            if ev.has(EvidenceSignal.WEBHOOK_INACTIVE):
                actions.append("Restart webhook server if possible.")
            actions.append("Escalate to operator with full diagnostic payload.")
            actions.append("If external dependency failure, document and implement containment.")

        return actions
