"""Tests for Phase 10: Causal Learning System.

Tests CausalModel, PatternLibrary, TraceabilityGraph, and their integration.
"""

from __future__ import annotations

import json
import os

import pytest

from novatrade.autonomy.causal_model import CausalModel, CausalRecord
from novatrade.autonomy.pattern_library import PatternLibrary
from novatrade.autonomy.traceability import TraceabilityGraph, TraceEntry


@pytest.fixture
def tmp_base(tmp_path):
    """Create a temporary base path with STATE/ directory."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    return str(tmp_path)


# -----------------------------------------------------------------------
# CausalModel tests
# -----------------------------------------------------------------------


class TestCausalModel:
    def test_record_persists(self, tmp_base):
        """record() creates STATE/causal_records.json with the entry."""
        model = CausalModel(base_path=tmp_base)
        rec = CausalRecord(
            action="restart service",
            target_dimension="system_health",
            pre_score=40.0,
            post_score=75.0,
            delta=35.0,
            effective=True,
            decision_id="repair_system_health_20260331T120000",
        )
        model.record(rec)

        path = os.path.join(tmp_base, "STATE", "causal_records.json")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.loads(f.read())
        assert len(data) == 1
        assert data[0]["action"] == "restart service"
        assert data[0]["effective"] is True
        assert data[0]["recorded_at"]  # auto-populated

    def test_rank_actions_correct_ranking(self, tmp_base):
        """rank_actions() sorts by success_rate descending."""
        model = CausalModel(base_path=tmp_base)
        # Action A: 2 effective, 1 not = 66% success
        for _i in range(2):
            model.record(
                CausalRecord(
                    action="action_a",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=65,
                    delta=15,
                    effective=True,
                )
            )
        model.record(
            CausalRecord(
                action="action_a",
                target_dimension="dim1",
                pre_score=50,
                post_score=48,
                delta=-2,
                effective=False,
            )
        )
        # Action B: 3 effective, 0 not = 100% success
        for _i in range(3):
            model.record(
                CausalRecord(
                    action="action_b",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=70,
                    delta=20,
                    effective=True,
                )
            )

        ranked = model.rank_actions("dim1")
        assert len(ranked) == 2
        assert ranked[0]["action"] == "action_b"
        assert ranked[0]["success_rate"] == 1.0
        assert ranked[1]["action"] == "action_a"
        assert abs(ranked[1]["success_rate"] - 2 / 3) < 0.01

    def test_rank_actions_unknown_dimension_empty(self, tmp_base):
        """rank_actions() for a non-existent dimension returns empty."""
        model = CausalModel(base_path=tmp_base)
        model.record(
            CausalRecord(
                action="a",
                target_dimension="dim1",
                pre_score=50,
                post_score=60,
                delta=10,
                effective=True,
            )
        )
        assert model.rank_actions("nonexistent") == []

    def test_avoid_list_filters(self, tmp_base):
        """avoid_list() returns actions with low success and enough samples."""
        model = CausalModel(base_path=tmp_base)
        # Action with 0% success, 4 samples
        for _ in range(4):
            model.record(
                CausalRecord(
                    action="bad_action",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=48,
                    delta=-2,
                    effective=False,
                )
            )
        # Action with 100% success, 3 samples — should NOT appear
        for _ in range(3):
            model.record(
                CausalRecord(
                    action="good_action",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=70,
                    delta=20,
                    effective=True,
                )
            )
        # Action with 0% success but only 2 samples — below min_samples
        for _ in range(2):
            model.record(
                CausalRecord(
                    action="low_sample_bad",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=48,
                    delta=-2,
                    effective=False,
                )
            )

        avoid = model.avoid_list("dim1", min_samples=3, max_success_rate=0.2)
        assert "bad_action" in avoid
        assert "good_action" not in avoid
        assert "low_sample_bad" not in avoid

    def test_record_cap(self, tmp_base):
        """Records are capped at max_records (500)."""
        model = CausalModel(base_path=tmp_base)
        model._max_records = 10  # lower for testing
        for i in range(15):
            model.record(
                CausalRecord(
                    action=f"action_{i}",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=60,
                    delta=10,
                    effective=True,
                )
            )
        records = model._load()
        assert len(records) == 10
        # Should keep the last 10 (action_5 through action_14)
        assert records[0]["action"] == "action_5"
        assert records[-1]["action"] == "action_14"


# -----------------------------------------------------------------------
# PatternLibrary tests
# -----------------------------------------------------------------------


class TestPatternLibrary:
    def test_derive_positive_pattern(self, tmp_base):
        """derive_patterns() creates a positive pattern when success rate >= 60%."""
        model = CausalModel(base_path=tmp_base)
        # 3 effective out of 4 = 75% success
        for _ in range(3):
            model.record(
                CausalRecord(
                    action="restart_service",
                    target_dimension="system_health",
                    pre_score=40,
                    post_score=70,
                    delta=30,
                    effective=True,
                )
            )
        model.record(
            CausalRecord(
                action="restart_service",
                target_dimension="system_health",
                pre_score=40,
                post_score=42,
                delta=2,
                effective=False,
            )
        )

        lib = PatternLibrary(base_path=tmp_base)
        patterns = lib.derive_patterns(model)
        assert len(patterns) == 1
        p = patterns[0]
        assert not p.is_anti_pattern
        assert p.recommended_action == "restart_service"
        assert p.success_rate == 0.75
        assert p.sample_size == 4

    def test_derive_anti_pattern(self, tmp_base):
        """derive_patterns() creates an anti-pattern when success rate <= 20%."""
        model = CausalModel(base_path=tmp_base)
        # 0 effective out of 5 = 0% success
        for _ in range(5):
            model.record(
                CausalRecord(
                    action="clear_cache",
                    target_dimension="performance_stability",
                    pre_score=60,
                    post_score=58,
                    delta=-2,
                    effective=False,
                )
            )

        lib = PatternLibrary(base_path=tmp_base)
        patterns = lib.derive_patterns(model)
        assert len(patterns) == 1
        p = patterns[0]
        assert p.is_anti_pattern
        assert p.recommended_action == ""  # no recommendation for anti-patterns
        assert p.success_rate == 0.0

    def test_derive_skips_low_samples(self, tmp_base):
        """derive_patterns() ignores actions with fewer than 3 samples."""
        model = CausalModel(base_path=tmp_base)
        for _ in range(2):
            model.record(
                CausalRecord(
                    action="tweak_config",
                    target_dimension="dim1",
                    pre_score=50,
                    post_score=80,
                    delta=30,
                    effective=True,
                )
            )

        lib = PatternLibrary(base_path=tmp_base)
        patterns = lib.derive_patterns(model)
        assert len(patterns) == 0

    def test_match_returns_patterns_for_dimension(self, tmp_base):
        """match() returns patterns matching the given dimension."""
        model = CausalModel(base_path=tmp_base)
        for _ in range(4):
            model.record(
                CausalRecord(
                    action="action_x",
                    target_dimension="dim_a",
                    pre_score=50,
                    post_score=70,
                    delta=20,
                    effective=True,
                )
            )
        for _ in range(4):
            model.record(
                CausalRecord(
                    action="action_y",
                    target_dimension="dim_b",
                    pre_score=50,
                    post_score=70,
                    delta=20,
                    effective=True,
                )
            )

        lib = PatternLibrary(base_path=tmp_base)
        lib.derive_patterns(model)

        matches_a = lib.match("dim_a")
        matches_b = lib.match("dim_b")
        assert len(matches_a) == 1
        assert matches_a[0].recommended_action == "action_x"
        assert len(matches_b) == 1
        assert matches_b[0].recommended_action == "action_y"

    def test_get_recommendation_returns_best(self, tmp_base):
        """get_recommendation() returns the best action."""
        model = CausalModel(base_path=tmp_base)
        # Action with 100% success
        for _ in range(3):
            model.record(
                CausalRecord(
                    action="best_action",
                    target_dimension="dim1",
                    pre_score=40,
                    post_score=80,
                    delta=40,
                    effective=True,
                )
            )
        # Action with 66% success
        for _ in range(2):
            model.record(
                CausalRecord(
                    action="ok_action",
                    target_dimension="dim1",
                    pre_score=40,
                    post_score=60,
                    delta=20,
                    effective=True,
                )
            )
        model.record(
            CausalRecord(
                action="ok_action",
                target_dimension="dim1",
                pre_score=40,
                post_score=42,
                delta=2,
                effective=False,
            )
        )

        lib = PatternLibrary(base_path=tmp_base)
        lib.derive_patterns(model)

        rec = lib.get_recommendation("dim1")
        assert rec == "best_action"

    def test_get_recommendation_none_when_no_patterns(self, tmp_base):
        """get_recommendation() returns None when no good patterns exist."""
        lib = PatternLibrary(base_path=tmp_base)
        assert lib.get_recommendation("nonexistent") is None


# -----------------------------------------------------------------------
# TraceabilityGraph tests
# -----------------------------------------------------------------------


class TestTraceabilityGraph:
    def test_record_trace_persists(self, tmp_base):
        """record_trace() writes to STATE/traceability_graph.json."""
        graph = TraceabilityGraph(base_path=tmp_base)
        entry = TraceEntry(
            sub_goal_id="sg_001",
            decision_id="repair_system_health_20260331",
            task_file="autonomy_repair_20260331.md",
            outcome="effective",
            delta=15.0,
            pattern_id=None,
            dimension="system_health",
        )
        graph.record_trace(entry)

        path = os.path.join(tmp_base, "STATE", "traceability_graph.json")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.loads(f.read())
        assert len(data) == 1
        assert data[0]["decision_id"] == "repair_system_health_20260331"
        assert data[0]["timestamp"]  # auto-populated

    def test_get_traces_for_subgoal(self, tmp_base):
        """get_traces_for_subgoal() filters correctly."""
        graph = TraceabilityGraph(base_path=tmp_base)
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_001",
                decision_id="d1",
                task_file="t1.md",
                outcome="effective",
                delta=10,
                pattern_id=None,
            )
        )
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_002",
                decision_id="d2",
                task_file="t2.md",
                outcome="ineffective",
                delta=-5,
                pattern_id=None,
            )
        )
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_001",
                decision_id="d3",
                task_file="t3.md",
                outcome="effective",
                delta=8,
                pattern_id=None,
            )
        )

        traces = graph.get_traces_for_subgoal("sg_001")
        assert len(traces) == 2
        assert all(t.sub_goal_id == "sg_001" for t in traces)

    def test_summary_correct_counts(self, tmp_base):
        """summary() returns correct totals and breakdowns."""
        graph = TraceabilityGraph(base_path=tmp_base)
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_001",
                decision_id="d1",
                task_file="t1.md",
                outcome="effective",
                delta=10,
                pattern_id=None,
                dimension="system_health",
            )
        )
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_002",
                decision_id="d2",
                task_file="t2.md",
                outcome="ineffective",
                delta=-5,
                pattern_id=None,
                dimension="strategy_validity",
            )
        )
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_003",
                decision_id="d3",
                task_file="t3.md",
                outcome="effective",
                delta=12,
                pattern_id=None,
                dimension="system_health",
            )
        )

        s = graph.summary()
        assert s["total"] == 3
        assert s["by_outcome"]["effective"] == 2
        assert s["by_outcome"]["ineffective"] == 1
        assert s["by_dimension"]["system_health"] == 2
        assert s["by_dimension"]["strategy_validity"] == 1


# -----------------------------------------------------------------------
# Integration test
# -----------------------------------------------------------------------


class TestCausalLearningIntegration:
    def test_full_flow_record_derive_match_recommend(self, tmp_base):
        """Full flow: record causal data -> derive patterns -> match -> recommend."""
        # 1. Record causal observations
        model = CausalModel(base_path=tmp_base)
        for _ in range(5):
            model.record(
                CausalRecord(
                    action="scale_workers",
                    target_dimension="performance_stability",
                    pre_score=45,
                    post_score=72,
                    delta=27,
                    effective=True,
                    decision_id="execute_perf_20260331",
                )
            )
        # Also record an ineffective action
        for _ in range(4):
            model.record(
                CausalRecord(
                    action="restart_feeds",
                    target_dimension="performance_stability",
                    pre_score=45,
                    post_score=46,
                    delta=1,
                    effective=False,
                    decision_id="repair_perf_20260331",
                )
            )

        # 2. Derive patterns
        lib = PatternLibrary(base_path=tmp_base)
        patterns = lib.derive_patterns(model)
        assert len(patterns) == 2  # one positive, one anti

        positive = [p for p in patterns if not p.is_anti_pattern]
        anti = [p for p in patterns if p.is_anti_pattern]
        assert len(positive) == 1
        assert len(anti) == 1
        assert positive[0].recommended_action == "scale_workers"

        # 3. Match and recommend
        rec = lib.get_recommendation("performance_stability")
        assert rec == "scale_workers"

        # 4. Verify avoid list
        avoid = model.avoid_list("performance_stability")
        assert "restart_feeds" in avoid
        assert "scale_workers" not in avoid

        # 5. Record in traceability graph
        graph = TraceabilityGraph(base_path=tmp_base)
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_perf",
                decision_id="execute_perf_20260331",
                task_file="autonomy_execute_20260331.md",
                outcome="effective",
                delta=27,
                pattern_id=positive[0].pattern_id,
                dimension="performance_stability",
            )
        )

        traces = graph.get_traces_for_subgoal("sg_perf")
        assert len(traces) == 1
        assert traces[0].outcome == "effective"
        assert traces[0].pattern_id == positive[0].pattern_id

    def test_traceability_get_traces_for_dimension(self, tmp_base):
        """TraceabilityGraph.get_traces_for_dimension() works correctly."""
        graph = TraceabilityGraph(base_path=tmp_base)
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_001",
                decision_id="d1",
                task_file="t1.md",
                outcome="effective",
                delta=10,
                pattern_id=None,
                dimension="system_health",
            )
        )
        graph.record_trace(
            TraceEntry(
                sub_goal_id="sg_002",
                decision_id="d2",
                task_file="t2.md",
                outcome="ineffective",
                delta=-5,
                pattern_id=None,
                dimension="strategy_validity",
            )
        )

        traces = graph.get_traces_for_dimension("system_health")
        assert len(traces) == 1
        assert traces[0].decision_id == "d1"
