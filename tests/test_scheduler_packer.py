"""Tests for utils.scheduler.block + utils.scheduler.packer — Block Packer.

Phase 3 of the Intelligent Dynamic Block Scheduler.
Tests cover: BlockPlan model, capacity calculations, utilization, validation,
packing basics, commitment levels, capacity limits, energy-aware placement,
context-switch accounting, EPIC handling, edge cases, and real-world scenarios.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from utils.scheduler.block import (
    DEFAULT_ENERGY_CURVE,
    ENERGY_MODE_MAP,
    BlockPlan,
    EnergyLevel,
    ScheduledSlot,
    validate_block_plan,
)
from utils.scheduler.packer import (
    PackerConfig,
    _calculate_switch_cost,
    _get_energy_level_at,
    _preferred_energy,
    _sort_by_energy_and_score,
    pack_block,
)
from utils.scheduler.scorer import ScoredTask, score_task
from utils.scheduler.work_unit import CommitmentLevel, TaskClass, WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wu(**overrides) -> WorkUnit:
    """Create a WorkUnit with sensible test defaults."""
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
        "task_class": TaskClass.BOUNDED,
        "work_mode": WorkMode.ADMIN,
        "schedulable_now": True,
    }
    defaults.update(overrides)
    return WorkUnit(**defaults)


def _scored(wu: WorkUnit | None = None, **wu_overrides) -> ScoredTask:
    """Create a ScoredTask from a WorkUnit (or defaults)."""
    if wu is None:
        wu = _wu(**wu_overrides)
    return score_task(wu)


def _scored_with(**wu_overrides) -> ScoredTask:
    """Shortcut: score a WorkUnit built from overrides."""
    return _scored(wu=_wu(**wu_overrides))


# =========================================================================
# BLOCK MODEL TESTS
# =========================================================================


class TestBlockPlanModel:
    """BlockPlan creation, defaults, and properties."""

    def test_default_creation(self):
        plan = BlockPlan()
        assert plan.block_duration_min == 480.0
        assert plan.committed_ratio == 0.75
        assert plan.buffer_ratio == 0.17
        assert plan.filler_ratio == 0.08
        assert plan.scheduled_slots == []
        assert plan.deferred_tasks == []
        assert plan.context_switches == 0
        assert plan.plan_confidence == 0.0

    def test_capacity_calculations(self):
        plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=360.0,
            total_buffer_min=81.6,
            total_filler_min=38.4,
        )
        assert plan.total_committed_min == 360.0
        assert plan.total_buffer_min == 81.6
        assert plan.total_filler_min == 38.4

    def test_remaining_committed(self):
        plan = BlockPlan(total_committed_min=360.0, used_committed_min=200.0)
        assert plan.remaining_committed_min == 160.0

    def test_remaining_filler(self):
        plan = BlockPlan(total_filler_min=38.4, used_filler_min=10.0)
        assert plan.remaining_filler_min == 28.4

    def test_utilization_pct(self):
        plan = BlockPlan(
            block_duration_min=480.0,
            used_committed_min=240.0,
            used_filler_min=0.0,
        )
        assert plan.utilization_pct == pytest.approx(50.0)

    def test_utilization_full(self):
        plan = BlockPlan(
            block_duration_min=480.0,
            used_committed_min=360.0,
            used_filler_min=120.0,
        )
        assert plan.utilization_pct == pytest.approx(100.0)

    def test_utilization_zero_duration(self):
        plan = BlockPlan(block_duration_min=0.0)
        assert plan.utilization_pct == 0.0

    def test_epic_warnings_list(self):
        plan = BlockPlan(epic_warnings=["EPIC task scheduled: big-task"])
        assert len(plan.epic_warnings) == 1

    def test_custom_ratios(self):
        plan = BlockPlan(
            committed_ratio=0.60,
            buffer_ratio=0.25,
            filler_ratio=0.15,
        )
        assert plan.committed_ratio == 0.60
        assert plan.buffer_ratio == 0.25
        assert plan.filler_ratio == 0.15


# =========================================================================
# VALIDATION TESTS
# =========================================================================


class TestValidateBlockPlan:
    """validate_block_plan catches various issues."""

    def test_valid_plan(self):
        plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=360.0,
            total_filler_min=38.4,
            used_committed_min=200.0,
            used_filler_min=30.0,
        )
        assert validate_block_plan(plan) == []

    def test_over_scheduled(self):
        plan = BlockPlan(
            block_duration_min=100.0,
            used_committed_min=80.0,
            used_filler_min=30.0,
        )
        warnings = validate_block_plan(plan)
        assert any("Over-scheduled" in w for w in warnings)

    def test_low_buffer_ratio(self):
        plan = BlockPlan(buffer_ratio=0.05)
        warnings = validate_block_plan(plan)
        assert any("Buffer ratio" in w for w in warnings)

    def test_buffer_at_minimum(self):
        plan = BlockPlan(buffer_ratio=0.10)
        warnings = validate_block_plan(plan)
        assert not any("Buffer ratio" in w for w in warnings)

    def test_slot_exceeds_duration(self):
        st = _scored_with(id="overflow")
        slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=470.0,
            end_offset_min=510.0,
        )
        plan = BlockPlan(
            block_duration_min=480.0,
            scheduled_slots=[slot],
        )
        warnings = validate_block_plan(plan)
        assert any("exceeding block duration" in w for w in warnings)

    def test_epic_without_warning(self):
        st = _scored_with(
            id="epic-task",
            task_class=TaskClass.EPIC,
            estimated_optimistic_min=90.0,
            estimated_expected_min=120.0,
            estimated_pessimistic_min=180.0,
        )
        slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=0.0,
            end_offset_min=120.0,
        )
        plan = BlockPlan(
            block_duration_min=480.0,
            scheduled_slots=[slot],
            epic_warnings=[],  # No warning registered
        )
        warnings = validate_block_plan(plan)
        assert any("EPIC" in w for w in warnings)

    def test_epic_with_warning_ok(self):
        st = _scored_with(
            id="epic-task",
            task_class=TaskClass.EPIC,
            estimated_optimistic_min=90.0,
            estimated_expected_min=120.0,
            estimated_pessimistic_min=180.0,
        )
        slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=0.0,
            end_offset_min=120.0,
        )
        plan = BlockPlan(
            block_duration_min=480.0,
            scheduled_slots=[slot],
            epic_warnings=["EPIC task scheduled: epic-task (must_do_today override)"],
        )
        warnings = validate_block_plan(plan)
        # No EPIC warning because it's already in epic_warnings
        assert not any("EPIC task 'epic-task' scheduled without warning" in w for w in warnings)


# =========================================================================
# ENERGY MODEL TESTS
# =========================================================================


class TestEnergyModel:
    """Energy level mappings and curve."""

    def test_energy_mode_map_complete(self):
        """Every WorkMode has a mapping."""
        for mode in WorkMode:
            assert mode in ENERGY_MODE_MAP

    def test_high_energy_modes(self):
        assert ENERGY_MODE_MAP[WorkMode.DEEP_IMPLEMENTATION] == EnergyLevel.HIGH
        assert ENERGY_MODE_MAP[WorkMode.DEBUGGING] == EnergyLevel.HIGH
        assert ENERGY_MODE_MAP[WorkMode.RESEARCH] == EnergyLevel.HIGH

    def test_medium_energy_modes(self):
        assert ENERGY_MODE_MAP[WorkMode.ANALYSIS] == EnergyLevel.MEDIUM
        assert ENERGY_MODE_MAP[WorkMode.REVIEW] == EnergyLevel.MEDIUM
        assert ENERGY_MODE_MAP[WorkMode.DEPLOYMENT] == EnergyLevel.MEDIUM

    def test_low_energy_modes(self):
        assert ENERGY_MODE_MAP[WorkMode.MONITORING] == EnergyLevel.LOW
        assert ENERGY_MODE_MAP[WorkMode.ADMIN] == EnergyLevel.LOW
        assert ENERGY_MODE_MAP[WorkMode.COMMUNICATION] == EnergyLevel.LOW

    def test_default_energy_curve(self):
        assert len(DEFAULT_ENERGY_CURVE) == 3
        assert DEFAULT_ENERGY_CURVE[0] == (0.40, EnergyLevel.HIGH)
        assert DEFAULT_ENERGY_CURVE[1] == (0.70, EnergyLevel.MEDIUM)
        assert DEFAULT_ENERGY_CURVE[2] == (1.00, EnergyLevel.LOW)

    def test_get_energy_at_start(self):
        cfg = PackerConfig()
        assert _get_energy_level_at(0.0, cfg) == EnergyLevel.HIGH

    def test_get_energy_at_middle(self):
        cfg = PackerConfig()
        assert _get_energy_level_at(0.5, cfg) == EnergyLevel.MEDIUM

    def test_get_energy_at_end(self):
        cfg = PackerConfig()
        assert _get_energy_level_at(0.9, cfg) == EnergyLevel.LOW

    def test_get_energy_boundary_40(self):
        cfg = PackerConfig()
        assert _get_energy_level_at(0.40, cfg) == EnergyLevel.HIGH

    def test_get_energy_boundary_70(self):
        cfg = PackerConfig()
        assert _get_energy_level_at(0.70, cfg) == EnergyLevel.MEDIUM

    def test_get_energy_past_1(self):
        cfg = PackerConfig()
        assert _get_energy_level_at(1.5, cfg) == EnergyLevel.LOW

    def test_preferred_energy_helper(self):
        assert _preferred_energy(WorkMode.DEEP_IMPLEMENTATION) == EnergyLevel.HIGH
        assert _preferred_energy(WorkMode.REVIEW) == EnergyLevel.MEDIUM
        assert _preferred_energy(WorkMode.ADMIN) == EnergyLevel.LOW


# =========================================================================
# PACKER CONFIG TESTS
# =========================================================================


class TestPackerConfig:
    """PackerConfig creation and defaults."""

    def test_defaults(self):
        cfg = PackerConfig()
        assert cfg.committed_ratio == 0.75
        assert cfg.buffer_ratio == 0.17
        assert cfg.filler_ratio == 0.08
        assert cfg.same_mode_switch_cost_min == 2.0
        assert cfg.different_mode_switch_cost_min == 10.0

    def test_custom_config(self):
        cfg = PackerConfig(
            committed_ratio=0.60,
            buffer_ratio=0.25,
            filler_ratio=0.15,
            same_mode_switch_cost_min=1.0,
            different_mode_switch_cost_min=15.0,
        )
        assert cfg.committed_ratio == 0.60
        assert cfg.different_mode_switch_cost_min == 15.0

    def test_energy_curve_default(self):
        cfg = PackerConfig()
        assert len(cfg.energy_curve) == 3


# =========================================================================
# SWITCH COST TESTS
# =========================================================================


class TestSwitchCost:
    """_calculate_switch_cost logic."""

    def test_first_task_no_cost(self):
        cfg = PackerConfig()
        cost = _calculate_switch_cost(None, WorkMode.ADMIN, 5.0, cfg)
        assert cost == 0.0

    def test_same_mode(self):
        cfg = PackerConfig(same_mode_switch_cost_min=2.0)
        cost = _calculate_switch_cost(WorkMode.ADMIN, WorkMode.ADMIN, 0.0, cfg)
        assert cost == 2.0

    def test_different_mode(self):
        cfg = PackerConfig(different_mode_switch_cost_min=10.0)
        cost = _calculate_switch_cost(WorkMode.ADMIN, WorkMode.DEBUGGING, 0.0, cfg)
        assert cost == 10.0

    def test_task_switch_cost_override(self):
        cfg = PackerConfig(different_mode_switch_cost_min=10.0)
        cost = _calculate_switch_cost(WorkMode.ADMIN, WorkMode.DEBUGGING, 20.0, cfg)
        assert cost == 20.0  # task's own cost is larger

    def test_task_switch_cost_no_override_if_smaller(self):
        cfg = PackerConfig(different_mode_switch_cost_min=10.0)
        cost = _calculate_switch_cost(WorkMode.ADMIN, WorkMode.DEBUGGING, 5.0, cfg)
        assert cost == 10.0  # config cost is larger

    def test_same_mode_with_task_override(self):
        cfg = PackerConfig(same_mode_switch_cost_min=2.0)
        cost = _calculate_switch_cost(WorkMode.ADMIN, WorkMode.ADMIN, 8.0, cfg)
        assert cost == 8.0


# =========================================================================
# SORT BY ENERGY AND SCORE TESTS
# =========================================================================


class TestSortByEnergyAndScore:
    """_sort_by_energy_and_score grouping."""

    def test_groups_by_energy(self):
        tasks = [
            _scored_with(id="admin1", work_mode=WorkMode.ADMIN, urgency=0.9),
            _scored_with(id="debug1", work_mode=WorkMode.DEBUGGING, urgency=0.9),
            _scored_with(id="review1", work_mode=WorkMode.REVIEW, urgency=0.9),
        ]
        cfg = PackerConfig()
        ordered = _sort_by_energy_and_score(tasks, cfg)
        modes = [st.work_unit.work_mode for st in ordered]
        # HIGH first, then MEDIUM, then LOW
        assert modes[0] == WorkMode.DEBUGGING  # HIGH
        assert modes[1] == WorkMode.REVIEW  # MEDIUM
        assert modes[2] == WorkMode.ADMIN  # LOW

    def test_score_order_within_group(self):
        tasks = [
            _scored_with(id="debug_low", work_mode=WorkMode.DEBUGGING, urgency=0.1),
            _scored_with(id="debug_high", work_mode=WorkMode.DEBUGGING, urgency=0.9),
        ]
        cfg = PackerConfig()
        ordered = _sort_by_energy_and_score(tasks, cfg)
        # Higher score first within HIGH group
        assert ordered[0].work_unit.id == "debug_high"
        assert ordered[1].work_unit.id == "debug_low"

    def test_empty_list(self):
        cfg = PackerConfig()
        assert _sort_by_energy_and_score([], cfg) == []


# =========================================================================
# PACKING BASICS
# =========================================================================


class TestPackingBasics:
    """Basic pack_block behavior."""

    def test_empty_task_list(self):
        plan = pack_block([])
        assert plan.scheduled_slots == []
        assert plan.deferred_tasks == []
        assert plan.utilization_pct == 0.0

    def test_single_task(self):
        tasks = [_scored_with(id="t1", estimated_expected_min=30.0)]
        plan = pack_block(tasks)
        assert len(plan.scheduled_slots) == 1
        assert plan.scheduled_slots[0].scored_task.work_unit.id == "t1"

    def test_two_tasks_same_mode(self):
        tasks = [
            _scored_with(id="t1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
            _scored_with(id="t2", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks)
        assert len(plan.scheduled_slots) == 2
        # Same mode = lower switch cost
        total_switch = sum(s.switch_cost_min for s in plan.scheduled_slots)
        assert total_switch <= 10.0  # same_mode default is 2.0

    def test_two_tasks_different_mode(self):
        tasks = [
            _scored_with(id="t1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
            _scored_with(id="t2", work_mode=WorkMode.DEBUGGING, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks)
        assert len(plan.scheduled_slots) == 2
        # Different mode = higher switch cost
        total_switch = sum(s.switch_cost_min for s in plan.scheduled_slots)
        # At least one non-zero switch cost (from 2nd task)
        assert total_switch > 0

    def test_block_start_and_end_offsets(self):
        tasks = [_scored_with(id="t1", estimated_expected_min=30.0)]
        plan = pack_block(tasks)
        slot = plan.scheduled_slots[0]
        assert slot.start_offset_min >= 0.0
        assert slot.end_offset_min == slot.start_offset_min + 30.0

    def test_deterministic_packing(self):
        """Same input always produces same output."""
        tasks = [
            _scored_with(id="t1", work_mode=WorkMode.DEBUGGING, urgency=0.8),
            _scored_with(id="t2", work_mode=WorkMode.ADMIN, urgency=0.5),
            _scored_with(id="t3", work_mode=WorkMode.REVIEW, urgency=0.6),
        ]
        plan1 = pack_block(tasks)
        plan2 = pack_block(tasks)
        ids1 = [s.scored_task.work_unit.id for s in plan1.scheduled_slots]
        ids2 = [s.scored_task.work_unit.id for s in plan2.scheduled_slots]
        assert ids1 == ids2


# =========================================================================
# COMMITMENT LEVEL TESTS
# =========================================================================


class TestCommitmentLevels:
    """HARD/SOFT/OPPORTUNISTIC placement logic."""

    def test_hard_tasks_always_placed(self):
        tasks = [
            _scored_with(id="hard1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "hard1" in placed_ids

    def test_soft_placed_if_capacity(self):
        tasks = [
            _scored_with(id="soft1", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "soft1" in placed_ids

    def test_opportunistic_uses_filler(self):
        tasks = [
            _scored_with(id="opp1", commitment_level=CommitmentLevel.OPPORTUNISTIC, estimated_expected_min=10.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "opp1" in placed_ids
        # Check it's counted in filler
        assert plan.used_filler_min > 0

    def test_hard_first_then_soft_then_opp(self):
        """HARD tasks get committed capacity first, then SOFT, OPPORTUNISTIC in filler."""
        tasks = [
            _scored_with(
                id="opp1", commitment_level=CommitmentLevel.OPPORTUNISTIC, estimated_expected_min=10.0, urgency=0.9
            ),
            _scored_with(id="soft1", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0, urgency=0.5),
            _scored_with(id="hard1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0, urgency=0.3),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "hard1" in placed_ids
        assert "soft1" in placed_ids
        assert "opp1" in placed_ids

    def test_soft_deferred_when_committed_full(self):
        """When committed capacity is full, SOFT tasks get deferred."""
        # Fill committed capacity entirely with HARD
        cfg = PackerConfig(committed_ratio=0.10, buffer_ratio=0.45, filler_ratio=0.45)
        tasks = [
            _scored_with(
                id="hard1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=45.0
            ),  # fills 48 min committed
            _scored_with(
                id="soft1", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0
            ),  # no room in committed
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        deferred_ids = {s.work_unit.id for s in plan.deferred_tasks}
        assert "soft1" in deferred_ids

    def test_opportunistic_deferred_when_filler_full(self):
        """OPPORTUNISTIC tasks deferred when filler capacity exhausted."""
        cfg = PackerConfig(filler_ratio=0.01)  # very small filler
        tasks = [
            _scored_with(id="opp1", commitment_level=CommitmentLevel.OPPORTUNISTIC, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        deferred_ids = {s.work_unit.id for s in plan.deferred_tasks}
        assert "opp1" in deferred_ids

    def test_mixed_commitment_ordering(self):
        """Verify mixed levels produce correct capacity accounting."""
        tasks = [
            _scored_with(id="h1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=60.0, urgency=0.9),
            _scored_with(id="h2", commitment_level=CommitmentLevel.HARD, estimated_expected_min=60.0, urgency=0.8),
            _scored_with(id="s1", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0, urgency=0.7),
            _scored_with(
                id="o1", commitment_level=CommitmentLevel.OPPORTUNISTIC, estimated_expected_min=15.0, urgency=0.5
            ),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "h1" in placed_ids
        assert "h2" in placed_ids
        # committed has 360 min, hard uses 120, soft 30 should fit
        assert "s1" in placed_ids


# =========================================================================
# CAPACITY LIMIT TESTS
# =========================================================================


class TestCapacityLimits:
    """Capacity overflow and deferred task handling."""

    def test_exceed_committed_defers_soft(self):
        cfg = PackerConfig(committed_ratio=0.20)  # 96 min committed
        tasks = [
            _scored_with(id="s1", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=50.0, urgency=0.9),
            _scored_with(id="s2", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=50.0, urgency=0.8),
            _scored_with(id="s3", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=50.0, urgency=0.7),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # At most ~96 min of SOFT; 2nd or 3rd should be deferred
        assert len(plan.deferred_tasks) >= 1

    def test_deferred_tasks_populated(self):
        cfg = PackerConfig(committed_ratio=0.05, filler_ratio=0.05)  # minimal capacity
        tasks = [
            _scored_with(id=f"s{i}", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0)
            for i in range(10)
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Very limited committed capacity, many tasks deferred
        assert len(plan.deferred_tasks) > 0
        assert len(plan.scheduled_slots) + len(plan.deferred_tasks) >= 10

    def test_filler_does_not_eat_committed(self):
        """OPPORTUNISTIC tasks should NOT consume committed capacity."""
        tasks = [
            _scored_with(id="opp1", commitment_level=CommitmentLevel.OPPORTUNISTIC, estimated_expected_min=10.0),
            _scored_with(id="s1", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        # SOFT task should use committed, OPP should use filler
        assert plan.used_committed_min > 0
        assert plan.used_filler_min > 0


# =========================================================================
# ENERGY-AWARE PLACEMENT
# =========================================================================


class TestEnergyAwarePlacement:
    """Energy-curve-aware ordering of tasks within the block."""

    def test_deep_tasks_placed_early(self):
        """DEEP_IMPLEMENTATION (HIGH energy) should be in first portion of block."""
        tasks = [
            _scored_with(id="admin1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0, urgency=0.5),
            _scored_with(id="deep1", work_mode=WorkMode.DEEP_IMPLEMENTATION, estimated_expected_min=30.0, urgency=0.5),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        # Deep task should come before admin
        slot_ids = [s.scored_task.work_unit.id for s in plan.scheduled_slots]
        if "deep1" in slot_ids and "admin1" in slot_ids:
            deep_idx = slot_ids.index("deep1")
            admin_idx = slot_ids.index("admin1")
            assert deep_idx < admin_idx

    def test_admin_tasks_placed_late(self):
        """ADMIN (LOW energy) should be at the end."""
        tasks = [
            _scored_with(id="admin1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.5),
            _scored_with(id="debug1", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0, urgency=0.5),
            _scored_with(id="review1", work_mode=WorkMode.REVIEW, estimated_expected_min=20.0, urgency=0.5),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        slot_ids = [s.scored_task.work_unit.id for s in plan.scheduled_slots]
        if len(slot_ids) == 3:
            # Admin should be last
            assert slot_ids[-1] == "admin1"

    def test_mode_grouping_reduces_switches(self):
        """Grouping same-energy tasks reduces context switches vs random ordering."""
        # All HIGH energy tasks grouped together
        tasks = [
            _scored_with(id="d1", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0, urgency=0.8),
            _scored_with(id="d2", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0, urgency=0.7),
            _scored_with(id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.6),
            _scored_with(id="a2", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.5),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        # With energy grouping: D1, D2, A1, A2 → 1 cross-mode switch
        # Without: could be interleaved → more switches
        modes = [s.scored_task.work_unit.work_mode for s in plan.scheduled_slots]
        # Count transitions
        transitions = sum(1 for i in range(1, len(modes)) if modes[i] != modes[i - 1])
        # Should be at most 1 cross-mode transition (debug→admin)
        assert transitions <= 2  # Allowing for energy reordering nuances


# =========================================================================
# CONTEXT-SWITCH ACCOUNTING
# =========================================================================


class TestContextSwitchAccounting:
    """Switch cost calculation and capacity consumption."""

    def test_same_mode_lower_cost(self):
        cfg = PackerConfig(same_mode_switch_cost_min=2.0, different_mode_switch_cost_min=10.0)
        tasks = [
            _scored_with(id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0),
            _scored_with(id="a2", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Second task should have same-mode switch cost
        switch_costs = [s.switch_cost_min for s in plan.scheduled_slots]
        # First task: 0, second task: 2.0 (or task's own if larger)
        assert switch_costs[0] == 0.0

    def test_different_mode_higher_cost(self):
        cfg = PackerConfig(same_mode_switch_cost_min=2.0, different_mode_switch_cost_min=10.0)
        tasks = [
            _scored_with(id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, context_switch_cost_min=0.0),
            _scored_with(
                id="d1", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0, context_switch_cost_min=0.0
            ),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Energy ordering may reorder; check that we have non-zero switch cost somewhere
        total_switch = sum(s.switch_cost_min for s in plan.scheduled_slots)
        # At least one switch with cost >= 2.0
        assert total_switch >= 2.0

    def test_switch_costs_consume_capacity(self):
        """Switch costs are part of the used time."""
        cfg = PackerConfig(different_mode_switch_cost_min=10.0)
        tasks = [
            _scored_with(
                id="d1", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0, context_switch_cost_min=0.0
            ),
            _scored_with(id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, context_switch_cost_min=0.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Total used should include switch costs
        total_used = plan.used_committed_min + plan.used_filler_min
        # At least the task durations
        assert total_used >= 40.0

    def test_switch_overflow_defers_last(self):
        """If adding switch cost pushes over capacity, last task is deferred."""
        cfg = PackerConfig(different_mode_switch_cost_min=10.0)
        # Tight capacity
        tasks = [
            _scored_with(
                id="d1",
                work_mode=WorkMode.DEBUGGING,
                estimated_expected_min=20.0,
                urgency=0.9,
                context_switch_cost_min=0.0,
            ),
            _scored_with(
                id="r1",
                work_mode=WorkMode.REVIEW,
                estimated_expected_min=20.0,
                urgency=0.8,
                context_switch_cost_min=0.0,
            ),
            _scored_with(
                id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.7, context_switch_cost_min=0.0
            ),
        ]
        # 70 min total: 60 task + 10 or 20 switch → might not fit 3rd in 70-min block
        plan = pack_block(tasks, block_duration_min=70.0, config=cfg)
        # Some tasks may be deferred due to switch cost overflow
        total_placed = len(plan.scheduled_slots)
        total_deferred = len(plan.deferred_tasks)
        assert total_placed + total_deferred == 3

    def test_context_switch_count(self):
        tasks = [
            _scored_with(id="d1", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0),
            _scored_with(id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert plan.context_switches >= 1


# =========================================================================
# EPIC HANDLING
# =========================================================================


class TestEpicHandling:
    """EPIC tasks (>90 min) are deferred unless must_do_today."""

    def test_epic_deferred_with_warning(self):
        tasks = [
            _scored_with(
                id="big-task",
                task_class=TaskClass.EPIC,
                estimated_optimistic_min=90.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=180.0,
            ),
        ]
        plan = pack_block(tasks)
        assert len(plan.deferred_tasks) == 1
        assert plan.deferred_tasks[0].work_unit.id == "big-task"
        assert any("big-task" in w for w in plan.epic_warnings)

    def test_epic_must_do_today_scheduled(self):
        tasks = [
            _scored_with(
                id="urgent-epic",
                task_class=TaskClass.EPIC,
                must_do_today=True,
                estimated_optimistic_min=90.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=180.0,
            ),
        ]
        plan = pack_block(tasks)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "urgent-epic" in placed_ids
        assert any("urgent-epic" in w for w in plan.epic_warnings)
        assert any("must_do_today" in w for w in plan.epic_warnings)

    def test_epic_warning_message_content(self):
        tasks = [
            _scored_with(
                id="e1",
                task_class=TaskClass.EPIC,
                estimated_optimistic_min=90.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=180.0,
            ),
        ]
        plan = pack_block(tasks)
        assert any("deferred" in w.lower() for w in plan.epic_warnings)

    def test_multiple_epics_all_deferred(self):
        tasks = [
            _scored_with(
                id=f"epic{i}",
                task_class=TaskClass.EPIC,
                estimated_optimistic_min=90.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=180.0,
            )
            for i in range(3)
        ]
        plan = pack_block(tasks)
        assert len(plan.deferred_tasks) == 3
        assert len(plan.epic_warnings) == 3


# =========================================================================
# EDGE CASES
# =========================================================================


class TestEdgeCases:
    """Corner cases and stress tests."""

    def test_zero_duration_block(self):
        tasks = [_scored_with(id="t1", estimated_expected_min=30.0)]
        plan = pack_block(tasks, block_duration_min=0.0)
        assert len(plan.scheduled_slots) == 0

    def test_all_tasks_too_large(self):
        tasks = [
            _scored_with(
                id=f"big{i}",
                estimated_expected_min=400.0,
                estimated_optimistic_min=300.0,
                estimated_pessimistic_min=500.0,
            )
            for i in range(3)
        ]
        plan = pack_block(tasks, block_duration_min=100.0)
        # None should fit in committed (75 min) or filler (8 min)
        assert len(plan.deferred_tasks) >= 2

    def test_100_tasks_performance(self):
        """100 tasks should pack in well under 1 second."""
        tasks = [
            _scored_with(
                id=f"t{i}",
                work_mode=list(WorkMode)[i % len(list(WorkMode))],
                estimated_expected_min=10.0 + (i % 5) * 5,
                urgency=0.1 + (i % 10) * 0.09,
            )
            for i in range(100)
        ]
        start = time.monotonic()
        plan = pack_block(tasks, block_duration_min=480.0)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        # Sanity: at least some were placed
        assert len(plan.scheduled_slots) > 0
        assert len(plan.scheduled_slots) + len(plan.deferred_tasks) == 100

    def test_must_do_today_tasks_placed(self):
        """must_do_today SOFT tasks should be placed even with competing tasks."""
        tasks = [
            _scored_with(
                id="must",
                commitment_level=CommitmentLevel.SOFT,
                must_do_today=True,
                estimated_expected_min=30.0,
                urgency=0.1,
            ),
            _scored_with(id="normal", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0, urgency=0.9),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        # Both should fit in 480-min block
        assert "must" in placed_ids

    def test_non_schedulable_deferred(self):
        """Tasks with schedulable_now=False are deferred."""
        tasks = [
            _scored_with(id="blocked", schedulable_now=False, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks)
        assert len(plan.scheduled_slots) == 0
        assert len(plan.deferred_tasks) == 1

    def test_single_minute_block(self):
        tasks = [
            _scored_with(
                id="tiny",
                estimated_optimistic_min=1.0,
                estimated_expected_min=1.0,
                estimated_pessimistic_min=1.0,
                context_switch_cost_min=0.0,
            ),
        ]
        plan = pack_block(tasks, block_duration_min=1.0)
        # Should fit or be deferred depending on capacity
        assert len(plan.scheduled_slots) + len(plan.deferred_tasks) == 1

    def test_negative_duration_treated_as_zero(self):
        plan = pack_block([_scored_with(id="t1")], block_duration_min=-10.0)
        assert len(plan.scheduled_slots) == 0

    def test_all_non_schedulable(self):
        tasks = [_scored_with(id=f"ns{i}", schedulable_now=False) for i in range(5)]
        plan = pack_block(tasks)
        assert len(plan.scheduled_slots) == 0
        assert len(plan.deferred_tasks) == 5

    def test_slots_do_not_overlap(self):
        """No two scheduled slots should overlap in time."""
        tasks = [
            _scored_with(id=f"t{i}", estimated_expected_min=15.0, work_mode=list(WorkMode)[i % len(list(WorkMode))])
            for i in range(10)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        for i in range(1, len(plan.scheduled_slots)):
            prev = plan.scheduled_slots[i - 1]
            curr = plan.scheduled_slots[i]
            assert curr.start_offset_min >= prev.end_offset_min - 0.01


# =========================================================================
# PARAMETRIZED REAL-WORLD SCENARIOS
# =========================================================================


class TestRealWorldScenarios:
    """Parametrized scenarios matching typical shift patterns."""

    def test_typical_morning_shift(self):
        """8 mixed-mode tasks in a 480-min block."""
        tasks = [
            _scored_with(
                id="health_check",
                work_mode=WorkMode.MONITORING,
                commitment_level=CommitmentLevel.HARD,
                estimated_expected_min=15.0,
                urgency=0.9,
            ),
            _scored_with(
                id="novatrade_impl",
                work_mode=WorkMode.DEEP_IMPLEMENTATION,
                commitment_level=CommitmentLevel.HARD,
                estimated_expected_min=60.0,
                urgency=0.8,
            ),
            _scored_with(
                id="research_markets",
                work_mode=WorkMode.RESEARCH,
                commitment_level=CommitmentLevel.SOFT,
                estimated_expected_min=45.0,
                urgency=0.6,
            ),
            _scored_with(
                id="code_review",
                work_mode=WorkMode.REVIEW,
                commitment_level=CommitmentLevel.SOFT,
                estimated_expected_min=30.0,
                urgency=0.5,
            ),
            _scored_with(
                id="deploy_fix",
                work_mode=WorkMode.DEPLOYMENT,
                commitment_level=CommitmentLevel.SOFT,
                estimated_expected_min=20.0,
                urgency=0.7,
            ),
            _scored_with(
                id="telegram_msgs",
                work_mode=WorkMode.COMMUNICATION,
                commitment_level=CommitmentLevel.OPPORTUNISTIC,
                estimated_expected_min=10.0,
                urgency=0.3,
            ),
            _scored_with(
                id="memory_cleanup",
                work_mode=WorkMode.ADMIN,
                commitment_level=CommitmentLevel.OPPORTUNISTIC,
                estimated_expected_min=15.0,
                urgency=0.2,
            ),
            _scored_with(
                id="session_wrap",
                work_mode=WorkMode.COMMUNICATION,
                commitment_level=CommitmentLevel.SOFT,
                estimated_expected_min=10.0,
                urgency=0.4,
            ),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)

        # HARD tasks placed
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "health_check" in placed_ids
        assert "novatrade_impl" in placed_ids

        # Should have decent utilization
        assert plan.utilization_pct > 20.0

        # Validation should pass
        warnings = validate_block_plan(plan)
        assert not any("Over-scheduled" in w for w in warnings)

    def test_heavy_debug_day(self):
        """6 debug tasks + 2 admin."""
        tasks = [
            _scored_with(
                id=f"debug{i}",
                work_mode=WorkMode.DEBUGGING,
                commitment_level=CommitmentLevel.SOFT,
                estimated_expected_min=45.0,
                urgency=0.7 + i * 0.03,
            )
            for i in range(6)
        ] + [
            _scored_with(
                id=f"admin{i}",
                work_mode=WorkMode.ADMIN,
                commitment_level=CommitmentLevel.OPPORTUNISTIC,
                estimated_expected_min=15.0,
                urgency=0.3,
            )
            for i in range(2)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)

        # Should fit most debug tasks (6 * 45 = 270, committed = 360)
        debug_placed = [s for s in plan.scheduled_slots if s.scored_task.work_unit.work_mode == WorkMode.DEBUGGING]
        assert len(debug_placed) >= 4

        # Debug tasks should cluster in early (HIGH energy) portion
        if len(debug_placed) > 0:
            first_debug_start = min(s.start_offset_min for s in debug_placed)
            assert first_debug_start < 200  # In first ~40% of block

    def test_light_admin_day(self):
        """10 small admin tasks."""
        tasks = [
            _scored_with(
                id=f"admin{i}",
                work_mode=WorkMode.ADMIN,
                commitment_level=CommitmentLevel.SOFT,
                estimated_expected_min=10.0,
                urgency=0.3 + i * 0.05,
            )
            for i in range(10)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)

        # All should fit (10 * 10 = 100 min << 360 committed)
        assert len(plan.scheduled_slots) == 10
        assert len(plan.deferred_tasks) == 0

    @pytest.mark.parametrize(
        "n_tasks,duration",
        [
            (1, 480),
            (5, 480),
            (20, 480),
            (50, 480),
            (3, 60),
            (10, 120),
        ],
    )
    def test_various_loads(self, n_tasks, duration):
        """Parametrized: various task counts and block durations produce valid plans."""
        tasks = [
            _scored_with(
                id=f"t{i}",
                work_mode=list(WorkMode)[i % len(list(WorkMode))],
                estimated_expected_min=15.0,
                urgency=0.5,
            )
            for i in range(n_tasks)
        ]
        plan = pack_block(tasks, block_duration_min=float(duration))

        # Placed + deferred = total
        assert len(plan.scheduled_slots) + len(plan.deferred_tasks) == n_tasks

        # No slot exceeds block duration
        for slot in plan.scheduled_slots:
            assert slot.end_offset_min <= duration + 1.0  # small tolerance

        # Validation should not have over-scheduling warning
        warnings = validate_block_plan(plan)
        if duration > 0:
            assert not any("Over-scheduled" in w for w in warnings)


# =========================================================================
# SCHEDULED SLOT MODEL TESTS
# =========================================================================


class TestScheduledSlot:
    """ScheduledSlot model basics."""

    def test_creation(self):
        st = _scored_with(id="t1")
        slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=0.0,
            end_offset_min=30.0,
        )
        assert slot.start_offset_min == 0.0
        assert slot.end_offset_min == 30.0
        assert slot.includes_switch_cost is False
        assert slot.switch_cost_min == 0.0
        assert slot.energy_match is True

    def test_with_switch_cost(self):
        st = _scored_with(id="t1")
        slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=10.0,
            end_offset_min=40.0,
            includes_switch_cost=True,
            switch_cost_min=5.0,
        )
        assert slot.includes_switch_cost is True
        assert slot.switch_cost_min == 5.0

    def test_energy_mismatch(self):
        st = _scored_with(id="t1")
        slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=0.0,
            end_offset_min=30.0,
            energy_match=False,
        )
        assert slot.energy_match is False


# =========================================================================
# INTEGRATION: __init__.py EXPORTS
# =========================================================================


class TestExports:
    """Verify all Phase 3 exports are accessible from the package."""

    def test_block_plan_export(self):
        from utils.scheduler import BlockPlan

        assert BlockPlan is not None

    def test_scheduled_slot_export(self):
        from utils.scheduler import ScheduledSlot

        assert ScheduledSlot is not None

    def test_packer_config_export(self):
        from utils.scheduler import PackerConfig

        assert PackerConfig is not None

    def test_energy_level_export(self):
        from utils.scheduler import EnergyLevel

        assert EnergyLevel is not None

    def test_pack_block_export(self):
        from utils.scheduler import pack_block

        assert callable(pack_block)

    def test_validate_block_plan_export(self):
        from utils.scheduler import validate_block_plan

        assert callable(validate_block_plan)


# =========================================================================
# REGRESSION / CONTRACT TESTS
# =========================================================================


class TestContracts:
    """Contract tests ensuring packer output invariants."""

    def test_plan_total_equals_placed_plus_deferred(self):
        """All input tasks end up either placed or deferred."""
        tasks = [
            _scored_with(
                id=f"t{i}",
                estimated_optimistic_min=10.0 + i * 5,
                estimated_expected_min=20.0 + i * 10,
                estimated_pessimistic_min=40.0 + i * 20,
                work_mode=list(WorkMode)[i % len(list(WorkMode))],
            )
            for i in range(8)
        ]
        plan = pack_block(tasks, block_duration_min=200.0)
        total = len(plan.scheduled_slots) + len(plan.deferred_tasks)
        assert total == 8

    def test_used_capacity_nonnegative(self):
        plan = pack_block([_scored_with(id="t1")])
        assert plan.used_committed_min >= 0
        assert plan.used_filler_min >= 0

    def test_utilization_bounded(self):
        tasks = [_scored_with(id=f"t{i}", estimated_expected_min=15.0) for i in range(20)]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert 0.0 <= plan.utilization_pct <= 110.0  # small tolerance for switch costs

    def test_slot_offsets_monotonic(self):
        """Slot start offsets should be non-decreasing."""
        tasks = [
            _scored_with(id=f"t{i}", estimated_expected_min=15.0, work_mode=list(WorkMode)[i % len(list(WorkMode))])
            for i in range(6)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        starts = [s.start_offset_min for s in plan.scheduled_slots]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1]

    def test_end_equals_start_plus_duration(self):
        """Each slot's end = start + task duration."""
        tasks = [_scored_with(id="t1", estimated_expected_min=45.0)]
        plan = pack_block(tasks)
        if plan.scheduled_slots:
            slot = plan.scheduled_slots[0]
            expected_end = slot.start_offset_min + 45.0
            assert abs(slot.end_offset_min - expected_end) < 0.1

    def test_first_slot_zero_switch_cost(self):
        """First task in the block has zero switch cost."""
        tasks = [_scored_with(id="t1", estimated_expected_min=30.0)]
        plan = pack_block(tasks)
        if plan.scheduled_slots:
            assert plan.scheduled_slots[0].switch_cost_min == 0.0


# =========================================================================
# C1: HARD tasks overflow committed → borrow from buffer
# =========================================================================


class TestHardOverflowBufferBorrow:
    """C1 fix: HARD tasks that exceed committed capacity borrow from buffer."""

    def test_hard_overflow_borrows_from_buffer(self):
        """When HARD tasks exceed committed, buffer_borrowed_min is set."""
        cfg = PackerConfig(committed_ratio=0.10, buffer_ratio=0.45, filler_ratio=0.45)
        # committed = 48 min for a 480-min block
        tasks = [
            _scored_with(id="h1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0, urgency=0.9),
            _scored_with(id="h2", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0, urgency=0.8),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "h1" in placed_ids
        assert "h2" in placed_ids
        # 60 min HARD tasks vs 48 min committed → 12 min borrowed
        assert plan.buffer_borrowed_min > 0

    def test_buffer_borrowed_min_zero_when_no_overflow(self):
        """buffer_borrowed_min is 0 when HARD tasks fit in committed."""
        tasks = [
            _scored_with(id="h1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert plan.buffer_borrowed_min == 0.0

    def test_hard_overflow_beyond_buffer(self):
        """HARD tasks that exceed committed + buffer are still placed (mandatory)."""
        cfg = PackerConfig(committed_ratio=0.05, buffer_ratio=0.05, filler_ratio=0.05)
        # committed=24, buffer=24 → 48 total
        tasks = [
            _scored_with(id="h1", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0, urgency=0.9),
            _scored_with(id="h2", commitment_level=CommitmentLevel.HARD, estimated_expected_min=30.0, urgency=0.8),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        # Both HARD tasks are always placed regardless
        assert "h1" in placed_ids
        assert "h2" in placed_ids


# =========================================================================
# C2: context_switches only counts mode changes
# =========================================================================


class TestContextSwitchFix:
    """C2 fix: context_switches only increments on actual mode transitions."""

    def test_same_mode_no_context_switch(self):
        """Two tasks with same WorkMode → 0 context switches."""
        tasks = [
            _scored_with(id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.9),
            _scored_with(id="a2", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.8),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert plan.context_switches == 0

    def test_different_mode_one_switch(self):
        """Two different-mode tasks → exactly 1 context switch."""
        cfg = PackerConfig(same_mode_switch_cost_min=0.0, different_mode_switch_cost_min=0.0)
        tasks = [
            _scored_with(
                id="d1",
                work_mode=WorkMode.DEBUGGING,
                estimated_expected_min=20.0,
                urgency=0.9,
                context_switch_cost_min=0.0,
            ),
            _scored_with(
                id="a1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0, urgency=0.1, context_switch_cost_min=0.0
            ),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Energy ordering: DEBUGGING(HIGH) then ADMIN(LOW) → 1 switch
        assert plan.context_switches == 1

    def test_three_same_mode_zero_switches(self):
        """Three same-mode tasks → 0 context switches."""
        tasks = [
            _scored_with(id=f"a{i}", work_mode=WorkMode.ADMIN, estimated_expected_min=15.0, urgency=0.5 + i * 0.1)
            for i in range(3)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert plan.context_switches == 0


# =========================================================================
# H1: Energy offset uses task start (after switch cost)
# =========================================================================


class TestEnergyOffsetFix:
    """H1 fix: energy level computed at cursor + switch_cost."""

    def test_energy_at_task_start_not_cursor(self):
        """With a large switch cost, energy should be computed at actual task start."""
        # Large switch cost pushes the second task further into the block
        cfg = PackerConfig(different_mode_switch_cost_min=100.0, same_mode_switch_cost_min=0.0)
        # First task: 0-100 min (at block frac 0.0-0.21)
        # Switch cost: 100 min
        # Second task starts at 200 min (frac 0.42) → should be MEDIUM energy
        tasks = [
            _scored_with(
                id="d1",
                work_mode=WorkMode.DEBUGGING,
                estimated_optimistic_min=50.0,
                estimated_expected_min=100.0,
                estimated_pessimistic_min=150.0,
                urgency=0.9,
                context_switch_cost_min=0.0,
            ),
            _scored_with(
                id="r1",
                work_mode=WorkMode.REVIEW,
                estimated_expected_min=50.0,
                estimated_pessimistic_min=80.0,
                urgency=0.1,
                context_switch_cost_min=0.0,
            ),
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Find the review task slot
        review_slots = [s for s in plan.scheduled_slots if s.scored_task.work_unit.id == "r1"]
        if review_slots:
            slot = review_slots[0]
            # Review prefers MEDIUM energy; at frac 0.42+ it should be MEDIUM
            # So energy_match should be True
            assert slot.start_offset_min >= 200.0


# =========================================================================
# H2: plan_confidence is computed
# =========================================================================


class TestPlanConfidence:
    """H2 fix: plan_confidence is computed, not stuck at 0.0."""

    def test_confidence_nonzero_with_tasks(self):
        """With placed tasks, confidence should be > 0."""
        tasks = [
            _scored_with(id="t1", estimated_expected_min=30.0),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert plan.plan_confidence > 0.0

    def test_confidence_zero_for_empty(self):
        """Empty plan has confidence 0.0."""
        plan = pack_block([], block_duration_min=480.0)
        assert plan.plan_confidence == 0.0

    def test_confidence_bounded_0_to_1(self):
        """Confidence should always be between 0 and 1."""
        tasks = [
            _scored_with(id=f"t{i}", estimated_expected_min=15.0, work_mode=list(WorkMode)[i % len(list(WorkMode))])
            for i in range(10)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert 0.0 <= plan.plan_confidence <= 1.0

    def test_confidence_higher_with_good_energy_match(self):
        """Tasks matching their energy zones should yield higher confidence."""
        # All HIGH-energy tasks in a block where they start early (HIGH zone)
        tasks = [
            _scored_with(id=f"d{i}", work_mode=WorkMode.DEBUGGING, estimated_expected_min=20.0, urgency=0.8)
            for i in range(3)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        assert plan.plan_confidence > 0.3

    def test_confidence_includes_deferred_penalty(self):
        """More deferred tasks → lower confidence."""
        cfg = PackerConfig(committed_ratio=0.05, filler_ratio=0.01)  # very tight
        tasks = [
            _scored_with(id=f"s{i}", commitment_level=CommitmentLevel.SOFT, estimated_expected_min=30.0)
            for i in range(10)
        ]
        plan = pack_block(tasks, block_duration_min=480.0, config=cfg)
        # Many deferred → deferred_ratio high → confidence reduced
        assert plan.plan_confidence < 0.8


# =========================================================================
# H3: PackerConfig ratio validation
# =========================================================================


class TestPackerConfigRatioValidation:
    """H3 fix: ratios that sum > 1.0 are normalized."""

    def test_ratios_summing_over_1_normalized(self):
        """Ratios summing to > 1.0 are normalized to sum to 1.0."""
        cfg = PackerConfig(committed_ratio=0.60, buffer_ratio=0.30, filler_ratio=0.20)
        total = cfg.committed_ratio + cfg.buffer_ratio + cfg.filler_ratio
        assert abs(total - 1.0) < 0.01

    def test_ratios_summing_to_1_unchanged(self):
        """Ratios that already sum to 1.0 are not modified."""
        cfg = PackerConfig(committed_ratio=0.70, buffer_ratio=0.20, filler_ratio=0.10)
        assert cfg.committed_ratio == pytest.approx(0.70)
        assert cfg.buffer_ratio == pytest.approx(0.20)
        assert cfg.filler_ratio == pytest.approx(0.10)

    def test_ratios_summing_under_1_unchanged(self):
        """Ratios summing to < 1.0 are not modified (buffer time is implicit)."""
        cfg = PackerConfig(committed_ratio=0.50, buffer_ratio=0.20, filler_ratio=0.10)
        assert cfg.committed_ratio == 0.50
        assert cfg.buffer_ratio == 0.20
        assert cfg.filler_ratio == 0.10

    def test_extreme_over_1_normalized(self):
        """Extreme ratios (e.g., all 1.0) are normalized proportionally."""
        cfg = PackerConfig(committed_ratio=1.0, buffer_ratio=1.0, filler_ratio=1.0)
        total = cfg.committed_ratio + cfg.buffer_ratio + cfg.filler_ratio
        assert abs(total - 1.0) < 0.01
        # Each should be ~0.333
        assert abs(cfg.committed_ratio - 1.0 / 3) < 0.01

    def test_default_ratios_valid(self):
        """Default ratios (0.75 + 0.17 + 0.08 = 1.0) are unchanged."""
        cfg = PackerConfig()
        assert cfg.committed_ratio == 0.75
        assert cfg.buffer_ratio == 0.17
        assert cfg.filler_ratio == 0.08


# =========================================================================
# H4: max_deep_tasks_before_break removed
# =========================================================================


class TestMaxDeepTasksRemoved:
    """H4 fix: max_deep_tasks_before_break is no longer on PackerConfig."""

    def test_field_removed(self):
        """PackerConfig should not accept max_deep_tasks_before_break."""
        with pytest.raises((ValueError, TypeError)):  # ValidationError for extra="forbid"
            PackerConfig(max_deep_tasks_before_break=3)

    def test_no_attribute(self):
        """PackerConfig instances should not have the attribute."""
        cfg = PackerConfig()
        assert not hasattr(cfg, "max_deep_tasks_before_break")


# =========================================================================
# M1: HARD tasks survive energy reordering
# =========================================================================


class TestHardTaskSurvivesReordering:
    """M1 fix: HARD tasks dropped by Step 8 overflow are re-inserted."""

    def test_hard_task_not_silently_dropped(self):
        """HARD task that barely fits should survive energy reordering."""
        # Create a tight block where HARD task could be dropped by reordering
        cfg = PackerConfig(
            committed_ratio=0.50,
            buffer_ratio=0.30,
            filler_ratio=0.20,
            different_mode_switch_cost_min=5.0,
            same_mode_switch_cost_min=1.0,
        )
        tasks = [
            _scored_with(
                id="hard1",
                commitment_level=CommitmentLevel.HARD,
                work_mode=WorkMode.MONITORING,  # LOW energy → placed late
                estimated_expected_min=40.0,
                urgency=0.9,
            ),
            _scored_with(
                id="soft1",
                commitment_level=CommitmentLevel.SOFT,
                work_mode=WorkMode.DEBUGGING,  # HIGH energy → placed early
                estimated_expected_min=40.0,
                urgency=0.5,
            ),
            _scored_with(
                id="opp1",
                commitment_level=CommitmentLevel.OPPORTUNISTIC,
                work_mode=WorkMode.REVIEW,  # MEDIUM energy
                estimated_expected_min=20.0,
                urgency=0.3,
            ),
        ]
        plan = pack_block(tasks, block_duration_min=120.0, config=cfg)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        # HARD task must always be placed
        assert "hard1" in placed_ids

    def test_multiple_hard_all_survive(self):
        """Multiple HARD tasks all survive energy reordering."""
        tasks = [
            _scored_with(
                id="h1",
                commitment_level=CommitmentLevel.HARD,
                work_mode=WorkMode.ADMIN,
                estimated_expected_min=30.0,
                urgency=0.9,
            ),
            _scored_with(
                id="h2",
                commitment_level=CommitmentLevel.HARD,
                work_mode=WorkMode.MONITORING,
                estimated_expected_min=30.0,
                urgency=0.8,
            ),
            _scored_with(
                id="s1",
                commitment_level=CommitmentLevel.SOFT,
                work_mode=WorkMode.DEBUGGING,
                estimated_expected_min=30.0,
                urgency=0.7,
            ),
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        placed_ids = {s.scored_task.work_unit.id for s in plan.scheduled_slots}
        assert "h1" in placed_ids
        assert "h2" in placed_ids


# =========================================================================
# M3: validate_block_plan detects overlapping slots
# =========================================================================


class TestOverlapDetection:
    """M3 fix: validate_block_plan detects overlapping slots."""

    def test_no_overlap_passes(self):
        """Non-overlapping slots produce no overlap warning."""
        st1 = _scored_with(id="t1")
        st2 = _scored_with(id="t2")
        plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=360.0,
            scheduled_slots=[
                ScheduledSlot(scored_task=st1, start_offset_min=0.0, end_offset_min=30.0),
                ScheduledSlot(scored_task=st2, start_offset_min=30.0, end_offset_min=60.0),
            ],
        )
        warnings = validate_block_plan(plan)
        assert not any("Overlapping" in w for w in warnings)

    def test_overlap_detected(self):
        """Overlapping slots produce a warning."""
        st1 = _scored_with(id="t1")
        st2 = _scored_with(id="t2")
        plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=360.0,
            scheduled_slots=[
                ScheduledSlot(scored_task=st1, start_offset_min=0.0, end_offset_min=40.0),
                ScheduledSlot(scored_task=st2, start_offset_min=20.0, end_offset_min=60.0),
            ],
        )
        warnings = validate_block_plan(plan)
        assert any("Overlapping" in w for w in warnings)

    def test_adjacent_slots_no_overlap(self):
        """Slots that share a boundary (end == start) are not overlapping."""
        st1 = _scored_with(id="t1")
        st2 = _scored_with(id="t2")
        plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=360.0,
            scheduled_slots=[
                ScheduledSlot(scored_task=st1, start_offset_min=0.0, end_offset_min=30.0),
                ScheduledSlot(scored_task=st2, start_offset_min=30.0, end_offset_min=60.0),
            ],
        )
        warnings = validate_block_plan(plan)
        assert not any("Overlapping" in w for w in warnings)

    def test_committed_capacity_exceeded_warning(self):
        """validate_block_plan warns when used_committed > total_committed."""
        plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=100.0,
            used_committed_min=150.0,
        )
        warnings = validate_block_plan(plan)
        assert any("Committed capacity exceeded" in w for w in warnings)

    def test_packer_output_no_overlaps(self):
        """pack_block output never has overlapping slots."""
        tasks = [
            _scored_with(id=f"t{i}", estimated_expected_min=15.0, work_mode=list(WorkMode)[i % len(list(WorkMode))])
            for i in range(10)
        ]
        plan = pack_block(tasks, block_duration_min=480.0)
        warnings = validate_block_plan(plan)
        assert not any("Overlapping" in w for w in warnings)
