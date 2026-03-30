"""Investigation Executor — multi-round evidence-following for complex failures.

Enhances the existing DirectActionExecutor (single-round diagnostics) with
multi-step investigation chains.  When a simple diagnostic check isn't enough,
the InvestigationExecutor follows evidence across multiple rounds:

  Round 1: Check service status, read state files
  Round 2: Follow leads from Round 1 (e.g., if service active but no trades,
           check feed health, then check MetaApi connection)
  Round 3: Cross-correlate findings and produce root cause hypothesis

Integrates with agents/self_heal_investigator.py for low-level context
collection and safe remediation.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.decision_engine import Decision
from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.investigation_executor")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class InvestigationStep:
    """A single step in a multi-round investigation."""

    round_num: int
    check: str  # what was checked
    result: str  # what was found
    status: str  # "ok", "warning", "critical", "info"
    follow_up: str | None = None  # what to check next


@dataclass
class InvestigationReport:
    """Complete investigation report across multiple rounds."""

    investigation_id: str
    target_dimension: str | None
    trigger_reason: str
    steps: list[InvestigationStep] = field(default_factory=list)
    root_cause: str | None = None
    root_cause_confidence: str = "low"  # high / medium / low
    recommended_action: str | None = None
    actions_taken: list[str] = field(default_factory=list)
    escalated: bool = False
    started_at: str = ""
    completed_at: str = ""

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Investigation chains — evidence-following patterns
# ---------------------------------------------------------------------------


_INVESTIGATION_CHAINS: dict[str, list[str]] = {
    "strategy_validity": [
        "check_service_status",
        "check_trade_log",
        "check_halt_state",
        "check_feed_health",
        "check_metaapi_connection",
        "check_signal_log",
    ],
    "execution_pipeline": [
        "check_service_status",
        "check_metaapi_connection",
        "check_order_log",
        "check_feed_health",
        "check_rejection_rate",
    ],
    "system_health": [
        "check_all_services",
        "check_disk_space",
        "check_memory",
        "check_error_rate",
    ],
    "risk_engine": [
        "check_halt_state",
        "check_daily_loss",
        "check_risk_gates",
    ],
    "performance_stability": [
        "check_equity_history",
        "check_drawdown",
        "check_trade_stats",
    ],
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class InvestigationExecutor:
    """Multi-round investigation executor for complex autonomy failures.

    Unlike DirectActionExecutor (single pass), this follows evidence chains
    across up to MAX_ROUNDS iterations, building a root-cause hypothesis.
    """

    MAX_ROUNDS = 3

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self.base_path = Path(base_path)
        self._state_dir = self.base_path / "STATE"
        self._novatrade_state = self._state_dir / "novatrade"
        self._logs_dir = self.base_path / "LOGS"
        self._investigations_path = self._state_dir / "investigations.json"

    def investigate(
        self,
        decision: Decision,
        report: ProgressReport,
    ) -> InvestigationReport:
        """Run a multi-round investigation for the given decision.

        Returns an InvestigationReport with steps, root cause, and recommendations.
        """
        inv_id = (
            f"inv_{decision.mode.value}_{decision.target_dimension or 'sys'}_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        )
        inv_report = InvestigationReport(
            investigation_id=inv_id,
            target_dimension=decision.target_dimension,
            trigger_reason=decision.reason[:200],
        )

        # Get the investigation chain for this dimension
        dimension = decision.target_dimension or ""
        chain = _INVESTIGATION_CHAINS.get(dimension, list(_INVESTIGATION_CHAINS.get("system_health", [])))

        # Run up to MAX_ROUNDS
        for round_num in range(1, self.MAX_ROUNDS + 1):
            round_checks = self._get_round_checks(chain, round_num, inv_report)
            if not round_checks:
                break

            for check_name in round_checks:
                step = self._execute_check(check_name, round_num, report)
                inv_report.steps.append(step)

                # If critical finding, generate follow-ups
                if step.status == "critical" and step.follow_up:
                    # Add follow-up to next round
                    chain.append(step.follow_up)

            # Early exit if we found a clear root cause
            root = self._synthesize_root_cause(inv_report)
            if root and root[1] == "high":
                inv_report.root_cause = root[0]
                inv_report.root_cause_confidence = root[1]
                break

        # Final synthesis
        if not inv_report.root_cause:
            root = self._synthesize_root_cause(inv_report)
            if root:
                inv_report.root_cause = root[0]
                inv_report.root_cause_confidence = root[1]

        # Recommended action
        inv_report.recommended_action = self._recommend_action(inv_report)
        inv_report.completed_at = datetime.now(timezone.utc).isoformat()

        # Persist
        self._persist_investigation(inv_report)

        log.info(
            "Investigation complete: %s — root_cause=%s (confidence=%s)",
            inv_id,
            inv_report.root_cause,
            inv_report.root_cause_confidence,
        )
        return inv_report

    # -------------------------------------------------------------------
    # Round management
    # -------------------------------------------------------------------

    def _get_round_checks(
        self,
        chain: list[str],
        round_num: int,
        inv_report: InvestigationReport,
    ) -> list[str]:
        """Get checks for a specific round."""
        already_done = {s.check for s in inv_report.steps}

        if round_num == 1:
            # First 3 checks from chain
            checks = [c for c in chain[:3] if c not in already_done]
        elif round_num == 2:
            # Next checks + any follow-ups from round 1
            checks = [c for c in chain[3:6] if c not in already_done]
        else:
            # Remaining checks + cross-correlations
            checks = [c for c in chain[6:] if c not in already_done]
            if not checks:
                checks = ["cross_correlate"]

        return checks

    # -------------------------------------------------------------------
    # Individual checks
    # -------------------------------------------------------------------

    def _execute_check(
        self,
        check_name: str,
        round_num: int,
        report: ProgressReport,
    ) -> InvestigationStep:
        """Execute a single investigation check."""
        dispatch = {
            "check_service_status": self._check_service_status,
            "check_all_services": self._check_all_services,
            "check_trade_log": self._check_trade_log,
            "check_halt_state": self._check_halt_state,
            "check_feed_health": self._check_feed_health,
            "check_metaapi_connection": self._check_metaapi_connection,
            "check_signal_log": self._check_signal_log,
            "check_order_log": self._check_order_log,
            "check_rejection_rate": self._check_rejection_rate,
            "check_disk_space": self._check_disk_space,
            "check_memory": self._check_memory,
            "check_error_rate": self._check_error_rate,
            "check_daily_loss": self._check_daily_loss,
            "check_risk_gates": self._check_risk_gates,
            "check_equity_history": self._check_equity_history,
            "check_drawdown": self._check_drawdown,
            "check_trade_stats": self._check_trade_stats,
            "cross_correlate": lambda r: self._cross_correlate(r, report),
        }

        handler = dispatch.get(check_name)
        if handler is None:
            return InvestigationStep(
                round_num=round_num,
                check=check_name,
                result=f"Unknown check: {check_name}",
                status="info",
            )

        try:
            return handler(round_num)
        except Exception as exc:
            return InvestigationStep(
                round_num=round_num,
                check=check_name,
                result=f"Check failed: {exc}",
                status="warning",
            )

    def _check_service_status(self, round_num: int) -> InvestigationStep:
        """Check novacore-novatrade service status."""
        service = "novacore-novatrade"
        try:
            cp = subprocess.run(
                ["systemctl", "is-active", f"{service}.service"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            is_active = cp.stdout.strip() == "active"
        except (subprocess.SubprocessError, OSError):
            is_active = False

        if is_active:
            return InvestigationStep(
                round_num=round_num,
                check="check_service_status",
                result=f"{service} is active",
                status="ok",
                follow_up="check_trade_log" if round_num == 1 else None,
            )
        return InvestigationStep(
            round_num=round_num,
            check="check_service_status",
            result=f"{service} is NOT active",
            status="critical",
            follow_up=None,
        )

    def _check_all_services(self, round_num: int) -> InvestigationStep:
        """Check all novacore services."""
        services = [
            "novacore-watcher",
            "novacore-telegram",
            "novacore-telegram-notifier",
            "novacore-novatrade",
        ]
        down: list[str] = []
        for svc in services:
            try:
                cp = subprocess.run(
                    ["systemctl", "is-active", f"{svc}.service"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if cp.stdout.strip() != "active":
                    down.append(svc)
            except (subprocess.SubprocessError, OSError):
                down.append(svc)

        if not down:
            return InvestigationStep(
                round_num=round_num,
                check="check_all_services",
                result="All 4 services active",
                status="ok",
            )
        return InvestigationStep(
            round_num=round_num,
            check="check_all_services",
            result=f"Services down: {', '.join(down)}",
            status="critical",
        )

    def _check_trade_log(self, round_num: int) -> InvestigationStep:
        """Check trade journal for recent activity."""
        path = self._novatrade_state / "trade_journal.jsonl"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_trade_log",
                result="trade_journal.jsonl does not exist — zero trades ever",
                status="critical",
                follow_up="check_halt_state",
            )
        try:
            open_count = 0
            with open(path) as f:
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
            return InvestigationStep(
                round_num=round_num,
                check="check_trade_log",
                result=f"{open_count} OPEN trades in journal",
                status="ok" if open_count > 0 else "critical",
                follow_up="check_feed_health" if open_count == 0 else None,
            )
        except OSError as exc:
            return InvestigationStep(
                round_num=round_num,
                check="check_trade_log",
                result=f"trade_journal.jsonl unreadable: {exc}",
                status="warning",
            )

    def _check_halt_state(self, round_num: int) -> InvestigationStep:
        """Check if trading is halted by risk engine."""
        path = self._novatrade_state / "halt_state.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_halt_state",
                result="No halt_state.json — no halt active",
                status="ok",
            )
        try:
            data = json.loads(path.read_text())
            if data.get("halted"):
                return InvestigationStep(
                    round_num=round_num,
                    check="check_halt_state",
                    result=f"HALT ACTIVE: {data.get('reason', 'unknown')}",
                    status="critical",
                )
            return InvestigationStep(
                round_num=round_num,
                check="check_halt_state",
                result="No halt active",
                status="ok",
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_halt_state",
                result="halt_state.json unreadable",
                status="warning",
            )

    def _check_feed_health(self, round_num: int) -> InvestigationStep:
        """Check price feed health from state."""
        path = self._novatrade_state / "feed_health.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_feed_health",
                result="No feed_health.json — feed status unknown",
                status="warning",
                follow_up="check_metaapi_connection",
            )
        try:
            data = json.loads(path.read_text())
            status = data.get("status", "unknown")
            return InvestigationStep(
                round_num=round_num,
                check="check_feed_health",
                result=f"Feed health: {status}",
                status="ok" if status in ("healthy", "ok") else "warning",
                follow_up="check_metaapi_connection" if status not in ("healthy", "ok") else None,
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_feed_health",
                result="feed_health.json unreadable",
                status="warning",
            )

    def _check_metaapi_connection(self, round_num: int) -> InvestigationStep:
        """Check MetaApi connection status."""
        path = self._novatrade_state / "connection_status.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_metaapi_connection",
                result="No connection_status.json — connection unknown",
                status="warning",
            )
        try:
            data = json.loads(path.read_text())
            status = data.get("status", "unknown")
            return InvestigationStep(
                round_num=round_num,
                check="check_metaapi_connection",
                result=f"MetaApi connection: {status}",
                status="ok" if status in ("connected", "ok") else "critical",
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_metaapi_connection",
                result="connection_status.json unreadable",
                status="warning",
            )

    def _check_signal_log(self, round_num: int) -> InvestigationStep:
        """Check if signals are being generated."""
        path = self._novatrade_state / "signal_log.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_signal_log",
                result="No signal_log.json — no signals recorded",
                status="warning",
            )
        try:
            data = json.loads(path.read_text())
            signals = data if isinstance(data, list) else data.get("signals", [])
            return InvestigationStep(
                round_num=round_num,
                check="check_signal_log",
                result=f"{len(signals)} signals recorded",
                status="ok" if signals else "warning",
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_signal_log",
                result="signal_log.json unreadable",
                status="warning",
            )

    def _check_order_log(self, round_num: int) -> InvestigationStep:
        """Check order execution log."""
        path = self._novatrade_state / "order_log.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_order_log",
                result="No order_log.json",
                status="info",
            )
        try:
            data = json.loads(path.read_text())
            orders = data if isinstance(data, list) else data.get("orders", [])
            return InvestigationStep(
                round_num=round_num,
                check="check_order_log",
                result=f"{len(orders)} orders in log",
                status="ok" if orders else "warning",
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_order_log",
                result="order_log.json unreadable",
                status="warning",
            )

    def _check_rejection_rate(self, round_num: int) -> InvestigationStep:
        """Check order rejection rate from metrics."""
        path = self._state_dir / "metrics.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_rejection_rate",
                result="No metrics.json",
                status="info",
            )
        try:
            data = json.loads(path.read_text())
            rejections = data.get("order_rejections", 0)
            total = data.get("orders_total", 1)
            rate = rejections / max(total, 1) * 100
            return InvestigationStep(
                round_num=round_num,
                check="check_rejection_rate",
                result=f"Rejection rate: {rate:.1f}% ({rejections}/{total})",
                status="ok" if rate < 10 else ("warning" if rate < 30 else "critical"),
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_rejection_rate",
                result="metrics.json unreadable",
                status="warning",
            )

    def _check_disk_space(self, round_num: int) -> InvestigationStep:
        """Check disk space usage."""
        try:
            stat = os.statvfs(str(self.base_path))
            used_pct = (1.0 - stat.f_bavail / stat.f_blocks) * 100
            return InvestigationStep(
                round_num=round_num,
                check="check_disk_space",
                result=f"Disk usage: {used_pct:.1f}%",
                status="ok" if used_pct < 85 else ("warning" if used_pct < 95 else "critical"),
            )
        except OSError as exc:
            return InvestigationStep(
                round_num=round_num,
                check="check_disk_space",
                result=f"Disk check failed: {exc}",
                status="warning",
            )

    def _check_memory(self, round_num: int) -> InvestigationStep:
        """Check memory usage from /proc/meminfo."""
        try:
            meminfo = Path("/proc/meminfo").read_text()
            total = available = 0
            for line in meminfo.splitlines():
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
            used_pct = (1.0 - available / max(total, 1)) * 100 if total else 0
            return InvestigationStep(
                round_num=round_num,
                check="check_memory",
                result=f"Memory usage: {used_pct:.1f}% ({available // 1024}MB available)",
                status="ok" if used_pct < 85 else ("warning" if used_pct < 95 else "critical"),
            )
        except (OSError, ValueError) as exc:
            return InvestigationStep(
                round_num=round_num,
                check="check_memory",
                result=f"Memory check failed: {exc}",
                status="warning",
            )

    def _check_error_rate(self, round_num: int) -> InvestigationStep:
        """Check recent error rate from logs."""
        log_dir = self._logs_dir
        if not log_dir.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_error_rate",
                result="No LOGS/ directory",
                status="info",
            )
        error_count = 0
        for log_file in log_dir.glob("*.log"):
            try:
                text = log_file.read_text(errors="ignore")
                error_count += text.count("ERROR")
            except OSError:
                pass
        return InvestigationStep(
            round_num=round_num,
            check="check_error_rate",
            result=f"{error_count} ERROR entries in logs",
            status="ok" if error_count < 50 else ("warning" if error_count < 200 else "critical"),
        )

    def _check_daily_loss(self, round_num: int) -> InvestigationStep:
        """Check daily loss tracker."""
        path = self._novatrade_state / "daily_loss_tracker.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_daily_loss",
                result="No daily_loss_tracker.json",
                status="info",
            )
        try:
            data = json.loads(path.read_text())
            loss_pct = data.get("daily_loss_pct", 0)
            return InvestigationStep(
                round_num=round_num,
                check="check_daily_loss",
                result=f"Daily loss: {loss_pct:.2f}%",
                status="ok" if loss_pct < 3 else ("warning" if loss_pct < 5 else "critical"),
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_daily_loss",
                result="daily_loss_tracker.json unreadable",
                status="warning",
            )

    def _check_risk_gates(self, round_num: int) -> InvestigationStep:
        """Check risk gate pass/fail status."""
        path = self._state_dir / "risk_gates.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_risk_gates",
                result="No risk_gates.json",
                status="info",
            )
        try:
            data = json.loads(path.read_text())
            gates = data if isinstance(data, dict) else {}
            failed = [g for g, v in gates.items() if not v]
            return InvestigationStep(
                round_num=round_num,
                check="check_risk_gates",
                result=f"Risk gates: {len(gates) - len(failed)}/{len(gates)} passing",
                status="ok" if not failed else "warning",
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_risk_gates",
                result="risk_gates.json unreadable",
                status="warning",
            )

    def _check_equity_history(self, round_num: int) -> InvestigationStep:
        """Check equity history for recent data."""
        path = self._novatrade_state / "equity_history.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_equity_history",
                result="No equity_history.json",
                status="warning",
            )
        try:
            data = json.loads(path.read_text())
            entries = data if isinstance(data, list) else data.get("history", [])
            return InvestigationStep(
                round_num=round_num,
                check="check_equity_history",
                result=f"{len(entries)} equity snapshots recorded",
                status="ok" if entries else "warning",
            )
        except (json.JSONDecodeError, OSError):
            return InvestigationStep(
                round_num=round_num,
                check="check_equity_history",
                result="equity_history.json unreadable",
                status="warning",
            )

    def _check_drawdown(self, round_num: int) -> InvestigationStep:
        """Check current drawdown from equity history."""
        path = self._novatrade_state / "equity_history.json"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_drawdown",
                result="No equity data for drawdown calc",
                status="info",
            )
        try:
            data = json.loads(path.read_text())
            entries = data if isinstance(data, list) else data.get("history", [])
            if not entries:
                return InvestigationStep(
                    round_num=round_num,
                    check="check_drawdown",
                    result="Empty equity history",
                    status="info",
                )
            equities = [
                float(e.get("equity", 0)) if isinstance(e, dict) else float(e)
                for e in entries
                if isinstance(e, (dict, int, float))
            ]
            if not equities:
                return InvestigationStep(
                    round_num=round_num,
                    check="check_drawdown",
                    result="No valid equity values",
                    status="warning",
                )
            peak = max(equities)
            current = equities[-1]
            dd = (peak - current) / peak * 100 if peak > 0 else 0
            return InvestigationStep(
                round_num=round_num,
                check="check_drawdown",
                result=f"Current drawdown: {dd:.2f}% (peak={peak:.0f}, current={current:.0f})",
                status="ok" if dd < 5 else ("warning" if dd < 10 else "critical"),
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return InvestigationStep(
                round_num=round_num,
                check="check_drawdown",
                result="Drawdown calculation failed",
                status="warning",
            )

    def _check_trade_stats(self, round_num: int) -> InvestigationStep:
        """Check trade statistics from trade journal."""
        path = self._novatrade_state / "trade_journal.jsonl"
        if not path.exists():
            return InvestigationStep(
                round_num=round_num,
                check="check_trade_stats",
                result="No trade_journal.jsonl",
                status="info",
            )
        try:
            closes: list[dict] = []
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("event") == "CLOSE":
                            closes.append(entry)
                    except json.JSONDecodeError:
                        continue
            wins = sum(1 for t in closes if t.get("pnl_usd", 0) > 0)
            win_rate = wins / len(closes) * 100 if closes else 0
            return InvestigationStep(
                round_num=round_num,
                check="check_trade_stats",
                result=f"{len(closes)} closed trades, {win_rate:.1f}% win rate",
                status="ok" if win_rate >= 40 else "warning",
            )
        except OSError:
            return InvestigationStep(
                round_num=round_num,
                check="check_trade_stats",
                result="Trade stats calculation failed",
                status="warning",
            )

    def _cross_correlate(self, round_num: int, report: ProgressReport) -> InvestigationStep:
        """Cross-correlate findings from previous rounds."""
        return InvestigationStep(
            round_num=round_num,
            check="cross_correlate",
            result="Cross-correlation complete — see root cause synthesis",
            status="info",
        )

    # -------------------------------------------------------------------
    # Root cause synthesis
    # -------------------------------------------------------------------

    def _synthesize_root_cause(self, inv_report: InvestigationReport) -> tuple[str, str] | None:
        """Synthesize a root cause hypothesis from investigation steps.

        Returns (root_cause_text, confidence) or None.
        """
        critical_steps = [s for s in inv_report.steps if s.status == "critical"]
        if not critical_steps:
            return None

        # Single critical finding = high confidence
        if len(critical_steps) == 1:
            step = critical_steps[0]
            return (
                f"Root cause: {step.check} — {step.result}",
                "high",
            )

        # Multiple critical findings — check for patterns
        checks = [s.check for s in critical_steps]

        # Service down + trade failure = service is the root cause
        if "check_service_status" in checks:
            return (
                "Root cause: novacore-novatrade service is down — "
                "all downstream failures (trades, signals, feed) are consequences.",
                "high",
            )

        # MetaApi connection + feed health = connectivity root cause
        if "check_metaapi_connection" in checks and "check_feed_health" in checks:
            return (
                "Root cause: MetaApi connection failure — "
                "feed health and trade execution are both affected by lost broker connectivity.",
                "high",
            )

        # Halt state = risk engine halted trading
        if "check_halt_state" in checks:
            halt_step = next(s for s in critical_steps if s.check == "check_halt_state")
            return (
                f"Root cause: Risk engine halt — {halt_step.result}",
                "high",
            )

        # Multiple unrelated criticals = medium confidence
        details = "; ".join(f"{s.check}: {s.result[:50]}" for s in critical_steps[:3])
        return (
            f"Multiple critical findings (may be correlated): {details}",
            "medium",
        )

    def _recommend_action(self, inv_report: InvestigationReport) -> str:
        """Generate action recommendation based on investigation."""
        if not inv_report.root_cause:
            return "Inconclusive — escalate to operator for manual investigation."

        root = inv_report.root_cause.lower()

        if "service is down" in root or "not active" in root:
            return "Restart novacore-novatrade service (safe, idempotent)."
        if "connection failure" in root or "metaapi" in root:
            return "Check MetaApi credentials and network, then restart service."
        if "halt" in root:
            return "Review and resolve risk halt condition before resuming trading."
        if "disk" in root:
            return "Free disk space — archive old logs and state files."
        if "memory" in root:
            return "Investigate memory pressure — check for memory leaks in running services."

        return f"Investigate: {inv_report.root_cause[:100]}"

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def _persist_investigation(self, inv_report: InvestigationReport) -> None:
        """Append investigation to history file."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        history: list[dict] = []
        if self._investigations_path.exists():
            try:
                data = json.loads(self._investigations_path.read_text())
                history = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                pass

        history.append(asdict(inv_report))
        history = history[-50:]  # keep last 50

        fd, tmp_path = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(history, f, indent=2, default=str)
            os.replace(tmp_path, str(self._investigations_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
