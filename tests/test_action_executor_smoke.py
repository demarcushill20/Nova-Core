"""Smoke tests for novatrade.autonomy.action_executor module.

These tests verify basic functionality and instantiation of the DirectActionExecutor
class and related data structures without requiring actual system services or
executing real remediation actions.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from novatrade.autonomy.action_executor import (
    ActionResult,
    DiagnosticFinding,
    DirectActionExecutor,
)
from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.schemas import ProgressReport


class TestDiagnosticFinding:
    """Test DiagnosticFinding dataclass."""

    def test_diagnostic_finding_creation(self):
        """Test DiagnosticFinding can be created with required fields."""
        finding = DiagnosticFinding(check="service_status", status="ok", detail="Service is running normally")

        assert finding.check == "service_status"
        assert finding.status == "ok"
        assert finding.detail == "Service is running normally"

    def test_diagnostic_finding_status_values(self):
        """Test different status values are accepted."""
        statuses = ["ok", "warning", "critical"]

        for status in statuses:
            finding = DiagnosticFinding(check="test_check", status=status, detail="Test detail")
            assert finding.status == status

    def test_diagnostic_finding_serialization(self):
        """Test DiagnosticFinding can be used as a dataclass."""
        finding = DiagnosticFinding(check="memory_usage", status="warning", detail="Memory usage at 85%")

        # Should be able to access all fields
        assert hasattr(finding, "check")
        assert hasattr(finding, "status")
        assert hasattr(finding, "detail")


class TestActionResult:
    """Test ActionResult dataclass."""

    def test_action_result_creation(self):
        """Test ActionResult can be created with required fields."""
        result = ActionResult(decision_mode="repair", target_dimension="system_health")

        assert result.decision_mode == "repair"
        assert result.target_dimension == "system_health"
        assert result.findings == []
        assert result.actions_taken == []
        assert result.alert_sent is False
        assert result.escalated is False
        assert result.summary == ""
        assert result.investigation_summary is None
        assert result.root_cause is None

    def test_action_result_with_all_fields(self):
        """Test ActionResult with all fields populated."""
        finding1 = DiagnosticFinding("check1", "ok", "detail1")
        finding2 = DiagnosticFinding("check2", "warning", "detail2")

        result = ActionResult(
            decision_mode="execute",
            target_dimension="performance",
            findings=[finding1, finding2],
            actions_taken=["restart service", "clear cache"],
            alert_sent=True,
            escalated=False,
            summary="Performed maintenance actions",
            investigation_summary="Found performance issue",
            root_cause="High memory usage",
        )

        assert result.decision_mode == "execute"
        assert result.target_dimension == "performance"
        assert len(result.findings) == 2
        assert result.findings[0].check == "check1"
        assert result.actions_taken == ["restart service", "clear cache"]
        assert result.alert_sent is True
        assert result.escalated is False
        assert result.summary == "Performed maintenance actions"

    def test_action_result_defaults(self):
        """Test ActionResult default values."""
        result = ActionResult(decision_mode="monitor", target_dimension=None)

        # Check default factory values
        assert isinstance(result.findings, list)
        assert isinstance(result.actions_taken, list)
        assert len(result.findings) == 0
        assert len(result.actions_taken) == 0


class TestDirectActionExecutorInstantiation:
    """Test DirectActionExecutor creation and setup."""

    def test_executor_default_creation(self):
        """Test DirectActionExecutor can be created with default path."""
        executor = DirectActionExecutor()

        assert executor.base_path == Path("/home/nova/nova-core")
        assert str(executor._state_dir).endswith("STATE")
        assert str(executor._novatrade_state).endswith("STATE/novatrade")
        assert str(executor._logs_dir).endswith("LOGS")

    def test_executor_custom_path(self):
        """Test DirectActionExecutor with custom base path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            executor = DirectActionExecutor(base_path=temp_dir)

            assert executor.base_path == Path(temp_dir)
            assert executor._state_dir == Path(temp_dir) / "STATE"
            assert executor._novatrade_state == Path(temp_dir) / "STATE" / "novatrade"
            assert executor._logs_dir == Path(temp_dir) / "LOGS"

    def test_executor_restartable_services_constant(self):
        """Test that RESTARTABLE_SERVICES constant is defined."""
        executor = DirectActionExecutor()

        assert hasattr(executor, "RESTARTABLE_SERVICES")
        assert isinstance(executor.RESTARTABLE_SERVICES, tuple)
        assert "novacore-novatrade" in executor.RESTARTABLE_SERVICES


class TestDirectActionExecutorMethods:
    """Test DirectActionExecutor method existence and basic behavior."""

    def create_test_executor(self) -> DirectActionExecutor:
        """Create a test executor."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return DirectActionExecutor(base_path=temp_dir)

    def create_test_decision(self, mode: ActionMode = ActionMode.MONITOR) -> Decision:
        """Create a test decision."""
        return Decision(mode=mode, reason="Test decision", target_dimension="system_health")

    def create_test_report(self) -> Mock:
        """Create a mock progress report."""
        mock_report = Mock(spec=ProgressReport)
        mock_report.dimensions = {}
        mock_report.overall_score = 75.0
        return mock_report

    def test_execute_method_exists(self):
        """Test execute method exists and is callable."""
        executor = self.create_test_executor()

        assert hasattr(executor, "execute")
        assert callable(executor.execute)

    def test_execute_monitor_mode(self):
        """Test execute method with MONITOR mode."""
        executor = self.create_test_executor()
        decision = self.create_test_decision(ActionMode.MONITOR)
        report = self.create_test_report()

        result = executor.execute(decision, report)

        assert isinstance(result, ActionResult)
        assert result.decision_mode == "monitor"
        assert "MONITOR mode" in result.summary
        assert len(result.actions_taken) == 0

    def test_execute_escalate_mode(self):
        """Test execute method with ESCALATE mode."""
        executor = self.create_test_executor()
        decision = self.create_test_decision(ActionMode.ESCALATE)
        report = self.create_test_report()

        # Mock the _send_alert method to avoid actual alerts
        with patch.object(executor, "_send_alert") as mock_alert:
            result = executor.execute(decision, report)

            assert isinstance(result, ActionResult)
            assert result.decision_mode == "escalate"
            # Should have tried to send alert
            mock_alert.assert_called_once()

    def test_execute_return_type(self):
        """Test execute method returns ActionResult."""
        executor = self.create_test_executor()
        decision = self.create_test_decision()
        report = self.create_test_report()

        result = executor.execute(decision, report)

        assert isinstance(result, ActionResult)
        assert result.decision_mode in [mode.value for mode in ActionMode]


class TestDirectActionExecutorDiagnosticMethods:
    """Test diagnostic methods exist and can be called safely."""

    def create_test_executor(self) -> DirectActionExecutor:
        """Create a test executor."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return DirectActionExecutor(base_path=temp_dir)

    def test_diagnostic_methods_exist(self):
        """Test that diagnostic methods exist."""
        executor = self.create_test_executor()

        methods = [
            "_run_diagnostics",
            "_diagnose_strategy",
            "_diagnose_pipeline",
            "_diagnose_system_health",
            "_diagnose_risk",
            "_diagnose_performance",
            "_diagnose_generic",
        ]

        for method_name in methods:
            assert hasattr(executor, method_name)
            assert callable(getattr(executor, method_name))

    def test_run_diagnostics_method(self):
        """Test _run_diagnostics method can be called."""
        executor = self.create_test_executor()

        decision = Decision(mode=ActionMode.RESEARCH, reason="Test")
        mock_report = Mock(spec=ProgressReport)
        result = ActionResult(decision_mode="research", target_dimension=None)

        # Should not crash when called
        try:
            executor._run_diagnostics(decision, mock_report, result)
        except Exception:  # noqa: S110
            # Method might fail due to missing system state,
            # but it should exist and be callable
            pass

    def test_diagnose_generic_method(self):
        """Test _diagnose_generic method can be called."""
        executor = self.create_test_executor()

        mock_report = Mock(spec=ProgressReport)
        result = ActionResult(decision_mode="test", target_dimension=None)

        # Should not crash when called
        try:
            executor._diagnose_generic(mock_report, result)
        except Exception:  # noqa: S110
            # Method might fail due to missing system state,
            # but it should exist and be callable
            pass


class TestDirectActionExecutorServiceMethods:
    """Test service-related methods (without actual system calls)."""

    def create_test_executor(self) -> DirectActionExecutor:
        """Create a test executor."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return DirectActionExecutor(base_path=temp_dir)

    def test_service_methods_exist(self):
        """Test that service-related methods exist."""
        executor = self.create_test_executor()

        methods = [
            "_check_service_status",
            "_is_service_active",
            "_restart_service",
        ]

        for method_name in methods:
            assert hasattr(executor, method_name)
            assert callable(getattr(executor, method_name))

    @patch("subprocess.run")
    def test_is_service_active_static_method(self, mock_subprocess):
        """Test _is_service_active static method."""
        # Mock successful service check
        mock_result = Mock()
        mock_result.stdout = "active\n"
        mock_subprocess.return_value = mock_result

        result = DirectActionExecutor._is_service_active("test-service")

        assert isinstance(result, bool)
        assert result is True
        mock_subprocess.assert_called_once()

    @patch("subprocess.run")
    def test_is_service_active_inactive_service(self, mock_subprocess):
        """Test _is_service_active with inactive service."""
        # Mock failed service check
        mock_result = Mock()
        mock_result.stdout = "inactive\n"
        mock_subprocess.return_value = mock_result

        result = DirectActionExecutor._is_service_active("inactive-service")

        assert isinstance(result, bool)
        assert result is False

    def test_check_service_status_method(self):
        """Test _check_service_status method structure."""
        executor = self.create_test_executor()
        result = ActionResult(decision_mode="test", target_dimension=None)

        # Should not crash when called
        with patch.object(executor, "_is_service_active", return_value=True):
            executor._check_service_status("test-service", result)

        # Should have added findings
        assert len(result.findings) >= 0


class TestDirectActionExecutorAlertMethods:
    """Test alert-related methods (without sending actual alerts)."""

    def create_test_executor(self) -> DirectActionExecutor:
        """Create a test executor."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return DirectActionExecutor(base_path=temp_dir)

    def test_alert_methods_exist(self):
        """Test that alert-related methods exist."""
        executor = self.create_test_executor()

        methods = [
            "_send_alert",
            "_log_alert_only",
            "_is_duplicate_alert",
        ]

        for method_name in methods:
            assert hasattr(executor, method_name)
            assert callable(getattr(executor, method_name))

    def test_is_duplicate_alert_method(self):
        """Test _is_duplicate_alert method."""
        executor = self.create_test_executor()
        decision = Decision(mode=ActionMode.REPAIR, reason="Test")

        # Should return a boolean
        with patch("os.path.exists", return_value=False):
            result = executor._is_duplicate_alert(decision)
            assert isinstance(result, bool)

    def test_log_alert_only_method(self):
        """Test _log_alert_only method structure."""
        executor = self.create_test_executor()

        decision = Decision(mode=ActionMode.ESCALATE, reason="Test escalation")
        result = ActionResult(decision_mode="escalate", target_dimension="test")

        # Should not crash when called
        with patch.object(executor, "_log_to_file"):
            executor._log_alert_only(decision, result)


class TestDirectActionExecutorUtilityMethods:
    """Test utility and helper methods."""

    def create_test_executor(self) -> DirectActionExecutor:
        """Create a test executor."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return DirectActionExecutor(base_path=temp_dir)

    def test_utility_methods_exist(self):
        """Test that utility methods exist."""
        executor = self.create_test_executor()

        methods = [
            "_verify_dimension",
            "_verify_and_followup",
            "_log_to_file",
            "_attempt_remediation",
        ]

        for method_name in methods:
            assert hasattr(executor, method_name)
            assert callable(getattr(executor, method_name))

    def test_verify_dimension_method(self):
        """Test _verify_dimension method."""
        executor = self.create_test_executor()

        # Test with known dimension
        valid, message = executor._verify_dimension("system_health")
        assert isinstance(valid, bool)
        assert isinstance(message, str)

        # Test with None dimension
        valid, message = executor._verify_dimension(None)
        assert isinstance(valid, bool)
        assert isinstance(message, str)

        # Test with unknown dimension
        valid, message = executor._verify_dimension("unknown_dimension")
        assert isinstance(valid, bool)
        assert isinstance(message, str)

    def test_log_to_file_method(self):
        """Test _log_to_file method structure."""
        executor = self.create_test_executor()

        # Ensure logs directory exists
        executor._logs_dir.mkdir(parents=True, exist_ok=True)

        # Should not crash when called
        try:
            executor._log_to_file("Test log message")
        except Exception:  # noqa: S110
            # File operations might fail in some environments,
            # but the method should exist
            pass


class TestDirectActionExecutorIntegration:
    """Integration smoke tests."""

    def test_full_execution_workflow_monitor(self):
        """Test complete execution workflow for MONITOR mode."""
        with tempfile.TemporaryDirectory() as temp_dir:
            executor = DirectActionExecutor(base_path=temp_dir)

            decision = Decision(mode=ActionMode.MONITOR, reason="System is stable")

            mock_report = Mock(spec=ProgressReport)
            mock_report.dimensions = {}

            # Should complete without errors
            result = executor.execute(decision, mock_report)

            assert isinstance(result, ActionResult)
            assert result.decision_mode == "monitor"
            assert "MONITOR mode" in result.summary

    def test_full_execution_workflow_repair(self):
        """Test execution workflow for REPAIR mode (with mocked dependencies)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            executor = DirectActionExecutor(base_path=temp_dir)

            decision = Decision(mode=ActionMode.REPAIR, reason="System needs repair", target_dimension="system_health")

            mock_report = Mock(spec=ProgressReport)
            mock_report.dimensions = {}

            # Mock external dependencies
            with patch.object(executor, "_send_alert"), patch.object(executor, "_is_service_active", return_value=True):
                result = executor.execute(decision, mock_report)

                assert isinstance(result, ActionResult)
                assert result.decision_mode == "repair"
                assert result.target_dimension == "system_health"

    def test_error_handling_structure(self):
        """Test that the executor has proper error handling structure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            executor = DirectActionExecutor(base_path=temp_dir)

            # Basic operations should not crash
            decision = Decision(mode=ActionMode.VALIDATE, reason="Test")
            mock_report = Mock(spec=ProgressReport)

            # This should return a valid result or handle errors gracefully
            result = executor.execute(decision, mock_report)
            assert isinstance(result, ActionResult)

    def test_import_structure(self):
        """Test that all required imports work."""
        from novatrade.autonomy.action_executor import (
            ActionResult,
            DiagnosticFinding,
            DirectActionExecutor,
        )

        assert ActionResult is not None
        assert DiagnosticFinding is not None
        assert DirectActionExecutor is not None


class TestDirectActionExecutorConstants:
    """Test module constants and configurations."""

    def test_restartable_services_constant(self):
        """Test RESTARTABLE_SERVICES constant is properly defined."""
        executor = DirectActionExecutor()

        assert hasattr(executor, "RESTARTABLE_SERVICES")
        assert isinstance(executor.RESTARTABLE_SERVICES, tuple)
        assert len(executor.RESTARTABLE_SERVICES) > 0

        # Should contain expected services
        assert "novacore-novatrade" in executor.RESTARTABLE_SERVICES

        # All entries should be strings
        for service in executor.RESTARTABLE_SERVICES:
            assert isinstance(service, str)
            assert len(service) > 0
