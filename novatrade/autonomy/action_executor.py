"""Direct Action Executor — takes immediate corrective action on critical decisions.

Instead of only generating task files for the watcher to pick up later, the
action executor runs inline during the heartbeat autonomy cycle.  This closes
the gap between "decide" and "act" for known, safe remediation patterns.

Actions are:
  1. Diagnose  — gather concrete evidence about why a dimension is failing
  2. Remediate — attempt safe, reversible fixes (service restarts, reconnects)
  3. Alert     — send Telegram notification with diagnosis + what was attempted
  4. Escalate  — if remediation fails, flag for human attention
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.schemas import ProgressReport

try:
    from novatrade.autonomy.investigation_executor import InvestigationExecutor as _InvestigationExecutor
except ImportError:
    _InvestigationExecutor = None  # type: ignore[assignment,misc]

log = logging.getLogger("novatrade.autonomy.action_executor")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DiagnosticFinding:
    """A single finding from a diagnostic check."""

    check: str
    status: str  # "ok", "warning", "critical"
    detail: str


@dataclass
class ActionResult:
    """Result of a direct action execution."""

    decision_mode: str
    target_dimension: str | None
    findings: list[DiagnosticFinding] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    alert_sent: bool = False
    escalated: bool = False
    summary: str = ""
    investigation_summary: str | None = None
    root_cause: str | None = None


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


class DirectActionExecutor:
    """Runs immediate diagnostics and safe remediation for critical decisions.

    Called inline during the heartbeat autonomy cycle — no task-file round-trip.
    """

    # Services that we are allowed to restart (safe, idempotent)
    RESTARTABLE_SERVICES = ("novacore-novatrade",)

    # NovaTrade dimensions — Telegram alerts suppressed for these; the session
    # watchdog's 12h no-trade threshold is the sole NovaTrade alert channel.
    _NOVATRADE_DIMENSIONS = frozenset(
        {
            "execution_pipeline",
            "strategy_validity",
            "risk_engine",
            "performance_stability",
        }
    )

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self.base_path = Path(base_path)
        self._state_dir = self.base_path / "STATE"
        self._novatrade_state = self._state_dir / "novatrade"
        self._logs_dir = self.base_path / "LOGS"

    def execute(self, decision: Decision, report: ProgressReport) -> ActionResult:
        """Run diagnostics and attempt remediation based on the decision."""
        result = ActionResult(
            decision_mode=decision.mode.value,
            target_dimension=decision.target_dimension,
        )

        # Only act on actionable modes
        if decision.mode == ActionMode.MONITOR:
            result.summary = "MONITOR mode — no action needed"
            return result

        # NovaTrade dimensions → log-only, no Telegram.  The session watchdog's
        # 12h no-trade threshold is the sole NovaTrade Telegram channel.
        _nt_suppress = (decision.target_dimension or "") in self._NOVATRADE_DIMENSIONS

        # ESCALATE: skip remediation, gather evidence only, always alert
        if decision.mode == ActionMode.ESCALATE:
            if decision.target_dimension:
                self._run_diagnostics(decision, report, result)
            result.escalated = True
            # Launch investigation to provide root cause context for escalation
            if _InvestigationExecutor is not None:
                try:
                    investigator = _InvestigationExecutor(base_path=str(self.base_path))
                    inv_report = investigator.investigate(decision=decision, report=report)
                    result.investigation_summary = inv_report.recommended_action or ""
                    result.root_cause = inv_report.root_cause or ""
                    log.info(
                        "Escalation investigation: %s (confidence=%s)",
                        inv_report.root_cause or "no root cause",
                        inv_report.root_cause_confidence,
                    )
                except Exception as inv_exc:
                    log.warning("Escalation investigation failed (non-fatal): %s", inv_exc)
            if _nt_suppress:
                self._log_alert_only(decision, result)
            else:
                self._send_alert(decision, result)
            # Build summary
            critical = [f for f in result.findings if f.status == "critical"]
            warnings = [f for f in result.findings if f.status == "warning"]
            root_info = f" — root cause: {result.root_cause}" if result.root_cause else ""
            result.summary = (
                f"ESCALATE on {decision.target_dimension or 'system'}: "
                f"{len(critical)} critical, {len(warnings)} warnings — "
                f"human intervention required{root_info}"
            )
            return result

        # Always run dimension-specific diagnostics
        if decision.target_dimension:
            self._run_diagnostics(decision, report, result)

        # For REPAIR/EXECUTE: attempt remediation
        if decision.mode in (ActionMode.REPAIR, ActionMode.EXECUTE):
            self._attempt_remediation(decision, report, result)

        # Alert on actionable decisions only (REPAIR/EXECUTE).
        # RESEARCH/PLAN are informational — log only, don't spam Telegram.
        # NovaTrade dimensions are always log-only (watchdog handles alerting).
        if _nt_suppress:
            self._log_alert_only(decision, result)
        elif decision.mode in (ActionMode.REPAIR, ActionMode.EXECUTE):
            self._send_alert(decision, result)
        elif decision.mode in (ActionMode.RESEARCH, ActionMode.PLAN):
            self._log_alert_only(decision, result)

        # Build summary
        critical = [f for f in result.findings if f.status == "critical"]
        warnings = [f for f in result.findings if f.status == "warning"]
        result.summary = (
            f"{decision.mode.value.upper()} on {decision.target_dimension or 'system'}: "
            f"{len(critical)} critical, {len(warnings)} warnings, "
            f"{len(result.actions_taken)} actions taken"
        )

        # Verify remediation worked and send follow-up alert
        if decision.mode in (ActionMode.REPAIR, ActionMode.EXECUTE) and result.actions_taken:
            self._verify_and_followup(decision, report, result)

        return result

    # -------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------

    def _run_diagnostics(self, decision: Decision, report: ProgressReport, result: ActionResult) -> None:
        """Run targeted diagnostics based on the failing dimension."""
        dim = decision.target_dimension or ""

        if dim == "strategy_validity":
            self._diagnose_strategy(report, result)
        elif dim == "execution_pipeline":
            self._diagnose_pipeline(report, result)
        elif dim == "system_health":
            self._diagnose_system_health(report, result)
        elif dim == "risk_engine":
            self._diagnose_risk(report, result)
        elif dim == "performance_stability":
            self._diagnose_performance(report, result)
        else:
            # Generic: check all dimensions for sub-metric failures
            self._diagnose_generic(report, result)

    def _diagnose_strategy(self, report: ProgressReport, result: ActionResult) -> None:
        """Diagnose strategy_validity failures — the 'not trading' detector."""
        dim = report.dimensions.get("strategy_validity")
        if not dim:
            result.findings.append(
                DiagnosticFinding(
                    check="strategy_validity_exists",
                    status="critical",
                    detail="strategy_validity dimension missing from report",
                )
            )
            return

        # Check each sub-metric
        for sm in dim.sub_metrics:
            if sm.name == "trades_last_24h" and sm.value <= 20:
                result.findings.append(
                    DiagnosticFinding(
                        check="trades_last_24h",
                        status="critical",
                        detail=f"ZERO trades in the last 24h (score={sm.value:.0f}). "
                        "NovaTrade is not executing trades.",
                    )
                )
            elif sm.name == "silent_failure_detected" and sm.value < 50:
                result.findings.append(
                    DiagnosticFinding(
                        check="silent_failure",
                        status="critical",
                        detail=f"Silent failure detected — no signal output for 4+ hours "
                        f"during market hours (score={sm.value:.0f}).",
                    )
                )
            elif sm.name == "signal_generation_rate" and sm.value < 30:
                result.findings.append(
                    DiagnosticFinding(
                        check="signal_rate",
                        status="warning",
                        detail=f"Signal generation rate is very low (score={sm.value:.0f}).",
                    )
                )
            elif sm.name == "signal_pipeline_health" and sm.value < 50:
                result.findings.append(
                    DiagnosticFinding(
                        check="pipeline_health",
                        status="warning",
                        detail=f"Signal pipeline health degraded (score={sm.value:.0f}).",
                    )
                )

        # Check trade journal directly (source of truth: JSONL format)
        journal_path = self._novatrade_state / "trade_journal.jsonl"
        if journal_path.exists():
            try:
                open_count = 0
                with open(journal_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("event") == "OPEN":
                                open_count += 1
                        except json.JSONDecodeError:
                            continue
                if open_count == 0:
                    result.findings.append(
                        DiagnosticFinding(
                            check="trade_journal_empty",
                            status="critical",
                            detail="trade_journal.jsonl exists but contains zero OPEN events.",
                        )
                    )
            except OSError:
                result.findings.append(
                    DiagnosticFinding(
                        check="trade_journal_corrupt",
                        status="warning",
                        detail="trade_journal.jsonl exists but is unreadable.",
                    )
                )
        else:
            result.findings.append(
                DiagnosticFinding(
                    check="trade_journal_missing",
                    status="critical",
                    detail="trade_journal.jsonl does not exist — NovaTrade may never have traded.",
                )
            )

        # Check NovaTrade service status
        self._check_service_status("novacore-novatrade", result)

        # Check halt state from the canonical risk-engine state file.
        risk_state_path = self._state_dir / "novatrade_risk_state.json"
        if risk_state_path.exists():
            try:
                rs = json.loads(risk_state_path.read_text())
                if rs.get("halted") or rs.get("breached"):
                    reason = rs.get("halt_reason") or rs.get("reason") or "unknown reason"
                    result.findings.append(
                        DiagnosticFinding(
                            check="risk_halt_active",
                            status="critical",
                            detail=f"Risk halt is ACTIVE: {reason}. Trading is stopped by the risk engine.",
                        )
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # Check MetaApi connection
        conn_path = self._novatrade_state / "connection_status.json"
        if conn_path.exists():
            try:
                conn = json.loads(conn_path.read_text())
                status = conn.get("status", "unknown")
                if status not in ("connected", "ok"):
                    result.findings.append(
                        DiagnosticFinding(
                            check="metaapi_connection",
                            status="critical",
                            detail=f"MetaApi connection status: {status}",
                        )
                    )
            except (json.JSONDecodeError, OSError):
                pass

    def _diagnose_pipeline(self, report: ProgressReport, result: ActionResult) -> None:
        """Diagnose execution_pipeline failures."""
        dim = report.dimensions.get("execution_pipeline")
        if not dim:
            return

        for sm in dim.sub_metrics:
            if sm.name == "pipeline_connectivity" and sm.value < 50:
                result.findings.append(
                    DiagnosticFinding(
                        check="broker_connectivity",
                        status="critical",
                        detail=f"Broker connectivity is degraded (score={sm.value:.0f}). High contract failure rate.",
                    )
                )
            elif sm.name == "rejection_rate" and sm.value < 50:
                result.findings.append(
                    DiagnosticFinding(
                        check="order_rejections",
                        status="critical",
                        detail=f"High order rejection rate (score={sm.value:.0f}). Broker may be rejecting orders.",
                    )
                )
            elif sm.name == "feed_health" and sm.value < 30:
                result.findings.append(
                    DiagnosticFinding(
                        check="feed_stale",
                        status="warning",
                        detail=f"Price feed data is stale (score={sm.value:.0f}).",
                    )
                )

        # Check NovaTrade service for pipeline issues too
        self._check_service_status("novacore-novatrade", result)

    def _diagnose_system_health(self, report: ProgressReport, result: ActionResult) -> None:
        """Diagnose system_health failures."""
        dim = report.dimensions.get("system_health")
        if not dim:
            return

        for sm in dim.sub_metrics:
            if sm.name == "service_status" and sm.value < 75:
                result.findings.append(
                    DiagnosticFinding(
                        check="services_down",
                        status="critical",
                        detail=f"One or more services are down (score={sm.value:.0f}).",
                    )
                )
            elif sm.name == "error_rate" and sm.value < 50:
                result.findings.append(
                    DiagnosticFinding(
                        check="high_error_rate",
                        status="warning",
                        detail=f"High error rate in logs (score={sm.value:.0f}).",
                    )
                )

    def _diagnose_risk(self, report: ProgressReport, result: ActionResult) -> None:
        """Diagnose risk_engine failures."""
        dim = report.dimensions.get("risk_engine")
        if not dim:
            return

        for sm in dim.sub_metrics:
            if sm.value < 30:
                result.findings.append(
                    DiagnosticFinding(
                        check=f"risk_{sm.name}",
                        status="warning",
                        detail=f"Risk sub-metric '{sm.name}' is low (score={sm.value:.0f}).",
                    )
                )

    def _diagnose_performance(self, report: ProgressReport, result: ActionResult) -> None:
        """Diagnose performance_stability failures."""
        dim = report.dimensions.get("performance_stability")
        if not dim:
            return

        for sm in dim.sub_metrics:
            if sm.name == "max_drawdown_30d" and sm.value < 30:
                result.findings.append(
                    DiagnosticFinding(
                        check="excessive_drawdown",
                        status="critical",
                        detail=f"Drawdown exceeding safe limits (score={sm.value:.0f}).",
                    )
                )
            elif sm.value < 30:
                result.findings.append(
                    DiagnosticFinding(
                        check=f"perf_{sm.name}",
                        status="warning",
                        detail=f"Performance metric '{sm.name}' is low (score={sm.value:.0f}).",
                    )
                )

    def _diagnose_generic(self, report: ProgressReport, result: ActionResult) -> None:
        """Generic diagnostics — check all dimensions for failing sub-metrics."""
        for dim_name, dim in report.dimensions.items():
            if dim.score < 50:
                result.findings.append(
                    DiagnosticFinding(
                        check=f"dim_{dim_name}",
                        status="warning",
                        detail=f"Dimension '{dim_name}' is below 50 (score={dim.score:.0f}).",
                    )
                )

    # -------------------------------------------------------------------
    # Remediation
    # -------------------------------------------------------------------

    def _attempt_remediation(self, decision: Decision, report: ProgressReport, result: ActionResult) -> None:
        """Attempt safe, reversible fixes for critical findings."""
        critical = [f for f in result.findings if f.status == "critical"]
        if not critical:
            return

        # Check if NovaTrade service needs restart
        service_down = any(f.check == "service_status" and "inactive" in f.detail.lower() for f in result.findings)
        trade_failure = any(
            f.check
            in (
                "trades_last_24h",
                "trade_journal_empty",
                "trade_journal_missing",
                "broker_connectivity",
                "order_rejections",
            )
            for f in critical
        )

        if service_down:
            self._restart_service("novacore-novatrade", result)
        elif trade_failure:
            # Service might be running but stuck — check and restart
            is_active = self._is_service_active("novacore-novatrade")
            if not is_active:
                self._restart_service("novacore-novatrade", result)
            else:
                result.actions_taken.append(
                    "novacore-novatrade is active but not trading — generated diagnostic task for deeper investigation"
                )
                result.escalated = True

    # -------------------------------------------------------------------
    # Service management helpers
    # -------------------------------------------------------------------

    def _check_service_status(self, service: str, result: ActionResult) -> None:
        """Check if a systemd service is active."""
        is_active = self._is_service_active(service)
        if is_active:
            result.findings.append(
                DiagnosticFinding(
                    check="service_status",
                    status="ok",
                    detail=f"{service} is active",
                )
            )
        else:
            result.findings.append(
                DiagnosticFinding(
                    check="service_status",
                    status="critical",
                    detail=f"{service} is inactive/dead",
                )
            )

    @staticmethod
    def _is_service_active(service: str) -> bool:
        """Check if a systemd service is active."""
        try:
            cp = subprocess.run(
                ["systemctl", "is-active", f"{service}.service"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return cp.stdout.strip() == "active"
        except (subprocess.SubprocessError, OSError):
            return False

    @staticmethod
    def _is_service_enabled(service: str) -> bool:
        """Return True only if systemd reports the unit as enabled.

        Used to distinguish an *intentionally disabled* unit (operator stopped
        and disabled it on purpose — `is-enabled` -> "disabled"/"masked") from a
        unit that is enabled-but-crashed (a real regression).  A disabled unit is
        an operator-intent signal, not a fault: scoring it as a fault drives the
        doomed REPAIR->ESCALATE loop (see OUTPUT/1008 escalation).
        """
        try:
            cp = subprocess.run(
                ["systemctl", "is-enabled", f"{service}.service"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # "enabled", "enabled-runtime", "static", "indirect", "alias" -> intended to run.
            # "disabled", "masked", "linked" (and exit!=0) -> not intended to run.
            return cp.returncode == 0 and cp.stdout.strip() not in {"disabled", "masked", "linked"}
        except (subprocess.SubprocessError, OSError):
            return False

    def _restart_service(self, service: str, result: ActionResult) -> None:
        """Attempt to restart a systemd service."""
        if service not in self.RESTARTABLE_SERVICES:
            result.actions_taken.append(f"SKIP restart of {service} — not in allow-list")
            return

        try:
            cp = subprocess.run(
                ["sudo", "systemctl", "restart", f"{service}.service"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if cp.returncode == 0:
                result.actions_taken.append(f"Restarted {service}.service successfully")
                log.info("Restarted %s.service", service)
            else:
                result.actions_taken.append(f"Failed to restart {service}: exit {cp.returncode} — {cp.stderr.strip()}")
                result.escalated = True
                log.warning("Failed to restart %s: %s", service, cp.stderr.strip())
        except (subprocess.SubprocessError, OSError) as exc:
            result.actions_taken.append(f"Restart of {service} failed: {exc}")
            result.escalated = True
            log.warning("Restart of %s failed: %s", service, exc)

    # -------------------------------------------------------------------
    # Alerting
    # -------------------------------------------------------------------

    # Alert dedup: suppress duplicate (mode, dimension) alerts within this window.
    # 4 hours — one Telegram alert per issue, then log-only until the window expires.
    _ALERT_DEDUP_SECONDS = 14400  # 4 hours

    def _is_duplicate_alert(self, decision: Decision) -> bool:
        """Check if we already sent this alert recently (file-based dedup)."""
        dedup_path = self._state_dir / "autonomy_alert_dedup.json"
        sig = f"{decision.mode.value}:{decision.target_dimension or 'system'}"
        now = time.time()
        try:
            if dedup_path.exists():
                data = json.loads(dedup_path.read_text())
            else:
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}

        last_ts = data.get(sig, 0)
        if now - last_ts < self._ALERT_DEDUP_SECONDS:
            log.info("Suppressed duplicate alert %s (sent %.0fs ago)", sig, now - last_ts)
            return True

        # Record this alert
        data[sig] = now
        # Prune entries older than the dedup window
        data = {k: v for k, v in data.items() if now - v < self._ALERT_DEDUP_SECONDS}
        try:
            dedup_path.parent.mkdir(parents=True, exist_ok=True)
            dedup_path.write_text(json.dumps(data))
        except OSError:
            pass
        return False

    def _send_alert(self, decision: Decision, result: ActionResult) -> None:
        """Send Telegram alert with diagnosis and actions taken."""
        if self._is_duplicate_alert(decision):
            return

        critical = [f for f in result.findings if f.status == "critical"]
        warnings = [f for f in result.findings if f.status == "warning"]

        # Build alert message
        emoji = {
            "repair": "\u26a0\ufe0f",  # warning sign
            "execute": "\u25b6\ufe0f",  # play button
            "research": "\U0001f50d",  # magnifying glass
            "plan": "\U0001f4cb",  # clipboard
            "escalate": "\U0001f6a8",  # rotating light
        }
        icon = emoji.get(decision.mode.value, "\u2139\ufe0f")

        lines = [
            f"{icon} AUTONOMY {decision.mode.value.upper()}: {decision.target_dimension or 'system'}",
            f"Reason: {decision.reason[:120]}",
            "",
        ]

        if critical:
            lines.append(f"Critical findings ({len(critical)}):")
            for f in critical:
                lines.append(f"  \u274c {f.check}: {f.detail[:100]}")
            lines.append("")

        if warnings:
            lines.append(f"Warnings ({len(warnings)}):")
            for f in warnings[:3]:  # limit to 3 warnings
                lines.append(f"  \u26a0\ufe0f {f.check}: {f.detail[:80]}")
            lines.append("")

        if result.actions_taken:
            lines.append("Actions taken:")
            for a in result.actions_taken:
                lines.append(f"  \u2192 {a[:100]}")
            lines.append("")

        if result.root_cause:
            lines.append(f"Root cause: {result.root_cause[:150]}")
        if result.investigation_summary:
            lines.append(f"Recommendation: {result.investigation_summary[:150]}")
        if result.root_cause or result.investigation_summary:
            lines.append("")

        if result.escalated:
            lines.append("\U0001f6a8 ESCALATED — needs human attention")

        text = "\n".join(lines)

        # Send via Telegram
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
        if token and chat_id:
            try:
                data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
                urllib.request.urlopen(req, timeout=15)  # noqa: S310
                result.alert_sent = True
                log.info("Sent autonomy alert to Telegram")
            except Exception as exc:
                log.warning("Failed to send Telegram alert: %s", exc)
        else:
            log.warning("Telegram credentials not configured — alert not sent")

        # Also log to file
        self._log_to_file(text)

    def _log_alert_only(self, decision: Decision, result: ActionResult) -> None:
        """Log RESEARCH/PLAN decisions to file only — no Telegram."""
        text = f"[{decision.mode.value.upper()}] {decision.target_dimension or 'system'}: {decision.reason[:120]}"
        log.info("Autonomy %s (log-only): %s", decision.mode.value, decision.target_dimension)
        self._log_to_file(text)

    # -------------------------------------------------------------------
    # Resolution verification & follow-up
    # -------------------------------------------------------------------

    # Known core services to check for system-wide health verification
    _CORE_SERVICES = (
        "novacore-watcher",
        "novacore-telegram",
        "novacore-telegram-notifier",
        "novacore-novatrade",
    )

    def _verify_dimension(self, dimension: str | None) -> tuple[bool, str]:
        """Quick re-check of a dimension after remediation.

        Returns (resolved: bool, detail: str).
        """
        dim = dimension or ""

        if dim == "system_health":
            # Re-check all core services
            failed = []
            for svc in self._CORE_SERVICES:
                if not self._is_service_active(svc):
                    failed.append(svc)
            if failed:
                return False, f"services still down: {', '.join(failed)}"
            return True, "All services active"

        if dim in ("strategy_validity", "execution_pipeline"):
            # The most actionable check: is novacore-novatrade running?
            if self._is_service_active("novacore-novatrade"):
                return True, "novacore-novatrade is active"
            # An intentionally-disabled unit is operator intent, not a fault.
            # Treat as resolved so the engine stops the doomed REPAIR->ESCALATE
            # loop against a daemon it (a) cannot restart under NoNewPrivileges
            # and (b) was deliberately stopped + disabled. See OUTPUT/1008.
            if not self._is_service_enabled("novacore-novatrade"):
                return True, (
                    "novacore-novatrade is inactive but DISABLED (operator intent) "
                    "— not treated as a strategy_validity fault"
                )
            return False, "novacore-novatrade is still inactive"

        # Default: check all 4 core services
        failed = []
        for svc in self._CORE_SERVICES:
            if not self._is_service_active(svc):
                failed.append(svc)
        if failed:
            return False, f"services still down: {', '.join(failed)}"
        return True, "All services active"

    def _verify_and_followup(self, decision: Decision, report: ProgressReport, result: ActionResult) -> None:
        """Wait briefly, re-check the remediated dimension, and log the result.

        Follow-ups are LOG-ONLY — never send to Telegram.  The initial alert
        already informed the operator; sending repeated "RESOLVED" messages is
        noise when the underlying condition (e.g. no trades) keeps recurring.

        When unresolved and the InvestigationExecutor is available, launches a
        multi-round investigation to identify the root cause.
        """
        # Brief wait for the service to come up
        time.sleep(5)

        resolved, detail = self._verify_dimension(decision.target_dimension)
        dim_label = decision.target_dimension or "system"
        action_summary = "; ".join(a[:100] for a in result.actions_taken[:3])

        if resolved:
            text = f"\u2705 RESOLVED: {dim_label}\nAuto-fix: {action_summary}\nStatus: {detail}"
        else:
            text = (
                f"\U0001f6a8 UNRESOLVED: {dim_label}\n"
                f"Attempted: {action_summary}\n"
                f"Still failing: {detail}\n"
                f"Escalating for human attention."
            )
            result.escalated = True

            # Launch multi-round investigation for unresolved issues
            if _InvestigationExecutor is not None:
                try:
                    investigator = _InvestigationExecutor(base_path=str(self.base_path))
                    inv_report = investigator.investigate(decision=decision, report=report)
                    result.investigation_summary = inv_report.recommended_action or ""
                    result.root_cause = inv_report.root_cause or ""
                    if inv_report.root_cause:
                        text += f"\nRoot cause: {inv_report.root_cause}"
                    log.info(
                        "Investigation complete: %s (confidence=%s)",
                        inv_report.root_cause or "no root cause identified",
                        inv_report.root_cause_confidence,
                    )
                except Exception as inv_exc:
                    log.warning("Investigation failed (non-fatal): %s", inv_exc)

        log.info("Follow-up verification: %s — %s", dim_label, "RESOLVED" if resolved else "UNRESOLVED")

        # Log to file only — no Telegram for follow-ups
        self._log_to_file(f"[FOLLOWUP] {text}")

    # -------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------

    def _log_to_file(self, text: str) -> None:
        """Append to autonomy_actions.log."""
        log_path = self.base_path / "LOGS" / "autonomy_actions.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                ts = datetime.now(timezone.utc).isoformat()
                f.write(f"[{ts}] {text}\n{'=' * 60}\n")
        except OSError:
            pass
