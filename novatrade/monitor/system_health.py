#!/usr/bin/env python3
"""Automated monitoring system health checks for NovaTrade integration.

This module implements the missing System Integration Testing gap identified
in the monitoring analysis - automated health checks for monitoring components.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novatrade.config import NovaTradeCfg
from novatrade.models import HealthState

log = logging.getLogger("novatrade.monitor.system_health")


@dataclass
class SystemHealthCheck:
    """Result of a system health check"""

    component: str
    status: HealthState
    message: str
    latency_ms: float | None = None
    last_check: datetime | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class SystemHealthReport:
    """Overall system health assessment"""

    timestamp: datetime
    overall_status: HealthState
    checks: list[SystemHealthCheck]
    readiness_score: int  # 0-100 scale


class MonitoringSystemHealthChecker:
    """Automated health checking for the monitoring system itself"""

    def __init__(self, config: NovaTradeCfg, state_dir: Path | None = None) -> None:
        self.config = config
        self.output_dir = Path(config.data_dir)  # Already includes "novatrade"
        self.state_dir = state_dir or (Path("STATE") / "novatrade")

    def run_health_checks(self) -> SystemHealthReport:
        """Run all automated health checks for the monitoring system"""

        checks = []

        # Check 1: Signal monitoring system
        checks.append(self._check_signal_monitoring())

        # Check 2: Evidence collection system
        checks.append(self._check_evidence_collection())

        # Check 3: Live metrics collection
        checks.append(self._check_live_metrics())

        # Check 4: Dashboard data availability
        checks.append(self._check_dashboard_data())

        # Check 5: Risk monitoring integration
        checks.append(self._check_risk_monitoring())

        # Determine overall health
        overall_status = self._compute_overall_status(checks)
        readiness_score = self._compute_readiness_score(checks)

        return SystemHealthReport(
            timestamp=datetime.now(timezone.utc),
            overall_status=overall_status,
            checks=checks,
            readiness_score=readiness_score,
        )

    def _check_signal_monitoring(self) -> SystemHealthCheck:
        """Check if signal monitoring is functioning"""
        try:
            signal_log_path = self.state_dir / "signal_log.json"

            if not signal_log_path.exists():
                return SystemHealthCheck(
                    component="signal_monitoring",
                    status=HealthState.DOWN,
                    message="Signal log file missing",
                )

            # Check if file was updated recently (within 1 hour)
            mtime = signal_log_path.stat().st_mtime
            age_seconds = time.time() - mtime

            if age_seconds > 3600:  # 1 hour
                return SystemHealthCheck(
                    component="signal_monitoring",
                    status=HealthState.DEGRADED,
                    message=f"Signal log stale ({age_seconds:.0f}s old)",
                    metadata={"age_seconds": age_seconds},
                )

            return SystemHealthCheck(
                component="signal_monitoring",
                status=HealthState.OK,
                message="Signal monitoring active",
                metadata={"age_seconds": age_seconds},
            )

        except Exception as e:
            return SystemHealthCheck(
                component="signal_monitoring",
                status=HealthState.DOWN,
                message=f"Signal monitoring check failed: {e}",
            )

    def _check_evidence_collection(self) -> SystemHealthCheck:
        """Check if evidence collection is functioning"""
        try:
            evidence_path = self.output_dir / "live_evidence.jsonl"

            if not evidence_path.exists():
                return SystemHealthCheck(
                    component="evidence_collection",
                    status=HealthState.DOWN,
                    message="Live evidence file missing",
                )

            # Check file size (should have some content if working)
            file_size = evidence_path.stat().st_size
            if file_size == 0:
                return SystemHealthCheck(
                    component="evidence_collection",
                    status=HealthState.DEGRADED,
                    message="Evidence file exists but empty",
                )

            return SystemHealthCheck(
                component="evidence_collection",
                status=HealthState.OK,
                message="Evidence collection active",
                metadata={"file_size_bytes": file_size},
            )

        except Exception as e:
            return SystemHealthCheck(
                component="evidence_collection",
                status=HealthState.DOWN,
                message=f"Evidence collection check failed: {e}",
            )

    def _check_live_metrics(self) -> SystemHealthCheck:
        """Check if live metrics are being collected"""
        try:
            metrics_path = self.state_dir / "live_metrics.json"

            if not metrics_path.exists():
                return SystemHealthCheck(
                    component="live_metrics",
                    status=HealthState.DOWN,
                    message="Live metrics file missing",
                )

            # Parse and validate metrics
            with open(metrics_path) as f:
                metrics = json.load(f)

            required_fields = ["ticks", "uptime_seconds", "queue_depth"]
            missing_fields = [field for field in required_fields if field not in metrics]

            if missing_fields:
                return SystemHealthCheck(
                    component="live_metrics",
                    status=HealthState.DEGRADED,
                    message=f"Missing metrics fields: {missing_fields}",
                )

            return SystemHealthCheck(
                component="live_metrics",
                status=HealthState.OK,
                message="Live metrics collection active",
                metadata={"tick_count": metrics.get("ticks", 0)},
            )

        except Exception as e:
            return SystemHealthCheck(
                component="live_metrics",
                status=HealthState.DOWN,
                message=f"Live metrics check failed: {e}",
            )

    def _check_dashboard_data(self) -> SystemHealthCheck:
        """Check if dashboard data is available and recent"""
        try:
            db_path = self.output_dir / "live_state.db"

            if not db_path.exists():
                return SystemHealthCheck(
                    component="dashboard_data",
                    status=HealthState.DOWN,
                    message="Dashboard state database missing",
                )

            # Check if database was updated recently
            mtime = db_path.stat().st_mtime
            age_seconds = time.time() - mtime

            if age_seconds > 300:  # 5 minutes
                return SystemHealthCheck(
                    component="dashboard_data",
                    status=HealthState.DEGRADED,
                    message=f"Dashboard data stale ({age_seconds:.0f}s old)",
                    metadata={"age_seconds": age_seconds},
                )

            return SystemHealthCheck(
                component="dashboard_data",
                status=HealthState.OK,
                message="Dashboard data current",
                metadata={"age_seconds": age_seconds},
            )

        except Exception as e:
            return SystemHealthCheck(
                component="dashboard_data",
                status=HealthState.DOWN,
                message=f"Dashboard data check failed: {e}",
            )

    def _check_risk_monitoring(self) -> SystemHealthCheck:
        """Check if risk monitoring integration is working"""
        try:
            risk_state_path = self.state_dir.parent / "novatrade_risk_state.json"

            if not risk_state_path.exists():
                return SystemHealthCheck(
                    component="risk_monitoring",
                    status=HealthState.DOWN,
                    message="Risk state file missing",
                )

            with open(risk_state_path) as f:
                risk_state = json.load(f)

            # Check if risk state was updated recently
            last_updated = risk_state.get("last_updated")
            if not last_updated:
                return SystemHealthCheck(
                    component="risk_monitoring",
                    status=HealthState.DEGRADED,
                    message="Risk state missing last_updated timestamp",
                )

            # Parse timestamp and check age
            from datetime import datetime

            update_time = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            age_seconds = (datetime.now(timezone.utc) - update_time).total_seconds()

            if age_seconds > 600:  # 10 minutes
                return SystemHealthCheck(
                    component="risk_monitoring",
                    status=HealthState.DEGRADED,
                    message=f"Risk state stale ({age_seconds:.0f}s old)",
                    metadata={"age_seconds": age_seconds},
                )

            return SystemHealthCheck(
                component="risk_monitoring",
                status=HealthState.OK,
                message="Risk monitoring active",
                metadata={
                    "breached": risk_state.get("breached", False),
                    "halted": risk_state.get("halted", False),
                },
            )

        except Exception as e:
            return SystemHealthCheck(
                component="risk_monitoring",
                status=HealthState.DOWN,
                message=f"Risk monitoring check failed: {e}",
            )

    def _compute_overall_status(self, checks: list[SystemHealthCheck]) -> HealthState:
        """Compute overall health status from individual checks"""
        if any(check.status == HealthState.DOWN for check in checks):
            return HealthState.DOWN

        if any(check.status == HealthState.DEGRADED for check in checks):
            return HealthState.DEGRADED

        return HealthState.OK

    def _compute_readiness_score(self, checks: list[SystemHealthCheck]) -> int:
        """Compute 0-100 readiness score based on health checks"""
        if not checks:
            return 0

        total_score = 0
        for check in checks:
            if check.status == HealthState.OK:
                total_score += 100
            elif check.status == HealthState.DEGRADED:
                total_score += 50
            # DOWN gets 0 points

        return total_score // len(checks)

    def save_health_report(self, report: SystemHealthReport) -> Path:
        """Save health report to disk for monitoring"""
        report_path = self.output_dir / "monitoring_health_report.json"

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Convert to JSON-serializable format
        report_data = {
            "timestamp": report.timestamp.isoformat(),
            "overall_status": report.overall_status.value,
            "readiness_score": report.readiness_score,
            "checks": [
                {
                    "component": check.component,
                    "status": check.status.value,
                    "message": check.message,
                    "latency_ms": check.latency_ms,
                    "last_check": check.last_check.isoformat() if check.last_check else None,
                    "metadata": check.metadata,
                }
                for check in report.checks
            ],
        }

        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)

        return report_path


def run_monitoring_health_check() -> SystemHealthReport:
    """Standalone function to run monitoring system health checks"""
    config = NovaTradeCfg()
    checker = MonitoringSystemHealthChecker(config)
    report = checker.run_health_checks()
    checker.save_health_report(report)
    return report


if __name__ == "__main__":
    # CLI usage
    report = run_monitoring_health_check()
    print(f"Overall Status: {report.overall_status.value}")
    print(f"Readiness Score: {report.readiness_score}/100")
    print("\nComponent Health:")
    for check in report.checks:
        print(f"  {check.component}: {check.status.value} - {check.message}")
