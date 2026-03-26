"""Comprehensive tests for Phase 3: Goal Decomposition Engine."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from novatrade.autonomy.decomposition.engine import GoalDecomposer
from novatrade.autonomy.decomposition.models import (
    GoalDecomposition,
    SubGoal,
    SubGoalStatus,
)
from novatrade.autonomy.decomposition.novatrade_tree import (
    GOAL_ID,
    build_novatrade_tree,
)
from novatrade.autonomy.schemas import DimensionScore, ProgressReport, ScoreTrend, SubMetric

# =====================================================================
# Helpers
# =====================================================================


def make_subgoal(
    sub_goal_id: str = "sg_test",
    dimension: str = "system_health",
    status: SubGoalStatus = SubGoalStatus.NOT_STARTED,
    progress: float = 0.0,
    dependencies: list[str] | None = None,
    priority: int = 5,
) -> SubGoal:
    return SubGoal(
        sub_goal_id=sub_goal_id,
        parent_goal_id="test_goal",
        dimension=dimension,
        title=f"Test: {sub_goal_id}",
        status=status,
        progress=progress,
        dependencies=dependencies or [],
        priority=priority,
    )


def make_decomposition(
    sub_goals: list[SubGoal] | None = None,
    edges: list[tuple[str, str]] | None = None,
) -> GoalDecomposition:
    sgs = sub_goals or [make_subgoal("sg_a"), make_subgoal("sg_b")]
    return GoalDecomposition(
        goal_id="test_goal",
        goal_description="Test goal",
        sub_goals=sgs,
        dag_edges=edges or [],
    )


def make_report(dims: dict[str, float]) -> ProgressReport:
    dimensions = {}
    trends = {}
    for name, score in dims.items():
        dimensions[name] = DimensionScore(
            name=name,
            score=score,
            sub_metrics=[SubMetric(name="m", value=score)],
        )
        trends[name] = ScoreTrend(current=score)
    overall = sum(dims.values()) / len(dims) if dims else 0.0
    return ProgressReport(
        overall_score=min(100.0, max(0.0, overall)),
        dimensions=dimensions,
        trends=trends,
    )


# =====================================================================
# SubGoal model tests
# =====================================================================


def test_subgoal_defaults():
    sg = make_subgoal()
    assert sg.status == SubGoalStatus.NOT_STARTED
    assert sg.progress == 0.0
    assert sg.completed_at is None


def test_subgoal_auto_complete():
    """Progress >= 1.0 auto-sets status to COMPLETED."""
    sg = make_subgoal(progress=1.0)
    assert sg.status == SubGoalStatus.COMPLETED
    assert sg.completed_at is not None


def test_subgoal_progress_bounds():
    with pytest.raises(ValidationError):
        make_subgoal(progress=-0.1)
    with pytest.raises(ValidationError):
        make_subgoal(progress=1.1)


def test_subgoal_priority_bounds():
    with pytest.raises(ValidationError):
        make_subgoal(priority=0)
    with pytest.raises(ValidationError):
        make_subgoal(priority=11)


def test_subgoal_serialization():
    sg = make_subgoal()
    data = json.loads(sg.model_dump_json())
    assert data["sub_goal_id"] == "sg_test"
    assert data["status"] == "not_started"
    # Round-trip
    sg2 = SubGoal.model_validate(data)
    assert sg2.sub_goal_id == sg.sub_goal_id


# =====================================================================
# GoalDecomposition model tests
# =====================================================================


def test_decomposition_basic():
    d = make_decomposition()
    assert len(d.sub_goals) == 2
    assert d.version == 1


def test_decomposition_completion_pct():
    sgs = [make_subgoal("sg_a", progress=0.5), make_subgoal("sg_b", progress=1.0)]
    d = make_decomposition(sub_goals=sgs)
    assert d.completion_pct == pytest.approx(0.75)


def test_decomposition_completion_empty():
    d = GoalDecomposition(goal_id="empty", goal_description="empty", sub_goals=[])
    assert d.completion_pct == 0.0


def test_decomposition_get_subgoal():
    d = make_decomposition()
    assert d.get_subgoal("sg_a") is not None
    assert d.get_subgoal("sg_nonexistent") is None


def test_decomposition_dag_validation():
    """Edges must reference existing sub-goals."""
    with pytest.raises(ValidationError):
        make_decomposition(edges=[("sg_a", "sg_nonexistent")])


def test_decomposition_self_loop():
    """Self-loops are rejected."""
    with pytest.raises(ValidationError):
        make_decomposition(edges=[("sg_a", "sg_a")])


def test_decomposition_valid_edges():
    d = make_decomposition(edges=[("sg_a", "sg_b")])
    assert len(d.dag_edges) == 1


def test_decomposition_serialization_roundtrip():
    d = make_decomposition(edges=[("sg_a", "sg_b")])
    json_str = d.model_dump_json()
    d2 = GoalDecomposition.model_validate_json(json_str)
    assert d2.goal_id == d.goal_id
    assert len(d2.sub_goals) == len(d.sub_goals)


# =====================================================================
# NovaTrade tree tests
# =====================================================================


def test_novatrade_tree_builds():
    tree = build_novatrade_tree()
    assert tree.goal_id == GOAL_ID
    assert len(tree.sub_goals) == 17


def test_novatrade_tree_all_dimensions():
    tree = build_novatrade_tree()
    dims = {sg.dimension for sg in tree.sub_goals}
    assert "system_health" in dims
    assert "execution_pipeline" in dims
    assert "strategy_validity" in dims
    assert "risk_engine" in dims
    assert "performance_stability" in dims


def test_novatrade_tree_unique_ids():
    tree = build_novatrade_tree()
    ids = [sg.sub_goal_id for sg in tree.sub_goals]
    assert len(ids) == len(set(ids))


def test_novatrade_tree_dependencies_valid():
    """All dependency IDs refer to existing sub-goals."""
    tree = build_novatrade_tree()
    valid_ids = {sg.sub_goal_id for sg in tree.sub_goals}
    for sg in tree.sub_goals:
        for dep in sg.dependencies:
            assert dep in valid_ids, f"{sg.sub_goal_id} depends on nonexistent {dep}"


def test_novatrade_tree_has_dag_edges():
    tree = build_novatrade_tree()
    assert len(tree.dag_edges) > 0


def test_novatrade_tree_no_cycle():
    tree = build_novatrade_tree()
    decomposer = GoalDecomposer()
    assert not decomposer.has_cycle(tree)


# =====================================================================
# GoalDecomposer — progress update tests
# =====================================================================


def test_update_progress_green_dims():
    """Score >= 70 → progress 1.0."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", dimension="system_health"),
            make_subgoal("sg_b", dimension="execution_pipeline"),
        ]
    )
    report = make_report({"system_health": 85.0, "execution_pipeline": 90.0})
    updated = decomposer.update_progress(tree, report)
    for sg in updated.sub_goals:
        assert sg.progress == 1.0
        assert sg.status == SubGoalStatus.COMPLETED


def test_update_progress_yellow_dims():
    """Score 40-70 → proportional progress."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", dimension="system_health"),
        ]
    )
    report = make_report({"system_health": 55.0})
    updated = decomposer.update_progress(tree, report)
    assert updated.sub_goals[0].progress == pytest.approx(0.5, abs=0.01)
    assert updated.sub_goals[0].status == SubGoalStatus.IN_PROGRESS


def test_update_progress_red_dims():
    """Score < 40 → progress 0.0."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", dimension="system_health"),
        ]
    )
    report = make_report({"system_health": 20.0})
    updated = decomposer.update_progress(tree, report)
    assert updated.sub_goals[0].progress == 0.0


def test_update_progress_skips_completed():
    """Don't override manually completed sub-goals."""
    decomposer = GoalDecomposer()
    sg = make_subgoal("sg_a", dimension="system_health", status=SubGoalStatus.COMPLETED, progress=1.0)
    tree = make_decomposition(sub_goals=[sg])
    report = make_report({"system_health": 20.0})
    updated = decomposer.update_progress(tree, report)
    assert updated.sub_goals[0].status == SubGoalStatus.COMPLETED
    assert updated.sub_goals[0].progress == 1.0


def test_update_progress_missing_dimension():
    """Sub-goal for unknown dimension is unchanged."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", dimension="unknown_dim"),
        ]
    )
    report = make_report({"system_health": 85.0})
    updated = decomposer.update_progress(tree, report)
    assert updated.sub_goals[0].progress == 0.0


# =====================================================================
# GoalDecomposer — actionable sub-goals tests
# =====================================================================


def test_actionable_no_deps():
    """Sub-goals with no deps are actionable."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", priority=2),
            make_subgoal("sg_b", priority=1),
        ]
    )
    actionable = decomposer.get_actionable_subgoals(tree)
    assert len(actionable) == 2
    assert actionable[0].sub_goal_id == "sg_b"  # priority 1 first


def test_actionable_deps_not_met():
    """Sub-goal with unmet deps is NOT actionable."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a"),
            make_subgoal("sg_b", dependencies=["sg_a"]),
        ],
        edges=[("sg_a", "sg_b")],
    )
    actionable = decomposer.get_actionable_subgoals(tree)
    assert len(actionable) == 1
    assert actionable[0].sub_goal_id == "sg_a"


def test_actionable_deps_met():
    """Sub-goal with completed deps is actionable."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", status=SubGoalStatus.COMPLETED, progress=1.0),
            make_subgoal("sg_b", dependencies=["sg_a"]),
        ],
        edges=[("sg_a", "sg_b")],
    )
    actionable = decomposer.get_actionable_subgoals(tree)
    assert len(actionable) == 1
    assert actionable[0].sub_goal_id == "sg_b"


def test_actionable_skipped_dep():
    """Skipped dependency counts as met."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", status=SubGoalStatus.SKIPPED),
            make_subgoal("sg_b", dependencies=["sg_a"]),
        ],
        edges=[("sg_a", "sg_b")],
    )
    actionable = decomposer.get_actionable_subgoals(tree)
    assert len(actionable) == 1


def test_actionable_excludes_completed():
    """Completed sub-goals are not returned as actionable."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", status=SubGoalStatus.COMPLETED, progress=1.0),
        ]
    )
    assert decomposer.get_actionable_subgoals(tree) == []


# =====================================================================
# GoalDecomposer — blocked sub-goals tests
# =====================================================================


def test_blocked_subgoals():
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a"),
            make_subgoal("sg_b", dependencies=["sg_a"]),
        ],
        edges=[("sg_a", "sg_b")],
    )
    blocked = decomposer.get_blocked_subgoals(tree)
    assert len(blocked) == 1
    sg, unmet = blocked[0]
    assert sg.sub_goal_id == "sg_b"
    assert unmet == ["sg_a"]


def test_no_blocked_when_deps_met():
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a", status=SubGoalStatus.COMPLETED, progress=1.0),
            make_subgoal("sg_b", dependencies=["sg_a"]),
        ],
        edges=[("sg_a", "sg_b")],
    )
    blocked = decomposer.get_blocked_subgoals(tree)
    assert len(blocked) == 0


# =====================================================================
# GoalDecomposer — topological sort tests
# =====================================================================


def test_topological_sort_simple():
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a"),
            make_subgoal("sg_b", dependencies=["sg_a"]),
            make_subgoal("sg_c", dependencies=["sg_b"]),
        ],
        edges=[("sg_a", "sg_b"), ("sg_b", "sg_c")],
    )
    order = decomposer.topological_sort(tree)
    ids = [sg.sub_goal_id for sg in order]
    assert ids.index("sg_a") < ids.index("sg_b")
    assert ids.index("sg_b") < ids.index("sg_c")


def test_topological_sort_parallel():
    """Independent sub-goals can appear in any order."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a"),
            make_subgoal("sg_b"),
        ]
    )
    order = decomposer.topological_sort(tree)
    assert len(order) == 2


def test_topological_sort_diamond():
    """Diamond dependency: A → B, A → C, B → D, C → D."""
    decomposer = GoalDecomposer()
    tree = make_decomposition(
        sub_goals=[
            make_subgoal("sg_a"),
            make_subgoal("sg_b", dependencies=["sg_a"]),
            make_subgoal("sg_c", dependencies=["sg_a"]),
            make_subgoal("sg_d", dependencies=["sg_b", "sg_c"]),
        ],
        edges=[("sg_a", "sg_b"), ("sg_a", "sg_c"), ("sg_b", "sg_d"), ("sg_c", "sg_d")],
    )
    order = decomposer.topological_sort(tree)
    ids = [sg.sub_goal_id for sg in order]
    assert ids.index("sg_a") < ids.index("sg_b")
    assert ids.index("sg_a") < ids.index("sg_c")
    assert ids.index("sg_b") < ids.index("sg_d")
    assert ids.index("sg_c") < ids.index("sg_d")


def test_has_cycle_false():
    decomposer = GoalDecomposer()
    tree = make_decomposition(edges=[("sg_a", "sg_b")])
    assert not decomposer.has_cycle(tree)


def test_novatrade_topological_sort():
    """NovaTrade tree produces valid topological order."""
    decomposer = GoalDecomposer()
    tree = build_novatrade_tree()
    order = decomposer.topological_sort(tree)
    assert len(order) == len(tree.sub_goals)


# =====================================================================
# GoalDecomposer — persistence tests
# =====================================================================


def test_persist_and_load(tmp_path):
    decomposer = GoalDecomposer(base_path=str(tmp_path))
    tree = build_novatrade_tree()
    decomposer.persist(tree)

    loaded = decomposer.load(GOAL_ID)
    assert loaded is not None
    assert loaded.goal_id == GOAL_ID
    assert len(loaded.sub_goals) == 17


def test_load_nonexistent(tmp_path):
    decomposer = GoalDecomposer(base_path=str(tmp_path))
    assert decomposer.load("nonexistent") is None


def test_persist_creates_dir(tmp_path):
    decomposer = GoalDecomposer(base_path=str(tmp_path))
    tree = make_decomposition()
    decomposer.persist(tree)
    assert (tmp_path / "STATE" / "goal_trees").is_dir()


def test_persist_overwrites(tmp_path):
    decomposer = GoalDecomposer(base_path=str(tmp_path))
    tree = make_decomposition()
    decomposer.persist(tree)
    tree.version = 2
    decomposer.persist(tree)
    loaded = decomposer.load("test_goal")
    assert loaded is not None
    assert loaded.version == 2
