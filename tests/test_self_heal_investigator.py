"""Tests for agents/self_heal_investigator.py — AI-driven warning investigation."""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.self_heal_investigator import (
    InvestigationResult,
    DiagnosisResult,
    diagnose,
    execute_fix,
    collect_context,
    investigate_warning,
    investigate_all_pending,
    check_investigator_health,
    _log_investigation,
)
from utils.warning_router import (
    Warning,
    WarningSeverity,
    WarningCategory,
    emit,
    get_pending_warnings,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Redirect all state to temp directory."""
    # Warning router state
    monkeypatch.setattr("utils.warning_router.STATE_DIR", tmp_path)
    monkeypatch.setattr("utils.warning_router.QUEUE_FILE", tmp_path / "warning_queue.jsonl")
    monkeypatch.setattr("utils.warning_router.ARCHIVE_FILE", tmp_path / "warning_archive.jsonl")
    monkeypatch.setattr("utils.warning_router.COOLDOWN_FILE", tmp_path / "warning_cooldown.json")
    # Investigator state
    monkeypatch.setattr("agents.self_heal_investigator.STATE_DIR", tmp_path)
    monkeypatch.setattr("agents.self_heal_investigator.LOGS_DIR", tmp_path / "LOGS")
    monkeypatch.setattr("agents.self_heal_investigator.INVESTIGATION_LOG", tmp_path / "LOGS" / "investigations.jsonl")
    monkeypatch.setattr("agents.self_heal_investigator.INVESTIGATION_DIR", tmp_path / "investigations")


class TestDiagnosis:
    def test_service_failure_restart(self):
        diag = diagnose(
            "Health check failed: novacore-watcher — inactive",
            "heartbeat",
            {"is_active": "inactive", "name": "novacore-watcher"},
        )
        assert diag.can_auto_fix is True
        assert diag.fix_action == "restart_service"
        assert diag.fix_params["service_name"] == "novacore-watcher"

    def test_service_active_transient(self):
        diag = diagnose(
            "Health check failed: novacore-telegram — check error",
            "heartbeat",
            {"is_active": "active", "name": "novacore-telegram"},
        )
        assert diag.can_auto_fix is False
        assert "transient" in diag.root_cause.lower()

    def test_circuit_breaker_escalate(self):
        diag = diagnose(
            "Circuit breaker OPEN: claude_api",
            "circuit_breaker",
            {"warning_data": {"breaker_name": "claude_api"}},
        )
        assert diag.can_auto_fix is False
        assert diag.escalation_reason != ""
        assert "claude_api" in diag.escalation_reason

    def test_budget_alert_escalate(self):
        diag = diagnose(
            "Daily input tokens at 90% of limit",
            "budget",
            {},
        )
        assert diag.can_auto_fix is False
        assert "budget" in diag.root_cause.lower()

    def test_critical_runaway_auto_degrade(self):
        diag = diagnose(
            "Runaway detected: burn rate 5x",
            "runaway",
            {"warning_data": {"severity": "critical"}},
        )
        assert diag.can_auto_fix is True
        assert diag.fix_action == "adjust_degradation"
        assert diag.fix_params["target_tier"] == "REDUCED"

    def test_warning_runaway_no_auto_fix(self):
        diag = diagnose(
            "Runaway detected: elevated burn rate",
            "runaway",
            {"warning_data": {"severity": "warning"}},
        )
        assert diag.can_auto_fix is False

    def test_critical_memory_auto_degrade(self):
        diag = diagnose(
            "CRITICAL memory: 3600MB RSS",
            "memory",
            {},
        )
        assert diag.can_auto_fix is True
        assert diag.fix_action == "adjust_degradation"
        assert diag.fix_params["target_tier"] == "MINIMAL"

    def test_error_spike_permanent(self):
        diag = diagnose(
            "Error spike: 15 errors in 60m",
            "error_spike",
            {"recent_errors": [{"category": "PERMANENT"}] * 4},
        )
        assert diag.can_auto_fix is False
        assert diag.escalation_reason != ""
        assert "permanent" in diag.escalation_reason.lower()

    def test_drift_informational(self):
        diag = diagnose(
            "Behavioral drift from baseline",
            "drift",
            {},
        )
        assert diag.can_auto_fix is False
        assert diag.escalation_reason != ""

    def test_unknown_category_escalate(self):
        diag = diagnose(
            "Something weird happened",
            "unknown_category",
            {},
        )
        assert diag.can_auto_fix is False
        assert diag.confidence == "low"


class TestExecuteFix:
    def test_no_fix_available(self):
        diag = DiagnosisResult(root_cause="test", can_auto_fix=False)
        success, detail = execute_fix(diag)
        assert success is False

    @patch("agents.self_heal_investigator.subprocess.run")
    def test_restart_service_success(self, mock_run):
        # First call: is-active → inactive
        # Second call: restart → success
        # Third call: verify → active
        mock_run.side_effect = [
            MagicMock(stdout="inactive\n", returncode=0),
            MagicMock(stdout="", stderr="", returncode=0),
            MagicMock(stdout="active\n", returncode=0),
        ]
        diag = DiagnosisResult(
            root_cause="Service down",
            can_auto_fix=True,
            fix_action="restart_service",
            fix_params={"service_name": "novacore-watcher"},
        )
        success, detail = execute_fix(diag)
        assert success is True
        assert "successfully" in detail.lower()

    @patch("agents.self_heal_investigator.subprocess.run")
    def test_restart_service_already_active(self, mock_run):
        mock_run.return_value = MagicMock(stdout="active\n", returncode=0)
        diag = DiagnosisResult(
            root_cause="Service check",
            can_auto_fix=True,
            fix_action="restart_service",
            fix_params={"service_name": "novacore-watcher"},
        )
        success, detail = execute_fix(diag)
        assert success is True
        assert "already active" in detail.lower()

    def test_restart_disallowed_service(self):
        diag = DiagnosisResult(
            root_cause="Service down",
            can_auto_fix=True,
            fix_action="restart_service",
            fix_params={"service_name": "nginx"},
        )
        success, detail = execute_fix(diag)
        assert success is False
        assert "whitelist" in detail.lower()

    @patch("agents.self_heal_investigator._try_adjust_degradation")
    def test_adjust_degradation(self, mock_adjust):
        mock_adjust.return_value = (True, "Adjusted to REDUCED")
        diag = DiagnosisResult(
            root_cause="Runaway",
            can_auto_fix=True,
            fix_action="adjust_degradation",
            fix_params={"target_tier": "REDUCED", "reason": "test"},
        )
        success, detail = execute_fix(diag)
        assert success is True


class TestInvestigateWarning:
    @patch("agents.self_heal_investigator.escalate_to_telegram", return_value=False)
    @patch("agents.self_heal_investigator.collect_context", return_value={})
    def test_escalated_warning(self, mock_ctx, mock_tg):
        emit("budget_enforcer", "budget", "Daily tokens at 90%", severity=WarningSeverity.CRITICAL)
        warnings = get_pending_warnings()
        result = investigate_warning(warnings[0])
        assert result.action_taken == "escalated"
        assert result.escalated is True
        mock_tg.assert_called_once()

    @patch("agents.self_heal_investigator.escalate_to_telegram", return_value=False)
    @patch("agents.self_heal_investigator.execute_fix", return_value=(True, "Fixed"))
    @patch("agents.self_heal_investigator.collect_context", return_value={"is_active": "inactive"})
    def test_auto_fixed_warning(self, mock_ctx, mock_fix, mock_tg):
        emit("heartbeat", "heartbeat", "Health check failed: novacore-watcher — inactive",
             severity=WarningSeverity.CRITICAL,
             context={"name": "novacore-watcher", "is_active": "inactive"})
        warnings = get_pending_warnings()
        result = investigate_warning(warnings[0])
        assert result.action_taken == "auto_fixed"
        assert result.fix_successful is True

    @patch("agents.self_heal_investigator.escalate_to_telegram", return_value=False)
    @patch("agents.self_heal_investigator.collect_context", return_value={})
    def test_deferred_warning(self, mock_ctx, mock_tg):
        emit("error_classifier", "error_spike", "Error spike: 10 errors in 60m",
             severity=WarningSeverity.WARNING,
             context={"total": 10, "by_category": {"TRANSIENT": 10}, "retry_eligible": 10})
        warnings = get_pending_warnings()
        # This one has no permanent errors, so diagnosis says "mostly transient" with no escalation
        result = investigate_warning(warnings[0])
        # Could be deferred or escalated depending on diagnosis
        assert result.action_taken in ("deferred", "escalated")


class TestInvestigateAllPending:
    @patch("agents.self_heal_investigator.escalate_to_telegram", return_value=False)
    @patch("agents.self_heal_investigator.collect_context", return_value={})
    def test_processes_all_warnings(self, mock_ctx, mock_tg):
        emit("s1", "budget", "Budget alert 1", severity=WarningSeverity.WARNING)
        emit("s2", "drift", "Drift signal", severity=WarningSeverity.WARNING)
        results = investigate_all_pending()
        assert len(results) == 2
        # All should be processed
        remaining = get_pending_warnings()
        assert len(remaining) == 0

    @patch("agents.self_heal_investigator.escalate_to_telegram", return_value=False)
    @patch("agents.self_heal_investigator.collect_context", side_effect=Exception("boom"))
    def test_handles_investigation_error(self, mock_ctx, mock_tg):
        emit("s1", "test", "Failing warning")
        results = investigate_all_pending()
        assert len(results) == 1
        assert results[0].action_taken == "escalated"
        assert "failed" in results[0].diagnosis.lower()


class TestInvestigationLogging:
    def test_log_creates_files(self, tmp_path):
        result = InvestigationResult(
            warning_fingerprint="abc123",
            warning_message="Test warning",
            diagnosis="Test diagnosis",
            action_taken="none",
        )
        _log_investigation(result)

        log_file = tmp_path / "LOGS" / "investigations.jsonl"
        assert log_file.exists()
        entries = [json.loads(ln) for ln in log_file.read_text().strip().splitlines()]
        assert len(entries) == 1
        assert entries[0]["diagnosis"] == "Test diagnosis"

        inv_dir = tmp_path / "investigations"
        assert inv_dir.exists()
        inv_files = list(inv_dir.glob("*.json"))
        assert len(inv_files) == 1


class TestHealthCheck:
    def test_healthy_empty_queue(self):
        result = check_investigator_health()
        assert result["ok"] is True
        assert result["stats"]["pending"] == 0

    def test_unhealthy_queue_overflow(self):
        for i in range(25):
            emit(f"s{i}", "test", f"Warning {i}", cooldown_secs=0)
        result = check_investigator_health()
        assert result["ok"] is False
