"""Tests for Phase 4: Rolling-Horizon Replanner.

60+ tests covering BlockState, replan triggers, execute_replan,
lifecycle hooks, three-horizon model, state persistence, and edge cases.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from utils.scheduler.block import BlockPlan, ScheduledSlot
from utils.scheduler.replanner import (
    BlockState,
    ReplanDecision,
    ReplannerConfig,
    ReplanReason,
    check_replan_triggers,
    execute_replan,
    get_execution_horizon,
    get_strategic_horizon,
    get_tactical_horizon,
    load_block_state,
    on_task_complete,
    on_task_fail,
    on_task_progress,
    on_task_start,
    save_block_state,
)
from utils.scheduler.scorer import ScoredTask, score_task
from utils.scheduler.work_unit import CommitmentLevel, WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 29, 12, 0, 0, tzinfo=timezone.utc)


def _make_work_unit(
    id: str = "task_1",
    title: str = "Test Task",
    estimated_expected_min: float = 30.0,
    estimated_optimistic_min: float = 20.0,
    estimated_pessimistic_min: float = 45.0,
    priority: str = "medium",
    commitment_level: CommitmentLevel = CommitmentLevel.SOFT,
    work_mode: WorkMode = WorkMode.ADMIN,
    dependency_ids: list[str] | None = None,
    **kwargs,
) -> WorkUnit:
    return WorkUnit(
        id=id,
        title=title,
        estimated_expected_min=estimated_expected_min,
        estimated_optimistic_min=estimated_optimistic_min,
        estimated_pessimistic_min=estimated_pessimistic_min,
        priority=priority,
        commitment_level=commitment_level,
        work_mode=work_mode,
        dependency_ids=dependency_ids or [],
        **kwargs,
    )


def _make_scored(work_unit: WorkUnit | None = None, **wu_kwargs) -> ScoredTask:
    wu = work_unit or _make_work_unit(**wu_kwargs)
    return score_task(wu)


def _make_slot(
    scored: ScoredTask,
    start: float = 0.0,
    end: float = 30.0,
) -> ScheduledSlot:
    return ScheduledSlot(
        scored_task=scored,
        start_offset_min=start,
        end_offset_min=end,
    )


def _make_block_plan(
    slots: list[ScheduledSlot] | None = None,
    duration: float = 480.0,
    buffer_min: float = 81.6,
) -> BlockPlan:
    return BlockPlan(
        block_duration_min=duration,
        scheduled_slots=slots or [],
        total_buffer_min=buffer_min,
        total_committed_min=duration * 0.75,
        total_filler_min=duration * 0.08,
    )


def _make_state(
    block_id: str = "block_001",
    plan: BlockPlan | None = None,
    elapsed_min: float = 0.0,
    **kwargs,
) -> BlockState:
    p = plan or _make_block_plan()
    return BlockState(
        block_id=block_id,
        original_plan=p,
        block_start_utc=_NOW,
        block_end_utc=_NOW + timedelta(minutes=p.block_duration_min),
        elapsed_min=elapsed_min,
        **kwargs,
    )


def _enabled_config(**overrides) -> ReplannerConfig:
    defaults: dict[str, Any] = {"enabled": True, "replan_cooldown_min": 0.0}
    defaults.update(overrides)
    return ReplannerConfig(**defaults)


# ===================================================================
# BlockState tests
# ===================================================================


class TestBlockStateCreation:
    """BlockState creation and defaults."""

    def test_create_with_defaults(self):
        state = _make_state()
        assert state.block_id == "block_001"
        assert state.completed_task_ids == []
        assert state.failed_task_ids == []
        assert state.in_progress_task_id is None
        assert state.actual_durations == {}
        assert state.elapsed_min == 0.0
        assert state.replan_count == 0
        assert state.last_replan_at is None

    def test_remaining_min_computation(self):
        state = _make_state(elapsed_min=120.0)
        assert state.remaining_min == pytest.approx(360.0, abs=0.1)

    def test_remaining_min_zero_when_elapsed_exceeds_block(self):
        state = _make_state(elapsed_min=500.0)
        assert state.remaining_min == 0.0

    def test_remaining_min_full_block(self):
        state = _make_state(elapsed_min=0.0)
        assert state.remaining_min == pytest.approx(480.0, abs=0.1)


class TestBlockStateMethods:
    """BlockState helper methods."""

    def test_remaining_scheduled_returns_uncompleted_slots(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        s3 = _make_scored(id="t3")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
                _make_slot(s3, 60, 90),
            ]
        )
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        remaining = state.remaining_scheduled()
        remaining_ids = [s.scored_task.work_unit.id for s in remaining]
        assert remaining_ids == ["t2", "t3"]

    def test_remaining_scheduled_excludes_failed(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan, failed_task_ids=["t1"])
        remaining = state.remaining_scheduled()
        assert len(remaining) == 1
        assert remaining[0].scored_task.work_unit.id == "t2"

    def test_mark_complete_updates_state(self):
        state = _make_state()
        state.mark_started("t1")
        assert state.in_progress_task_id == "t1"

        state.mark_complete("t1", 25.0)
        assert "t1" in state.completed_task_ids
        assert state.actual_durations["t1"] == 25.0
        assert state.in_progress_task_id is None

    def test_mark_complete_idempotent(self):
        state = _make_state()
        state.mark_complete("t1", 25.0)
        state.mark_complete("t1", 30.0)
        assert state.completed_task_ids.count("t1") == 1
        assert state.actual_durations["t1"] == 30.0  # updated

    def test_mark_failed_updates_state(self):
        state = _make_state()
        state.mark_started("t1")
        state.mark_failed("t1", "timeout")
        assert "t1" in state.failed_task_ids
        assert state.in_progress_task_id is None

    def test_mark_failed_idempotent(self):
        state = _make_state()
        state.mark_failed("t1", "error")
        state.mark_failed("t1", "retry_error")
        assert state.failed_task_ids.count("t1") == 1

    def test_mark_started_sets_in_progress(self):
        state = _make_state()
        state.mark_started("t1")
        assert state.in_progress_task_id == "t1"

    def test_mark_started_replaces_previous(self):
        state = _make_state()
        state.mark_started("t1")
        state.mark_started("t2")
        assert state.in_progress_task_id == "t2"

    def test_is_task_done_completed(self):
        state = _make_state()
        state.mark_complete("t1", 10.0)
        assert state.is_task_done("t1") is True

    def test_is_task_done_failed(self):
        state = _make_state()
        state.mark_failed("t1")
        assert state.is_task_done("t1") is True

    def test_is_task_done_not_done(self):
        state = _make_state()
        assert state.is_task_done("t1") is False

    def test_active_plan_returns_original_when_no_current(self):
        state = _make_state()
        assert state.active_plan is state.original_plan

    def test_active_plan_returns_current_when_set(self):
        new_plan = _make_block_plan(duration=240.0)
        state = _make_state(current_plan=new_plan)
        assert state.active_plan is new_plan
        assert state.active_plan.block_duration_min == 240.0


# ===================================================================
# ReplannerConfig tests
# ===================================================================


class TestReplannerConfig:
    """ReplannerConfig creation and validation."""

    def test_defaults(self):
        cfg = ReplannerConfig()
        assert cfg.enabled is True
        assert cfg.replan_cooldown_min == 10.0
        assert cfg.early_completion_threshold_pct == 20.0
        assert cfg.overrun_threshold_pct == 25.0
        assert cfg.buffer_health_min_pct == 15.0
        assert cfg.max_replans_per_block == 5

    def test_custom_values(self):
        cfg = ReplannerConfig(
            enabled=True,
            replan_cooldown_min=5.0,
            max_replans_per_block=10,
        )
        assert cfg.enabled is True
        assert cfg.replan_cooldown_min == 5.0
        assert cfg.max_replans_per_block == 10


# ===================================================================
# ReplanDecision tests
# ===================================================================


class TestReplanDecision:
    """ReplanDecision model."""

    def test_no_replan(self):
        d = ReplanDecision(should_replan=False)
        assert d.should_replan is False
        assert d.reason is None
        assert d.new_plan is None
        assert d.deferred_from_old == []
        assert d.newly_scheduled == []

    def test_with_reason(self):
        d = ReplanDecision(
            should_replan=True,
            reason=ReplanReason.OVERRUN,
            reason_detail="task overran",
        )
        assert d.should_replan is True
        assert d.reason == ReplanReason.OVERRUN
        assert d.reason_detail == "task overran"


# ===================================================================
# Replan trigger tests
# ===================================================================


class TestReplanTriggerGuards:
    """Guard conditions that prevent replanning."""

    def test_feature_flag_disabled(self):
        state = _make_state()
        cfg = ReplannerConfig(enabled=False)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_cooldown_not_elapsed(self):
        state = _make_state(
            last_replan_at=_NOW - timedelta(minutes=2),
            replan_count=1,
        )
        cfg = _enabled_config(replan_cooldown_min=10.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_cooldown_elapsed_allows_replan(self):
        # Build a state that would trigger early completion
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(
            plan=plan,
            completed_task_ids=["t1"],
            last_replan_at=_NOW - timedelta(minutes=15),
            replan_count=1,
        )
        state.actual_durations["t1"] = 10.0  # 67% early
        cfg = _enabled_config(replan_cooldown_min=10.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True

    def test_max_replans_reached(self):
        state = _make_state(replan_count=5)
        cfg = _enabled_config(max_replans_per_block=5)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_max_replans_not_reached_allows_check(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(
            plan=plan,
            replan_count=4,
            completed_task_ids=["t1"],
        )
        state.actual_durations["t1"] = 5.0  # very early
        cfg = _enabled_config(max_replans_per_block=5)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True


class TestEarlyCompletionTrigger:
    """Early completion trigger."""

    def test_early_completion_triggers_replan(self):
        """Task finishes >20% early."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        state.actual_durations["t1"] = 20.0  # 33% early (>20% threshold)

        cfg = _enabled_config(early_completion_threshold_pct=20.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.EARLY_COMPLETION

    def test_early_completion_within_threshold_no_replan(self):
        """Task finishes only 10% early — within 20% threshold."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        state.actual_durations["t1"] = 27.0  # only 10% early

        cfg = _enabled_config(early_completion_threshold_pct=20.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_early_completion_exact_threshold_no_replan(self):
        """Task finishes exactly at the threshold boundary — not strictly early enough."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        # 30 * (1 - 0.20) = 24.0 — right at threshold, not strictly less
        state.actual_durations["t1"] = 24.0

        cfg = _enabled_config(early_completion_threshold_pct=20.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False


class TestOverrunTrigger:
    """Overrun detection trigger."""

    def test_overrun_triggers_replan(self):
        """In-progress task exceeds estimate by >25%."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, in_progress_task_id="t1")
        state.actual_durations["t1"] = 40.0  # 33% over (>25%)

        cfg = _enabled_config(overrun_threshold_pct=25.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.OVERRUN

    def test_overrun_within_threshold_no_replan(self):
        """In-progress task only 15% over — within 25% threshold."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, in_progress_task_id="t1")
        state.actual_durations["t1"] = 34.5  # 15% over

        cfg = _enabled_config(overrun_threshold_pct=25.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_overrun_no_actual_duration_tracked(self):
        """In-progress task without actual duration in map — no trigger."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, in_progress_task_id="t1")
        # No actual_durations entry for t1

        cfg = _enabled_config(overrun_threshold_pct=25.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False


class TestDependencyFailureTrigger:
    """Dependency failure trigger."""

    def test_dependency_failure_triggers_replan(self):
        """Remaining task depends on a failed task."""
        wu_dep = _make_work_unit(id="t2", dependency_ids=["t1"])
        s1 = _make_scored(id="t1")
        s2 = _make_scored(work_unit=wu_dep)
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan, failed_task_ids=["t1"])

        cfg = _enabled_config()
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.DEPENDENCY_FAILURE

    def test_dependency_failure_no_dependent_remaining(self):
        """Failed task but no remaining tasks depend on it."""
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan, failed_task_ids=["t1"], completed_task_ids=["t2"])
        # t2 is completed on-time so no early completion trigger
        state.actual_durations["t2"] = 30.0
        # t2 is completed, so remaining is empty — no dependency failure
        cfg = _enabled_config()
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_dependency_failure_unrelated_task_failed(self):
        """Failed task, remaining tasks have no dependency on it."""
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")  # no dependency_ids
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan, failed_task_ids=["t1"])

        cfg = _enabled_config()
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False


class TestBufferHealthTrigger:
    """Buffer health trigger."""

    def test_buffer_health_low_triggers_replan(self):
        """Cumulative overruns erode buffer below threshold."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[_make_slot(s1, 0, 30), _make_slot(s2, 30, 60)],
            buffer_min=20.0,
        )
        state = _make_state(plan=plan, completed_task_ids=["t1", "t2"])
        # t1 overran by 10, t2 overran by 10 -> 20 min buffer used out of 20 = 0% health
        state.actual_durations["t1"] = 40.0
        state.actual_durations["t2"] = 40.0

        cfg = _enabled_config(buffer_health_min_pct=15.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.BUFFER_HEALTH_LOW

    def test_buffer_health_ok_no_replan(self):
        """Buffer still healthy — no trigger."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[_make_slot(s1, 0, 30)],
            buffer_min=100.0,
        )
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        state.actual_durations["t1"] = 32.0  # only 2 min overrun on 100 min buffer

        cfg = _enabled_config(buffer_health_min_pct=15.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_buffer_zero_no_division_error(self):
        """Buffer is zero — no trigger (division guard)."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[_make_slot(s1, 0, 30)],
            buffer_min=0.0,
        )
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        state.actual_durations["t1"] = 40.0

        cfg = _enabled_config(buffer_health_min_pct=15.0)
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False  # buffer_total is 0, guard skips


class TestTriggerPriority:
    """Multiple triggers — highest priority wins."""

    def test_overrun_wins_over_early_completion(self):
        """If both overrun and early completion could fire, overrun checked first."""
        # Set up overrun trigger
        s1 = _make_scored(
            id="t_running", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t_done", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(
            plan=plan,
            in_progress_task_id="t_running",
            completed_task_ids=["t_done"],
        )
        state.actual_durations["t_running"] = 50.0  # overrun
        state.actual_durations["t_done"] = 10.0  # early completion

        cfg = _enabled_config()
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.OVERRUN


# ===================================================================
# Execute replan tests
# ===================================================================


class TestExecuteReplan:
    """execute_replan logic."""

    def test_replan_produces_new_block_plan(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan, completed_task_ids=["t1"], elapsed_min=30.0)
        state.actual_durations["t1"] = 25.0

        decision = execute_replan(state, config=_enabled_config(), now=_NOW)
        assert decision.should_replan is True
        assert decision.new_plan is not None
        assert isinstance(decision.new_plan, BlockPlan)

    def test_remaining_tasks_rescored_and_repacked(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s3 = _make_scored(
            id="t3", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
                _make_slot(s3, 60, 90),
            ]
        )
        state = _make_state(plan=plan, completed_task_ids=["t1"], elapsed_min=30.0)
        state.actual_durations["t1"] = 30.0

        decision = execute_replan(state, config=_enabled_config(), now=_NOW)
        # t2 and t3 should be re-packed
        new_ids = {s.scored_task.work_unit.id for s in decision.new_plan.scheduled_slots}
        assert "t2" in new_ids or "t3" in new_ids

    def test_new_tasks_added_to_pool(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, completed_task_ids=["t1"], elapsed_min=30.0)
        state.actual_durations["t1"] = 30.0

        new_wu = _make_work_unit(
            id="t_new", estimated_expected_min=20.0, estimated_optimistic_min=15.0, estimated_pessimistic_min=30.0
        )
        decision = execute_replan(state, new_tasks=[new_wu], config=_enabled_config(), now=_NOW)
        new_ids = {s.scored_task.work_unit.id for s in decision.new_plan.scheduled_slots}
        assert "t_new" in new_ids
        assert "t_new" in decision.newly_scheduled

    def test_failed_dependency_tasks_removed(self):
        wu2 = _make_work_unit(id="t2", dependency_ids=["t1"])
        s1 = _make_scored(id="t1")
        s2 = _make_scored(work_unit=wu2)
        s3 = _make_scored(id="t3")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
                _make_slot(s3, 60, 90),
            ]
        )
        state = _make_state(plan=plan, failed_task_ids=["t1"], elapsed_min=30.0)

        decision = execute_replan(
            state,
            config=_enabled_config(),
            reason=ReplanReason.DEPENDENCY_FAILURE,
            now=_NOW,
        )
        new_ids = {s.scored_task.work_unit.id for s in decision.new_plan.scheduled_slots}
        # t2 should be removed because it depends on failed t1
        assert "t2" not in new_ids
        assert "t2" in decision.deferred_from_old

    def test_state_updated_after_replan(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, elapsed_min=30.0, completed_task_ids=["t1"])
        state.actual_durations["t1"] = 30.0

        assert state.replan_count == 0
        assert state.last_replan_at is None

        execute_replan(state, config=_enabled_config(), now=_NOW)
        assert state.replan_count == 1
        assert state.last_replan_at == _NOW
        assert state.current_plan is not None

    def test_replan_with_no_remaining_time(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, elapsed_min=480.0)

        decision = execute_replan(state, config=_enabled_config(), now=_NOW)
        assert decision.new_plan is not None
        # With 0 remaining time, packer returns empty plan
        assert len(decision.new_plan.scheduled_slots) == 0

    def test_duplicate_new_tasks_not_doubled(self):
        """New tasks with same ID as remaining should not duplicate."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, elapsed_min=0.0)

        dup = _make_work_unit(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        decision = execute_replan(state, new_tasks=[dup], config=_enabled_config(), now=_NOW)
        # t1 should appear only once in the new plan
        t1_count = sum(1 for s in decision.new_plan.scheduled_slots if s.scored_task.work_unit.id == "t1")
        assert t1_count <= 1


# ===================================================================
# Lifecycle hook tests
# ===================================================================


class TestOnTaskStart:
    """on_task_start lifecycle hook."""

    def test_marks_in_progress(self):
        state = _make_state()
        on_task_start(state, "t1")
        assert state.in_progress_task_id == "t1"


class TestOnTaskComplete:
    """on_task_complete lifecycle hook."""

    def test_returns_no_replan_when_no_trigger(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)
        state.mark_started("t1")

        # 28 min actual vs 30 estimated = only 7% early, under 20% threshold
        decision = on_task_complete(
            state,
            "t1",
            28.0,
            config=_enabled_config(),
            now=_NOW,
        )
        assert decision.should_replan is False

    def test_triggers_replan_on_early_completion(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)
        state.mark_started("t1")

        decision = on_task_complete(
            state,
            "t1",
            15.0,  # 50% early
            config=_enabled_config(),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.EARLY_COMPLETION

    def test_marks_complete_even_when_disabled(self):
        """State is updated regardless of whether replan is enabled."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)
        state.mark_started("t1")

        cfg = ReplannerConfig(enabled=False)
        decision = on_task_complete(state, "t1", 15.0, config=cfg, now=_NOW)
        assert decision.should_replan is False
        assert "t1" in state.completed_task_ids
        assert state.actual_durations["t1"] == 15.0


class TestOnTaskFail:
    """on_task_fail lifecycle hook."""

    def test_triggers_dependency_check(self):
        wu2 = _make_work_unit(id="t2", dependency_ids=["t1"])
        s1 = _make_scored(id="t1")
        s2 = _make_scored(work_unit=wu2)
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)

        decision = on_task_fail(
            state,
            "t1",
            reason="crash",
            config=_enabled_config(),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.DEPENDENCY_FAILURE

    def test_no_dependent_tasks_no_replan(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")  # no deps
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)

        decision = on_task_fail(
            state,
            "t1",
            reason="crash",
            config=_enabled_config(),
            now=_NOW,
        )
        assert decision.should_replan is False

    def test_marks_failed_even_when_disabled(self):
        s1 = _make_scored(id="t1")
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)

        cfg = ReplannerConfig(enabled=False)
        decision = on_task_fail(state, "t1", reason="error", config=cfg, now=_NOW)
        assert decision.should_replan is False
        assert "t1" in state.failed_task_ids


# ===================================================================
# Three-horizon model tests
# ===================================================================


class TestStrategicHorizon:
    """get_strategic_horizon returns the full plan."""

    def test_returns_full_plan(self):
        plan = _make_block_plan(duration=480.0)
        state = _make_state(plan=plan)
        result = get_strategic_horizon(state)
        assert result is plan

    def test_returns_replanned_plan_after_replan(self):
        old_plan = _make_block_plan(duration=480.0)
        new_plan = _make_block_plan(duration=240.0)
        state = _make_state(plan=old_plan, current_plan=new_plan)
        result = get_strategic_horizon(state)
        assert result is new_plan


class TestTacticalHorizon:
    """get_tactical_horizon returns next 2 hours of slots."""

    def test_returns_next_two_hours(self):
        slots = [_make_slot(_make_scored(id=f"t{i}"), start=i * 30.0, end=(i + 1) * 30.0) for i in range(8)]
        plan = _make_block_plan(slots=slots, duration=240.0)
        state = _make_state(plan=plan, elapsed_min=0.0)

        tactical = get_tactical_horizon(state)
        # 120 min window from elapsed=0 -> slots starting before 120
        # t0(0-30), t1(30-60), t2(60-90), t3(90-120) all start < 120
        assert len(tactical) == 4

    def test_tactical_from_midblock(self):
        slots = [_make_slot(_make_scored(id=f"t{i}"), start=i * 30.0, end=(i + 1) * 30.0) for i in range(8)]
        plan = _make_block_plan(slots=slots, duration=240.0)
        state = _make_state(plan=plan, elapsed_min=60.0, completed_task_ids=["t0", "t1"])

        tactical = get_tactical_horizon(state)
        # Remaining: t2(60-90), t3(90-120), t4(120-150), t5(150-180), t6(180-210), t7(210-240)
        # Window: elapsed=60 to 180
        # Slots starting before 180: t2, t3, t4, t5
        assert len(tactical) == 4

    def test_tactical_empty_when_all_done(self):
        s1 = _make_scored(id="t1")
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        assert get_tactical_horizon(state) == []


class TestExecutionHorizon:
    """get_execution_horizon returns (current, next)."""

    def test_returns_current_and_next(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        s3 = _make_scored(id="t3")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
                _make_slot(s3, 60, 90),
            ]
        )
        state = _make_state(plan=plan, in_progress_task_id="t1")
        current, next_t = get_execution_horizon(state)
        assert current is not None
        assert current.scored_task.work_unit.id == "t1"
        assert next_t is not None
        assert next_t.scored_task.work_unit.id == "t2"

    def test_no_in_progress_returns_first_remaining(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)
        current, next_t = get_execution_horizon(state)
        assert current is not None
        assert current.scored_task.work_unit.id == "t1"
        assert next_t is not None
        assert next_t.scored_task.work_unit.id == "t2"

    def test_all_tasks_done_returns_none_none(self):
        s1 = _make_scored(id="t1")
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, completed_task_ids=["t1"])
        current, next_t = get_execution_horizon(state)
        assert current is None
        assert next_t is None

    def test_single_remaining_returns_current_only(self):
        s1 = _make_scored(id="t1")
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)
        current, next_t = get_execution_horizon(state)
        assert current is not None
        assert current.scored_task.work_unit.id == "t1"
        assert next_t is None

    def test_in_progress_with_single_remaining(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan, in_progress_task_id="t2", completed_task_ids=["t1"])
        current, next_t = get_execution_horizon(state)
        assert current is not None
        assert current.scored_task.work_unit.id == "t2"
        assert next_t is None


# ===================================================================
# State persistence tests
# ===================================================================


class TestStatePersistence:
    """save_block_state and load_block_state."""

    def test_save_writes_valid_json(self, tmp_path):
        state = _make_state(block_id="test_block")
        path = save_block_state(state, tmp_path)
        assert path.exists()
        assert path.name == "test_block_state.json"
        # Validate JSON
        data = json.loads(path.read_text())
        assert data["block_id"] == "test_block"

    def test_load_reads_back_correctly(self, tmp_path):
        state = _make_state(block_id="test_block", elapsed_min=42.0)
        state.mark_complete("t1", 20.0)
        save_block_state(state, tmp_path)

        loaded = load_block_state("test_block", tmp_path)
        assert loaded is not None
        assert loaded.block_id == "test_block"
        assert loaded.elapsed_min == 42.0
        assert "t1" in loaded.completed_task_ids
        assert loaded.actual_durations["t1"] == 20.0

    def test_load_missing_file_returns_none(self, tmp_path):
        result = load_block_state("nonexistent", tmp_path)
        assert result is None

    def test_roundtrip_preserves_data(self, tmp_path):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=20.0, estimated_optimistic_min=15.0, estimated_pessimistic_min=30.0
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 50),
            ]
        )
        state = _make_state(
            block_id="roundtrip_block",
            plan=plan,
            elapsed_min=30.0,
            replan_count=2,
            completed_task_ids=["t1"],
            in_progress_task_id="t2",
        )
        state.actual_durations["t1"] = 25.0
        state.last_replan_at = _NOW

        save_block_state(state, tmp_path)
        loaded = load_block_state("roundtrip_block", tmp_path)

        assert loaded is not None
        assert loaded.block_id == "roundtrip_block"
        assert loaded.elapsed_min == 30.0
        assert loaded.replan_count == 2
        assert loaded.completed_task_ids == ["t1"]
        assert loaded.in_progress_task_id == "t2"
        assert loaded.actual_durations["t1"] == 25.0
        assert loaded.last_replan_at is not None
        assert len(loaded.original_plan.scheduled_slots) == 2

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        state = _make_state(block_id="dir_test")
        path = save_block_state(state, nested)
        assert path.exists()
        assert nested.is_dir()

    def test_load_corrupt_json_returns_none(self, tmp_path):
        target = tmp_path / "bad_state.json"
        target.write_text("not valid json {{{")
        # load_block_state looks for {block_id}_state.json
        corrupt_path = tmp_path / "corrupt_state.json"
        corrupt_path.write_text("not valid json {{{")
        result = load_block_state("corrupt", tmp_path)
        assert result is None


# ===================================================================
# Edge case tests
# ===================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_block_plan_no_triggers(self):
        plan = _make_block_plan(slots=[])
        state = _make_state(plan=plan)
        cfg = _enabled_config()
        decision = check_replan_triggers(state, cfg, now=_NOW)
        assert decision.should_replan is False

    def test_all_tasks_completed_no_remaining(self):
        s1 = _make_scored(id="t1")
        s2 = _make_scored(id="t2")
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(
            plan=plan,
            completed_task_ids=["t1", "t2"],
        )
        state.actual_durations["t1"] = 30.0
        state.actual_durations["t2"] = 30.0
        remaining = state.remaining_scheduled()
        assert remaining == []

    def test_single_task_block(self):
        s1 = _make_scored(
            id="solo", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)

        current, next_t = get_execution_horizon(state)
        assert current is not None
        assert current.scored_task.work_unit.id == "solo"
        assert next_t is None

    def test_replan_enum_values(self):
        assert ReplanReason.EARLY_COMPLETION.value == "early_completion"
        assert ReplanReason.OVERRUN.value == "overrun"
        assert ReplanReason.DEPENDENCY_FAILURE.value == "dependency_failure"
        assert ReplanReason.NEW_HIGH_PRIORITY_TASK.value == "new_high_priority_task"
        assert ReplanReason.BUFFER_HEALTH_LOW.value == "buffer_health_low"
        assert ReplanReason.CRITICAL_INTERRUPT.value == "critical_interrupt"
        assert ReplanReason.MANUAL.value == "manual"

    def test_block_state_with_current_plan_remaining_scheduled(self):
        """remaining_scheduled uses current_plan when set."""
        s1 = _make_scored(id="old_t1")
        s2 = _make_scored(id="new_t1")
        old_plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        new_plan = _make_block_plan(slots=[_make_slot(s2, 0, 30)])

        state = _make_state(plan=old_plan, current_plan=new_plan)
        remaining = state.remaining_scheduled()
        remaining_ids = [s.scored_task.work_unit.id for s in remaining]
        assert "new_t1" in remaining_ids
        assert "old_t1" not in remaining_ids

    def test_execute_replan_increments_count_each_call(self):
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, elapsed_min=0.0)

        execute_replan(state, config=_enabled_config(), now=_NOW)
        assert state.replan_count == 1

        execute_replan(state, config=_enabled_config(), now=_NOW + timedelta(minutes=1))
        assert state.replan_count == 2

    def test_multiple_failed_deps_all_removed(self):
        """Multiple tasks with failed deps are all removed."""
        wu2 = _make_work_unit(id="t2", dependency_ids=["t1"])
        wu3 = _make_work_unit(id="t3", dependency_ids=["t1"])
        wu4 = _make_work_unit(id="t4")  # no deps
        s1 = _make_scored(id="t1")
        s2 = _make_scored(work_unit=wu2)
        s3 = _make_scored(work_unit=wu3)
        s4 = _make_scored(work_unit=wu4)
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
                _make_slot(s3, 60, 90),
                _make_slot(s4, 90, 120),
            ]
        )
        state = _make_state(plan=plan, failed_task_ids=["t1"], elapsed_min=30.0)

        decision = execute_replan(
            state,
            config=_enabled_config(),
            reason=ReplanReason.DEPENDENCY_FAILURE,
            now=_NOW,
        )
        new_ids = {s.scored_task.work_unit.id for s in decision.new_plan.scheduled_slots}
        assert "t2" not in new_ids
        assert "t3" not in new_ids
        assert "t2" in decision.deferred_from_old
        assert "t3" in decision.deferred_from_old

    def test_on_task_complete_with_overrun_fires_before_buffer(self):
        """on_task_complete with massive overrun fires OVERRUN (higher priority than buffer)."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[_make_slot(s1, 0, 30), _make_slot(s2, 30, 60)],
            buffer_min=5.0,  # Very small buffer
        )
        state = _make_state(plan=plan)
        state.mark_started("t1")

        # Complete t1 with a massive overrun: 50 min actual vs 30 estimated = 67% overrun
        # Overrun check fires BEFORE mark_complete clears in_progress
        decision = on_task_complete(
            state,
            "t1",
            50.0,
            config=_enabled_config(),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.OVERRUN

    def test_on_task_complete_with_buffer_health_trigger(self):
        """on_task_complete fires buffer_health_low when overrun is below threshold."""
        s1 = _make_scored(
            id="t1", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        s2 = _make_scored(
            id="t2", estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0
        )
        plan = _make_block_plan(
            slots=[_make_slot(s1, 0, 30), _make_slot(s2, 30, 60)],
            buffer_min=5.0,  # Very small buffer
        )
        state = _make_state(plan=plan)
        # t1 already completed with slight overrun (within 25% threshold)
        state.mark_complete("t1", 36.0)  # 20% over — under default 25% overrun threshold
        state.actual_durations["t1"] = 36.0
        state.mark_started("t2")

        # Complete t2 with another slight overrun: 36 min vs 30 est = 20% over
        # Cumulative buffer: overrun1=6 + overrun2=6 = 12 min used out of 5 = negative health
        # Overrun per task (20%) is under 25% threshold so overrun trigger skipped
        decision = on_task_complete(
            state,
            "t2",
            36.0,
            config=_enabled_config(overrun_threshold_pct=25.0),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.BUFFER_HEALTH_LOW

    def test_on_task_fail_with_multiple_dependents(self):
        """on_task_fail where many tasks depend on the failed one."""
        wu2 = _make_work_unit(id="t2", dependency_ids=["t1"])
        wu3 = _make_work_unit(id="t3", dependency_ids=["t1", "t2"])
        s1 = _make_scored(id="t1")
        s2 = _make_scored(work_unit=wu2)
        s3 = _make_scored(work_unit=wu3)
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
                _make_slot(s3, 60, 90),
            ]
        )
        state = _make_state(plan=plan)

        decision = on_task_fail(
            state,
            "t1",
            reason="oom",
            config=_enabled_config(),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.DEPENDENCY_FAILURE


# ===================================================================
# H1/H2: Overrun detection through on_task_complete + on_task_progress
# ===================================================================


class TestOverrunThroughOnTaskComplete:
    """H1+H2 fix: overrun is checked BEFORE mark_complete clears in_progress."""

    def test_overrun_detected_through_on_task_complete(self):
        """Task took 2x the estimate — overrun fires through lifecycle hook."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        s2 = _make_scored(
            id="t2",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)
        state.mark_started("t1")

        # Complete t1 with 2x the estimate (60 min vs 30 est = 100% overrun)
        decision = on_task_complete(
            state,
            "t1",
            60.0,
            config=_enabled_config(overrun_threshold_pct=25.0),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.OVERRUN
        # State should still be properly updated
        assert "t1" in state.completed_task_ids
        assert state.in_progress_task_id is None

    def test_overrun_not_triggered_when_within_threshold(self):
        """Task only slightly over — no overrun through lifecycle hook."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        s2 = _make_scored(
            id="t2",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)
        state.mark_started("t1")

        # Complete t1 at 34 min vs 30 est = 13% over (under 25% threshold)
        decision = on_task_complete(
            state,
            "t1",
            34.0,
            config=_enabled_config(overrun_threshold_pct=25.0),
            now=_NOW,
        )
        # Not an overrun, and not early enough either (only 13% over, not early)
        assert decision.should_replan is False

    def test_overrun_with_disabled_config_skips_check(self):
        """Overrun detection skipped when config disabled — state still updated."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)
        state.mark_started("t1")

        cfg = ReplannerConfig(enabled=False)
        decision = on_task_complete(state, "t1", 60.0, config=cfg, now=_NOW)
        assert decision.should_replan is False
        assert "t1" in state.completed_task_ids


class TestOnTaskProgress:
    """H2 fix: on_task_progress updates elapsed and can trigger overrun."""

    def test_progress_updates_actual_durations(self):
        """on_task_progress records elapsed time in actual_durations."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)
        state.mark_started("t1")

        on_task_progress(state, "t1", 15.0)
        assert state.actual_durations["t1"] == 15.0

    def test_progress_triggers_overrun(self):
        """on_task_progress triggers overrun when elapsed exceeds threshold."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        s2 = _make_scored(
            id="t2",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(
            slots=[
                _make_slot(s1, 0, 30),
                _make_slot(s2, 30, 60),
            ]
        )
        state = _make_state(plan=plan)
        state.mark_started("t1")

        # 40 min elapsed vs 30 est = 33% over (>25% threshold)
        decision = on_task_progress(
            state,
            "t1",
            40.0,
            config=_enabled_config(overrun_threshold_pct=25.0),
            now=_NOW,
        )
        assert decision.should_replan is True
        assert decision.reason == ReplanReason.OVERRUN
        # Task is still in progress (not completed)
        assert state.in_progress_task_id == "t1"

    def test_progress_no_overrun_when_within_threshold(self):
        """on_task_progress does not trigger when within threshold."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan)
        state.mark_started("t1")

        # 35 min elapsed vs 30 est = 16.7% over (under 25% threshold)
        decision = on_task_progress(
            state,
            "t1",
            35.0,
            config=_enabled_config(overrun_threshold_pct=25.0),
            now=_NOW,
        )
        assert decision.should_replan is False

    def test_progress_no_replan_when_disabled(self):
        """on_task_progress returns no-replan when config is disabled."""
        state = _make_state()
        state.mark_started("t1")

        cfg = ReplannerConfig(enabled=False)
        decision = on_task_progress(state, "t1", 999.0, config=cfg, now=_NOW)
        assert decision.should_replan is False
        # But actual_durations is still updated
        assert state.actual_durations["t1"] == 999.0

    def test_progress_no_config_returns_no_replan(self):
        """on_task_progress with no config returns no-replan."""
        state = _make_state()
        state.mark_started("t1")

        decision = on_task_progress(state, "t1", 50.0)
        assert decision.should_replan is False
        assert state.actual_durations["t1"] == 50.0


# ===================================================================
# M1: execute_replan feature flag guard
# ===================================================================


class TestExecuteReplanFeatureFlag:
    """M1 fix: execute_replan checks feature flag."""

    def test_execute_replan_disabled_returns_no_replan(self):
        """execute_replan with enabled=False returns no-replan immediately."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, elapsed_min=0.0)

        cfg = ReplannerConfig(enabled=False)
        decision = execute_replan(state, config=cfg, now=_NOW)
        assert decision.should_replan is False
        # State should NOT be modified
        assert state.replan_count == 0
        assert state.last_replan_at is None
        assert state.current_plan is None

    def test_execute_replan_enabled_proceeds(self):
        """execute_replan with enabled=True proceeds normally."""
        s1 = _make_scored(
            id="t1",
            estimated_expected_min=30.0,
            estimated_optimistic_min=20.0,
            estimated_pessimistic_min=45.0,
        )
        plan = _make_block_plan(slots=[_make_slot(s1, 0, 30)])
        state = _make_state(plan=plan, elapsed_min=0.0)

        cfg = _enabled_config()
        decision = execute_replan(state, config=cfg, now=_NOW)
        assert decision.should_replan is True
        assert state.replan_count == 1


# ===================================================================
# M2: mark_started overwrite warning
# ===================================================================


class TestMarkStartedWarning:
    """M2 fix: mark_started logs warning when overwriting."""

    def test_mark_started_overwrite_logs_warning(self, caplog):
        """Overwriting in-progress task emits a warning."""
        import logging

        state = _make_state()
        state.mark_started("t1")
        with caplog.at_level(logging.WARNING, logger="nova.scheduler.replanner"):
            state.mark_started("t2")

        assert state.in_progress_task_id == "t2"
        assert "Overwriting in-progress task t1 with t2" in caplog.text

    def test_mark_started_same_task_no_warning(self, caplog):
        """Re-marking the same task does not warn."""
        import logging

        state = _make_state()
        state.mark_started("t1")
        with caplog.at_level(logging.WARNING, logger="nova.scheduler.replanner"):
            state.mark_started("t1")

        assert state.in_progress_task_id == "t1"
        assert "Overwriting" not in caplog.text

    def test_mark_started_from_none_no_warning(self, caplog):
        """Starting from no in-progress task does not warn."""
        import logging

        state = _make_state()
        with caplog.at_level(logging.WARNING, logger="nova.scheduler.replanner"):
            state.mark_started("t1")

        assert state.in_progress_task_id == "t1"
        assert "Overwriting" not in caplog.text
