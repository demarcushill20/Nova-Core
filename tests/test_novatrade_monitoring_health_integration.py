"""Integration tests for automated monitoring system health checks.

This addresses the final monitoring gap: System Integration Testing for
automated monitoring system health checks.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.models import HealthState
from novatrade.monitor.system_health import (
    MonitoringSystemHealthChecker,
    SystemHealthCheck,
    run_monitoring_health_check,
)


class TestMonitoringSystemHealthChecker:
    """Test the automated monitoring system health checker"""

    @pytest.fixture
    def config(self, tmp_path: Path) -> NovaTradeCfg:
        """Test configuration with temporary directories"""
        config = NovaTradeCfg()
        config.data_dir = tmp_path / "output" / "novatrade"
        config.log_dir = tmp_path / "logs" / "novatrade"
        return config

    @pytest.fixture
    def checker(self, config: NovaTradeCfg, tmp_path: Path) -> MonitoringSystemHealthChecker:
        """Create health checker with test configuration"""
        state_dir = tmp_path / "state" / "novatrade"
        return MonitoringSystemHealthChecker(config, state_dir=state_dir)

    @pytest.fixture
    def setup_healthy_state(self, config: NovaTradeCfg, tmp_path: Path) -> None:
        """Set up files for a healthy monitoring system state"""
        output_dir = Path(config.data_dir)  # Already includes "novatrade"
        state_dir = tmp_path / "state" / "novatrade"
        state_root = tmp_path / "state"

        # Create directories
        output_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        # Create signal log
        signal_log = {"timestamp": time.time(), "signals": []}
        (state_dir / "signal_log.json").write_text(json.dumps(signal_log))

        # Create live evidence
        (output_dir / "live_evidence.jsonl").write_text('{"type": "test"}\n')

        # Create live metrics
        metrics = {
            "ticks": 100,
            "uptime_seconds": 3600,
            "queue_depth": 0,
            "errors": 0,
        }
        (state_dir / "live_metrics.json").write_text(json.dumps(metrics))

        # Create dashboard state
        (output_dir / "live_state.db").touch()

        # Create risk state
        risk_state = {
            "breached": False,
            "halted": False,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        (state_root / "novatrade_risk_state.json").write_text(json.dumps(risk_state))

    @pytest.mark.asyncio
    async def test_healthy_system_check(
        self,
        checker: MonitoringSystemHealthChecker,
        setup_healthy_state: None,
    ) -> None:
        """Test health check on a fully healthy monitoring system"""
        report = checker.run_health_checks()

        assert report.overall_status == HealthState.OK
        assert report.readiness_score == 100
        assert len(report.checks) == 5

        # All components should be healthy
        for check in report.checks:
            assert check.status == HealthState.OK

        component_names = {check.component for check in report.checks}
        expected_components = {
            "signal_monitoring",
            "evidence_collection",
            "live_metrics",
            "dashboard_data",
            "risk_monitoring",
        }
        assert component_names == expected_components

    @pytest.mark.asyncio
    async def test_missing_signal_log(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when signal log is missing"""
        # Ensure signal log file is removed if it exists
        signal_log_path = checker.state_dir / "signal_log.json"
        # Also create the directory if it doesn't exist to ensure proper test isolation
        signal_log_path.parent.mkdir(parents=True, exist_ok=True)
        if signal_log_path.exists():
            signal_log_path.unlink()

        report = checker.run_health_checks()

        signal_check = next(check for check in report.checks if check.component == "signal_monitoring")
        assert signal_check.status == HealthState.DOWN
        assert "Signal log file missing" in signal_check.message
        assert report.overall_status == HealthState.DOWN

    @pytest.mark.asyncio
    async def test_stale_signal_log(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when signal log is stale"""
        state_dir = checker.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)

        # Create old signal log (2 hours ago)
        signal_log_path = state_dir / "signal_log.json"
        signal_log_path.write_text('{"signals": []}')

        # Modify timestamp to be 2 hours old
        old_time = time.time() - 7200  # 2 hours
        os.utime(signal_log_path, times=(old_time, old_time))

        report = checker.run_health_checks()

        signal_check = next(check for check in report.checks if check.component == "signal_monitoring")
        assert signal_check.status == HealthState.DEGRADED
        assert "Signal log stale" in signal_check.message

    @pytest.mark.asyncio
    async def test_empty_evidence_file(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when evidence file exists but is empty"""
        output_dir = checker.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create empty evidence file
        (output_dir / "live_evidence.jsonl").touch()

        report = checker.run_health_checks()

        evidence_check = next(check for check in report.checks if check.component == "evidence_collection")
        assert evidence_check.status == HealthState.DEGRADED
        assert "Evidence file exists but empty" in evidence_check.message

    @pytest.mark.asyncio
    async def test_invalid_live_metrics(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when live metrics are missing required fields"""
        state_dir = checker.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)

        # Create metrics with missing fields
        incomplete_metrics = {"ticks": 100}  # Missing uptime_seconds, queue_depth
        (state_dir / "live_metrics.json").write_text(json.dumps(incomplete_metrics))

        report = checker.run_health_checks()

        metrics_check = next(check for check in report.checks if check.component == "live_metrics")
        assert metrics_check.status == HealthState.DEGRADED
        assert "Missing metrics fields" in metrics_check.message

    @pytest.mark.asyncio
    async def test_stale_dashboard_data(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when dashboard data is stale"""
        output_dir = checker.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create dashboard DB and make it old (10 minutes)
        db_path = output_dir / "live_state.db"
        db_path.touch()
        old_time = time.time() - 600  # 10 minutes
        os.utime(db_path, times=(old_time, old_time))

        report = checker.run_health_checks()

        dashboard_check = next(check for check in report.checks if check.component == "dashboard_data")
        assert dashboard_check.status == HealthState.DEGRADED
        assert "Dashboard data stale" in dashboard_check.message

    @pytest.mark.asyncio
    async def test_missing_risk_state(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when risk state file is missing"""
        # Don't create risk state file
        report = checker.run_health_checks()

        risk_check = next(check for check in report.checks if check.component == "risk_monitoring")
        assert risk_check.status == HealthState.DOWN
        assert "Risk state file missing" in risk_check.message

    @pytest.mark.asyncio
    async def test_stale_risk_state(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check when risk state is stale"""
        state_root = checker.state_dir.parent
        state_root.mkdir(parents=True, exist_ok=True)

        # Create risk state with old timestamp (15 minutes ago)
        old_timestamp = datetime.now(timezone.utc).timestamp() - 900
        old_datetime = datetime.fromtimestamp(old_timestamp, timezone.utc)
        risk_state = {
            "breached": False,
            "halted": False,
            "last_updated": old_datetime.isoformat(),
        }
        (state_root / "novatrade_risk_state.json").write_text(json.dumps(risk_state))

        report = checker.run_health_checks()

        risk_check = next(check for check in report.checks if check.component == "risk_monitoring")
        assert risk_check.status == HealthState.DEGRADED
        assert "Risk state stale" in risk_check.message

    @pytest.mark.asyncio
    async def test_mixed_health_status(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test health check with mix of healthy and degraded components"""
        output_dir = checker.output_dir
        state_dir = checker.state_dir
        state_root = checker.state_dir.parent

        # Create directories
        output_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        # Healthy signal log
        signal_log = {"timestamp": time.time(), "signals": []}
        (state_dir / "signal_log.json").write_text(json.dumps(signal_log))

        # Empty evidence file (degraded)
        (output_dir / "live_evidence.jsonl").touch()

        # Healthy metrics
        metrics = {"ticks": 100, "uptime_seconds": 3600, "queue_depth": 0}
        (state_dir / "live_metrics.json").write_text(json.dumps(metrics))

        # Healthy dashboard
        (output_dir / "live_state.db").touch()

        # Healthy risk state
        risk_state = {
            "breached": False,
            "halted": False,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        (state_root / "novatrade_risk_state.json").write_text(json.dumps(risk_state))

        report = checker.run_health_checks()

        # Should be degraded overall due to empty evidence file
        assert report.overall_status == HealthState.DEGRADED
        assert 0 < report.readiness_score < 100

        # Check specific components
        evidence_check = next(check for check in report.checks if check.component == "evidence_collection")
        assert evidence_check.status == HealthState.DEGRADED

        signal_check = next(check for check in report.checks if check.component == "signal_monitoring")
        assert signal_check.status == HealthState.OK

    @pytest.mark.asyncio
    async def test_save_health_report(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test saving health report to disk"""
        # Create a minimal report
        report = checker.run_health_checks()
        report_path = checker.save_health_report(report)

        # Verify file was created
        assert report_path.exists()
        assert report_path.name == "monitoring_health_report.json"

        # Verify content
        with report_path.open() as f:
            saved_data = json.load(f)

        assert "timestamp" in saved_data
        assert "overall_status" in saved_data
        assert "readiness_score" in saved_data
        assert "checks" in saved_data
        assert isinstance(saved_data["checks"], list)

    @pytest.mark.asyncio
    async def test_exception_handling(
        self,
        checker: MonitoringSystemHealthChecker,
        config: NovaTradeCfg,
    ) -> None:
        """Test that exceptions in health checks are handled gracefully"""
        # Create a corrupted JSON file
        state_dir = checker.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "live_metrics.json").write_text("invalid json{")

        report = checker.run_health_checks()

        metrics_check = next(check for check in report.checks if check.component == "live_metrics")
        assert metrics_check.status == HealthState.DOWN
        assert "check failed" in metrics_check.message

    @pytest.mark.asyncio
    async def test_readiness_score_calculation(
        self,
        checker: MonitoringSystemHealthChecker,
    ) -> None:
        """Test readiness score calculation logic"""
        # Test all healthy
        all_ok = [
            SystemHealthCheck("comp1", HealthState.OK, "ok"),
            SystemHealthCheck("comp2", HealthState.OK, "ok"),
        ]
        score = checker._compute_readiness_score(all_ok)
        assert score == 100

        # Test all degraded
        all_degraded = [
            SystemHealthCheck("comp1", HealthState.DEGRADED, "degraded"),
            SystemHealthCheck("comp2", HealthState.DEGRADED, "degraded"),
        ]
        score = checker._compute_readiness_score(all_degraded)
        assert score == 50

        # Test mixed
        mixed = [
            SystemHealthCheck("comp1", HealthState.OK, "ok"),
            SystemHealthCheck("comp2", HealthState.DEGRADED, "degraded"),
            SystemHealthCheck("comp3", HealthState.DOWN, "down"),
        ]
        score = checker._compute_readiness_score(mixed)
        assert score == 50  # (100 + 50 + 0) / 3


class TestStandaloneFunction:
    """Test the standalone health check function"""

    @patch("novatrade.monitor.system_health.NovaTradeCfg")
    def test_run_monitoring_health_check(self, mock_config_class) -> None:
        """Test the standalone health check function"""
        # Mock configuration
        mock_config = mock_config_class.return_value
        mock_config.data_dir = "/tmp/test_output/novatrade"
        mock_config.log_dir = "/tmp/test_logs/novatrade"

        # Mock the health checker to avoid file system operations
        with patch("novatrade.monitor.system_health.MonitoringSystemHealthChecker") as mock_checker_class:
            mock_checker = mock_checker_class.return_value
            mock_report = type(
                "MockReport",
                (),
                {
                    "overall_status": HealthState.OK,
                    "readiness_score": 85,
                    "checks": [],
                },
            )()
            mock_checker.run_health_checks.return_value = mock_report
            mock_checker.save_health_report.return_value = Path("/tmp/report.json")

            # Run the function
            report = run_monitoring_health_check()

            # Verify it returned the mock report
            assert report.overall_status == HealthState.OK
            assert report.readiness_score == 85

            # Verify the checker was created and methods called
            mock_checker_class.assert_called_once_with(mock_config)
            mock_checker.run_health_checks.assert_called_once()
            mock_checker.save_health_report.assert_called_once_with(mock_report)


# Integration test markers
integration = pytest.mark.integration
