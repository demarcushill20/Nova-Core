"""Tests for Phase 6: Operational Goal Tree.

Covers:
- Per-subgoal evaluators (engine.py)
- GoalDecomposer.update_progress with evaluators and fallback
- Goal-driven decision selection (_find_top_actionable_subgoal)
- Task-to-subgoal linking (TaskSpec fields + frontmatter)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from novatrade.autonomy.decision_context import DecisionContext
from novatrade.autonomy.decision_engine import DecisionEngine
from novatrade.autonomy.decomposition.engine import (
    SUBGOAL_EVALUATORS,
    GoalDecomposer,
    _eval_drawdown_safe,
    _eval_no_orphan_tasks,
    _eval_risk_not_halted,
    _eval_signals_generating,
    _eval_state_file,
)
from novatrade.autonomy.decomposition.models import (
    GoalDecomposition,
    SubGoal,
    SubGoalStatus,
)
from novatrade.autonomy.schemas import (
    AlertLevel,
    DimensionScore,
    ProgressReport,
    ScoreTrend,
)
from novatrade.autonomy.task_generator import TaskSpec, TaskSpecGenerator

# =====================================================================
# Helpers
# =====================================================================


def _make_subgoal(
    sub_goal_id: str = "sg_test",
    dimension: str = "system_health",
    status: SubGoalStatus = SubGoalStatus.NOT_STARTED,
    progress: float = 0.0,
    priority: int = 5,
    dependencies: list[str] | None = None,
) -> SubGoal:
    return SubGoal(
        sub_goal_id=sub_goal_id,
        parent_goal_id="novatrade_100pct",
        dimension=dimension,
        title=f"Test: {sub_goal_id}",
        status=status,
        progress=progress,
        priority=priority,
        dependencies=dependencies or [],
    )


def _make_decomposition(sub_goals: list[SubGoal] | None = None) -> GoalDecomposition:
    sgs = sub_goals or [
        _make_subgoal("sg_services_running", "system_health", priority=1),
        _make_subgoal("sg_no_orphan_tasks", "system_health", priority=3),
        _make_subgoal("sg_webhook_live", "execution_pipeline", priority=2),
        _make_subgoal("sg_signals_generating", "strategy_validity", priority=1),
    ]
    return GoalDecomposition(
        goal_id="novatrade_100pct_operational",
        goal_description="Get NovaTrade 100% operational",
        sub_goals=sgs,
    )


def _make_report(
    dim_scores: dict[str, float] | None = None,
) -> ProgressReport:
    scores = dim_scores or {
        "system_health": 80.0,
        "execution_pipeline": 60.0,
        "strategy_validity": 45.0,
        "risk_engine": 70.0,
        "performance_stability": 55.0,
    }
    dims = {}
    for name, score in scores.items():
        if score >= 70:
            alert = AlertLevel.GREEN
        elif score >= 40:
            alert = AlertLevel.YELLOW
        else:
            alert = AlertLevel.RED
        dims[name] = DimensionScore(
            name=name,
            score=score,
            alert_level=alert,
            sub_metrics=[],
            warnings=[],
        )
    trends = {name: ScoreTrend(current=score, direction="stable", avg_6h=score) for name, score in scores.items()}
    return ProgressReport(
        overall_score=sum(scores.values()) / len(scores),
        overall_alert=AlertLevel.YELLOW,
        dimensions=dims,
        trends=trends,
    )


# =====================================================================
# Per-SubGoal Evaluator Tests
# =====================================================================


class TestEvalStateFile:
    """Tests for the generic _eval_state_file evaluator."""

    def test_missing_file(self, tmp_path: Path):
        progress, evidence, failure = _eval_state_file(tmp_path, "nonexistent.json")
        assert progress == 0.0
        assert failure is not None
        assert "missing" in failure

    def test_fresh_file(self, tmp_path: Path):
        (tmp_path / "test.json").write_text("{}")
        progress, evidence, failure = _eval_state_file(tmp_path, "test.json")
        assert progress == 1.0
        assert failure is None

    def test_aging_file(self, tmp_path: Path):
        p = tmp_path / "test.json"
        p.write_text("{}")
        # Set mtime to 10 minutes ago (between max_fresh_age and max_aging_age)
        import os

        old_time = time.time() - 600
        os.utime(p, (old_time, old_time))
        progress, evidence, failure = _eval_state_file(tmp_path, "test.json")
        assert progress == 0.7
        assert failure is None

    def test_stale_file(self, tmp_path: Path):
        p = tmp_path / "test.json"
        p.write_text("{}")
        import os

        old_time = time.time() - 7200  # 2 hours old
        os.utime(p, (old_time, old_time))
        progress, evidence, failure = _eval_state_file(tmp_path, "test.json")
        assert progress == 0.3
        assert failure is not None


class TestEvalNoOrphanTasks:
    """Tests for the orphan task evaluator."""

    def test_no_tasks_dir(self, tmp_path: Path):
        progress, evidence, failure = _eval_no_orphan_tasks(tmp_path)
        assert progress == 1.0
        assert failure is None

    def test_no_stale_tasks(self, tmp_path: Path):
        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        (tasks / "0001_test.md").write_text("task")
        progress, evidence, failure = _eval_no_orphan_tasks(tmp_path)
        assert progress == 1.0

    def test_stale_inprogress_tasks(self, tmp_path: Path):
        import os

        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        p = tasks / "0001_test.inprogress"
        p.write_text("stale")
        old_time = time.time() - 5 * 3600  # 5 hours old
        os.utime(p, (old_time, old_time))
        progress, evidence, failure = _eval_no_orphan_tasks(tmp_path)
        assert progress < 1.0
        assert failure is not None
        assert "stale" in failure.lower()


class TestEvalRiskNotHalted:
    """Tests for the risk halt evaluator."""

    def test_no_halt_file(self, tmp_path: Path):
        progress, evidence, failure = _eval_risk_not_halted(tmp_path)
        assert progress == 0.5
        assert failure == "Halt state unknown"

    def test_halted(self, tmp_path: Path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)
        (state_dir / "novatrade_risk_state.json").write_text(
            json.dumps({"halted": True, "halt_reason": "daily loss limit"})
        )
        progress, evidence, failure = _eval_risk_not_halted(tmp_path)
        assert progress == 0.0
        assert failure and "halted" in failure.lower()

    def test_not_halted(self, tmp_path: Path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)
        (state_dir / "novatrade_risk_state.json").write_text(json.dumps({"halted": False}))
        progress, evidence, failure = _eval_risk_not_halted(tmp_path)
        assert progress == 1.0


class TestEvalSignalsGenerating:
    """Tests for the signal generation evaluator."""

    def test_no_signal_log(self, tmp_path: Path):
        progress, evidence, failure = _eval_signals_generating(tmp_path)
        assert progress == 0.0
        assert failure is not None

    def test_empty_signal_log(self, tmp_path: Path):
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "signal_log.json").write_text("[]")
        progress, evidence, failure = _eval_signals_generating(tmp_path)
        assert progress == 0.0

    def test_fresh_signals(self, tmp_path: Path):
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "signal_log.json").write_text(json.dumps([{"signal": "buy", "ts": "2026-03-31T12:00:00Z"}]))
        progress, evidence, failure = _eval_signals_generating(tmp_path)
        assert progress == 1.0
        assert failure is None


class TestEvalDrawdownSafe:
    """Tests for the drawdown safety evaluator."""

    def test_no_tracker(self, tmp_path: Path):
        progress, evidence, failure = _eval_drawdown_safe(tmp_path)
        assert progress == 0.5

    def test_safe_drawdown(self, tmp_path: Path):
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "daily_loss_tracker.json").write_text(
            json.dumps({"current_drawdown_pct": 1.0, "daily_limit_pct": 5.0})
        )
        progress, evidence, failure = _eval_drawdown_safe(tmp_path)
        assert progress == 1.0
        assert failure is None

    def test_critical_drawdown(self, tmp_path: Path):
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        (state_dir / "daily_loss_tracker.json").write_text(
            json.dumps({"current_drawdown_pct": 4.5, "daily_limit_pct": 5.0})
        )
        progress, evidence, failure = _eval_drawdown_safe(tmp_path)
        assert progress == 0.1
        assert failure is not None


# =====================================================================
# GoalDecomposer Tests
# =====================================================================


class TestGoalDecomposerUpdateProgress:
    """Tests for GoalDecomposer.update_progress with evaluators."""

    def test_evaluator_used_when_available(self, tmp_path: Path):
        """Evaluator should be called for sub-goals that have one."""

        def mock_eval(base_path: Path):
            return 0.8, ["mock evidence"], None

        decomp = _make_decomposition(
            [
                _make_subgoal("sg_test_eval", "system_health"),
            ]
        )
        report = _make_report()
        decomposer = GoalDecomposer(
            base_path=str(tmp_path),
            evaluators={"sg_test_eval": mock_eval},
        )
        result = decomposer.update_progress(decomp, report)
        sg = result.sub_goals[0]
        assert sg.progress == 0.8
        assert sg.evidence == ["mock evidence"]
        assert sg.failure_reason is None
        assert sg.last_checked is not None

    def test_fallback_to_dimension_scoring(self, tmp_path: Path):
        """Sub-goals without evaluators fall back to dimension score."""
        decomp = _make_decomposition(
            [
                _make_subgoal("sg_no_evaluator", "strategy_validity"),
            ]
        )
        report = _make_report({"strategy_validity": 55.0})
        decomposer = GoalDecomposer(base_path=str(tmp_path), evaluators={})
        result = decomposer.update_progress(decomp, report)
        sg = result.sub_goals[0]
        # score 55 → (55 - 40) / 30 = 0.5
        assert sg.progress == 0.5

    def test_dimension_green_completes_subgoal(self, tmp_path: Path):
        """Score >= 70 should complete a fallback sub-goal."""
        decomp = _make_decomposition(
            [
                _make_subgoal("sg_fallback", "system_health"),
            ]
        )
        report = _make_report({"system_health": 85.0})
        decomposer = GoalDecomposer(base_path=str(tmp_path), evaluators={})
        result = decomposer.update_progress(decomp, report)
        sg = result.sub_goals[0]
        assert sg.progress == 1.0
        assert sg.status == SubGoalStatus.COMPLETED

    def test_dimension_red_zeros_subgoal(self, tmp_path: Path):
        """Score < 40 should zero out a fallback sub-goal."""
        decomp = _make_decomposition(
            [
                _make_subgoal("sg_fallback", "strategy_validity"),
            ]
        )
        report = _make_report({"strategy_validity": 30.0})
        decomposer = GoalDecomposer(base_path=str(tmp_path), evaluators={})
        result = decomposer.update_progress(decomp, report)
        assert result.sub_goals[0].progress == 0.0

    def test_evaluator_failure_falls_back(self, tmp_path: Path):
        """If evaluator raises, fall back to dimension scoring."""

        def broken_eval(base_path: Path):
            raise RuntimeError("evaluator exploded")

        decomp = _make_decomposition(
            [
                _make_subgoal("sg_broken", "system_health"),
            ]
        )
        report = _make_report({"system_health": 55.0})
        decomposer = GoalDecomposer(
            base_path=str(tmp_path),
            evaluators={"sg_broken": broken_eval},
        )
        result = decomposer.update_progress(decomp, report)
        # Should fall back to dimension score: (55-40)/30 = 0.5
        assert result.sub_goals[0].progress == 0.5

    def test_completed_subgoals_skipped(self, tmp_path: Path):
        """Completed sub-goals should not be re-evaluated."""

        def should_not_be_called(base_path: Path):
            raise AssertionError("Should not be called for completed sub-goals")

        sg = _make_subgoal("sg_done", "system_health", SubGoalStatus.COMPLETED, 1.0)
        decomp = _make_decomposition([sg])
        report = _make_report()
        decomposer = GoalDecomposer(
            base_path=str(tmp_path),
            evaluators={"sg_done": should_not_be_called},
        )
        result = decomposer.update_progress(decomp, report)
        assert result.sub_goals[0].status == SubGoalStatus.COMPLETED

    def test_blocked_detection(self, tmp_path: Path):
        """Sub-goal with unmet deps and zero progress should be BLOCKED."""
        sgs = [
            _make_subgoal("sg_prereq", "system_health"),
            _make_subgoal("sg_dependent", "system_health", dependencies=["sg_prereq"]),
        ]
        decomp = _make_decomposition(sgs)
        report = _make_report({"system_health": 30.0})
        decomposer = GoalDecomposer(base_path=str(tmp_path), evaluators={})
        result = decomposer.update_progress(decomp, report)
        assert result.sub_goals[1].status == SubGoalStatus.BLOCKED

    def test_evidence_tracking(self, tmp_path: Path):
        """Evaluator evidence should be stored on the sub-goal."""

        def detailed_eval(base_path: Path):
            return 0.6, ["checked service A", "checked service B", "service C failed"], "service C down"

        decomp = _make_decomposition(
            [
                _make_subgoal("sg_evidence", "system_health"),
            ]
        )
        report = _make_report()
        decomposer = GoalDecomposer(
            base_path=str(tmp_path),
            evaluators={"sg_evidence": detailed_eval},
        )
        result = decomposer.update_progress(decomp, report)
        sg = result.sub_goals[0]
        assert len(sg.evidence) == 3
        assert sg.failure_reason == "service C down"


class TestGoalDecomposerActionable:
    """Tests for get_actionable_subgoals."""

    def test_all_actionable_when_no_deps(self, tmp_path: Path):
        sgs = [
            _make_subgoal("sg_a", priority=3),
            _make_subgoal("sg_b", priority=1),
        ]
        decomp = _make_decomposition(sgs)
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        actionable = decomposer.get_actionable_subgoals(decomp)
        assert len(actionable) == 2
        assert actionable[0].sub_goal_id == "sg_b"  # priority 1 first

    def test_blocked_not_actionable(self, tmp_path: Path):
        sgs = [
            _make_subgoal("sg_prereq", priority=2),
            _make_subgoal("sg_dep", priority=1, dependencies=["sg_prereq"]),
        ]
        decomp = _make_decomposition(sgs)
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        actionable = decomposer.get_actionable_subgoals(decomp)
        assert len(actionable) == 1
        assert actionable[0].sub_goal_id == "sg_prereq"

    def test_completed_not_actionable(self, tmp_path: Path):
        sgs = [
            _make_subgoal("sg_done", status=SubGoalStatus.COMPLETED, progress=1.0),
            _make_subgoal("sg_todo", priority=1),
        ]
        decomp = _make_decomposition(sgs)
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        actionable = decomposer.get_actionable_subgoals(decomp)
        assert len(actionable) == 1
        assert actionable[0].sub_goal_id == "sg_todo"


class TestGoalDecomposerPersistence:
    """Tests for persist/load round-trip."""

    def test_persist_and_load(self, tmp_path: Path):
        decomp = _make_decomposition()
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        decomposer.persist(decomp)
        loaded = decomposer.load("novatrade_100pct_operational")
        assert loaded is not None
        assert len(loaded.sub_goals) == len(decomp.sub_goals)

    def test_load_nonexistent(self, tmp_path: Path):
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        assert decomposer.load("nonexistent") is None


# =====================================================================
# Goal-Driven Decision Selection (Step 6.3)
# =====================================================================


class TestGoalDrivenDecisionSelection:
    """Tests for _find_top_actionable_subgoal in DecisionEngine."""

    def test_returns_first_actionable(self):
        engine = DecisionEngine()
        context = DecisionContext(
            progress_report=_make_report(),
            actionable_subgoals=["Fix webhook", "Deploy strategy"],
        )
        result = engine._find_top_actionable_subgoal("execution_pipeline", context)
        assert result == "Fix webhook"

    def test_returns_none_when_no_dimension(self):
        engine = DecisionEngine()
        context = DecisionContext(
            progress_report=_make_report(),
            actionable_subgoals=["Fix webhook"],
        )
        result = engine._find_top_actionable_subgoal(None, context)
        assert result is None

    def test_returns_none_when_no_subgoals(self):
        engine = DecisionEngine()
        context = DecisionContext(
            progress_report=_make_report(),
            actionable_subgoals=[],
        )
        result = engine._find_top_actionable_subgoal("system_health", context)
        assert result is None

    @pytest.mark.asyncio
    async def test_subgoal_appended_to_decision_reason(self, tmp_path: Path):
        """Decision reason should include the top actionable sub-goal."""
        engine = DecisionEngine(base_path=str(tmp_path))
        report = _make_report(
            {
                "strategy_validity": 35.0,
                "system_health": 80.0,
                "execution_pipeline": 80.0,
                "risk_engine": 80.0,
                "performance_stability": 80.0,
            }
        )
        context = DecisionContext(
            progress_report=report,
            actionable_subgoals=["Generate trading signals"],
        )
        decision = await engine.decide(context)
        assert "Generate trading signals" in decision.reason


# =====================================================================
# Task-to-SubGoal Linking (Step 6.4)
# =====================================================================


class TestTaskSubGoalLinking:
    """Tests for TaskSpec fields and frontmatter generation."""

    def test_taskspec_has_linking_fields(self):
        spec = TaskSpec(
            title="Test task",
            body="test",
            decision_id="repair_sv_20260331",
            sub_goal_id="sg_signals_generating",
            success_metric="strategy_validity >= 60",
            expiry_window_minutes=90,
        )
        assert spec.decision_id == "repair_sv_20260331"
        assert spec.sub_goal_id == "sg_signals_generating"
        assert spec.success_metric == "strategy_validity >= 60"
        assert spec.expiry_window_minutes == 90

    def test_frontmatter_includes_linking_fields(self, tmp_path: Path):
        gen = TaskSpecGenerator(base_path=str(tmp_path))
        spec = TaskSpec(
            title="Repair strategy validity",
            body="Fix it",
            category="repair",
            target_dimension="strategy_validity",
            goal_justification="Score is low",
            decision_id="repair_sv_20260331",
            sub_goal_id="sg_signals_generating",
            success_metric="strategy_validity >= 60",
            expiry_window_minutes=90,
        )
        path = gen.write_task_file(spec, skip_dedup=True)
        assert path is not None
        content = path.read_text()
        assert "decision_id: repair_sv_20260331" in content
        assert "sub_goal_id: sg_signals_generating" in content
        assert 'success_metric: "strategy_validity >= 60"' in content
        assert "expiry_minutes: 90" in content

    def test_frontmatter_omits_empty_linking_fields(self, tmp_path: Path):
        gen = TaskSpecGenerator(base_path=str(tmp_path))
        spec = TaskSpec(
            title="Monitor health",
            body="All good",
            category="monitor",
            goal_justification="Nominal",
        )
        path = gen.write_task_file(spec, skip_dedup=True)
        assert path is not None
        content = path.read_text()
        # Empty decision_id and None sub_goal_id should not appear
        assert "decision_id:" not in content
        assert "sub_goal_id:" not in content

    def test_default_expiry_window(self):
        spec = TaskSpec(title="test", body="test")
        assert spec.expiry_window_minutes == 120


# =====================================================================
# Evaluator Registry
# =====================================================================


class TestEvaluatorRegistry:
    """Tests for the SUBGOAL_EVALUATORS registry."""

    def test_registry_has_expected_evaluators(self):
        expected = {
            "sg_services_running",
            "sg_no_orphan_tasks",
            "sg_webhook_live",
            "sg_adapter_connected",
            "sg_feed_healthy",
            "sg_strategy_loaded",
            "sg_signals_generating",
            "sg_risk_not_halted",
            "sg_drawdown_safe",
        }
        assert set(SUBGOAL_EVALUATORS.keys()) == expected

    def test_all_evaluators_callable(self):
        for name, func in SUBGOAL_EVALUATORS.items():
            assert callable(func), f"{name} is not callable"

    def test_evaluators_return_correct_shape(self, tmp_path: Path):
        """All evaluators should return (float, list[str], str|None)."""
        for name, func in SUBGOAL_EVALUATORS.items():
            # Skip service-dependent evaluators that need systemctl
            if name in ("sg_services_running", "sg_webhook_live"):
                continue
            result = func(tmp_path)
            assert isinstance(result, tuple) and len(result) == 3, f"{name} returned wrong shape"
            progress, evidence, failure = result
            assert isinstance(progress, float), f"{name} progress not float"
            assert isinstance(evidence, list), f"{name} evidence not list"
            assert failure is None or isinstance(failure, str), f"{name} failure not str|None"
