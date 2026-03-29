"""Tests for utils.scheduler.dependency_graph — Phase 7: Dependency Graph & Unlock Chains.

Tests cover: graph basics, cycle detection, ready tasks, critical path,
unlock values, topological sort, prune completed, build_graph, fail-fast
probe generation, epic decomposition, and edge cases.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from utils.scheduler.dependency_graph import (
    DecompositionSuggestion,
    DependencyGraph,
    ProbeTask,
    build_graph,
    generate_probe,
    get_critical_path,
    get_unlock_values,
    is_on_critical_path,
    suggest_decomposition,
)
from utils.scheduler.work_unit import (
    CommitmentLevel,
    TaskClass,
    WorkMode,
    WorkUnit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wu(**overrides) -> WorkUnit:
    """Create a WorkUnit with sensible test defaults, accepting overrides."""
    defaults: dict[str, Any] = {
        "id": "t1",
        "title": "test task",
        "urgency": 0.5,
        "strategic_value": 0.5,
        "unblock_value": 0.0,
        "risk_reduction": 0.0,
        "confidence": 0.5,
        "success_probability": 0.7,
        "estimated_optimistic_min": 10.0,
        "estimated_expected_min": 30.0,
        "estimated_pessimistic_min": 60.0,
        "context_switch_cost_min": 5.0,
        "priority": "medium",
        "commitment_level": CommitmentLevel.SOFT,
        "deferment_age_days": 0.0,
    }
    defaults.update(overrides)
    return WorkUnit(**defaults)


def _make_chain(ids: list[str], durations: list[float] | None = None) -> list[WorkUnit]:
    """Create a linear chain: ids[0] -> ids[1] -> ... -> ids[-1].

    Each task depends on the previous one.
    """
    if durations is None:
        durations = [30.0] * len(ids)
    units = []
    for i, tid in enumerate(ids):
        deps = [ids[i - 1]] if i > 0 else []
        units.append(
            _wu(
                id=tid,
                title=f"Task {tid}",
                dependency_ids=deps,
                estimated_optimistic_min=durations[i] * 0.5,
                estimated_expected_min=durations[i],
                estimated_pessimistic_min=durations[i] * 2.0,
            )
        )
    return units


# ===========================================================================
# Graph basics
# ===========================================================================


class TestGraphBasics:
    def test_add_task_and_retrieve(self):
        g = DependencyGraph()
        wu = _wu(id="a")
        g.add_task(wu)
        assert g.get_task("a") is wu
        assert g.size() == 1

    def test_add_multiple_tasks(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        assert g.size() == 2

    def test_add_dependency_wires_edges(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_dependency("a", "b")
        assert "b" in g.get_dependents("a")
        assert "a" in g.get_dependencies("b")

    def test_remove_task_removes_edges(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_dependency("a", "b")
        g.remove_task("a")
        assert g.get_task("a") is None
        assert g.size() == 1
        assert g.get_dependencies("b") == []

    def test_remove_nonexistent_task_is_noop(self):
        g = DependencyGraph()
        g.remove_task("nonexistent")  # should not raise
        assert g.size() == 0

    def test_size_tracking(self):
        g = DependencyGraph()
        assert g.size() == 0
        g.add_task(_wu(id="a"))
        assert g.size() == 1
        g.add_task(_wu(id="b"))
        assert g.size() == 2
        g.remove_task("a")
        assert g.size() == 1

    def test_get_task_returns_none_for_missing(self):
        g = DependencyGraph()
        assert g.get_task("missing") is None

    def test_get_dependents_empty(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        assert g.get_dependents("a") == []

    def test_get_dependencies_empty(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        assert g.get_dependencies("a") == []

    def test_add_task_auto_wires_dependency_ids(self):
        """If A is in the graph and B has dependency_ids=[A], adding B wires the edge."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b", dependency_ids=["a"]))
        assert "b" in g.get_dependents("a")
        assert "a" in g.get_dependencies("b")

    def test_add_task_auto_wires_reverse(self):
        """If B (with dep on A) is added first, then A is added, edge is wired."""
        g = DependencyGraph()
        g.add_task(_wu(id="b", dependency_ids=["a"]))
        # A not in graph yet, so edge not wired
        assert g.get_dependencies("b") == []
        g.add_task(_wu(id="a"))
        # Now the edge should be wired
        assert "a" in g.get_dependencies("b")
        assert "b" in g.get_dependents("a")

    def test_add_task_empty_id_raises(self):
        g = DependencyGraph()
        with pytest.raises(ValueError, match="non-empty id"):
            g.add_task(_wu(id=""))


# ===========================================================================
# Cycle detection
# ===========================================================================


class TestCycleDetection:
    def test_no_cycle_linear(self):
        """A -> B -> C: no cycle."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_task(_wu(id="c"))
        g.add_dependency("a", "b")
        g.add_dependency("b", "c")
        # No cycle should be detected
        assert not g.has_cycle("a", "c")

    def test_cycle_two_nodes(self):
        """A -> B, adding B -> A should detect cycle."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_dependency("a", "b")
        assert g.has_cycle("b", "a")
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            g.add_dependency("b", "a")

    def test_cycle_three_nodes(self):
        """A -> B -> C, adding C -> A should detect cycle."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_task(_wu(id="c"))
        g.add_dependency("a", "b")
        g.add_dependency("b", "c")
        assert g.has_cycle("c", "a")
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            g.add_dependency("c", "a")

    def test_self_loop_detected(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        assert g.has_cycle("a", "a")
        with pytest.raises(ValueError, match=r"[Ss]elf-loop"):
            g.add_dependency("a", "a")

    def test_no_false_positive_on_diamond(self):
        """A -> B, A -> C, B -> D, C -> D: diamond is valid DAG (no cycle).

        Adding D -> B or D -> C *would* create a cycle (since B->D/C->D exist).
        But adding a new node E with D->E is safe. Also B->C is safe.
        """
        g = DependencyGraph()
        for tid in ["a", "b", "c", "d"]:
            g.add_task(_wu(id=tid))
        g.add_dependency("a", "b")
        g.add_dependency("a", "c")
        g.add_dependency("b", "d")
        g.add_dependency("c", "d")
        # D -> B *would* cycle (B->D->B), so has_cycle is True
        assert g.has_cycle("d", "b")
        assert g.has_cycle("d", "c")
        # But B -> C would NOT cycle (no path from C back to B)
        assert not g.has_cycle("b", "c")
        # Adding a new downstream node E is always safe
        g.add_task(_wu(id="e"))
        assert not g.has_cycle("d", "e")

    def test_has_cycle_returns_false_when_safe(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        assert not g.has_cycle("a", "b")

    def test_cycle_preserved_after_failed_add(self):
        """Graph should be unchanged after a cycle-causing add_dependency fails."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_dependency("a", "b")
        with pytest.raises(ValueError):
            g.add_dependency("b", "a")
        # Graph should still only have a -> b
        assert g.get_dependents("a") == ["b"]
        assert g.get_dependents("b") == []


# ===========================================================================
# Ready tasks
# ===========================================================================


class TestReadyTasks:
    def test_no_dependencies_all_ready(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        ready = g.get_ready_tasks()
        assert len(ready) == 2
        ready_ids = {wu.id for wu in ready}
        assert ready_ids == {"a", "b"}

    def test_dependency_blocks_readiness(self):
        """A -> B: only A is ready."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b", dependency_ids=["a"]))
        ready = g.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "a"

    def test_prune_makes_dependent_ready(self):
        """A -> B: after pruning A, B becomes ready."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b", dependency_ids=["a"]))
        g.prune_completed({"a"})
        ready = g.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "b"

    def test_multiple_dependencies_all_must_be_met(self):
        """A -> C, B -> C: C not ready until both A and B are done."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_task(_wu(id="c", dependency_ids=["a", "b"]))
        ready = g.get_ready_tasks()
        ready_ids = {wu.id for wu in ready}
        assert "c" not in ready_ids
        g.prune_completed({"a"})
        ready = g.get_ready_tasks()
        ready_ids = {wu.id for wu in ready}
        assert "c" not in ready_ids  # still blocked by b
        g.prune_completed({"b"})
        ready = g.get_ready_tasks()
        ready_ids = {wu.id for wu in ready}
        assert "c" in ready_ids

    def test_empty_graph_ready_tasks(self):
        g = DependencyGraph()
        assert g.get_ready_tasks() == []


# ===========================================================================
# Critical path
# ===========================================================================


class TestCriticalPath:
    def test_linear_chain_is_critical_path(self):
        units = _make_chain(["a", "b", "c"], [10.0, 20.0, 30.0])
        g = build_graph(units)
        path = g.get_critical_path()
        assert path == ["a", "b", "c"]

    def test_diamond_picks_longest_branch(self):
        """A -> B (60min), A -> C (10min), B -> D, C -> D.
        Critical path: A -> B -> D (longer through B).
        """
        a = _wu(id="a", estimated_expected_min=10.0, estimated_optimistic_min=5.0, estimated_pessimistic_min=20.0)
        b = _wu(
            id="b",
            dependency_ids=["a"],
            estimated_expected_min=60.0,
            estimated_optimistic_min=30.0,
            estimated_pessimistic_min=120.0,
        )
        c = _wu(
            id="c",
            dependency_ids=["a"],
            estimated_expected_min=10.0,
            estimated_optimistic_min=5.0,
            estimated_pessimistic_min=20.0,
        )
        d = _wu(
            id="d",
            dependency_ids=["b", "c"],
            estimated_expected_min=10.0,
            estimated_optimistic_min=5.0,
            estimated_pessimistic_min=20.0,
        )
        g = build_graph([a, b, c, d])
        path = g.get_critical_path()
        assert path == ["a", "b", "d"]

    def test_single_task_is_its_own_path(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        path = g.get_critical_path()
        assert path == ["a"]

    def test_empty_graph_empty_path(self):
        g = DependencyGraph()
        assert g.get_critical_path() == []

    def test_is_on_critical_path(self):
        units = _make_chain(["a", "b", "c"])
        g = build_graph(units)
        assert is_on_critical_path(g, "a")
        assert is_on_critical_path(g, "b")
        assert is_on_critical_path(g, "c")

    def test_not_on_critical_path(self):
        """Short branch off the critical path."""
        a = _wu(id="a", estimated_expected_min=10.0, estimated_optimistic_min=5.0, estimated_pessimistic_min=20.0)
        b = _wu(
            id="b",
            dependency_ids=["a"],
            estimated_expected_min=100.0,
            estimated_optimistic_min=50.0,
            estimated_pessimistic_min=200.0,
        )
        c = _wu(
            id="c",
            dependency_ids=["a"],
            estimated_expected_min=5.0,
            estimated_optimistic_min=3.0,
            estimated_pessimistic_min=10.0,
        )
        g = build_graph([a, b, c])
        path = get_critical_path(g)
        assert "b" in path
        assert "c" not in path
        assert not is_on_critical_path(g, "c")

    def test_disconnected_subgraphs(self):
        """Two separate chains — critical path picks the longer one."""
        chain1 = _make_chain(["a1", "a2"], [10.0, 10.0])  # total 20
        chain2 = _make_chain(["b1", "b2", "b3"], [30.0, 30.0, 30.0])  # total 90
        g = build_graph(chain1 + chain2)
        path = g.get_critical_path()
        assert path == ["b1", "b2", "b3"]


# ===========================================================================
# Unlock values
# ===========================================================================


class TestUnlockValues:
    def test_single_task_zero_unlock(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        vals = get_unlock_values(g)
        assert vals["a"] == 0.0

    def test_parent_has_unlock_value(self):
        """A -> B: A should have positive unlock value."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b", dependency_ids=["a"], urgency=0.8, strategic_value=0.6))
        vals = get_unlock_values(g)
        assert vals["a"] > 0.0
        assert vals["b"] == 0.0  # leaf node

    def test_chain_upstream_higher_value(self):
        """A -> B -> C: A should have higher unlock value than B."""
        units = _make_chain(["a", "b", "c"])
        for u in units:
            u.urgency = 0.5
            u.strategic_value = 0.5
        g = build_graph(units)
        vals = get_unlock_values(g)
        assert vals["a"] > vals["b"]
        assert vals["b"] > vals["c"]

    def test_normalized_to_zero_one(self):
        """All values should be between 0 and 1."""
        units = _make_chain(["a", "b", "c", "d"])
        g = build_graph(units)
        vals = get_unlock_values(g)
        for v in vals.values():
            assert 0.0 <= v <= 1.0

    def test_max_value_is_one(self):
        """The highest unlock value should be normalized to 1.0."""
        units = _make_chain(["a", "b", "c"])
        for u in units:
            u.urgency = 0.5
            u.strategic_value = 0.5
        g = build_graph(units)
        vals = get_unlock_values(g)
        assert max(vals.values()) == 1.0

    def test_empty_graph(self):
        g = DependencyGraph()
        assert get_unlock_values(g) == {}

    def test_get_unlock_value_missing_task(self):
        g = DependencyGraph()
        assert g.get_unlock_value("nonexistent") == 0.0

    def test_diamond_parent_accumulates_both_branches(self):
        """A -> B, A -> C: A's unlock value includes both B and C."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b", dependency_ids=["a"], urgency=0.8))
        g.add_task(_wu(id="c", dependency_ids=["a"], urgency=0.6))
        val_a = g.get_unlock_value("a")
        assert val_a > 0.0


# ===========================================================================
# Topological sort
# ===========================================================================


class TestTopologicalSort:
    def test_valid_ordering_linear(self):
        units = _make_chain(["a", "b", "c"])
        g = build_graph(units)
        order = g.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_valid_ordering_diamond(self):
        a = _wu(id="a")
        b = _wu(id="b", dependency_ids=["a"])
        c = _wu(id="c", dependency_ids=["a"])
        d = _wu(id="d", dependency_ids=["b", "c"])
        g = build_graph([a, b, c, d])
        order = g.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_single_node(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        assert g.topological_sort() == ["a"]

    def test_empty_graph(self):
        g = DependencyGraph()
        assert g.topological_sort() == []

    def test_no_deps_all_present(self):
        """All independent tasks appear in topo sort."""
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_task(_wu(id="c"))
        order = g.topological_sort()
        assert set(order) == {"a", "b", "c"}

    def test_returns_all_nodes(self):
        units = _make_chain(["a", "b", "c", "d", "e"])
        g = build_graph(units)
        order = g.topological_sort()
        assert len(order) == 5


# ===========================================================================
# Prune completed
# ===========================================================================


class TestPruneCompleted:
    def test_prune_removes_tasks(self):
        units = _make_chain(["a", "b", "c"])
        g = build_graph(units)
        g.prune_completed({"a"})
        assert g.get_task("a") is None
        assert g.size() == 2

    def test_prune_updates_edges(self):
        units = _make_chain(["a", "b", "c"])
        g = build_graph(units)
        g.prune_completed({"a"})
        assert g.get_dependencies("b") == []

    def test_prune_makes_dependents_ready(self):
        units = _make_chain(["a", "b"])
        g = build_graph(units)
        assert len(g.get_ready_tasks()) == 1
        g.prune_completed({"a"})
        ready = g.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "b"

    def test_prune_multiple(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.add_task(_wu(id="b"))
        g.add_task(_wu(id="c"))
        g.prune_completed({"a", "b"})
        assert g.size() == 1
        assert g.get_task("c") is not None

    def test_prune_nonexistent_is_safe(self):
        g = DependencyGraph()
        g.add_task(_wu(id="a"))
        g.prune_completed({"nonexistent"})
        assert g.size() == 1


# ===========================================================================
# Build graph
# ===========================================================================


class TestBuildGraph:
    def test_from_list(self):
        units = [_wu(id="a"), _wu(id="b"), _wu(id="c")]
        g = build_graph(units)
        assert g.size() == 3

    def test_handles_missing_dependency_ids(self):
        """Tasks with dependency on non-existent ID: edge skipped, no crash."""
        units = [_wu(id="a", dependency_ids=["nonexistent"])]
        g = build_graph(units)
        assert g.size() == 1
        assert g.get_dependencies("a") == []

    def test_empty_list_empty_graph(self):
        g = build_graph([])
        assert g.size() == 0

    def test_wires_existing_deps(self):
        a = _wu(id="a")
        b = _wu(id="b", dependency_ids=["a"])
        g = build_graph([a, b])
        assert g.get_dependents("a") == ["b"]
        assert g.get_dependencies("b") == ["a"]

    def test_skips_empty_id(self):
        units = [_wu(id=""), _wu(id="a")]
        g = build_graph(units)
        assert g.size() == 1

    def test_skips_duplicate_ids(self):
        units = [_wu(id="a", title="first"), _wu(id="a", title="second")]
        g = build_graph(units)
        assert g.size() == 1

    def test_complex_graph(self):
        """Build a more complex graph and verify structure."""
        a = _wu(id="a")
        b = _wu(id="b", dependency_ids=["a"])
        c = _wu(id="c", dependency_ids=["a"])
        d = _wu(id="d", dependency_ids=["b", "c"])
        e = _wu(id="e", dependency_ids=["d"])
        g = build_graph([a, b, c, d, e])
        assert g.size() == 5
        assert set(g.get_dependents("a")) == {"b", "c"}
        assert set(g.get_dependencies("d")) == {"b", "c"}
        assert g.get_dependents("d") == ["e"]


# ===========================================================================
# Probe generation
# ===========================================================================


class TestProbeGeneration:
    def test_low_confidence_generates_probe(self):
        wu = _wu(id="t1", confidence=0.3)
        probe = generate_probe(wu)
        assert probe is not None
        assert isinstance(probe, ProbeTask)
        assert probe.original_task_id == "t1"
        assert probe.probe_id == "probe_t1"

    def test_high_pessimistic_ratio_generates_probe(self):
        wu = _wu(
            id="t1",
            confidence=0.6,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=120.0,
            estimated_optimistic_min=10.0,
        )
        probe = generate_probe(wu)
        assert probe is not None

    def test_high_confidence_no_probe(self):
        wu = _wu(
            id="t1",
            confidence=0.8,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=50.0,
            estimated_optimistic_min=20.0,
        )
        probe = generate_probe(wu)
        assert probe is None

    def test_probe_type_debugging(self):
        wu = _wu(id="t1", confidence=0.2, work_mode=WorkMode.DEBUGGING)
        probe = generate_probe(wu)
        assert probe is not None
        assert probe.probe_type == "reproduce"

    def test_probe_type_deployment(self):
        wu = _wu(id="t1", confidence=0.2, work_mode=WorkMode.DEPLOYMENT)
        probe = generate_probe(wu)
        assert probe is not None
        assert probe.probe_type == "test_access"

    def test_probe_type_research(self):
        wu = _wu(id="t1", confidence=0.2, work_mode=WorkMode.RESEARCH)
        probe = generate_probe(wu)
        assert probe is not None
        assert probe.probe_type == "validate"

    def test_probe_type_default(self):
        wu = _wu(id="t1", confidence=0.2, work_mode=WorkMode.ADMIN)
        probe = generate_probe(wu)
        assert probe is not None
        assert probe.probe_type == "inspect"

    def test_probe_duration_bounded(self):
        """Probe duration should be between 10 and 20 minutes."""
        # Small task
        wu_small = _wu(
            id="t1",
            confidence=0.2,
            estimated_expected_min=5.0,
            estimated_optimistic_min=3.0,
            estimated_pessimistic_min=10.0,
        )
        probe = generate_probe(wu_small)
        assert probe is not None
        assert 10.0 <= probe.estimated_duration_min <= 20.0

        # Large task
        wu_large = _wu(
            id="t2",
            confidence=0.2,
            estimated_expected_min=200.0,
            estimated_optimistic_min=100.0,
            estimated_pessimistic_min=400.0,
        )
        probe = generate_probe(wu_large)
        assert probe is not None
        assert 10.0 <= probe.estimated_duration_min <= 20.0

    def test_probe_work_mode_is_analysis(self):
        wu = _wu(id="t1", confidence=0.2, work_mode=WorkMode.DEEP_IMPLEMENTATION)
        probe = generate_probe(wu)
        assert probe is not None
        assert probe.work_mode == WorkMode.ANALYSIS

    def test_probe_has_done_condition(self):
        wu = _wu(id="t1", confidence=0.2, title="Fix the widget")
        probe = generate_probe(wu)
        assert probe is not None
        assert len(probe.done_condition) > 0
        assert "Fix the widget" in probe.done_condition

    def test_borderline_confidence_no_probe(self):
        """Confidence exactly 0.5 should not generate probe."""
        wu = _wu(
            id="t1",
            confidence=0.5,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=50.0,
            estimated_optimistic_min=20.0,
        )
        probe = generate_probe(wu)
        assert probe is None

    def test_borderline_ratio_generates_probe(self):
        """Pessimistic exactly 2x expected should generate probe."""
        wu = _wu(
            id="t1",
            confidence=0.6,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=60.0,
            estimated_optimistic_min=20.0,
        )
        probe = generate_probe(wu)
        assert probe is not None


# ===========================================================================
# Epic decomposition
# ===========================================================================


class TestDecomposition:
    def test_epic_generates_suggestion(self):
        wu = _wu(
            id="epic1",
            title="Big Feature",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        assert isinstance(result, DecompositionSuggestion)
        assert result.original_task_id == "epic1"

    def test_non_epic_returns_none(self):
        wu = _wu(id="t1", task_class=TaskClass.BOUNDED)
        assert suggest_decomposition(wu) is None

    def test_atomic_returns_none(self):
        wu = _wu(id="t1", task_class=TaskClass.ATOMIC)
        assert suggest_decomposition(wu) is None

    def test_subtasks_count_3_to_5(self):
        for mode in [WorkMode.DEEP_IMPLEMENTATION, WorkMode.RESEARCH, WorkMode.DEBUGGING, WorkMode.ADMIN]:
            wu = _wu(
                id="epic",
                task_class=TaskClass.EPIC,
                work_mode=mode,
                estimated_expected_min=120.0,
                estimated_optimistic_min=60.0,
                estimated_pessimistic_min=240.0,
            )
            result = suggest_decomposition(wu)
            assert result is not None
            assert 3 <= len(result.suggested_subtasks) <= 5, (
                f"mode={mode} got {len(result.suggested_subtasks)} subtasks"
            )

    def test_subtasks_sum_roughly_original_duration(self):
        wu = _wu(
            id="epic1",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            estimated_expected_min=100.0,
            estimated_optimistic_min=50.0,
            estimated_pessimistic_min=200.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        total = sum(st["estimated_min"] for st in result.suggested_subtasks)
        # Should be close to 120 (100 * 1.2 overhead)
        assert 100.0 <= total <= 140.0

    def test_strategy_deep_implementation(self):
        wu = _wu(
            id="epic1",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        assert result.decomposition_strategy == "split_by_component"

    def test_strategy_research(self):
        wu = _wu(
            id="epic1",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.RESEARCH,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        assert result.decomposition_strategy == "split_by_phase"

    def test_strategy_debugging(self):
        wu = _wu(
            id="epic1",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.DEBUGGING,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        assert result.decomposition_strategy == "split_by_phase"

    def test_strategy_default(self):
        wu = _wu(
            id="epic1",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.ADMIN,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        assert result.decomposition_strategy == "split_evenly"

    def test_subtasks_have_required_keys(self):
        wu = _wu(
            id="epic1",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        for st in result.suggested_subtasks:
            assert "title" in st
            assert "estimated_min" in st
            assert "work_mode" in st
            assert isinstance(st["estimated_min"], float)

    def test_subtask_titles_include_original(self):
        wu = _wu(
            id="epic1",
            title="Build the widget",
            task_class=TaskClass.EPIC,
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            estimated_expected_min=120.0,
            estimated_optimistic_min=60.0,
            estimated_pessimistic_min=240.0,
        )
        result = suggest_decomposition(wu)
        assert result is not None
        for st in result.suggested_subtasks:
            assert "Build the widget" in st["title"]


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_empty_graph_operations(self):
        g = DependencyGraph()
        assert g.size() == 0
        assert g.get_ready_tasks() == []
        assert g.topological_sort() == []
        assert g.get_critical_path() == []
        assert get_unlock_values(g) == {}

    def test_single_node_graph(self):
        g = DependencyGraph()
        g.add_task(_wu(id="solo"))
        assert g.size() == 1
        assert len(g.get_ready_tasks()) == 1
        assert g.topological_sort() == ["solo"]
        assert g.get_critical_path() == ["solo"]
        vals = get_unlock_values(g)
        assert vals["solo"] == 0.0

    def test_large_graph_performance(self):
        """100 nodes, linear chain — operations should complete in <1s."""
        ids = [f"t{i:03d}" for i in range(100)]
        durations = [10.0] * 100
        units = _make_chain(ids, durations)

        start = time.time()
        g = build_graph(units)
        _ = g.topological_sort()
        _ = g.get_critical_path()
        _ = get_unlock_values(g)
        _ = g.get_ready_tasks()
        elapsed = time.time() - start

        assert g.size() == 100
        assert elapsed < 1.0, f"Operations took {elapsed:.3f}s (limit: 1.0s)"

    def test_disconnected_subgraphs(self):
        """Two independent chains should both be handled correctly."""
        chain1 = _make_chain(["a1", "a2", "a3"])
        chain2 = _make_chain(["b1", "b2"])
        g = build_graph(chain1 + chain2)
        assert g.size() == 5

        ready = g.get_ready_tasks()
        ready_ids = {wu.id for wu in ready}
        assert ready_ids == {"a1", "b1"}

        order = g.topological_sort()
        assert order.index("a1") < order.index("a2")
        assert order.index("a2") < order.index("a3")
        assert order.index("b1") < order.index("b2")

    def test_wide_graph_many_dependents(self):
        """One root with 20 children — all children ready after root pruned."""
        root = _wu(id="root")
        children = [_wu(id=f"c{i}", dependency_ids=["root"]) for i in range(20)]
        g = build_graph([root] + children)
        assert g.size() == 21
        assert len(g.get_ready_tasks()) == 1  # only root

        g.prune_completed({"root"})
        assert g.size() == 20
        assert len(g.get_ready_tasks()) == 20

    def test_remove_middle_of_chain(self):
        """Removing a middle node breaks the chain."""
        units = _make_chain(["a", "b", "c"])
        g = build_graph(units)
        g.remove_task("b")
        assert g.size() == 2
        assert g.get_dependents("a") == []
        assert g.get_dependencies("c") == []

    def test_get_dependents_for_missing_task(self):
        g = DependencyGraph()
        assert g.get_dependents("nonexistent") == []

    def test_get_dependencies_for_missing_task(self):
        g = DependencyGraph()
        assert g.get_dependencies("nonexistent") == []


# ===========================================================================
# Integration: import from scheduler package
# ===========================================================================


class TestPackageExports:
    def test_dependency_graph_importable(self):
        from utils.scheduler import DependencyGraph as DG

        assert DG is DependencyGraph

    def test_build_graph_importable(self):
        from utils.scheduler import build_graph as bg

        assert bg is build_graph

    def test_get_unlock_values_importable(self):
        from utils.scheduler import get_unlock_values as guv

        assert guv is get_unlock_values

    def test_get_critical_path_importable(self):
        from utils.scheduler import get_critical_path as gcp

        assert gcp is get_critical_path

    def test_is_on_critical_path_importable(self):
        from utils.scheduler import is_on_critical_path as iocp

        assert iocp is is_on_critical_path

    def test_probe_task_importable(self):
        from utils.scheduler import ProbeTask as PT

        assert PT is ProbeTask

    def test_generate_probe_importable(self):
        from utils.scheduler import generate_probe as gp

        assert gp is generate_probe

    def test_decomposition_suggestion_importable(self):
        from utils.scheduler import DecompositionSuggestion as DS

        assert DS is DecompositionSuggestion

    def test_suggest_decomposition_importable(self):
        from utils.scheduler import suggest_decomposition as sd

        assert sd is suggest_decomposition
