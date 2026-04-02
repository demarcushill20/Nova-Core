"""Integration tests for Phase 2.4 + 3.4: Autonomy → Heartbeat wiring.

Tests the full pipeline: ProgressScorer → DecisionEngine → GoalDecomposer →
TaskSpecGenerator → write_heartbeat() → _gather_extended_state().
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from novatrade.autonomy import (
    ActionMode,
    ContextAssembler,
    DecisionEngine,
    GoalDecomposer,
    ProgressScorer,
    TaskSpecGenerator,
    build_novatrade_tree,
)
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    SubMetric,
)

# =====================================================================
# Helpers
# =====================================================================


def make_dimension(name: str, score: float, warnings: list[str] | None = None) -> DimensionScore:
    return DimensionScore(
        name=name,
        score=score,
        sub_metrics=[SubMetric(name="m1", value=score)],
        warnings=warnings or [],
    )


def make_trend(current: float, direction: str = "stable") -> ScoreTrend:
    return ScoreTrend(current=current, avg_6h=current, direction=direction)


def make_report(
    dims: dict[str, float] | None = None,
) -> ProgressReport:
    dims = dims or {
        "system_health": 80.0,
        "execution_pipeline": 75.0,
        "strategy_validity": 70.0,
        "risk_engine": 85.0,
        "performance_stability": 60.0,
    }
    dimensions = {name: make_dimension(name, score) for name, score in dims.items()}
    trends = {name: make_trend(score) for name, score in dims.items()}
    overall = sum(dims.values()) / len(dims)
    return ProgressReport(
        overall_score=round(overall, 1),
        dimensions=dimensions,
        trends=trends,
    )


# =====================================================================
# Phase 2.4: write_heartbeat with autonomy snapshot
# =====================================================================


class TestWriteHeartbeatAutonomy:
    """Test that write_heartbeat() renders the autonomy progress section."""

    def test_write_heartbeat_without_snapshot(self, tmp_path):
        """write_heartbeat still works without autonomy data."""
        import heartbeat

        orig = heartbeat.HEARTBEAT_FILE
        heartbeat.HEARTBEAT_FILE = tmp_path / "HEARTBEAT.md"
        try:
            checks = [{"name": "test_check", "ok": True, "detail": "all good"}]
            heartbeat.write_heartbeat(checks)
            content = heartbeat.HEARTBEAT_FILE.read_text()
            assert "# NovaCore Heartbeat" in content
            assert "HEALTHY" in content
            assert "Progress Score" not in content
        finally:
            heartbeat.HEARTBEAT_FILE = orig

    def test_write_heartbeat_with_snapshot(self, tmp_path):
        """write_heartbeat renders the autonomy section when snapshot is provided."""
        import heartbeat

        orig = heartbeat.HEARTBEAT_FILE
        heartbeat.HEARTBEAT_FILE = tmp_path / "HEARTBEAT.md"
        try:
            checks = [{"name": "test_check", "ok": True, "detail": "ok"}]
            snapshot = {
                "overall_score": 74.0,
                "alert_level": "green",
                "dimensions": {
                    "system_health": {"score": 80, "trend": "stable"},
                    "risk_engine": {"score": 85, "trend": "improving"},
                },
                "decision": {"mode": "monitor", "reason": "All systems nominal."},
                "goal_tree": {"completed": 5, "total": 17},
            }
            heartbeat.write_heartbeat(checks, autonomy_snapshot=snapshot)
            content = heartbeat.HEARTBEAT_FILE.read_text()
            assert "Progress Score: 74.0/100 (GREEN)" in content
            assert "system_health: 80" in content
            assert "risk_engine: 85" in content
            assert "Decision: **monitor**" in content
            assert "5/17 sub-goals complete" in content
        finally:
            heartbeat.HEARTBEAT_FILE = orig


# =====================================================================
# Phase 2.4c: _gather_extended_state reads autonomy snapshot
# =====================================================================


class TestGatherExtendedStateAutonomy:
    """Test that _gather_extended_state() includes autonomy data."""

    def test_extended_state_includes_autonomy(self, tmp_path, monkeypatch):
        """The LLM prompt includes autonomy data when snapshot exists."""
        import heartbeat
        from prompts.heartbeat_prompts import _gather_extended_state

        # Point STATE_DIR to tmp
        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        monkeypatch.setattr(heartbeat, "OUTPUT_DIR", tmp_path / "OUTPUT")
        monkeypatch.setattr(heartbeat, "HEARTBEAT_AGENT_LOG", tmp_path / "agent.log")
        (tmp_path / "STATE").mkdir(parents=True)
        (tmp_path / "TASKS").mkdir(parents=True)
        (tmp_path / "OUTPUT").mkdir(parents=True)

        snap = {
            "overall_score": 56.0,
            "alert_level": "yellow",
            "dimensions": {
                "system_health": {"score": 47, "trend": "stable"},
                "risk_engine": {"score": 87, "trend": "improving"},
            },
            "decision": {"mode": "plan", "reason": "system_health score low"},
            "goal_tree": {
                "completed": 3,
                "total": 17,
                "actionable": ["All core services active", "Memory systems reachable"],
            },
        }
        (tmp_path / "STATE" / "autonomy_snapshot.json").write_text(json.dumps(snap))

        checks = [{"name": "test", "ok": True, "detail": "ok"}]
        state = _gather_extended_state(checks)

        assert "Autonomy Progress: 56.0/100 (YELLOW)" in state
        assert "system_health: 47" in state
        assert "risk_engine: 87" in state
        assert "Last decision: plan" in state
        assert "3/17 sub-goals complete" in state
        assert "All core services active" in state

    def test_extended_state_without_autonomy(self, tmp_path, monkeypatch):
        """No crash when autonomy snapshot doesn't exist."""
        import heartbeat
        from prompts.heartbeat_prompts import _gather_extended_state

        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        monkeypatch.setattr(heartbeat, "OUTPUT_DIR", tmp_path / "OUTPUT")
        monkeypatch.setattr(heartbeat, "HEARTBEAT_AGENT_LOG", tmp_path / "agent.log")
        (tmp_path / "STATE").mkdir(parents=True)
        (tmp_path / "TASKS").mkdir(parents=True)
        (tmp_path / "OUTPUT").mkdir(parents=True)

        checks = [{"name": "test", "ok": True, "detail": "ok"}]
        state = _gather_extended_state(checks)

        assert "Autonomy Progress" not in state


# =====================================================================
# Phase 3.4: Full autonomy pipeline integration
# =====================================================================


class TestAutonomyPipeline:
    """End-to-end tests for the full autonomy wiring."""

    @pytest.mark.asyncio
    async def test_scorer_to_decision_pipeline(self, tmp_path):
        """ProgressReport → ContextAssembler → DecisionEngine produces a valid decision."""
        report = make_report()
        assembler = ContextAssembler(base_path=str(tmp_path))
        context = await assembler.assemble(report)

        engine = DecisionEngine(base_path=str(tmp_path))
        decision = await engine.decide(context)

        assert decision.mode in ActionMode
        assert decision.reason
        assert decision.confidence in ("high", "medium", "low")

    @pytest.mark.asyncio
    async def test_decision_to_task_generation(self, tmp_path):
        """Non-MONITOR decisions produce task files in TASKS/."""
        # Low scores → should trigger PLAN or RESEARCH
        report = make_report(
            dims={
                "system_health": 30.0,
                "execution_pipeline": 25.0,
                "strategy_validity": 20.0,
                "risk_engine": 85.0,
                "performance_stability": 40.0,
            }
        )
        assembler = ContextAssembler(base_path=str(tmp_path))
        context = await assembler.assemble(report)

        engine = DecisionEngine(base_path=str(tmp_path))
        decision = await engine.decide(context)

        assert decision.mode != ActionMode.MONITOR

        gen = TaskSpecGenerator(base_path=str(tmp_path))
        spec = gen.from_decision(decision)
        task_path = gen.write_task_file(spec)

        assert task_path.exists()
        content = task_path.read_text()
        assert "---" in content
        assert "source: autonomy-decision-engine" in content

    @pytest.mark.asyncio
    async def test_monitor_skips_task_generation(self, tmp_path):
        """MONITOR decisions should not generate meaningful tasks."""
        # High scores → MONITOR
        report = make_report(
            dims={
                "system_health": 90.0,
                "execution_pipeline": 85.0,
                "strategy_validity": 80.0,
                "risk_engine": 95.0,
                "performance_stability": 75.0,
            }
        )
        assembler = ContextAssembler(base_path=str(tmp_path))
        context = await assembler.assemble(report)

        engine = DecisionEngine(base_path=str(tmp_path))
        decision = await engine.decide(context)

        assert decision.mode == ActionMode.MONITOR

    def test_goal_tree_bootstrap_and_update(self, tmp_path):
        """GoalDecomposer bootstraps tree, updates progress, persists."""
        decomposer = GoalDecomposer(base_path=str(tmp_path))

        # First run — no tree on disk
        tree = decomposer.load("novatrade_100pct_operational")
        assert tree is None

        # Bootstrap
        tree = build_novatrade_tree()
        assert len(tree.sub_goals) == 17

        # Update with a report
        report = make_report(
            dims={
                "system_health": 80.0,
                "execution_pipeline": 50.0,
                "strategy_validity": 70.0,
                "risk_engine": 90.0,
                "performance_stability": 30.0,
            }
        )
        tree = decomposer.update_progress(tree, report)

        # system_health >= 70 → completed sub-goals
        sh_goals = [sg for sg in tree.sub_goals if sg.dimension == "system_health"]
        completed_sh = [sg for sg in sh_goals if sg.status.value == "completed"]
        assert len(completed_sh) > 0

        # Persist
        decomposer.persist(tree)
        reloaded = decomposer.load("novatrade_100pct_operational")
        assert reloaded is not None
        assert len(reloaded.sub_goals) == 17

    def test_goal_tree_actionable_subgoals(self, tmp_path):
        """After progress update, get_actionable_subgoals returns only feasible items."""
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        tree = build_novatrade_tree()

        # All low → most are actionable (no deps blocking)
        report = make_report(
            dims={
                "system_health": 20.0,
                "execution_pipeline": 20.0,
                "strategy_validity": 20.0,
                "risk_engine": 20.0,
                "performance_stability": 20.0,
            }
        )
        tree = decomposer.update_progress(tree, report)
        actionable = decomposer.get_actionable_subgoals(tree)
        # Root-level goals (no deps) should be actionable
        assert len(actionable) > 0


# =====================================================================
# Phase 3.4: _run_autonomy_cycle integration
# =====================================================================


class TestRunAutonomyCycle:
    """Test _run_autonomy_cycle from heartbeat.py."""

    def test_returns_none_when_autonomy_unavailable(self):
        """Returns None when ProgressScorer is not importable."""
        import heartbeat

        orig = heartbeat._ProgressScorer
        heartbeat._ProgressScorer = None
        try:
            result = heartbeat._run_autonomy_cycle()
            assert result is None
        finally:
            heartbeat._ProgressScorer = orig

    def _run_cycle_with_mocked_decision(self, tmp_path, monkeypatch, mode, report=None):
        """Helper: run _run_autonomy_cycle with a mocked decision mode."""
        import heartbeat

        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        (tmp_path / "TASKS").mkdir(parents=True, exist_ok=True)

        # Disable optional pipeline components that interact with real filesystem
        # state and can non-deterministically override the mocked decision (e.g.,
        # ReasoningEngine upgrades MONITOR → PLAN based on heuristic analysis).
        monkeypatch.setattr(heartbeat, "_ReasoningEngine", None)
        monkeypatch.setattr(heartbeat, "_OutcomeAnalyzer", None)
        monkeypatch.setattr(heartbeat, "_TaskReconciler", None)
        monkeypatch.setattr(heartbeat, "_CausalModel", None)
        monkeypatch.setattr(heartbeat, "_DirectActionExecutor", None)

        report = report or make_report()
        base = str(tmp_path)

        async def mock_score(self):
            return report

        async def mock_assemble(self, r):
            from novatrade.autonomy.decision_context import DecisionContext

            return DecisionContext(
                progress_report=r,
                pending_tasks=[],
                active_goals=[],
                recent_decisions=[],
                time_of_day="morning",
            )

        from novatrade.autonomy.decision_engine import Decision

        async def mock_decide(self, ctx):
            return Decision(
                mode=mode,
                reason=f"Test decision: {mode.value}",
                target_dimension="system_health" if mode != ActionMode.MONITOR else None,
                suggested_actions=["Test action"],
                confidence="high",
            )

        # Patch constructors to use tmp_path for all file I/O
        orig_scorer_init = ProgressScorer.__init__
        orig_assembler_init = ContextAssembler.__init__
        orig_engine_init = DecisionEngine.__init__
        orig_decomposer_init = GoalDecomposer.__init__
        orig_gen_init = TaskSpecGenerator.__init__

        def patched_scorer_init(self, config=None, base_path=base):
            orig_scorer_init(self, config=config, base_path=base)

        def patched_assembler_init(self, base_path=base):
            orig_assembler_init(self, base_path=base)

        def patched_engine_init(self, config=None, base_path=base):
            orig_engine_init(self, config=config, base_path=base)

        def patched_decomposer_init(self, base_path=base):
            orig_decomposer_init(self, base_path=base)

        def patched_gen_init(self, base_path=base):
            orig_gen_init(self, base_path=base)

        with (
            patch.object(ProgressScorer, "score", mock_score),
            patch.object(ContextAssembler, "assemble", mock_assemble),
            patch.object(DecisionEngine, "decide", mock_decide),
            patch.object(ProgressScorer, "__init__", patched_scorer_init),
            patch.object(ContextAssembler, "__init__", patched_assembler_init),
            patch.object(DecisionEngine, "__init__", patched_engine_init),
            patch.object(GoalDecomposer, "__init__", patched_decomposer_init),
            patch.object(TaskSpecGenerator, "__init__", patched_gen_init),
        ):
            return heartbeat._run_autonomy_cycle()

    def test_full_cycle_monitor_produces_snapshot(self, tmp_path, monkeypatch):
        """MONITOR cycle writes STATE/autonomy_snapshot.json without generating tasks."""
        result = self._run_cycle_with_mocked_decision(tmp_path, monkeypatch, ActionMode.MONITOR)

        assert result is not None
        assert result["decision"]["mode"] == "monitor"

        # Snapshot file should exist
        snap_path = tmp_path / "STATE" / "autonomy_snapshot.json"
        assert snap_path.exists()
        snap_data = json.loads(snap_path.read_text())
        assert snap_data["overall_score"] == result["overall_score"]

        # No task files generated
        task_files = list((tmp_path / "TASKS").glob("*.md"))
        assert len(task_files) == 0

    def test_full_cycle_research_generates_task(self, tmp_path, monkeypatch):
        """RESEARCH cycle generates a task file in TASKS/."""
        result = self._run_cycle_with_mocked_decision(tmp_path, monkeypatch, ActionMode.RESEARCH)

        assert result is not None
        assert result["decision"]["mode"] == "research"

        # Task file should be generated
        task_files = list((tmp_path / "TASKS").glob("*.md"))
        assert len(task_files) == 1
        content = task_files[0].read_text()
        assert "source: autonomy-decision-engine" in content

    def test_full_cycle_skips_duplicate_task(self, tmp_path, monkeypatch):
        """Second RESEARCH cycle for same dimension skips task generation (dedup)."""
        # First cycle — generates task
        self._run_cycle_with_mocked_decision(tmp_path, monkeypatch, ActionMode.RESEARCH)
        task_files_1 = list((tmp_path / "TASKS").glob("*.md"))
        assert len(task_files_1) == 1

        # Second cycle — should skip due to existing pending task
        self._run_cycle_with_mocked_decision(tmp_path, monkeypatch, ActionMode.RESEARCH)
        task_files_2 = list((tmp_path / "TASKS").glob("*.md"))
        assert len(task_files_2) == 1  # Still just 1


# =====================================================================
# Edge cases
# =====================================================================


class TestAutonomyEdgeCases:
    """Edge case coverage for the wiring."""

    @pytest.mark.asyncio
    async def test_decision_cooldown_prevents_same_mode_spam(self, tmp_path):
        """Running decide() twice rapidly for the same mode triggers cooldown → MONITOR."""

        # Use very low thresholds so both calls classify to RESEARCH
        report = make_report(
            dims={
                "system_health": 30.0,
                "execution_pipeline": 25.0,
                "strategy_validity": 20.0,
                "risk_engine": 85.0,
                "performance_stability": 40.0,
            }
        )
        # Add knowledge gaps by giving many warnings
        for dim in report.dimensions.values():
            dim.warnings = ["warn1", "warn2"]
            for sm in dim.sub_metrics:
                sm.value = 0.0

        assembler = ContextAssembler(base_path=str(tmp_path))
        engine = DecisionEngine(base_path=str(tmp_path))

        # First decision — should produce RESEARCH (knowledge gaps + low score)
        ctx1 = await assembler.assemble(report)
        d1 = await engine.decide(ctx1)
        assert d1.mode == ActionMode.RESEARCH

        # Second decision immediately — same mode in cooldown
        ctx2 = await assembler.assemble(report)
        d2 = await engine.decide(ctx2)
        assert d2.mode == ActionMode.MONITOR
        assert "Rate-limited" in d2.reason

    def test_corrupt_autonomy_snapshot_handled(self, tmp_path, monkeypatch):
        """Corrupt JSON in autonomy_snapshot.json doesn't crash extended state."""
        import heartbeat
        from prompts.heartbeat_prompts import _gather_extended_state

        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        monkeypatch.setattr(heartbeat, "OUTPUT_DIR", tmp_path / "OUTPUT")
        monkeypatch.setattr(heartbeat, "HEARTBEAT_AGENT_LOG", tmp_path / "agent.log")
        (tmp_path / "STATE").mkdir(parents=True)
        (tmp_path / "TASKS").mkdir(parents=True)
        (tmp_path / "OUTPUT").mkdir(parents=True)

        (tmp_path / "STATE" / "autonomy_snapshot.json").write_text("{corrupt json!!!")

        checks = [{"name": "test", "ok": True, "detail": "ok"}]
        # Should not raise
        state = _gather_extended_state(checks)
        assert "Autonomy Progress" not in state

    @pytest.mark.asyncio
    async def test_task_generator_sequence_numbers(self, tmp_path):
        """Multiple task generations produce incrementing sequence numbers."""
        gen = TaskSpecGenerator(base_path=str(tmp_path))

        from novatrade.autonomy.decision_engine import Decision

        for i in range(3):
            decision = Decision(
                mode=ActionMode.RESEARCH,
                reason=f"Test {i}",
                target_dimension="system_health",
                suggested_actions=[f"Action {i}"],
            )
            spec = gen.from_decision(decision)
            path = gen.write_task_file(spec, skip_dedup=True)
            assert f"{i + 1:04d}_" in path.name

    def test_goal_tree_persistence_roundtrip(self, tmp_path):
        """Goal tree survives persist → load roundtrip."""
        decomposer = GoalDecomposer(base_path=str(tmp_path))
        tree = build_novatrade_tree()

        report = make_report(
            dims={
                "system_health": 75.0,
                "execution_pipeline": 45.0,
                "strategy_validity": 70.0,
                "risk_engine": 90.0,
                "performance_stability": 50.0,
            }
        )
        tree = decomposer.update_progress(tree, report)
        decomposer.persist(tree)

        loaded = decomposer.load("novatrade_100pct_operational")
        assert loaded is not None
        assert loaded.goal_id == tree.goal_id
        assert len(loaded.sub_goals) == len(tree.sub_goals)

        # Verify progress was preserved
        for orig, reloaded in zip(tree.sub_goals, loaded.sub_goals, strict=True):
            assert orig.progress == reloaded.progress
            assert orig.status == reloaded.status
