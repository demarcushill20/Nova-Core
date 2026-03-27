"""Tests for DirectActionExecutor — inline diagnostics and remediation.

Covers:
  - Diagnostic dispatch per dimension
  - NovaTrade-specific trade detection
  - Service status checking
  - Remediation logic
  - Alert construction
  - ActionResult building
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from novatrade.autonomy.action_executor import (
    ActionResult,
    DiagnosticFinding,
    DirectActionExecutor,
)
from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    SubMetric,
)

# =====================================================================
# Helpers
# =====================================================================


def make_sub_metric(name: str, value: float, raw_value: float | None = None) -> SubMetric:
    return SubMetric(name=name, value=value, raw_value=raw_value)


def make_dimension(
    name: str,
    score: float,
    sub_metrics: list[SubMetric] | None = None,
    warnings: list[str] | None = None,
) -> DimensionScore:
    return DimensionScore(
        name=name,
        score=score,
        sub_metrics=sub_metrics or [make_sub_metric("m1", score)],
        warnings=warnings or [],
    )


def make_trend(current: float, direction: str = "stable") -> ScoreTrend:
    return ScoreTrend(current=current, avg_6h=current, direction=direction)


def make_report(
    dims: dict[str, tuple[float, list[SubMetric] | None]] | None = None,
) -> ProgressReport:
    if dims is None:
        dims = {
            "system_health": (80.0, None),
            "execution_pipeline": (75.0, None),
            "strategy_validity": (70.0, None),
            "risk_engine": (85.0, None),
            "performance_stability": (60.0, None),
        }
    dimensions = {name: make_dimension(name, score, subs) for name, (score, subs) in dims.items()}
    trends = {name: make_trend(score) for name, (score, _) in dims.items()}
    overall = sum(s for s, _ in dims.values()) / len(dims)
    return ProgressReport(
        overall_score=round(overall, 1),
        dimensions=dimensions,
        trends=trends,
    )


def make_decision(
    mode: ActionMode = ActionMode.REPAIR,
    target: str = "strategy_validity",
    reason: str = "test decision",
) -> Decision:
    return Decision(
        mode=mode,
        reason=reason,
        target_dimension=target,
        suggested_actions=["action1", "action2"],
        confidence="high",
    )


# =====================================================================
# Tests: MONITOR mode
# =====================================================================


class TestMonitorMode:
    def test_monitor_returns_no_action(self, tmp_path):
        """MONITOR decisions should produce no findings or actions."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        decision = make_decision(mode=ActionMode.MONITOR, target=None)
        report = make_report()
        result = executor.execute(decision, report)
        assert result.decision_mode == "monitor"
        assert len(result.findings) == 0
        assert len(result.actions_taken) == 0
        assert not result.alert_sent
        assert not result.escalated


# =====================================================================
# Tests: Strategy validity diagnostics
# =====================================================================


class TestStrategyDiagnostics:
    def test_zero_trades_detected(self, tmp_path):
        """Zero trades in 24h should produce a critical finding."""
        (tmp_path / "STATE" / "novatrade").mkdir(parents=True)
        executor = DirectActionExecutor(base_path=str(tmp_path))

        subs = [
            make_sub_metric("trades_last_24h", 20.0, raw_value=0.0),
            make_sub_metric("silent_failure_detected", 100.0),
            make_sub_metric("signal_generation_rate", 80.0),
            make_sub_metric("signal_pipeline_health", 90.0),
        ]
        report = make_report(
            {
                "strategy_validity": (50.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        critical = [f for f in result.findings if f.status == "critical"]
        trades_finding = [f for f in critical if f.check == "trades_last_24h"]
        assert len(trades_finding) == 1
        assert "ZERO trades" in trades_finding[0].detail

    def test_silent_failure_detected(self, tmp_path):
        """Silent failure (score < 50) should produce a critical finding."""
        (tmp_path / "STATE" / "novatrade").mkdir(parents=True)
        executor = DirectActionExecutor(base_path=str(tmp_path))

        subs = [
            make_sub_metric("trades_last_24h", 80.0, raw_value=3.0),
            make_sub_metric("silent_failure_detected", 0.0),
            make_sub_metric("signal_generation_rate", 80.0),
            make_sub_metric("signal_pipeline_health", 90.0),
        ]
        report = make_report(
            {
                "strategy_validity": (50.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        critical = [f for f in result.findings if f.status == "critical"]
        silent = [f for f in critical if f.check == "silent_failure"]
        assert len(silent) == 1
        assert "Silent failure" in silent[0].detail

    def test_trade_log_empty(self, tmp_path):
        """Empty trade log file should produce a critical finding."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "trade_log.json").write_text("[]")

        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report(
            {
                "strategy_validity": (50.0, [make_sub_metric("trades_last_24h", 20.0)]),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        empty_log = [f for f in result.findings if f.check == "trade_log_empty"]
        assert len(empty_log) == 1

    def test_trade_log_missing(self, tmp_path):
        """Missing trade log file should produce a critical finding."""
        (tmp_path / "STATE" / "novatrade").mkdir(parents=True)
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report(
            {
                "strategy_validity": (50.0, [make_sub_metric("trades_last_24h", 20.0)]),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        missing = [f for f in result.findings if f.check == "trade_log_missing"]
        assert len(missing) == 1

    def test_risk_halt_active(self, tmp_path):
        """Active risk halt should produce a critical finding."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "halt_state.json").write_text(json.dumps({"halted": True, "reason": "daily loss limit exceeded"}))

        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report(
            {
                "strategy_validity": (50.0, [make_sub_metric("trades_last_24h", 20.0)]),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        halt = [f for f in result.findings if f.check == "risk_halt_active"]
        assert len(halt) == 1
        assert "daily loss limit" in halt[0].detail

    def test_metaapi_connection_failure(self, tmp_path):
        """Failed MetaApi connection should produce a critical finding."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "connection_status.json").write_text(json.dumps({"status": "disconnected", "error": "timeout"}))

        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report(
            {
                "strategy_validity": (50.0, [make_sub_metric("trades_last_24h", 20.0)]),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        conn = [f for f in result.findings if f.check == "metaapi_connection"]
        assert len(conn) == 1
        assert "disconnected" in conn[0].detail


# =====================================================================
# Tests: Execution pipeline diagnostics
# =====================================================================


class TestPipelineDiagnostics:
    def test_broker_connectivity_critical(self, tmp_path):
        """Low pipeline connectivity should produce a critical finding."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        subs = [
            make_sub_metric("pipeline_connectivity", 10.0),
            make_sub_metric("rejection_rate", 80.0),
            make_sub_metric("last_signal_age", 90.0),
            make_sub_metric("feed_health", 80.0),
        ]
        report = make_report(
            {
                "execution_pipeline": (40.0, subs),
                "system_health": (80.0, None),
                "strategy_validity": (70.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(target="execution_pipeline")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        critical = [f for f in result.findings if f.check == "broker_connectivity"]
        assert len(critical) == 1

    def test_high_rejection_rate(self, tmp_path):
        """High order rejection rate should produce a critical finding."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        subs = [
            make_sub_metric("pipeline_connectivity", 80.0),
            make_sub_metric("rejection_rate", 15.0),
            make_sub_metric("last_signal_age", 90.0),
            make_sub_metric("feed_health", 80.0),
        ]
        report = make_report(
            {
                "execution_pipeline": (50.0, subs),
                "system_health": (80.0, None),
                "strategy_validity": (70.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(target="execution_pipeline")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        rejections = [f for f in result.findings if f.check == "order_rejections"]
        assert len(rejections) == 1


# =====================================================================
# Tests: Remediation logic
# =====================================================================


class TestRemediation:
    def test_restart_when_service_down(self, tmp_path):
        """REPAIR with service down should attempt restart."""
        (tmp_path / "STATE" / "novatrade").mkdir(parents=True)
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report(
            {
                "strategy_validity": (30.0, [make_sub_metric("trades_last_24h", 20.0)]),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=False),
            patch.object(DirectActionExecutor, "_restart_service") as mock_restart,
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)
            mock_restart.assert_called_once_with("novacore-novatrade", result)

    def test_escalate_when_service_running_but_not_trading(self, tmp_path):
        """Service running + not trading should escalate (not restart)."""
        (tmp_path / "STATE" / "novatrade").mkdir(parents=True)
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report(
            {
                "strategy_validity": (30.0, [make_sub_metric("trades_last_24h", 20.0)]),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        with (
            patch.object(DirectActionExecutor, "_is_service_active", return_value=True),
            patch.object(DirectActionExecutor, "_send_alert"),
        ):
            result = executor.execute(decision, report)

        assert result.escalated
        assert any("not trading" in a for a in result.actions_taken)

    def test_no_remediation_for_research(self, tmp_path):
        """RESEARCH mode should diagnose but not remediate."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.RESEARCH, target="strategy_validity")

        with patch.object(DirectActionExecutor, "_send_alert"):
            result = executor.execute(decision, report)

        # No restart or remediation actions should be taken for RESEARCH
        assert not any("Restart" in a for a in result.actions_taken)

    def test_monitor_skips_everything(self, tmp_path):
        """MONITOR mode should skip diagnostics and remediation entirely."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.MONITOR, target=None)
        result = executor.execute(decision, report)
        assert len(result.findings) == 0
        assert len(result.actions_taken) == 0


# =====================================================================
# Tests: Alerting
# =====================================================================


class TestAlerting:
    def test_alert_sent_for_repair(self, tmp_path):
        """REPAIR decisions should trigger a Telegram alert."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        with patch.object(DirectActionExecutor, "_send_alert") as mock_alert:
            executor.execute(decision, report)
            mock_alert.assert_called_once()

    def test_alert_sent_for_execute(self, tmp_path):
        """EXECUTE decisions should trigger a Telegram alert."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.EXECUTE, target="strategy_validity")

        with patch.object(DirectActionExecutor, "_send_alert") as mock_alert:
            executor.execute(decision, report)
            mock_alert.assert_called_once()

    def test_no_alert_for_monitor(self, tmp_path):
        """MONITOR decisions should NOT trigger a Telegram alert."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.MONITOR, target=None)

        with patch.object(DirectActionExecutor, "_send_alert") as mock_alert:
            executor.execute(decision, report)
            mock_alert.assert_not_called()

    def test_no_alert_for_validate(self, tmp_path):
        """VALIDATE decisions should NOT trigger a Telegram alert."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.VALIDATE, target=None)

        with patch.object(DirectActionExecutor, "_send_alert") as mock_alert:
            executor.execute(decision, report)
            mock_alert.assert_not_called()

    def test_log_file_written(self, tmp_path):
        """Alert should also write to LOGS/autonomy_actions.log."""
        (tmp_path / "LOGS").mkdir()
        executor = DirectActionExecutor(base_path=str(tmp_path))
        make_report()
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        # Call _send_alert directly (no Telegram creds → won't actually send)
        result = ActionResult(
            decision_mode="repair",
            target_dimension="strategy_validity",
            findings=[DiagnosticFinding(check="test", status="critical", detail="test detail")],
        )
        executor._send_alert(decision, result)

        log_path = tmp_path / "LOGS" / "autonomy_actions.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "AUTONOMY REPAIR" in content


# =====================================================================
# Tests: Decision engine NovaTrade-specific detection
# =====================================================================


class TestDecisionEngineNovaTradeDetection:
    """Test the new priority Rule 0 detectors in DecisionEngine."""

    @pytest.fixture
    def engine(self, tmp_path):
        from novatrade.autonomy.decision_engine import DecisionEngine

        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)
        return DecisionEngine(base_path=str(tmp_path))

    def test_zero_trades_triggers_repair(self, engine):
        """Zero trades during market hours should override to REPAIR."""
        subs = [
            make_sub_metric("trades_last_24h", 20.0, raw_value=0.0),
            make_sub_metric("silent_failure_detected", 100.0),
        ]
        report = make_report(
            {
                "strategy_validity": (50.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        result = engine._detect_novatrade_not_trading(report)
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "ZERO trades" in result.reason
        assert result.target_dimension == "strategy_validity"

    def test_normal_trades_no_override(self, engine):
        """Normal trading activity should not trigger priority override."""
        subs = [
            make_sub_metric("trades_last_24h", 80.0, raw_value=5.0),
            make_sub_metric("silent_failure_detected", 100.0),
        ]
        report = make_report(
            {
                "strategy_validity": (80.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        result = engine._detect_novatrade_not_trading(report)
        assert result is None

    def test_silent_failure_triggers_repair(self, engine):
        """4+ hour signal silence should trigger REPAIR."""
        subs = [
            make_sub_metric("trades_last_24h", 80.0, raw_value=5.0),
            make_sub_metric("silent_failure_detected", 0.0),
        ]
        report = make_report(
            {
                "strategy_validity": (40.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (75.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        result = engine._detect_novatrade_not_trading(report)
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "silent failure" in result.reason.lower()

    def test_pipeline_broken_triggers_repair(self, engine):
        """Critically low broker connectivity should trigger REPAIR."""
        subs = [
            make_sub_metric("pipeline_connectivity", 10.0),
            make_sub_metric("rejection_rate", 80.0),
        ]
        report = make_report(
            {
                "execution_pipeline": (30.0, subs),
                "system_health": (80.0, None),
                "strategy_validity": (70.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        result = engine._detect_pipeline_broken(report)
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "connectivity" in result.reason.lower()

    def test_high_rejection_triggers_repair(self, engine):
        """Extremely high order rejection rate should trigger REPAIR."""
        subs = [
            make_sub_metric("pipeline_connectivity", 80.0),
            make_sub_metric("rejection_rate", 10.0),
        ]
        report = make_report(
            {
                "execution_pipeline": (45.0, subs),
                "system_health": (80.0, None),
                "strategy_validity": (70.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )

        result = engine._detect_pipeline_broken(report)
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "rejection" in result.reason.lower()


# =====================================================================
# Tests: Task generator auto_execute flag
# =====================================================================


class TestTaskGeneratorAutoExecute:
    def test_auto_execute_in_frontmatter(self, tmp_path):
        """Generated task files should include auto_execute: true."""
        from novatrade.autonomy.task_generator import TaskSpecGenerator

        gen = TaskSpecGenerator(base_path=str(tmp_path))
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")
        spec = gen.from_decision(decision)
        assert spec.auto_execute is True

        path = gen.write_task_file(spec, skip_dedup=True)
        assert path is not None
        content = path.read_text()
        assert "auto_execute: true" in content

    def test_repair_task_has_autonomy_note(self, tmp_path):
        """REPAIR tasks should include the autonomy note."""
        from novatrade.autonomy.task_generator import TaskSpecGenerator

        gen = TaskSpecGenerator(base_path=str(tmp_path))
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")
        spec = gen.from_decision(decision)
        assert "CRITICAL auto-generated repair task" in spec.body
        assert "Do NOT wait for human approval" in spec.body

    def test_execute_task_has_autonomy_note(self, tmp_path):
        """EXECUTE tasks should include the autonomy note."""
        from novatrade.autonomy.task_generator import TaskSpecGenerator

        gen = TaskSpecGenerator(base_path=str(tmp_path))
        decision = make_decision(mode=ActionMode.EXECUTE, target="strategy_validity")
        spec = gen.from_decision(decision)
        assert "auto-generated by the decision engine" in spec.body
        assert "Do NOT wait for human approval" in spec.body

    def test_research_task_no_autonomy_note(self, tmp_path):
        """RESEARCH tasks should not have the 'don't wait' autonomy note."""
        from novatrade.autonomy.task_generator import TaskSpecGenerator

        gen = TaskSpecGenerator(base_path=str(tmp_path))
        decision = make_decision(mode=ActionMode.RESEARCH, target="strategy_validity")
        spec = gen.from_decision(decision)
        assert "Do NOT wait for human approval" not in spec.body


# =====================================================================
# Tests: ActionResult summary
# =====================================================================


class TestActionResultSummary:
    def test_summary_includes_mode_and_counts(self, tmp_path):
        """ActionResult summary should include mode, critical count, and action count."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        report = make_report()
        decision = make_decision(mode=ActionMode.REPAIR, target="strategy_validity")

        with patch.object(DirectActionExecutor, "_send_alert"):
            result = executor.execute(decision, report)

        assert "REPAIR" in result.summary
        assert "strategy_validity" in result.summary


# =====================================================================
# Tests: Generic dimension diagnostics
# =====================================================================


class TestGenericDiagnostics:
    def test_system_health_diagnosis(self, tmp_path):
        """System health diagnostics should detect service failures."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        subs = [
            make_sub_metric("service_status", 25.0),
            make_sub_metric("error_rate", 40.0),
        ]
        report = make_report(
            {
                "system_health": (30.0, subs),
                "execution_pipeline": (80.0, None),
                "strategy_validity": (80.0, None),
                "risk_engine": (85.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(mode=ActionMode.REPAIR, target="system_health")

        with patch.object(DirectActionExecutor, "_send_alert"):
            result = executor.execute(decision, report)

        service_findings = [f for f in result.findings if f.check == "services_down"]
        assert len(service_findings) == 1

    def test_risk_engine_diagnosis(self, tmp_path):
        """Risk engine diagnostics should flag low sub-metrics."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        subs = [make_sub_metric("drawdown_enforcement", 20.0)]
        report = make_report(
            {
                "risk_engine": (20.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (80.0, None),
                "strategy_validity": (80.0, None),
                "performance_stability": (60.0, None),
            }
        )
        decision = make_decision(mode=ActionMode.REPAIR, target="risk_engine")

        with patch.object(DirectActionExecutor, "_send_alert"):
            result = executor.execute(decision, report)

        risk_findings = [f for f in result.findings if "risk_" in f.check]
        assert len(risk_findings) >= 1

    def test_performance_drawdown_critical(self, tmp_path):
        """Excessive drawdown should produce a critical finding."""
        executor = DirectActionExecutor(base_path=str(tmp_path))
        subs = [make_sub_metric("max_drawdown_30d", 10.0)]
        report = make_report(
            {
                "performance_stability": (10.0, subs),
                "system_health": (80.0, None),
                "execution_pipeline": (80.0, None),
                "strategy_validity": (80.0, None),
                "risk_engine": (85.0, None),
            }
        )
        decision = make_decision(mode=ActionMode.REPAIR, target="performance_stability")

        with patch.object(DirectActionExecutor, "_send_alert"):
            result = executor.execute(decision, report)

        dd_findings = [f for f in result.findings if f.check == "excessive_drawdown"]
        assert len(dd_findings) == 1
        assert dd_findings[0].status == "critical"
