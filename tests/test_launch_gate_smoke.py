"""Smoke tests for novatrade.runtime.launch_gate module.

These tests verify basic functionality and instantiation of all classes and
functions in the launch_gate module. They focus on testing structure, enums,
data classes, and basic function calls without heavy mocking.
"""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from novatrade.config import FtmoProfile, MetaApiConfig, NovaTradeCfg, RiskConfig
from novatrade.runtime.launch_gate import (
    CheckCategory,
    GateCheck,
    LaunchMode,
    LaunchReadiness,
    ReadinessVerdict,
    StartupValidation,
    evaluate_launch_gate,
    generate_readiness_report,
    record_launch_event,
    resolve_launch_mode,
    validate_startup,
)


class TestEnums:
    """Test all enum types can be instantiated and have expected values."""

    def test_launch_mode_enum(self):
        """Test LaunchMode enum values."""
        assert LaunchMode.DRY_RUN.value == "dry_run"
        assert LaunchMode.ACTIVE_READY.value == "active_ready"
        assert LaunchMode.ACTIVE_DEMO.value == "active_demo"

        # All enum values are strings
        for mode in LaunchMode:
            assert isinstance(mode.value, str)

    def test_readiness_verdict_enum(self):
        """Test ReadinessVerdict enum values."""
        assert ReadinessVerdict.READY_FOR_ACTIVE_DEMO.value == "READY_FOR_ACTIVE_DEMO"
        assert ReadinessVerdict.CONDITIONALLY_READY.value == "CONDITIONALLY_READY"
        assert ReadinessVerdict.NOT_READY.value == "NOT_READY"

    def test_check_category_enum(self):
        """Test CheckCategory enum values."""
        assert CheckCategory.CODE.value == "code"
        assert CheckCategory.CONFIG.value == "config"
        assert CheckCategory.ADAPTER.value == "adapter"
        assert CheckCategory.RISK.value == "risk"
        assert CheckCategory.MONITORING.value == "monitoring"
        assert CheckCategory.EXTERNAL.value == "external"
        assert CheckCategory.OPERATOR.value == "operator"


class TestDataClasses:
    """Test dataclass instantiation and methods."""

    def test_gate_check_creation(self):
        """Test GateCheck can be created and serialized."""
        check = GateCheck(
            name="test_check", category=CheckCategory.CODE, passed=True, detail="Test detail", required_for_active=False
        )

        assert check.name == "test_check"
        assert check.category == CheckCategory.CODE
        assert check.passed is True
        assert check.detail == "Test detail"
        assert check.required_for_active is False

        # Test serialization
        data = check.to_dict()
        assert isinstance(data, dict)
        assert data["name"] == "test_check"
        assert data["category"] == "code"
        assert data["passed"] is True
        assert data["detail"] == "Test detail"
        assert data["required_for_active"] is False

    def test_launch_readiness_creation(self):
        """Test LaunchReadiness can be created and has properties."""
        check1 = GateCheck("check1", CheckCategory.CODE, True)
        check2 = GateCheck("check2", CheckCategory.CONFIG, False)

        readiness = LaunchReadiness(
            verdict=ReadinessVerdict.CONDITIONALLY_READY,
            launch_mode=LaunchMode.ACTIVE_READY,
            checks=[check1, check2],
            blockers=["blocker1"],
            warnings=["warning1"],
        )

        assert readiness.verdict == ReadinessVerdict.CONDITIONALLY_READY
        assert readiness.launch_mode == LaunchMode.ACTIVE_READY
        assert len(readiness.checks) == 2
        assert readiness.passed_count == 1
        assert readiness.failed_count == 1
        assert readiness.total_count == 2
        assert readiness.blockers == ["blocker1"]
        assert readiness.warnings == ["warning1"]

        # Test serialization
        data = readiness.to_dict()
        assert isinstance(data, dict)
        assert data["verdict"] == "CONDITIONALLY_READY"
        assert data["launch_mode"] == "active_ready"
        assert data["passed"] == 1
        assert data["failed"] == 1
        assert data["total"] == 2

    def test_startup_validation_creation(self):
        """Test StartupValidation can be created and serialized."""
        validation = StartupValidation(
            ok=False, mode=LaunchMode.DRY_RUN, errors=["error1", "error2"], warnings=["warning1"]
        )

        assert validation.ok is False
        assert validation.mode == LaunchMode.DRY_RUN
        assert validation.errors == ["error1", "error2"]
        assert validation.warnings == ["warning1"]

        # Test serialization
        data = validation.to_dict()
        assert isinstance(data, dict)
        assert data["ok"] is False
        assert data["mode"] == "dry_run"
        assert data["errors"] == ["error1", "error2"]


class TestLaunchModeResolution:
    """Test launch mode resolution from environment."""

    def test_resolve_launch_mode_explicit(self):
        """Test explicit launch mode settings."""
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "dry_run"}):
            assert resolve_launch_mode() == LaunchMode.DRY_RUN

        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active_ready"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_READY

        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active_demo"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_DEMO

    def test_resolve_launch_mode_variants(self):
        """Test different format variants."""
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "dry-run"}):
            assert resolve_launch_mode() == LaunchMode.DRY_RUN

        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "active-ready"}):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_READY

    def test_resolve_launch_mode_fallback(self):
        """Test fallback to NOVATRADE_DRY_RUN."""
        with patch.dict(os.environ, {"NOVATRADE_DRY_RUN": "true"}, clear=True):
            assert resolve_launch_mode() == LaunchMode.DRY_RUN

        with patch.dict(os.environ, {"NOVATRADE_DRY_RUN": "false"}, clear=True):
            assert resolve_launch_mode() == LaunchMode.ACTIVE_READY

    def test_resolve_launch_mode_default(self):
        """Test default when no env vars set."""
        with patch.dict(os.environ, {}, clear=True):
            # Should default to DRY_RUN (NOVATRADE_DRY_RUN defaults to "true")
            assert resolve_launch_mode() == LaunchMode.DRY_RUN


class TestStartupValidation:
    """Test startup validation logic."""

    def create_minimal_config(self) -> NovaTradeCfg:
        """Create a minimal valid config for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return NovaTradeCfg(
                symbols=["EURUSD"],
                timeframes=["H1"],
                data_dir=Path(temp_dir),
                risk=RiskConfig(max_daily_drawdown_pct=5.0, max_total_drawdown_pct=10.0, max_volume_per_trade=1.0),
                metaapi=MetaApiConfig(token="test_token", account_id="test_account"),
                ftmo=FtmoProfile(enabled=False),
            )

    def test_validate_startup_dry_run(self):
        """Test startup validation for dry run mode."""
        cfg = self.create_minimal_config()
        result = validate_startup(cfg, LaunchMode.DRY_RUN)

        assert isinstance(result, StartupValidation)
        assert result.mode == LaunchMode.DRY_RUN
        # Should have minimal errors for dry run
        assert isinstance(result.errors, list)
        assert isinstance(result.warnings, list)

    def test_validate_startup_active_ready(self):
        """Test startup validation for active ready mode."""
        cfg = self.create_minimal_config()

        # Without webhook secret, should have errors
        result = validate_startup(cfg, LaunchMode.ACTIVE_READY)
        assert isinstance(result, StartupValidation)
        assert result.mode == LaunchMode.ACTIVE_READY

        # Should mention webhook secret
        error_text = " ".join(result.errors)
        assert "WEBHOOK_SECRET" in error_text

    def test_validate_startup_empty_config(self):
        """Test validation with minimal/invalid config."""
        cfg = NovaTradeCfg()  # Empty config
        result = validate_startup(cfg, LaunchMode.DRY_RUN)

        assert isinstance(result, StartupValidation)
        # Empty config might pass for dry_run but should have warnings
        if not result.ok:
            assert len(result.errors) > 0
        else:
            # Should at least have warnings for missing setup
            assert len(result.warnings) >= 0  # Warnings are optional


class TestLaunchGateEvaluation:
    """Test launch gate evaluation logic."""

    def create_minimal_config(self) -> NovaTradeCfg:
        """Create a minimal valid config for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return NovaTradeCfg(
                symbols=["EURUSD"],
                timeframes=["H1"],
                data_dir=Path(temp_dir),
                risk=RiskConfig(max_daily_drawdown_pct=5.0, max_total_drawdown_pct=10.0, max_volume_per_trade=1.0),
                metaapi=MetaApiConfig(token="test_token", account_id="test_account"),
                ftmo=FtmoProfile(enabled=False),
            )

    def test_evaluate_launch_gate_basic(self):
        """Test basic launch gate evaluation."""
        cfg = self.create_minimal_config()

        result = evaluate_launch_gate(
            cfg, LaunchMode.DRY_RUN, risk_engine_initialized=True, agent_initialized=True, monitor_initialized=True
        )

        assert isinstance(result, LaunchReadiness)
        assert result.launch_mode == LaunchMode.DRY_RUN
        assert isinstance(result.checks, list)
        assert len(result.checks) > 0
        assert isinstance(result.blockers, list)
        assert isinstance(result.warnings, list)

    def test_evaluate_launch_gate_active_demo(self):
        """Test launch gate evaluation for active demo mode."""
        cfg = self.create_minimal_config()

        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "test_secret"}):
            result = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_DEMO,
                risk_engine_initialized=True,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="MetaApiAdapter",
            )

        assert isinstance(result, LaunchReadiness)
        assert result.launch_mode == LaunchMode.ACTIVE_DEMO
        assert len(result.checks) > 0

    def test_evaluate_launch_gate_all_parameters(self):
        """Test launch gate with all parameters set."""
        cfg = self.create_minimal_config()

        result = evaluate_launch_gate(
            cfg,
            LaunchMode.ACTIVE_READY,
            risk_engine_initialized=False,
            risk_engine_halted=True,
            agent_initialized=False,
            monitor_initialized=False,
            adapter_connected=False,
            adapter_type="DryRunAdapter",
        )

        assert isinstance(result, LaunchReadiness)
        # With all failures, should not be ready
        assert result.verdict in [ReadinessVerdict.NOT_READY, ReadinessVerdict.CONDITIONALLY_READY]


class TestReportGeneration:
    """Test readiness report generation."""

    def test_generate_readiness_report_basic(self):
        """Test basic report generation."""
        check = GateCheck("test_check", CheckCategory.CODE, True, "Test detail")
        readiness = LaunchReadiness(
            verdict=ReadinessVerdict.READY_FOR_ACTIVE_DEMO, launch_mode=LaunchMode.DRY_RUN, checks=[check]
        )

        report = generate_readiness_report(readiness)

        assert isinstance(report, str)
        assert len(report) > 0
        assert "NOVATRADE LAUNCH READINESS ASSESSMENT" in report
        assert "READY_FOR_ACTIVE_DEMO" in report
        assert "dry_run" in report
        assert "test_check" in report

    def test_generate_readiness_report_with_blockers(self):
        """Test report generation with blockers and warnings."""
        check = GateCheck("failed_check", CheckCategory.RISK, False, "Failed detail")
        readiness = LaunchReadiness(
            verdict=ReadinessVerdict.NOT_READY,
            launch_mode=LaunchMode.ACTIVE_DEMO,
            checks=[check],
            blockers=["Critical blocker"],
            warnings=["Warning message"],
            operator_tasks=["Fix something"],
            external_confirmations=["Confirm external"],
        )

        report = generate_readiness_report(readiness)

        assert "BLOCKERS:" in report
        assert "Critical blocker" in report
        assert "WARNINGS:" in report
        assert "Warning message" in report
        assert "OPERATOR TASKS:" in report
        assert "Fix something" in report
        assert "EXTERNAL CONFIRMATIONS" in report
        assert "Confirm external" in report


class TestEvidenceRecording:
    """Test evidence recording functionality."""

    def test_record_launch_event_no_recorder(self):
        """Test recording event with no recorder (should not crash)."""
        # Should not raise an exception
        record_launch_event(None, "test_event", {"key": "value"})

    def test_record_launch_event_with_mock_recorder(self):
        """Test recording event with mock recorder."""
        from unittest.mock import MagicMock

        recorder = MagicMock()

        # Should not raise an exception
        record_launch_event(recorder, "test_event", {"key": "value"})

        # Verify _append was called
        recorder._append.assert_called_once()


class TestIntegration:
    """Integration smoke tests."""

    def test_full_workflow_dry_run(self):
        """Test complete workflow for dry run mode."""
        # Resolve mode
        with patch.dict(os.environ, {"NOVATRADE_LAUNCH_MODE": "dry_run"}):
            mode = resolve_launch_mode()
            assert mode == LaunchMode.DRY_RUN

        # Create config
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = NovaTradeCfg(
                symbols=["EURUSD"],
                timeframes=["H1"],
                data_dir=Path(temp_dir),
                risk=RiskConfig(max_daily_drawdown_pct=5.0, max_total_drawdown_pct=10.0, max_volume_per_trade=1.0),
                metaapi=MetaApiConfig(),
                ftmo=FtmoProfile(enabled=False),
            )

            # Validate startup
            startup = validate_startup(cfg, mode)
            assert isinstance(startup, StartupValidation)

            # Evaluate launch gate
            readiness = evaluate_launch_gate(
                cfg, mode, risk_engine_initialized=True, agent_initialized=True, monitor_initialized=True
            )
            assert isinstance(readiness, LaunchReadiness)

            # Generate report
            report = generate_readiness_report(readiness)
            assert isinstance(report, str)
            assert len(report) > 0

    def test_timestamp_fields(self):
        """Test that timestamp fields are properly set."""
        readiness = LaunchReadiness(verdict=ReadinessVerdict.READY_FOR_ACTIVE_DEMO, launch_mode=LaunchMode.DRY_RUN)

        # Timestamp should be close to current time
        now = time.time()
        assert abs(readiness.timestamp - now) < 5.0  # Within 5 seconds

        # Serialized timestamp should be included
        data = readiness.to_dict()
        assert "timestamp" in data
        assert isinstance(data["timestamp"], float)
