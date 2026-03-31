"""Phase 2: Close the Feedback Loop — tests for OutcomeAnalyzer wiring,
DecisionContext outcome fields, and outcome-informed decision engine.

Tests cover:
- Step 2.1: OutcomeAnalyzer integration in heartbeat pipeline
- Step 2.2: DecisionContext.recent_outcomes and effectiveness_summary
- Step 2.3: Outcome-informed _detect_repair_futility()
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.autonomy.decision_context import (
    ContextAssembler,
    DecisionContext,
)
from novatrade.autonomy.decision_engine import (
    ActionMode,
    Decision,
    DecisionEngine,
)
from novatrade.autonomy.outcome_analyzer import (
    DecisionSnapshot,
    OutcomeAnalyzer,
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


def _dim(name: str, score: float, sub_metrics: list[SubMetric] | None = None) -> DimensionScore:
    sms = sub_metrics or [SubMetric(name="default", value=score)]
    return DimensionScore(name=name, score=score, sub_metrics=sms, warnings=[])


def _trend(current: float, avg_6h: float | None = None, direction: str = "stable") -> ScoreTrend:
    return ScoreTrend(current=current, avg_6h=avg_6h, direction=direction)


def _report(
    dims: dict[str, DimensionScore] | None = None,
    trends: dict[str, ScoreTrend] | None = None,
    overall: float | None = None,
) -> ProgressReport:
    if dims is None:
        dims = {
            "system_health": _dim("system_health", 80.0),
            "strategy_validity": _dim("strategy_validity", 70.0),
        }
    if overall is None:
        overall = sum(d.score for d in dims.values()) / len(dims) if dims else 0.0
    return ProgressReport(
        overall_score=overall,
        dimensions=dims,
        trends=trends or {n: _trend(d.score) for n, d in dims.items()},
    )


def _ctx(
    report: ProgressReport | None = None,
    recent_decisions: list[dict] | None = None,
    recent_outcomes: list[dict] | None = None,
    effectiveness_summary: dict[str, float] | None = None,
) -> DecisionContext:
    return DecisionContext(
        progress_report=report or _report(),
        recent_decisions=recent_decisions or [],
        recent_outcomes=recent_outcomes or [],
        effectiveness_summary=effectiveness_summary or {},
    )


def _make_resolved_outcome(
    mode: str = "repair",
    target_dimension: str = "strategy_validity",
    effectiveness: str = "neutral",
    delta: float = 0.0,
    decided_at: str | None = None,
) -> dict:
    """Build a resolved outcome dict matching DecisionSnapshot serialization."""
    ts = decided_at or datetime.now(timezone.utc).isoformat()
    return {
        "decision_id": f"{mode}_{target_dimension}_{ts[:19].replace(':', '')}",
        "mode": mode,
        "target_dimension": target_dimension,
        "reason": f"Test {mode} decision",
        "decided_at": ts,
        "scores_before": {target_dimension: 50.0},
        "overall_before": 60.0,
        "scores_after": {target_dimension: 50.0 + delta},
        "overall_after": 60.0 + delta,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "effectiveness": effectiveness,
        "delta": delta,
    }


# =====================================================================
# Step 2.2: DecisionContext — new fields
# =====================================================================


class TestDecisionContextOutcomeFields:
    """Verify DecisionContext has recent_outcomes and effectiveness_summary."""

    def test_default_empty_outcomes(self):
        """Default DecisionContext has empty outcomes."""
        ctx = DecisionContext(progress_report=_report())
        assert ctx.recent_outcomes == []
        assert ctx.effectiveness_summary == {}

    def test_outcomes_populated(self):
        """DecisionContext accepts recent_outcomes and effectiveness_summary."""
        outcomes = [_make_resolved_outcome(effectiveness="effective", delta=5.0)]
        ctx = DecisionContext(
            progress_report=_report(),
            recent_outcomes=outcomes,
            effectiveness_summary={"repair": 5.0},
        )
        assert len(ctx.recent_outcomes) == 1
        assert ctx.effectiveness_summary["repair"] == 5.0


# =====================================================================
# Step 2.2: ContextAssembler — loads outcomes
# =====================================================================


class TestContextAssemblerOutcomes:
    """Verify ContextAssembler loads outcomes from STATE/decision_outcomes.json."""

    @pytest.mark.asyncio
    async def test_assembler_loads_outcomes_from_disk(self, tmp_path: Path):
        """Assembler reads resolved outcomes from decision_outcomes.json."""
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)

        outcomes = [
            {
                "decision_id": "repair_sv_1",
                "mode": "repair",
                "target_dimension": "strategy_validity",
                "reason": "fix",
                "decided_at": "2026-03-30T10:00:00+00:00",
                "scores_before": {"strategy_validity": 40.0},
                "overall_before": 55.0,
                "scores_after": {"strategy_validity": 60.0},
                "overall_after": 70.0,
                "resolved_at": "2026-03-30T10:30:00+00:00",
                "effectiveness": "effective",
                "delta": 15.0,
            },
            {
                "decision_id": "monitor_sys_2",
                "mode": "monitor",
                "target_dimension": None,
                "reason": "all ok",
                "decided_at": "2026-03-30T11:00:00+00:00",
                "scores_before": {},
                "overall_before": 80.0,
                "scores_after": None,
                "overall_after": None,
                "resolved_at": None,  # NOT resolved — should be filtered
                "effectiveness": None,
                "delta": None,
            },
        ]
        (state_dir / "decision_outcomes.json").write_text(json.dumps(outcomes))

        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(_report())

        # Only the resolved outcome should be present
        assert len(ctx.recent_outcomes) == 1
        assert ctx.recent_outcomes[0]["effectiveness"] == "effective"

    @pytest.mark.asyncio
    async def test_assembler_computes_effectiveness_summary(self, tmp_path: Path):
        """Assembler computes average delta per mode."""
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)

        outcomes = [
            _make_resolved_outcome(mode="repair", delta=10.0, effectiveness="effective"),
            _make_resolved_outcome(mode="repair", delta=-2.0, effectiveness="counterproductive"),
            _make_resolved_outcome(mode="research", delta=5.0, effectiveness="effective"),
        ]
        (state_dir / "decision_outcomes.json").write_text(json.dumps(outcomes))

        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(_report())

        assert "repair" in ctx.effectiveness_summary
        assert ctx.effectiveness_summary["repair"] == pytest.approx(4.0)  # (10 + -2) / 2
        assert ctx.effectiveness_summary["research"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_assembler_no_outcomes_file(self, tmp_path: Path):
        """Assembler works when decision_outcomes.json doesn't exist."""
        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(_report())
        assert ctx.recent_outcomes == []
        assert ctx.effectiveness_summary == {}

    @pytest.mark.asyncio
    async def test_assembler_corrupt_outcomes_file(self, tmp_path: Path):
        """Assembler handles corrupt JSON gracefully."""
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)
        (state_dir / "decision_outcomes.json").write_text("{bad json")

        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(_report())
        assert ctx.recent_outcomes == []
        assert ctx.effectiveness_summary == {}

    @pytest.mark.asyncio
    async def test_assembler_outcomes_limit(self, tmp_path: Path):
        """Assembler caps outcomes to limit (default 10)."""
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True)

        outcomes = [_make_resolved_outcome(delta=float(i), effectiveness="neutral") for i in range(20)]
        (state_dir / "decision_outcomes.json").write_text(json.dumps(outcomes))

        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(_report())
        assert len(ctx.recent_outcomes) == 10  # capped


class TestComputeEffectivenessSummary:
    """Unit tests for _compute_effectiveness_summary."""

    def test_empty_outcomes(self):
        result = ContextAssembler._compute_effectiveness_summary([])
        assert result == {}

    def test_single_mode(self):
        outcomes = [
            {"mode": "repair", "delta": 5.0},
            {"mode": "repair", "delta": 3.0},
        ]
        result = ContextAssembler._compute_effectiveness_summary(outcomes)
        assert result == {"repair": pytest.approx(4.0)}

    def test_multiple_modes(self):
        outcomes = [
            {"mode": "repair", "delta": 10.0},
            {"mode": "research", "delta": 2.0},
            {"mode": "repair", "delta": -4.0},
        ]
        result = ContextAssembler._compute_effectiveness_summary(outcomes)
        assert result["repair"] == pytest.approx(3.0)
        assert result["research"] == pytest.approx(2.0)

    def test_missing_delta_skipped(self):
        outcomes = [
            {"mode": "repair", "delta": None},
            {"mode": "repair", "delta": 5.0},
        ]
        result = ContextAssembler._compute_effectiveness_summary(outcomes)
        assert result["repair"] == pytest.approx(5.0)

    def test_missing_mode_skipped(self):
        outcomes = [
            {"mode": "", "delta": 5.0},
            {"mode": "repair", "delta": 3.0},
        ]
        result = ContextAssembler._compute_effectiveness_summary(outcomes)
        assert "repair" in result
        assert "" not in result


# =====================================================================
# Step 2.3: Outcome-informed _detect_repair_futility
# =====================================================================


class TestOutcomeInformedRepairFutility:
    """Test the enhanced _detect_repair_futility that uses outcome data."""

    def test_no_outcomes_no_decisions_returns_none(self):
        """No data at all → no futility."""
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=[], recent_decisions=[])
        assert engine._detect_repair_futility(ctx) is None

    def test_outcome_based_3_ineffective_repairs(self):
        """3 repair outcomes for same dim, all non-effective → futile."""
        outcomes = [
            _make_resolved_outcome(
                mode="repair",
                target_dimension="strategy_validity",
                effectiveness="neutral",
                delta=0.5,
            ),
            _make_resolved_outcome(
                mode="repair",
                target_dimension="strategy_validity",
                effectiveness="counterproductive",
                delta=-2.0,
            ),
            _make_resolved_outcome(
                mode="repair",
                target_dimension="strategy_validity",
                effectiveness="neutral",
                delta=1.0,
            ),
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=outcomes)
        result = engine._detect_repair_futility(ctx)
        assert result == "strategy_validity"

    def test_outcome_based_one_effective_not_futile(self):
        """If at least 1 of last 3 was effective → not futile."""
        outcomes = [
            _make_resolved_outcome(mode="repair", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="effective", delta=5.0
            ),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="counterproductive"
            ),
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=outcomes)
        result = engine._detect_repair_futility(ctx)
        assert result is None

    def test_outcome_based_fewer_than_3_repairs(self):
        """Only 2 repair outcomes → not enough data → no futility."""
        outcomes = [
            _make_resolved_outcome(mode="repair", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="counterproductive"
            ),
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=outcomes)
        result = engine._detect_repair_futility(ctx)
        assert result is None

    def test_outcome_based_mixed_dimensions(self):
        """Futility is per-dimension. Different dims should not cross-contaminate."""
        outcomes = [
            _make_resolved_outcome(mode="repair", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(mode="repair", target_dimension="system_health", effectiveness="neutral"),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="counterproductive"
            ),
            _make_resolved_outcome(mode="repair", target_dimension="system_health", effectiveness="neutral"),
            _make_resolved_outcome(mode="repair", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(
                mode="repair", target_dimension="system_health", effectiveness="effective", delta=5.0
            ),
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=outcomes)
        result = engine._detect_repair_futility(ctx)
        # strategy_validity: 3 repairs, 0 effective → futile
        # system_health: 3 repairs, 1 effective → NOT futile
        assert result == "strategy_validity"

    def test_outcome_based_non_repair_modes_ignored(self):
        """Only repair mode outcomes count toward futility."""
        outcomes = [
            _make_resolved_outcome(mode="research", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(mode="plan", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(mode="execute", target_dimension="strategy_validity", effectiveness="neutral"),
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=outcomes)
        result = engine._detect_repair_futility(ctx)
        assert result is None

    def test_decision_fallback_still_works(self):
        """When outcomes are empty, decision-based fallback detects futility."""
        decisions = [
            {"mode": "repair", "target_dimension": "strategy_validity"},
            {"mode": "repair", "target_dimension": "strategy_validity"},
            {"mode": "repair", "target_dimension": "strategy_validity"},
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=[], recent_decisions=decisions)
        result = engine._detect_repair_futility(ctx)
        assert result == "strategy_validity"

    def test_outcome_preferred_over_decisions(self):
        """When outcomes say NOT futile but decisions say futile,
        outcomes win (outcomes are checked first and return early)."""
        outcomes = [
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="effective", delta=5.0
            ),
            _make_resolved_outcome(mode="repair", target_dimension="strategy_validity", effectiveness="neutral"),
            _make_resolved_outcome(mode="repair", target_dimension="strategy_validity", effectiveness="neutral"),
        ]
        # Decisions all say repair with no positive followup — would be futile by old logic
        decisions = [
            {"mode": "repair", "target_dimension": "strategy_validity"},
            {"mode": "repair", "target_dimension": "strategy_validity"},
            {"mode": "repair", "target_dimension": "strategy_validity"},
        ]
        engine = DecisionEngine()
        ctx = _ctx(recent_outcomes=outcomes, recent_decisions=decisions)
        result = engine._detect_repair_futility(ctx)
        # Outcome-based: 1 effective in last 3 → NOT futile
        assert result is None


class TestRepairFutilityTriggersEscalate:
    """Test that repair futility → ESCALATE in full decision flow."""

    @pytest.mark.asyncio
    async def test_futile_repairs_escalate(self, tmp_path: Path):
        """3 ineffective repair outcomes for same dim → ESCALATE decision."""
        (tmp_path / "STATE").mkdir(parents=True)

        # Write outcomes to disk so ContextAssembler picks them up
        outcomes = [
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="neutral", delta=0.0
            ),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="counterproductive", delta=-2.0
            ),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="neutral", delta=1.0
            ),
        ]
        (tmp_path / "STATE" / "decision_outcomes.json").write_text(json.dumps(outcomes))

        report = _report(
            dims={
                "strategy_validity": _dim("strategy_validity", 45.0),
                "system_health": _dim("system_health", 80.0),
            }
        )

        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(report)

        # Verify outcomes were loaded
        assert len(ctx.recent_outcomes) == 3

        engine = DecisionEngine(base_path=str(tmp_path))
        decision = await engine.decide(ctx)

        assert decision.mode == ActionMode.ESCALATE
        assert "strategy_validity" in decision.reason
        assert decision.target_dimension == "strategy_validity"

    @pytest.mark.asyncio
    async def test_effective_repairs_no_escalate(self, tmp_path: Path):
        """When repairs are effective, should NOT escalate."""
        (tmp_path / "STATE").mkdir(parents=True)

        outcomes = [
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="effective", delta=10.0
            ),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="neutral", delta=1.0
            ),
            _make_resolved_outcome(
                mode="repair", target_dimension="strategy_validity", effectiveness="effective", delta=8.0
            ),
        ]
        (tmp_path / "STATE" / "decision_outcomes.json").write_text(json.dumps(outcomes))

        report = _report(
            dims={
                "strategy_validity": _dim("strategy_validity", 45.0),
                "system_health": _dim("system_health", 80.0),
            }
        )

        assembler = ContextAssembler(base_path=str(tmp_path))
        ctx = await assembler.assemble(report)

        engine = DecisionEngine(base_path=str(tmp_path))
        decision = await engine.decide(ctx)

        # Should NOT be ESCALATE — repairs are working
        assert decision.mode != ActionMode.ESCALATE


# =====================================================================
# Step 2.1: OutcomeAnalyzer record → resolve cycle
# =====================================================================


class TestOutcomeAnalyzerRecordResolveCycle:
    """End-to-end record → resolve cycle."""

    def test_record_then_resolve(self, tmp_path: Path):
        """Record a decision, then resolve it after the outcome window."""
        analyzer = OutcomeAnalyzer(str(tmp_path))

        # Record a decision
        decision = Decision(
            mode=ActionMode.REPAIR,
            reason="Fix strategy",
            target_dimension="strategy_validity",
        )
        report_before = _report(
            dims={
                "strategy_validity": _dim("strategy_validity", 40.0),
                "system_health": _dim("system_health", 80.0),
            },
            overall=60.0,
        )
        snap = analyzer.record_decision(decision, report_before)
        assert snap.resolved_at is None
        assert snap.overall_before == 60.0

        # Manipulate the decided_at to be past the outcome window
        snapshots = analyzer._load_snapshots()
        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        snapshots[0].decided_at = old_time.isoformat()
        analyzer._save_snapshots(snapshots)

        # Resolve with improved scores
        report_after = _report(
            dims={
                "strategy_validity": _dim("strategy_validity", 65.0),
                "system_health": _dim("system_health", 82.0),
            },
            overall=73.5,
        )
        resolved = analyzer.resolve_pending(report_after)

        assert len(resolved) == 1
        assert resolved[0].effectiveness == "effective"
        assert resolved[0].delta == pytest.approx(13.5)

    def test_get_recent_outcomes_returns_resolved_only(self, tmp_path: Path):
        """get_recent_outcomes only returns resolved snapshots."""
        analyzer = OutcomeAnalyzer(str(tmp_path))

        # Save a mix of resolved and unresolved
        snapshots = [
            DecisionSnapshot(
                decision_id="unresolved_1",
                mode="repair",
                target_dimension="sv",
                reason="pending",
                decided_at=datetime.now(timezone.utc).isoformat(),
                scores_before={},
                overall_before=50.0,
            ),
            DecisionSnapshot(
                decision_id="resolved_1",
                mode="repair",
                target_dimension="sv",
                reason="done",
                decided_at="2026-01-01T00:00:00+00:00",
                scores_before={},
                overall_before=50.0,
                resolved_at="2026-01-01T00:30:00+00:00",
                effectiveness="effective",
                delta=10.0,
            ),
        ]
        analyzer._save_snapshots(snapshots)

        recent = analyzer.get_recent_outcomes(10)
        assert len(recent) == 1
        assert recent[0].decision_id == "resolved_1"


# =====================================================================
# Step 2.1: Heartbeat pipeline integration (mocked)
# =====================================================================


class TestHeartbeatOutcomeWiring:
    """Verify OutcomeAnalyzer is called during _run_autonomy_cycle."""

    def test_pipeline_records_and_resolves(self, tmp_path, monkeypatch):
        """The heartbeat pipeline calls record_decision and resolve_pending."""
        import heartbeat
        from novatrade.autonomy import (
            ContextAssembler,
            DecisionEngine,
            GoalDecomposer,
            OutcomeAnalyzer,
            ProgressScorer,
            TaskSpecGenerator,
        )

        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        (tmp_path / "TASKS").mkdir(parents=True, exist_ok=True)

        report = _report()
        base = str(tmp_path)

        async def mock_score(self):
            return report

        async def mock_assemble(self, r):
            return DecisionContext(
                progress_report=r,
                pending_tasks=[],
                active_goals=[],
                recent_decisions=[],
                time_of_day="morning",
            )

        async def mock_decide(self, ctx):
            return Decision(
                mode=ActionMode.MONITOR,
                reason="Test monitoring",
                confidence="high",
            )

        # Track calls to OutcomeAnalyzer
        resolve_calls = []
        record_calls = []
        original_resolve = OutcomeAnalyzer.resolve_pending
        original_record = OutcomeAnalyzer.record_decision

        def tracking_resolve(self, rpt):
            result = original_resolve(self, rpt)
            resolve_calls.append(result)
            return result

        def tracking_record(self, dec, rpt):
            result = original_record(self, dec, rpt)
            record_calls.append(result)
            return result

        orig_scorer_init = ProgressScorer.__init__
        orig_assembler_init = ContextAssembler.__init__
        orig_engine_init = DecisionEngine.__init__
        orig_decomposer_init = GoalDecomposer.__init__
        orig_gen_init = TaskSpecGenerator.__init__
        orig_oa_init = OutcomeAnalyzer.__init__

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

        def patched_oa_init(self, base_path=base):
            orig_oa_init(self, base_path=base)

        with (
            patch.object(ProgressScorer, "score", mock_score),
            patch.object(ContextAssembler, "assemble", mock_assemble),
            patch.object(DecisionEngine, "decide", mock_decide),
            patch.object(ProgressScorer, "__init__", patched_scorer_init),
            patch.object(ContextAssembler, "__init__", patched_assembler_init),
            patch.object(DecisionEngine, "__init__", patched_engine_init),
            patch.object(GoalDecomposer, "__init__", patched_decomposer_init),
            patch.object(TaskSpecGenerator, "__init__", patched_gen_init),
            patch.object(OutcomeAnalyzer, "__init__", patched_oa_init),
            patch.object(OutcomeAnalyzer, "resolve_pending", tracking_resolve),
            patch.object(OutcomeAnalyzer, "record_decision", tracking_record),
        ):
            result = heartbeat._run_autonomy_cycle()

        assert result is not None
        # resolve_pending should have been called once
        assert len(resolve_calls) == 1
        # record_decision should have been called once
        assert len(record_calls) == 1

    def test_pipeline_snapshot_includes_resolved_outcomes(self, tmp_path, monkeypatch):
        """Snapshot dict includes resolved_outcomes when available."""
        import heartbeat
        from novatrade.autonomy import (
            ContextAssembler,
            DecisionEngine,
            GoalDecomposer,
            OutcomeAnalyzer,
            ProgressScorer,
            TaskSpecGenerator,
        )

        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        (tmp_path / "TASKS").mkdir(parents=True, exist_ok=True)

        # Pre-populate with an old unresolved outcome that should get resolved
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True, exist_ok=True)
        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        outcomes = [
            asdict(
                DecisionSnapshot(
                    decision_id="old_repair",
                    mode="repair",
                    target_dimension="strategy_validity",
                    reason="old fix",
                    decided_at=old_time.isoformat(),
                    scores_before={"strategy_validity": 40.0, "system_health": 80.0},
                    overall_before=60.0,
                )
            )
        ]
        (state_dir / "decision_outcomes.json").write_text(json.dumps(outcomes))

        report = _report(
            dims={
                "strategy_validity": _dim("strategy_validity", 70.0),
                "system_health": _dim("system_health", 80.0),
            },
            overall=75.0,
        )
        base = str(tmp_path)

        async def mock_score(self):
            return report

        async def mock_assemble(self, r):
            return DecisionContext(
                progress_report=r,
                pending_tasks=[],
                active_goals=[],
                recent_decisions=[],
                time_of_day="morning",
            )

        async def mock_decide(self, ctx):
            return Decision(
                mode=ActionMode.MONITOR,
                reason="Test monitoring",
                confidence="high",
            )

        orig_scorer_init = ProgressScorer.__init__
        orig_assembler_init = ContextAssembler.__init__
        orig_engine_init = DecisionEngine.__init__
        orig_decomposer_init = GoalDecomposer.__init__
        orig_gen_init = TaskSpecGenerator.__init__
        orig_oa_init = OutcomeAnalyzer.__init__

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

        def patched_oa_init(self, base_path=base):
            orig_oa_init(self, base_path=base)

        with (
            patch.object(ProgressScorer, "score", mock_score),
            patch.object(ContextAssembler, "assemble", mock_assemble),
            patch.object(DecisionEngine, "decide", mock_decide),
            patch.object(ProgressScorer, "__init__", patched_scorer_init),
            patch.object(ContextAssembler, "__init__", patched_assembler_init),
            patch.object(DecisionEngine, "__init__", patched_engine_init),
            patch.object(GoalDecomposer, "__init__", patched_decomposer_init),
            patch.object(TaskSpecGenerator, "__init__", patched_gen_init),
            patch.object(OutcomeAnalyzer, "__init__", patched_oa_init),
        ):
            result = heartbeat._run_autonomy_cycle()

        assert result is not None
        # The old outcome should have been resolved → delta = 75.0 - 60.0 = 15.0
        assert "resolved_outcomes" in result
        assert len(result["resolved_outcomes"]) == 1
        assert result["resolved_outcomes"][0]["id"] == "old_repair"
        assert result["resolved_outcomes"][0]["effectiveness"] == "effective"
        assert result["resolved_outcomes"][0]["delta"] == pytest.approx(15.0)

    def test_pipeline_resilient_to_analyzer_failure(self, tmp_path, monkeypatch):
        """Pipeline continues even if OutcomeAnalyzer raises."""
        import heartbeat
        from novatrade.autonomy import (
            ContextAssembler,
            DecisionEngine,
            GoalDecomposer,
            OutcomeAnalyzer,
            ProgressScorer,
            TaskSpecGenerator,
        )

        monkeypatch.setattr(heartbeat, "STATE_DIR", tmp_path / "STATE")
        monkeypatch.setattr(heartbeat, "TASKS_DIR", tmp_path / "TASKS")
        (tmp_path / "TASKS").mkdir(parents=True, exist_ok=True)

        report = _report()
        base = str(tmp_path)

        async def mock_score(self):
            return report

        async def mock_assemble(self, r):
            return DecisionContext(progress_report=r, time_of_day="morning")

        async def mock_decide(self, ctx):
            return Decision(mode=ActionMode.MONITOR, reason="ok", confidence="high")

        def failing_resolve(self, rpt):
            raise RuntimeError("OutcomeAnalyzer exploded!")

        def failing_record(self, dec, rpt):
            raise RuntimeError("OutcomeAnalyzer record exploded!")

        orig_scorer_init = ProgressScorer.__init__
        orig_assembler_init = ContextAssembler.__init__
        orig_engine_init = DecisionEngine.__init__
        orig_decomposer_init = GoalDecomposer.__init__
        orig_gen_init = TaskSpecGenerator.__init__
        orig_oa_init = OutcomeAnalyzer.__init__

        with (
            patch.object(ProgressScorer, "score", mock_score),
            patch.object(ContextAssembler, "assemble", mock_assemble),
            patch.object(DecisionEngine, "decide", mock_decide),
            patch.object(
                ProgressScorer,
                "__init__",
                lambda self, config=None, base_path=base: orig_scorer_init(self, config=config, base_path=base),
            ),
            patch.object(
                ContextAssembler, "__init__", lambda self, base_path=base: orig_assembler_init(self, base_path=base)
            ),
            patch.object(
                DecisionEngine,
                "__init__",
                lambda self, config=None, base_path=base: orig_engine_init(self, config=config, base_path=base),
            ),
            patch.object(
                GoalDecomposer, "__init__", lambda self, base_path=base: orig_decomposer_init(self, base_path=base)
            ),
            patch.object(
                TaskSpecGenerator, "__init__", lambda self, base_path=base: orig_gen_init(self, base_path=base)
            ),
            patch.object(OutcomeAnalyzer, "__init__", lambda self, base_path=base: orig_oa_init(self, base_path=base)),
            patch.object(OutcomeAnalyzer, "resolve_pending", failing_resolve),
            patch.object(OutcomeAnalyzer, "record_decision", failing_record),
        ):
            result = heartbeat._run_autonomy_cycle()

        # Pipeline should complete despite analyzer failures
        assert result is not None
        assert "resolved_outcomes" not in result  # no resolved outcomes due to failure
