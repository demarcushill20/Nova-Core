"""Tests for InvestigationExecutor — multi-round evidence-following."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.investigation_executor import (
    InvestigationExecutor,
    InvestigationReport,
    InvestigationStep,
)
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    overall: float = 75.0,
    dims: dict[str, float] | None = None,
) -> ProgressReport:
    if dims is None:
        dims = {
            "system_health": 90.0,
            "strategy_validity": 60.0,
            "execution_pipeline": 80.0,
        }
    return ProgressReport(
        overall_score=overall,
        dimensions={name: DimensionScore(name=name, score=score) for name, score in dims.items()},
        trends={name: ScoreTrend(current=dims[name]) for name in dims},
    )


def _make_decision(
    mode: ActionMode = ActionMode.REPAIR,
    target: str = "strategy_validity",
) -> Decision:
    return Decision(
        mode=mode,
        reason="Test investigation trigger",
        target_dimension=target,
        confidence="high",
    )


def _setup_state(tmp_path: Path, files: dict[str, dict | list] | None = None) -> None:
    """Create STATE/novatrade/ directory with optional state files."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True, exist_ok=True)
    if files:
        for name, content in files.items():
            (state_dir / name).write_text(json.dumps(content))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInvestigationBasics:
    """Test basic investigation flow."""

    def test_investigate_returns_report(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            report = executor.investigate(_make_decision(), _make_report())

        assert isinstance(report, InvestigationReport)
        assert report.investigation_id.startswith("inv_repair_strategy_validity_")
        assert report.completed_at
        assert len(report.steps) > 0

    def test_investigate_persists_to_disk(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            executor.investigate(_make_decision(), _make_report())

        inv_path = tmp_path / "STATE" / "investigations.json"
        assert inv_path.exists()
        data = json.loads(inv_path.read_text())
        assert len(data) == 1

    def test_investigation_caps_at_50(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            for _ in range(55):
                executor.investigate(_make_decision(), _make_report())

        inv_path = tmp_path / "STATE" / "investigations.json"
        data = json.loads(inv_path.read_text())
        assert len(data) == 50


class TestServiceChecks:
    """Test service status checks."""

    def test_service_active(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            step = executor._check_service_status(1)

        assert step.status == "ok"
        assert "active" in step.result

    def test_service_inactive(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "inactive\n", "returncode": 3})()
            step = executor._check_service_status(1)

        assert step.status == "critical"
        assert "NOT active" in step.result

    def test_all_services_up(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            step = executor._check_all_services(1)

        assert step.status == "ok"
        assert "4 services" in step.result

    def test_some_services_down(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        call_count = [0]

        def mock_run_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return type("CP", (), {"stdout": "active\n", "returncode": 0})()
            return type("CP", (), {"stdout": "inactive\n", "returncode": 3})()

        with patch("subprocess.run", side_effect=mock_run_side_effect):
            step = executor._check_all_services(1)

        assert step.status == "critical"
        assert "down" in step.result.lower()


class TestTradeLogChecks:
    """Test trade log investigation."""

    def test_trade_log_missing(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_trade_log(1)
        assert step.status == "critical"
        assert "does not exist" in step.result

    def test_trade_log_empty(self, tmp_path: Path) -> None:
        _setup_state(tmp_path, {"trade_log.json": []})
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_trade_log(1)
        assert step.status == "critical"
        assert "0 trades" in step.result

    def test_trade_log_with_trades(self, tmp_path: Path) -> None:
        _setup_state(tmp_path, {"trade_log.json": [{"id": 1}, {"id": 2}]})
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_trade_log(1)
        assert step.status == "ok"
        assert "2 trades" in step.result


class TestHaltStateChecks:
    """Test halt state investigation."""

    def test_no_halt(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_halt_state(1)
        assert step.status == "ok"

    def test_halt_active(self, tmp_path: Path) -> None:
        _setup_state(tmp_path, {"halt_state.json": {"halted": True, "reason": "Daily loss limit"}})
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_halt_state(1)
        assert step.status == "critical"
        assert "HALT ACTIVE" in step.result

    def test_halt_inactive(self, tmp_path: Path) -> None:
        _setup_state(tmp_path, {"halt_state.json": {"halted": False}})
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_halt_state(1)
        assert step.status == "ok"


class TestConnectionChecks:
    """Test MetaApi connection checks."""

    def test_connected(self, tmp_path: Path) -> None:
        _setup_state(tmp_path, {"connection_status.json": {"status": "connected"}})
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_metaapi_connection(1)
        assert step.status == "ok"

    def test_disconnected(self, tmp_path: Path) -> None:
        _setup_state(tmp_path, {"connection_status.json": {"status": "disconnected"}})
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_metaapi_connection(1)
        assert step.status == "critical"

    def test_no_status_file(self, tmp_path: Path) -> None:
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        step = executor._check_metaapi_connection(1)
        assert step.status == "warning"


class TestResourceChecks:
    """Test disk and memory checks."""

    def test_disk_space(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        step = executor._check_disk_space(1)
        assert step.status in ("ok", "warning", "critical")
        assert "Disk usage" in step.result

    def test_memory(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        step = executor._check_memory(1)
        # On Linux this should work, on other platforms may fail gracefully
        assert step.status in ("ok", "warning", "critical")


class TestRootCauseSynthesis:
    """Test root cause analysis."""

    def test_single_critical_high_confidence(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="strategy_validity",
            trigger_reason="Test",
            steps=[
                InvestigationStep(
                    round_num=1, check="check_halt_state", result="HALT ACTIVE: daily loss", status="critical"
                ),
            ],
        )

        root = executor._synthesize_root_cause(report)
        assert root is not None
        assert root[1] == "high"  # confidence
        assert "halt_state" in root[0]

    def test_service_down_root_cause(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="strategy_validity",
            trigger_reason="Test",
            steps=[
                InvestigationStep(
                    round_num=1,
                    check="check_service_status",
                    result="novacore-novatrade is NOT active",
                    status="critical",
                ),
                InvestigationStep(round_num=1, check="check_trade_log", result="0 trades", status="critical"),
            ],
        )

        root = executor._synthesize_root_cause(report)
        assert root is not None
        assert root[1] == "high"
        assert "service is down" in root[0].lower()

    def test_connectivity_root_cause(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="execution_pipeline",
            trigger_reason="Test",
            steps=[
                InvestigationStep(
                    round_num=1, check="check_metaapi_connection", result="disconnected", status="critical"
                ),
                InvestigationStep(round_num=1, check="check_feed_health", result="Feed stale", status="critical"),
            ],
        )

        root = executor._synthesize_root_cause(report)
        assert root is not None
        assert root[1] == "high"
        assert "connection" in root[0].lower()

    def test_no_critical_no_root_cause(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="system_health",
            trigger_reason="Test",
            steps=[
                InvestigationStep(round_num=1, check="check_all_services", result="All 4 services active", status="ok"),
            ],
        )

        root = executor._synthesize_root_cause(report)
        assert root is None


class TestActionRecommendation:
    """Test action recommendation generation."""

    def test_service_down_recommendation(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="strategy_validity",
            trigger_reason="Test",
            root_cause="Service is down",
        )

        action = executor._recommend_action(report)
        assert "restart" in action.lower()

    def test_halt_recommendation(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="risk_engine",
            trigger_reason="Test",
            root_cause="Risk engine halt active",
        )

        action = executor._recommend_action(report)
        assert "halt" in action.lower()

    def test_no_root_cause_escalate(self, tmp_path: Path) -> None:
        executor = InvestigationExecutor(str(tmp_path))
        report = InvestigationReport(
            investigation_id="test",
            target_dimension="system_health",
            trigger_reason="Test",
        )

        action = executor._recommend_action(report)
        assert "escalate" in action.lower()


class TestMultiRoundInvestigation:
    """Test multi-round investigation flow."""

    def test_full_investigation_with_state(self, tmp_path: Path) -> None:
        """Full investigation with state files present."""
        _setup_state(
            tmp_path,
            {
                "trade_log.json": [],
                "halt_state.json": {"halted": False},
                "connection_status.json": {"status": "connected"},
            },
        )
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            report = executor.investigate(
                _make_decision(target="strategy_validity"),
                _make_report(),
            )

        # Should have multiple steps across rounds
        assert len(report.steps) >= 3
        assert report.completed_at
        assert report.recommended_action

    def test_investigation_unknown_dimension(self, tmp_path: Path) -> None:
        """Unknown dimension falls back to system_health chain."""
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            report = executor.investigate(
                Decision(mode=ActionMode.REPAIR, reason="Unknown", target_dimension="unknown_dim"),
                _make_report(),
            )

        assert len(report.steps) >= 1

    def test_investigation_no_dimension(self, tmp_path: Path) -> None:
        """None dimension uses system_health chain."""
        _setup_state(tmp_path)
        executor = InvestigationExecutor(str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("CP", (), {"stdout": "active\n", "returncode": 0})()
            report = executor.investigate(
                Decision(mode=ActionMode.REPAIR, reason="System", target_dimension=None),
                _make_report(),
            )

        assert len(report.steps) >= 1


class TestInvestigationStep:
    """Test InvestigationStep data structure."""

    def test_step_creation(self) -> None:
        step = InvestigationStep(
            round_num=1,
            check="check_service_status",
            result="Service active",
            status="ok",
        )
        assert step.follow_up is None
        assert step.round_num == 1

    def test_step_with_follow_up(self) -> None:
        step = InvestigationStep(
            round_num=1,
            check="check_service_status",
            result="Service down",
            status="critical",
            follow_up="check_logs",
        )
        assert step.follow_up == "check_logs"
