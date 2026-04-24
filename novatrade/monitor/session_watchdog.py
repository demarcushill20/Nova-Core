"""Session Watchdog — tiered anomaly detection for NovaTrade runtime.

Detects three levels of operational anomaly during expected active sessions:

  Level 1 (Minor): No alerts for X period, stale quotes, degraded readiness,
                    adapter disconnected.
  Level 2 (Major): No trades for 12 hours during active session with corroborating
                    symptoms (adapter down, readiness false, quotes stale, etc.).
  Level 3 (Critical): Crash loop, adapter hard failure, webhook not booting,
                       continuous readiness failure.

Safeguards against false positives:
  - Respects market session / configured active trading windows
  - Respects weekends / market closed conditions
  - Respects strategy flat/no-signal regimes
  - Distinguishes "no trades because no valid setup" from "system broken"
  - Uses evidence from alerts, quotes, readiness, adapter state, rejection counters

Persists state to STATE/novatrade/session_watchdog.json for heartbeat visibility.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from novatrade.monitor.signal_monitor import (
    get_session,
    is_weekend,
    load_signal_stats,
)

log = logging.getLogger("novatrade.monitor.session_watchdog")

STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")
WATCHDOG_STATE_FILE = STATE_DIR / "session_watchdog.json"
LIVE_METRICS_FILE = STATE_DIR / "live_metrics.json"
SIGNAL_STATS_FILE = STATE_DIR / "signal_stats.json"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AnomalyLevel(Enum):
    """Severity tier for detected anomalies."""

    NONE = "NONE"
    LEVEL_1 = "LEVEL_1"  # Minor — log + diagnostics + early warning
    LEVEL_2 = "LEVEL_2"  # Major — investigate + diagnose + repair plan
    LEVEL_3 = "LEVEL_3"  # Critical — incident + contain + escalate


class WatchdogAction(Enum):
    """Action to take in response to a detected anomaly."""

    NONE = "NONE"
    LOG_ANOMALY = "LOG_ANOMALY"
    RUN_DIAGNOSTICS = "RUN_DIAGNOSTICS"
    SEND_EARLY_WARNING = "SEND_EARLY_WARNING"
    INVESTIGATE = "INVESTIGATE"
    GENERATE_REPAIR_PLAN = "GENERATE_REPAIR_PLAN"
    APPLY_FIX = "APPLY_FIX"
    RESTART_SERVICE = "RESTART_SERVICE"
    OPEN_INCIDENT = "OPEN_INCIDENT"
    ESCALATE = "ESCALATE"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SymptomEvidence:
    """Evidence gathered for anomaly classification."""

    # Adapter state
    adapter_connected: bool = True
    adapter_state: str = "OK"  # OK, DEGRADED, DOWN
    adapter_latency_ms: float = 0.0

    # Alert/signal state
    last_alert_age_seconds: float = 0.0
    alerts_received_1h: int = 0
    signals_1h: int = 0
    signals_4h: int = 0

    # Quote/feed state
    quotes_stale: bool = False
    quote_age_seconds: float = 0.0

    # Readiness state
    readiness_ok: bool = True
    readiness_degraded: bool = False
    readiness_failures: int = 0

    # Webhook state
    webhook_active: bool = True
    last_webhook_age_seconds: float = 0.0

    # Rejection tracking
    rejected_signals: int = 0
    consecutive_rejections: int = 0

    # Trade state
    last_trade_age_seconds: float = 0.0
    trades_today: int = 0

    # Market context
    session: str = ""  # weekend, asian, london, overlap, newyork
    regime: str = "ranging"
    market_closed: bool = False

    # Crash indicators
    startup_failures: int = 0
    consecutive_health_failures: int = 0


@dataclass
class AnomalyAssessment:
    """Result of a single watchdog evaluation cycle."""

    level: AnomalyLevel = AnomalyLevel.NONE
    symptoms: list[str] = field(default_factory=list)
    actions: list[WatchdogAction] = field(default_factory=list)
    evidence: SymptomEvidence = field(default_factory=SymptomEvidence)
    suppressed: bool = False  # True if anomaly was suppressed by safeguards
    suppression_reason: str = ""
    timestamp: float = field(default_factory=time.time)
    diagnosis: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.value
        d["actions"] = [a.value for a in self.actions]
        return d


@dataclass
class WatchdogState:
    """Persistent watchdog state across evaluation cycles."""

    # Escalation tracking
    current_level: str = "NONE"
    level_since: float = 0.0
    consecutive_level1: int = 0
    consecutive_level2: int = 0
    consecutive_level3: int = 0

    # Health failure tracking
    consecutive_health_failures: int = 0
    last_healthy_at: float = 0.0

    # Alert tracking
    last_alert_at: float = 0.0
    last_trade_at: float = 0.0
    last_webhook_at: float = 0.0

    # Cooldown tracking (avoid alert storms)
    last_level1_alert_at: float = 0.0
    last_level2_alert_at: float = 0.0
    last_level3_alert_at: float = 0.0

    # Crash tracking
    startup_failures: int = 0
    last_startup_failure_at: float = 0.0

    # Metadata
    updated_at: float = 0.0
    evaluations_run: int = 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogConfig:
    """Immutable watchdog configuration with escalation thresholds."""

    # Level 1 thresholds
    no_alert_threshold_seconds: float = 43200.0  # 12 hours no alerts
    quote_stale_threshold_seconds: float = 120.0  # 2 minutes stale
    max_consecutive_rejections: int = 5

    # Level 2 thresholds
    no_trade_threshold_seconds: float = 43200.0  # 12 hours no trades
    level2_min_symptoms: int = 1  # at least 1 corroborating symptom

    # Level 3 thresholds
    crash_loop_threshold: int = 3  # 3 startup failures
    crash_loop_window_seconds: float = 1800.0  # within 30 minutes
    continuous_health_failure_threshold: int = 10  # 10 consecutive failures

    # Cooldown periods (seconds between re-alerting at each level)
    level1_cooldown_seconds: float = 14400.0  # 4 hours
    level2_cooldown_seconds: float = 43200.0  # 12 hours — match trade gap threshold
    level3_cooldown_seconds: float = 300.0  # 5 minutes

    # Session leniency
    asian_session_multiplier: float = 2.0  # 2x thresholds during Asian
    quiet_regime_multiplier: float = 1.5  # 1.5x thresholds during quiet regime
    quiet_regime_max_hours: float = 6.0  # Hard ceiling — quiet suppression expires after this


# ---------------------------------------------------------------------------
# Session Watchdog
# ---------------------------------------------------------------------------


class SessionWatchdog:
    """Tiered anomaly detection for NovaTrade runtime sessions.

    Evaluates system health evidence and classifies anomalies into three
    escalation levels, while suppressing false positives from market closure,
    session transitions, and strategy flat regimes.

    Usage::

        watchdog = SessionWatchdog()
        assessment = watchdog.evaluate(evidence)
        if assessment.level != AnomalyLevel.NONE:
            handle_anomaly(assessment)
    """

    def __init__(
        self,
        config: WatchdogConfig | None = None,
        *,
        clock: _ClockFn | None = None,
    ) -> None:
        self._cfg = config or WatchdogConfig()
        self._clock = clock or time.time
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, evidence: SymptomEvidence) -> AnomalyAssessment:
        """Evaluate the current system state and classify any anomaly.

        Args:
            evidence: Gathered symptom evidence from all monitoring sources.

        Returns:
            AnomalyAssessment with level, symptoms, and recommended actions.
        """
        self._state.evaluations_run += 1
        now = self._clock()

        assessment = AnomalyAssessment(
            evidence=evidence,
            timestamp=now,
        )

        # -- Safeguard 1: Market closed (weekends) --------------------------
        if evidence.market_closed:
            assessment.suppressed = True
            assessment.suppression_reason = "market_closed"
            assessment.diagnosis = "Market is closed (weekend). No anomaly expected."
            self._persist_state(now)
            return assessment

        # -- Safeguard 2: Weekend detection via datetime --------------------
        utc_now = datetime.fromtimestamp(now, tz=timezone.utc)
        if is_weekend(utc_now):
            assessment.suppressed = True
            assessment.suppression_reason = "weekend"
            assessment.diagnosis = "Weekend — forex market closed. All checks suppressed."
            self._persist_state(now)
            return assessment

        # -- Gather session context -----------------------------------------
        session = evidence.session or get_session(utc_now)
        evidence.session = session

        # -- Apply session/regime leniency multipliers ----------------------
        threshold_multiplier = self._get_threshold_multiplier(session, evidence.regime)

        # -- Check Level 3 first (most severe) ------------------------------
        level3_symptoms = self._check_level3(evidence, now)
        if level3_symptoms:
            assessment.level = AnomalyLevel.LEVEL_3
            assessment.symptoms = level3_symptoms
            assessment.actions = [
                WatchdogAction.OPEN_INCIDENT,
                WatchdogAction.RUN_DIAGNOSTICS,
                WatchdogAction.ESCALATE,
            ]
            assessment.diagnosis = self._diagnose_level3(level3_symptoms, evidence)
            self._update_escalation(AnomalyLevel.LEVEL_3, now)
            self._persist_state(now)
            log.critical("Level 3 anomaly detected: %s", assessment.diagnosis)
            return assessment

        # -- Check Level 2 --------------------------------------------------
        level2_symptoms = self._check_level2(evidence, now, threshold_multiplier)
        if level2_symptoms:
            assessment.level = AnomalyLevel.LEVEL_2
            assessment.symptoms = level2_symptoms
            assessment.actions = [
                WatchdogAction.INVESTIGATE,
                WatchdogAction.RUN_DIAGNOSTICS,
                WatchdogAction.GENERATE_REPAIR_PLAN,
            ]
            assessment.diagnosis = self._diagnose_level2(level2_symptoms, evidence)
            self._update_escalation(AnomalyLevel.LEVEL_2, now)
            self._persist_state(now)
            log.warning("Level 2 anomaly detected: %s", assessment.diagnosis)
            return assessment

        # -- Check Level 1 --------------------------------------------------
        level1_symptoms = self._check_level1(evidence, now, threshold_multiplier)
        if level1_symptoms:
            # Safeguard 3: Suppress Level 1 during Asian session + quiet regime
            if session == "asian" and evidence.regime == "quiet":
                gap_hours = evidence.last_trade_age_seconds / 3600.0
                if gap_hours <= self._cfg.quiet_regime_max_hours:
                    assessment.suppressed = True
                    assessment.suppression_reason = "asian_quiet_regime"
                    assessment.diagnosis = (
                        "Level 1 symptoms detected but suppressed:"
                        " Asian session with quiet regime — low activity expected."
                    )
                    self._persist_state(now)
                    return assessment
                log.warning(
                    "Quiet regime suppression EXPIRED (%.1fh > %.1fh ceiling) — allowing Level 1 escalation.",
                    gap_hours,
                    self._cfg.quiet_regime_max_hours,
                )

            assessment.level = AnomalyLevel.LEVEL_1
            assessment.symptoms = level1_symptoms
            assessment.actions = [
                WatchdogAction.LOG_ANOMALY,
                WatchdogAction.RUN_DIAGNOSTICS,
                WatchdogAction.SEND_EARLY_WARNING,
            ]
            assessment.diagnosis = self._diagnose_level1(level1_symptoms, evidence)
            self._update_escalation(AnomalyLevel.LEVEL_1, now)
            self._persist_state(now)
            log.info("Level 1 anomaly detected: %s", assessment.diagnosis)
            return assessment

        # -- All clear ------------------------------------------------------
        self._update_escalation(AnomalyLevel.NONE, now)
        if evidence.adapter_connected:
            self._state.last_healthy_at = now
            self._state.consecutive_health_failures = 0
        self._persist_state(now)
        return assessment

    def gather_evidence(self) -> SymptomEvidence:
        """Gather evidence from persisted state files.

        This is a convenience method that reads STATE/novatrade/ files
        to build a SymptomEvidence snapshot without needing live adapter access.
        Useful for heartbeat-style checks.
        """
        now = self._clock()
        utc_now = datetime.fromtimestamp(now, tz=timezone.utc)
        evidence = SymptomEvidence()

        # Market context
        evidence.session = get_session(utc_now)
        evidence.market_closed = is_weekend(utc_now)

        # Signal stats
        signal_stats = load_signal_stats()
        if signal_stats:
            evidence.signals_1h = signal_stats.signals_1h
            evidence.signals_4h = signal_stats.signals_4h
            evidence.regime = signal_stats.regime or "ranging"

            if signal_stats.last_signal_at:
                try:
                    last_dt = datetime.fromisoformat(signal_stats.last_signal_at)
                    evidence.last_alert_age_seconds = now - last_dt.timestamp()
                except (ValueError, OSError):
                    pass

            if signal_stats.last_trade_signal_at:
                try:
                    last_dt = datetime.fromisoformat(signal_stats.last_trade_signal_at)
                    evidence.last_trade_age_seconds = now - last_dt.timestamp()
                except (ValueError, OSError):
                    pass

        # Live metrics (if available)
        evidence = self._load_live_metrics(evidence, now)

        # Webhook state from watchdog persisted state
        if self._state.last_webhook_at > 0:
            evidence.last_webhook_age_seconds = now - self._state.last_webhook_at
            # Consider webhook inactive if no activity in 2 hours
            evidence.webhook_active = evidence.last_webhook_age_seconds < 7200

        # Carry forward crash/health counters from persistent state
        evidence.startup_failures = self._state.startup_failures
        evidence.consecutive_health_failures = self._state.consecutive_health_failures

        return evidence

    def record_alert_received(self) -> None:
        """Record that an alert/webhook was received."""
        self._state.last_alert_at = self._clock()
        self._state.last_webhook_at = self._clock()
        self._persist_state(self._clock())

    def record_trade_executed(self) -> None:
        """Record that a trade was executed."""
        self._state.last_trade_at = self._clock()
        self._persist_state(self._clock())

    def record_health_failure(self) -> None:
        """Record a health check failure."""
        self._state.consecutive_health_failures += 1
        self._persist_state(self._clock())

    def record_health_ok(self) -> None:
        """Record a successful health check."""
        self._state.consecutive_health_failures = 0
        self._state.last_healthy_at = self._clock()
        self._persist_state(self._clock())

    def record_startup_failure(self) -> None:
        """Record a startup failure (for crash loop detection)."""
        now = self._clock()
        # Reset counter if outside crash loop window
        if now - self._state.last_startup_failure_at > self._cfg.crash_loop_window_seconds:
            self._state.startup_failures = 0
        self._state.startup_failures += 1
        self._state.last_startup_failure_at = now
        self._persist_state(now)

    def should_alert(self, level: AnomalyLevel) -> bool:
        """Check if enough time has passed since the last alert at this level."""
        now = self._clock()
        if level == AnomalyLevel.LEVEL_1:
            return (now - self._state.last_level1_alert_at) >= self._cfg.level1_cooldown_seconds
        elif level == AnomalyLevel.LEVEL_2:
            return (now - self._state.last_level2_alert_at) >= self._cfg.level2_cooldown_seconds
        elif level == AnomalyLevel.LEVEL_3:
            return (now - self._state.last_level3_alert_at) >= self._cfg.level3_cooldown_seconds
        return False

    def mark_alerted(self, level: AnomalyLevel) -> None:
        """Record that an alert was sent for the given level."""
        now = self._clock()
        if level == AnomalyLevel.LEVEL_1:
            self._state.last_level1_alert_at = now
        elif level == AnomalyLevel.LEVEL_2:
            self._state.last_level2_alert_at = now
        elif level == AnomalyLevel.LEVEL_3:
            self._state.last_level3_alert_at = now
        self._persist_state(now)

    @property
    def state(self) -> WatchdogState:
        """Access the current persistent state (read-only)."""
        return self._state

    # ------------------------------------------------------------------
    # Level checks
    # ------------------------------------------------------------------

    def _check_level3(
        self,
        evidence: SymptomEvidence,
        now: float,
    ) -> list[str]:
        """Check for Level 3 (critical outage) symptoms.

        - Startup crash loop
        - Adapter hard failure preventing runtime start
        - Webhook server not booting
        - Readiness failing continuously beyond threshold
        """
        symptoms: list[str] = []

        # Crash loop detection
        if evidence.startup_failures >= self._cfg.crash_loop_threshold:
            time_since = now - self._state.last_startup_failure_at
            if time_since < self._cfg.crash_loop_window_seconds:
                symptoms.append(f"crash_loop: {evidence.startup_failures} startup failures in {time_since:.0f}s")

        # Adapter hard failure (DOWN state)
        if evidence.adapter_state == "DOWN" and not evidence.adapter_connected:
            symptoms.append("adapter_hard_failure: adapter DOWN and disconnected")

        # Continuous readiness failure
        if evidence.consecutive_health_failures >= self._cfg.continuous_health_failure_threshold:
            symptoms.append(
                f"continuous_health_failure: {evidence.consecutive_health_failures} "
                f"consecutive failures (threshold: {self._cfg.continuous_health_failure_threshold})"
            )

        # Webhook not responding + adapter down (combined = can't trade at all)
        if not evidence.webhook_active and evidence.adapter_state == "DOWN":
            symptoms.append("total_pipeline_failure: webhook inactive AND adapter DOWN")

        return symptoms

    def _check_level2(
        self,
        evidence: SymptomEvidence,
        now: float,
        threshold_multiplier: float,
    ) -> list[str]:
        """Check for Level 2 (major anomaly) symptoms.

        Trigger: No trades for 3 hours during active trading session
        AND at least one of the corroborating symptoms.
        """
        adjusted_no_trade = self._cfg.no_trade_threshold_seconds * threshold_multiplier

        # Primary condition: no trades for extended period
        no_trades = evidence.last_trade_age_seconds > adjusted_no_trade
        if not no_trades:
            return []

        # Gather corroborating symptoms
        symptoms: list[str] = []

        if evidence.last_alert_age_seconds > adjusted_no_trade:
            symptoms.append(f"no_alerts: {evidence.last_alert_age_seconds / 3600:.1f}h since last alert")

        if not evidence.adapter_connected or evidence.adapter_state == "DOWN":
            symptoms.append(f"adapter_disconnected: state={evidence.adapter_state}")

        if not evidence.readiness_ok or evidence.readiness_degraded:
            symptoms.append(f"readiness_degraded: ok={evidence.readiness_ok}, failures={evidence.readiness_failures}")

        if evidence.quotes_stale:
            symptoms.append(f"quotes_stale: age={evidence.quote_age_seconds:.0f}s")

        if not evidence.webhook_active:
            symptoms.append(f"webhook_inactive: last={evidence.last_webhook_age_seconds / 3600:.1f}h ago")

        if evidence.consecutive_rejections >= self._cfg.max_consecutive_rejections:
            symptoms.append(f"repeated_rejections: {evidence.consecutive_rejections} consecutive")

        if evidence.signals_4h == 0 and evidence.signals_1h == 0:
            # Strategy should have been able to act but pipeline is blocked
            symptoms.append("pipeline_blocked: 0 signals in 4h window")

        # Level 2 requires the primary condition + at least N corroborating symptoms
        if len(symptoms) >= self._cfg.level2_min_symptoms:
            # Safeguard: Distinguish "no trades because no setup" from "broken"
            if self._is_legitimate_no_trade(evidence):
                return []

            symptoms.insert(
                0,
                f"no_trades: {evidence.last_trade_age_seconds / 3600:.1f}h "
                f"since last trade (threshold: {adjusted_no_trade / 3600:.1f}h)",
            )
            return symptoms

        return []

    def _check_level1(
        self,
        evidence: SymptomEvidence,
        now: float,
        threshold_multiplier: float,
    ) -> list[str]:
        """Check for Level 1 (minor anomaly) symptoms.

        Any one of:
        - No inbound alerts for X period during expected active session
        - No quotes / stale broker state
        - Runtime alive but readiness degraded
        - Adapter disconnected
        """
        symptoms: list[str] = []
        adjusted_alert = self._cfg.no_alert_threshold_seconds * threshold_multiplier
        adjusted_quote = self._cfg.quote_stale_threshold_seconds * threshold_multiplier

        # No alerts for threshold period
        if evidence.last_alert_age_seconds > adjusted_alert:
            symptoms.append(
                f"no_alerts: {evidence.last_alert_age_seconds / 3600:.1f}h "
                f"since last alert (threshold: {adjusted_alert / 3600:.1f}h)"
            )

        # Stale quotes
        if evidence.quotes_stale or evidence.quote_age_seconds > adjusted_quote:
            symptoms.append(f"stale_quotes: age={evidence.quote_age_seconds:.0f}s (threshold: {adjusted_quote:.0f}s)")

        # Degraded readiness (but not fully down — that's Level 2/3)
        if evidence.readiness_degraded and evidence.readiness_ok:
            symptoms.append(f"readiness_degraded: {evidence.readiness_failures} failures")

        # Adapter disconnected but not hard-down
        if not evidence.adapter_connected and evidence.adapter_state != "DOWN":
            symptoms.append(f"adapter_disconnected: state={evidence.adapter_state}")

        return symptoms

    # ------------------------------------------------------------------
    # Safeguards
    # ------------------------------------------------------------------

    def _is_legitimate_no_trade(self, evidence: SymptomEvidence) -> bool:
        """Distinguish 'no trades because market gave no valid setup' from 'broken'.

        Returns True if the absence of trades is explainable by market conditions
        rather than system failure.
        """
        # If adapter is connected, readiness OK, signals are flowing but
        # no trade signals generated → market gave no valid setup.
        if (
            evidence.adapter_connected
            and evidence.readiness_ok
            and not evidence.readiness_degraded
            and evidence.signals_4h > 0  # signals are flowing
            and evidence.webhook_active
            and evidence.consecutive_rejections < self._cfg.max_consecutive_rejections
        ):
            log.info(
                "No-trade period is legitimate: adapter OK, readiness OK, "
                "signals flowing (%d in 4h), no excessive rejections.",
                evidence.signals_4h,
            )
            return True

        # Quiet regime — strategy is intentionally inactive (with hard ceiling)
        if evidence.regime == "quiet" and evidence.adapter_connected:
            gap_hours = evidence.last_trade_age_seconds / 3600.0
            if gap_hours <= self._cfg.quiet_regime_max_hours:
                log.info("No-trade period legitimate: quiet regime with connected adapter (%.1fh).", gap_hours)
                return True
            log.warning(
                "Quiet regime suppression EXPIRED (%.1fh > %.1fh ceiling) — escalating.",
                gap_hours,
                self._cfg.quiet_regime_max_hours,
            )

        return False

    def _get_threshold_multiplier(self, session: str, regime: str) -> float:
        """Calculate threshold multiplier based on session and regime.

        Higher multiplier = more lenient (wider thresholds before alerting).
        """
        multiplier = 1.0

        # Session adjustments
        if session == "asian":
            multiplier *= self._cfg.asian_session_multiplier
        elif session == "overlap":
            multiplier *= 0.8  # tighter during peak hours

        # Regime adjustments
        if regime == "quiet":
            multiplier *= self._cfg.quiet_regime_multiplier
        elif regime == "volatile":
            multiplier *= 0.7  # tighter during volatile conditions

        return multiplier

    # ------------------------------------------------------------------
    # Diagnosis helpers
    # ------------------------------------------------------------------

    def _diagnose_level1(self, symptoms: list[str], evidence: SymptomEvidence) -> str:
        parts = [f"Minor anomaly during {evidence.session} session"]
        parts.append(f"({len(symptoms)} symptom(s))")
        parts.append(f"Regime: {evidence.regime}")
        parts.append("Symptoms: " + "; ".join(symptoms))
        return " | ".join(parts)

    def _diagnose_level2(self, symptoms: list[str], evidence: SymptomEvidence) -> str:
        parts = [f"Major anomaly during {evidence.session} session"]
        parts.append(f"({len(symptoms)} symptom(s))")
        parts.append(f"Regime: {evidence.regime}")
        parts.append("Symptoms: " + "; ".join(symptoms))

        # Add probable cause
        if not evidence.adapter_connected:
            parts.append("Probable cause: adapter disconnection")
        elif not evidence.webhook_active:
            parts.append("Probable cause: webhook server down")
        elif evidence.consecutive_rejections >= self._cfg.max_consecutive_rejections:
            parts.append("Probable cause: systematic signal rejection")
        elif evidence.quotes_stale:
            parts.append("Probable cause: stale data feed")
        else:
            parts.append("Probable cause: pipeline blockage (investigation needed)")

        return " | ".join(parts)

    def _diagnose_level3(self, symptoms: list[str], evidence: SymptomEvidence) -> str:
        parts = ["CRITICAL OUTAGE"]
        parts.append(f"({len(symptoms)} symptom(s))")
        parts.append("Symptoms: " + "; ".join(symptoms))

        if evidence.startup_failures >= self._cfg.crash_loop_threshold:
            parts.append("Type: crash loop — runtime cannot start")
        elif evidence.adapter_state == "DOWN":
            parts.append("Type: adapter hard failure")
        elif evidence.consecutive_health_failures >= self._cfg.continuous_health_failure_threshold:
            parts.append("Type: continuous health degradation")
        else:
            parts.append("Type: total pipeline failure")

        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Escalation tracking
    # ------------------------------------------------------------------

    def _update_escalation(self, level: AnomalyLevel, now: float) -> None:
        """Track consecutive evaluations at each level."""
        level_str = level.value

        if self._state.current_level != level_str:
            # Level changed — reset counters
            self._state.current_level = level_str
            self._state.level_since = now
            self._state.consecutive_level1 = 0
            self._state.consecutive_level2 = 0
            self._state.consecutive_level3 = 0

        if level == AnomalyLevel.LEVEL_1:
            self._state.consecutive_level1 += 1
        elif level == AnomalyLevel.LEVEL_2:
            self._state.consecutive_level2 += 1
        elif level == AnomalyLevel.LEVEL_3:
            self._state.consecutive_level3 += 1

    # ------------------------------------------------------------------
    # Live metrics loader
    # ------------------------------------------------------------------

    def _load_live_metrics(self, evidence: SymptomEvidence, now: float) -> SymptomEvidence:
        """Load live metrics from STATE/novatrade/live_metrics.json."""
        try:
            if LIVE_METRICS_FILE.exists():
                data = json.loads(LIVE_METRICS_FILE.read_text())

                evidence.rejected_signals = data.get("rejected", 0)
                evidence.trades_today = data.get("approved", 0)

                # Estimate consecutive rejections from recent data
                total_signals = data.get("signals_entry", 0) + data.get("signals_exit", 0)
                if total_signals > 0 and data.get("approved", 0) == 0:
                    evidence.consecutive_rejections = data.get("rejected", 0)

        except (json.JSONDecodeError, OSError) as exc:
            log.debug("Failed to load live metrics: %s", exc)

        return evidence

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> WatchdogState:
        """Load persistent state from disk."""
        try:
            if WATCHDOG_STATE_FILE.exists():
                data = json.loads(WATCHDOG_STATE_FILE.read_text())
                return WatchdogState(**{k: v for k, v in data.items() if k in WatchdogState.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            log.warning("Failed to load watchdog state: %s", exc)
        return WatchdogState()

    def _persist_state(self, now: float) -> None:
        """Atomically persist state to disk."""
        self._state.updated_at = now
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self._state), f, indent=2)
            os.replace(tmp, str(WATCHDOG_STATE_FILE))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Type alias (matching signal_monitor pattern)
# ---------------------------------------------------------------------------
_ClockFn = Callable[[], float]


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


_global_watchdog: SessionWatchdog | None = None


def get_watchdog(config: WatchdogConfig | None = None) -> SessionWatchdog:
    """Get or create the global SessionWatchdog instance."""
    global _global_watchdog
    if _global_watchdog is None:
        _global_watchdog = SessionWatchdog(config)
    return _global_watchdog


def run_watchdog_check(config: WatchdogConfig | None = None) -> AnomalyAssessment:
    """Run a single watchdog evaluation using persisted state files.

    Convenience function for heartbeat integration.
    """
    watchdog = get_watchdog(config)
    evidence = watchdog.gather_evidence()
    return watchdog.evaluate(evidence)
