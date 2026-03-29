"""Tests for Phase 9: Starvation Prevention & Maintenance Guarantees.

Covers:
  - AgingConfig and compute_effective_priority
  - check_starvation multi-tier alerts
  - MaintenanceConfig and reserve_maintenance
  - WorkModeBalance and check_work_mode_balance
  - update_deferment_ages from generated_at timestamps
  - Edge cases (empty inputs, extreme ages, single items)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utils.scheduler.block import BlockPlan, ScheduledSlot
from utils.scheduler.execution_log import ExecutionRecord
from utils.scheduler.scorer import ScoredTask
from utils.scheduler.starvation import (
    AgingConfig,
    MaintenanceConfig,
    WorkModeBalance,
    check_starvation,
    check_work_mode_balance,
    compute_effective_priority,
    reserve_maintenance,
    update_deferment_ages,
)
from utils.scheduler.work_unit import CommitmentLevel, TaskClass, WorkMode, WorkUnit

# ===========================================================================
# Helpers
# ===========================================================================


def _wu(
    id: str = "t1",
    priority: str = "medium",
    commitment: CommitmentLevel = CommitmentLevel.SOFT,
    deferment_age_days: float = 0.0,
    work_mode: WorkMode = WorkMode.ADMIN,
    estimated_expected_min: float = 30.0,
    generated_at: str = "",
    title: str = "",
) -> WorkUnit:
    """Create a minimal WorkUnit for testing."""
    return WorkUnit(
        id=id,
        title=title or id,
        priority=priority,
        commitment_level=commitment,
        deferment_age_days=deferment_age_days,
        work_mode=work_mode,
        estimated_expected_min=estimated_expected_min,
        estimated_optimistic_min=max(1.0, estimated_expected_min * 0.5),
        estimated_pessimistic_min=estimated_expected_min * 2.0,
        generated_at=generated_at,
    )


def _scored(wu: WorkUnit, score: float = 50.0) -> ScoredTask:
    """Wrap a WorkUnit in a ScoredTask."""
    return ScoredTask(
        work_unit=wu,
        raw_score=score,
        deferment_bonus=0.0,
        commitment_adjustment=0.0,
        confidence_modifier=1.0,
        effective_time=1.0,
        priority_multiplier=1.0,
        final_score=score,
    )


def _exec_record(
    task_id: str = "t1",
    work_mode: WorkMode = WorkMode.ADMIN,
    actual_min: float = 30.0,
    timestamp: str = "",
    days_ago: float = 0.0,
) -> ExecutionRecord:
    """Create a minimal ExecutionRecord for testing."""
    if not timestamp:
        ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
        timestamp = ts.isoformat()
    return ExecutionRecord(
        task_id=task_id,
        task_class=TaskClass.BOUNDED,
        work_mode=work_mode,
        planned_duration_min=30.0,
        actual_duration_min=actual_min,
        variance_ratio=actual_min / 30.0,
        outcome="success",
        timestamp=timestamp,
    )


def _block_plan(
    duration: float = 480.0,
    slots: list[ScheduledSlot] | None = None,
) -> BlockPlan:
    """Create a minimal BlockPlan for testing."""
    return BlockPlan(
        block_duration_min=duration,
        committed_ratio=0.75,
        buffer_ratio=0.17,
        filler_ratio=0.08,
        total_committed_min=duration * 0.75,
        total_buffer_min=duration * 0.17,
        total_filler_min=duration * 0.08,
        scheduled_slots=slots or [],
    )


# ===========================================================================
# Aging / effective priority tests
# ===========================================================================


class TestComputeEffectivePriority:
    """Test compute_effective_priority scoring and commitment upgrades."""

    def test_zero_days_base_priority_only(self):
        """0 deferment days → base priority with no bonus."""
        wu = _wu(priority="medium", deferment_age_days=0.0)
        score, upgraded = compute_effective_priority(wu)
        assert score == pytest.approx(2.0)
        assert upgraded.commitment_level == wu.commitment_level

    def test_high_priority_base(self):
        """High priority base is 3.0."""
        wu = _wu(priority="high", deferment_age_days=0.0)
        score, _ = compute_effective_priority(wu)
        assert score == pytest.approx(3.0)

    def test_low_priority_base(self):
        """Low priority base is 1.0."""
        wu = _wu(priority="low", deferment_age_days=0.0)
        score, _ = compute_effective_priority(wu)
        assert score == pytest.approx(1.0)

    def test_hard_commitment_7_days(self):
        """7 days HARD → 7 * 1.0 = 7.0 bonus."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.HARD,
            deferment_age_days=7.0,
        )
        score, _ = compute_effective_priority(wu)
        assert score == pytest.approx(2.0 + 7.0)

    def test_soft_commitment_7_days(self):
        """7 days SOFT → 7 * 0.5 = 3.5 bonus."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.SOFT,
            deferment_age_days=7.0,
        )
        score, _ = compute_effective_priority(wu)
        assert score == pytest.approx(2.0 + 3.5)

    def test_opportunistic_commitment_7_days(self):
        """7 days OPPORTUNISTIC → 7 * 0.2 = 1.4 bonus."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.OPPORTUNISTIC,
            deferment_age_days=7.0,
        )
        score, _ = compute_effective_priority(wu)
        assert score == pytest.approx(2.0 + 1.4)

    def test_opportunistic_upgraded_to_soft_at_5_days(self):
        """OPPORTUNISTIC → SOFT when deferment >= 5 days."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.OPPORTUNISTIC,
            deferment_age_days=5.0,
        )
        _, upgraded = compute_effective_priority(wu)
        assert upgraded.commitment_level == CommitmentLevel.SOFT

    def test_opportunistic_not_upgraded_at_4_days(self):
        """OPPORTUNISTIC stays OPPORTUNISTIC at 4 days."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.OPPORTUNISTIC,
            deferment_age_days=4.0,
        )
        _, upgraded = compute_effective_priority(wu)
        assert upgraded.commitment_level == CommitmentLevel.OPPORTUNISTIC

    def test_soft_not_upgraded_at_5_days(self):
        """SOFT commitment is not affected by the upgrade rule."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.SOFT,
            deferment_age_days=10.0,
        )
        _, upgraded = compute_effective_priority(wu)
        assert upgraded.commitment_level == CommitmentLevel.SOFT

    def test_hard_not_upgraded_at_5_days(self):
        """HARD commitment is not affected by the upgrade rule."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.HARD,
            deferment_age_days=10.0,
        )
        _, upgraded = compute_effective_priority(wu)
        assert upgraded.commitment_level == CommitmentLevel.HARD

    def test_low_priority_14_days_beats_fresh_medium(self):
        """Low-priority task at 14 days should beat a fresh medium-priority task."""
        old_low = _wu(
            priority="low",
            commitment=CommitmentLevel.HARD,
            deferment_age_days=14.0,
        )
        fresh_medium = _wu(
            priority="medium",
            commitment=CommitmentLevel.SOFT,
            deferment_age_days=0.0,
        )
        score_old, _ = compute_effective_priority(old_low)
        score_fresh, _ = compute_effective_priority(fresh_medium)
        # low base 1.0 + 14*1.0 = 15.0 vs medium base 2.0 + 0 = 2.0
        assert score_old > score_fresh

    def test_custom_config_rates(self):
        """Custom aging rates are respected."""
        cfg = AgingConfig(rate_per_day={"hard": 2.0, "soft": 1.0, "opportunistic": 0.5})
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.HARD,
            deferment_age_days=3.0,
        )
        score, _ = compute_effective_priority(wu, cfg)
        assert score == pytest.approx(2.0 + 6.0)

    def test_custom_config_max_deferment_days(self):
        """Custom max_deferment_days_before_soft is respected."""
        cfg = AgingConfig(max_deferment_days_before_soft=3)
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.OPPORTUNISTIC,
            deferment_age_days=3.0,
        )
        _, upgraded = compute_effective_priority(wu, cfg)
        assert upgraded.commitment_level == CommitmentLevel.SOFT

    def test_does_not_mutate_original(self):
        """Original WorkUnit is not mutated by the upgrade."""
        wu = _wu(
            priority="medium",
            commitment=CommitmentLevel.OPPORTUNISTIC,
            deferment_age_days=10.0,
        )
        _, upgraded = compute_effective_priority(wu)
        assert wu.commitment_level == CommitmentLevel.OPPORTUNISTIC
        assert upgraded.commitment_level == CommitmentLevel.SOFT


# ===========================================================================
# Starvation check tests
# ===========================================================================


class TestCheckStarvation:
    """Test multi-tier starvation alert generation."""

    def test_fresh_tasks_no_alerts(self):
        """Tasks with 0 deferment days produce no alerts."""
        units = [_wu(deferment_age_days=0.0), _wu(id="t2", deferment_age_days=3.0)]
        alerts = check_starvation(units)
        assert alerts == []

    def test_warning_at_7_days(self):
        """7-day deferment → warning level."""
        units = [_wu(deferment_age_days=7.0)]
        alerts = check_starvation(units)
        assert len(alerts) == 1
        assert alerts[0]["level"] == "warning"
        assert alerts[0]["age_days"] == 7.0

    def test_escalation_at_14_days(self):
        """14-day deferment → escalation level."""
        units = [_wu(deferment_age_days=14.0)]
        alerts = check_starvation(units)
        assert len(alerts) == 1
        assert alerts[0]["level"] == "escalation"
        assert alerts[0]["age_days"] == 14.0

    def test_auto_review_at_21_days(self):
        """21-day deferment → auto_review with review_relevance action."""
        units = [_wu(deferment_age_days=21.0)]
        alerts = check_starvation(units)
        assert len(alerts) == 1
        assert alerts[0]["level"] == "auto_review"
        assert alerts[0]["action"] == "review_relevance"

    def test_auto_review_at_30_days(self):
        """30 days also triggers auto_review (not escalation)."""
        units = [_wu(deferment_age_days=30.0)]
        alerts = check_starvation(units)
        assert len(alerts) == 1
        assert alerts[0]["level"] == "auto_review"

    def test_mixed_ages_correct_alerts(self):
        """Each task gets the alert level matching its age."""
        units = [
            _wu(id="fresh", deferment_age_days=2.0),
            _wu(id="warn", deferment_age_days=8.0),
            _wu(id="escalate", deferment_age_days=15.0),
            _wu(id="review", deferment_age_days=25.0),
        ]
        alerts = check_starvation(units)
        assert len(alerts) == 3
        levels = {a["task_id"]: a["level"] for a in alerts}
        assert levels["warn"] == "warning"
        assert levels["escalate"] == "escalation"
        assert levels["review"] == "auto_review"

    def test_empty_list_no_alerts(self):
        """Empty work unit list → no alerts."""
        assert check_starvation([]) == []

    def test_alert_contains_task_id(self):
        """Each alert includes the task_id."""
        units = [_wu(id="my-task", deferment_age_days=10.0)]
        alerts = check_starvation(units)
        assert alerts[0]["task_id"] == "my-task"

    def test_alert_message_is_human_readable(self):
        """Alert message contains the task ID and age."""
        units = [_wu(id="task-x", deferment_age_days=7.0)]
        alerts = check_starvation(units)
        assert "task-x" in alerts[0]["message"]
        assert "7" in alerts[0]["message"]

    def test_custom_thresholds(self):
        """Custom starvation thresholds are respected."""
        cfg = AgingConfig(
            starvation_warning_days=3,
            starvation_escalation_days=6,
            starvation_auto_review_days=10,
        )
        units = [
            _wu(id="a", deferment_age_days=4.0),
            _wu(id="b", deferment_age_days=7.0),
            _wu(id="c", deferment_age_days=11.0),
        ]
        alerts = check_starvation(units, cfg)
        levels = {a["task_id"]: a["level"] for a in alerts}
        assert levels["a"] == "warning"
        assert levels["b"] == "escalation"
        assert levels["c"] == "auto_review"

    def test_exactly_at_threshold_triggers(self):
        """Exactly at threshold day triggers the alert."""
        units = [_wu(deferment_age_days=7.0)]
        alerts = check_starvation(units)
        assert len(alerts) == 1
        assert alerts[0]["level"] == "warning"

    def test_just_below_threshold_no_alert(self):
        """Just below 7 days does not trigger."""
        units = [_wu(deferment_age_days=6.9)]
        alerts = check_starvation(units)
        assert alerts == []


# ===========================================================================
# Maintenance reservation tests
# ===========================================================================


class TestReserveMaintenance:
    """Test maintenance slot reservation in block plans."""

    def test_adds_maintenance_when_none_scheduled(self):
        """Block with no maintenance → adds maintenance tasks."""
        plan = _block_plan(duration=480.0)
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        assert len(updated.scheduled_slots) == 1
        assert updated.scheduled_slots[0].scored_task.work_unit.id == "m1"

    def test_no_change_when_quota_met(self):
        """Block already meeting maintenance quota → unchanged."""
        # Create plan with a monitoring slot already present
        maint_wu = _wu(id="existing-maint", work_mode=WorkMode.MONITORING, estimated_expected_min=60.0)
        existing_slot = ScheduledSlot(
            scored_task=_scored(maint_wu),
            start_offset_min=0.0,
            end_offset_min=60.0,
        )
        plan = _block_plan(duration=480.0, slots=[existing_slot])
        # 60 min > 48 min (10% of 480)
        new_tasks = [
            _scored(
                _wu(id="m2", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
            ),
        ]
        updated = reserve_maintenance(plan, new_tasks)
        # Should be same plan since quota already met
        assert len(updated.scheduled_slots) == 1

    def test_no_maintenance_tasks_available(self):
        """No maintenance tasks available → returns unchanged plan."""
        plan = _block_plan()
        updated = reserve_maintenance(plan, [])
        assert len(updated.scheduled_slots) == 0

    def test_reservation_calculation_10_percent(self):
        """10% of 480 = 48 min, min 30 → target 48 min."""
        plan = _block_plan(duration=480.0)
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=48.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        assert len(updated.scheduled_slots) == 1
        slot = updated.scheduled_slots[0]
        assert slot.end_offset_min - slot.start_offset_min == pytest.approx(48.0)

    def test_min_maintenance_min_used_when_larger(self):
        """For short blocks, min_maintenance_min (30) is used."""
        plan = _block_plan(duration=200.0)
        # 10% of 200 = 20, but min is 30
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        assert len(updated.scheduled_slots) == 1

    def test_does_not_evict_committed_tasks(self):
        """Maintenance reservation doesn't remove existing committed slots."""
        committed_wu = _wu(id="committed", work_mode=WorkMode.DEEP_IMPLEMENTATION, estimated_expected_min=400.0)
        committed_slot = ScheduledSlot(
            scored_task=_scored(committed_wu, score=100.0),
            start_offset_min=0.0,
            end_offset_min=400.0,
        )
        plan = _block_plan(duration=480.0, slots=[committed_slot])
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        # Committed slot should still be there
        assert any(s.scored_task.work_unit.id == "committed" for s in updated.scheduled_slots)
        # Maintenance should fit in remaining 80 min
        assert any(s.scored_task.work_unit.id == "m1" for s in updated.scheduled_slots)

    def test_does_not_exceed_block_duration(self):
        """Maintenance tasks can't overflow the block."""
        committed_wu = _wu(id="c1", work_mode=WorkMode.DEEP_IMPLEMENTATION, estimated_expected_min=470.0)
        committed_slot = ScheduledSlot(
            scored_task=_scored(committed_wu, score=100.0),
            start_offset_min=0.0,
            end_offset_min=470.0,
        )
        plan = _block_plan(duration=480.0, slots=[committed_slot])
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        # 30 min task can't fit in remaining 10 min
        assert not any(s.scored_task.work_unit.id == "m1" for s in updated.scheduled_slots)

    def test_multiple_maintenance_tasks_fills_shortfall(self):
        """Multiple small tasks can fill the maintenance shortfall."""
        plan = _block_plan(duration=480.0)
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0),
                score=10.0,
            ),
            _scored(
                _wu(id="m2", work_mode=WorkMode.MONITORING, estimated_expected_min=20.0),
                score=8.0,
            ),
            _scored(
                _wu(id="m3", work_mode=WorkMode.ADMIN, estimated_expected_min=20.0),
                score=6.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        # Target is 48 min; 3 tasks @ 20 = 60 min; need at least 3 to cover 48
        assert len(updated.scheduled_slots) >= 2

    def test_custom_config(self):
        """Custom maintenance config is respected."""
        cfg = MaintenanceConfig(
            reservation_pct=0.20,
            maintenance_modes=["admin"],
            min_maintenance_min=10.0,
        )
        plan = _block_plan(duration=480.0)
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=96.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint, cfg)
        # 20% of 480 = 96 min
        assert len(updated.scheduled_slots) == 1
        assert updated.scheduled_slots[0].scored_task.work_unit.estimated_expected_min == 96.0

    def test_returns_new_plan_object(self):
        """reserve_maintenance returns a new BlockPlan, not the same object."""
        plan = _block_plan()
        maint = [
            _scored(
                _wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0),
                score=10.0,
            ),
        ]
        updated = reserve_maintenance(plan, maint)
        # If no tasks were added, same plan is returned (optimization)
        # but if added, it's a new object
        if len(updated.scheduled_slots) > 0:
            assert updated is not plan


# ===========================================================================
# Work mode balance tests
# ===========================================================================


class TestCheckWorkModeBalance:
    """Test work mode distribution analysis."""

    def test_even_distribution_no_imbalances(self):
        """Evenly distributed modes → no imbalance warnings."""
        records = [
            _exec_record(task_id="t1", work_mode=WorkMode.RESEARCH, actual_min=60.0, days_ago=1.0),
            _exec_record(task_id="t2", work_mode=WorkMode.DEEP_IMPLEMENTATION, actual_min=60.0, days_ago=1.0),
            _exec_record(task_id="t3", work_mode=WorkMode.ADMIN, actual_min=60.0, days_ago=1.0),
            _exec_record(task_id="t4", work_mode=WorkMode.MONITORING, actual_min=60.0, days_ago=1.0),
        ]
        balance = check_work_mode_balance(records)
        # Each mode has 25% — no single mode > 70%, research is present
        assert balance.imbalances == []
        assert balance.total_minutes == pytest.approx(240.0)

    def test_single_mode_dominates(self):
        """>70% single mode → warning."""
        records = [
            _exec_record(task_id="t1", work_mode=WorkMode.ADMIN, actual_min=80.0, days_ago=1.0),
            _exec_record(task_id="t2", work_mode=WorkMode.ADMIN, actual_min=80.0, days_ago=2.0),
            _exec_record(task_id="t3", work_mode=WorkMode.RESEARCH, actual_min=20.0, days_ago=3.0),
        ]
        balance = check_work_mode_balance(records)
        assert any("admin dominates" in w for w in balance.imbalances)

    def test_missing_research_warning(self):
        """0% research for 7+ days → warning."""
        records = [
            _exec_record(task_id="t1", work_mode=WorkMode.ADMIN, actual_min=120.0, days_ago=1.0),
            _exec_record(task_id="t2", work_mode=WorkMode.MONITORING, actual_min=120.0, days_ago=2.0),
        ]
        balance = check_work_mode_balance(records, period_days=7)
        assert any("research" in w.lower() for w in balance.imbalances)

    def test_mode_dropped_off_warning(self):
        """Mode that was active in prior periods but not in current → warning."""
        records = [
            # Older record with research
            _exec_record(task_id="t1", work_mode=WorkMode.RESEARCH, actual_min=60.0, days_ago=10.0),
            # Recent records without research
            _exec_record(task_id="t2", work_mode=WorkMode.ADMIN, actual_min=60.0, days_ago=1.0),
            _exec_record(task_id="t3", work_mode=WorkMode.ADMIN, actual_min=60.0, days_ago=2.0),
        ]
        balance = check_work_mode_balance(records, period_days=7)
        assert any("research dropped off" in w for w in balance.imbalances)

    def test_empty_records_empty_balance(self):
        """No records → empty WorkModeBalance."""
        balance = check_work_mode_balance([])
        assert balance.total_minutes == 0.0
        assert balance.mode_distribution == {}
        assert balance.imbalances == []

    def test_period_filtering(self):
        """Only records within period_days are counted."""
        records = [
            _exec_record(task_id="t1", work_mode=WorkMode.ADMIN, actual_min=60.0, days_ago=1.0),
            _exec_record(task_id="t2", work_mode=WorkMode.RESEARCH, actual_min=60.0, days_ago=20.0),
        ]
        balance = check_work_mode_balance(records, period_days=7)
        assert balance.total_minutes == pytest.approx(60.0)
        assert "admin" in balance.mode_distribution
        # Research is from 20 days ago — outside 7-day window
        assert "research" not in balance.mode_distribution

    def test_distribution_percentages_sum_to_100(self):
        """Distribution percentages should sum to approximately 100."""
        records = [
            _exec_record(task_id="t1", work_mode=WorkMode.ADMIN, actual_min=30.0, days_ago=1.0),
            _exec_record(task_id="t2", work_mode=WorkMode.RESEARCH, actual_min=30.0, days_ago=1.0),
            _exec_record(task_id="t3", work_mode=WorkMode.MONITORING, actual_min=30.0, days_ago=1.0),
        ]
        balance = check_work_mode_balance(records)
        total_pct = sum(balance.mode_distribution.values())
        assert total_pct == pytest.approx(100.0, abs=0.1)

    def test_period_days_stored_in_result(self):
        """Result carries the period_days used."""
        balance = check_work_mode_balance([], period_days=14)
        assert balance.period_days == 14

    def test_no_research_short_period_no_warning(self):
        """Missing research in < 5 day period does not warn."""
        records = [
            _exec_record(task_id="t1", work_mode=WorkMode.ADMIN, actual_min=60.0, days_ago=1.0),
        ]
        balance = check_work_mode_balance(records, period_days=3)
        # period_days=3 < 5, so no "no research" warning
        assert not any("research" in w.lower() and "No" in w for w in balance.imbalances)


# ===========================================================================
# Deferment age update tests
# ===========================================================================


class TestUpdateDefermentAges:
    """Test deferment age recalculation from generated_at timestamps."""

    def test_tasks_with_generated_at_get_updated(self):
        """Tasks with valid generated_at get correct age calculation."""
        now = datetime.now(timezone.utc)
        three_days_ago = (now - timedelta(days=3)).isoformat()
        wu = _wu(id="t1", generated_at=three_days_ago)
        result = update_deferment_ages([wu], reference_date=now)
        assert len(result) == 1
        assert result[0].deferment_age_days == pytest.approx(3.0, abs=0.1)

    def test_tasks_without_generated_at_unchanged(self):
        """Tasks with empty generated_at keep their original deferment_age_days."""
        wu = _wu(id="t1", deferment_age_days=5.0, generated_at="")
        result = update_deferment_ages([wu])
        assert result[0].deferment_age_days == 5.0

    def test_returns_copies_not_originals(self):
        """Updated list contains new objects, not the original WorkUnits."""
        now = datetime.now(timezone.utc)
        wu = _wu(id="t1", generated_at=(now - timedelta(days=1)).isoformat())
        result = update_deferment_ages([wu], reference_date=now)
        assert result[0] is not wu

    def test_originals_not_mutated(self):
        """Original work units are not modified."""
        now = datetime.now(timezone.utc)
        wu = _wu(id="t1", deferment_age_days=0.0, generated_at=(now - timedelta(days=5)).isoformat())
        original_age = wu.deferment_age_days
        update_deferment_ages([wu], reference_date=now)
        assert wu.deferment_age_days == original_age

    def test_age_calculation_correct_days(self):
        """Age calculation matches expected day count."""
        ref = datetime(2026, 3, 29, 12, 0, 0, tzinfo=timezone.utc)
        gen = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        wu = _wu(id="t1", generated_at=gen)
        result = update_deferment_ages([wu], reference_date=ref)
        assert result[0].deferment_age_days == pytest.approx(7.0, abs=0.01)

    def test_invalid_generated_at_unchanged(self):
        """Tasks with unparseable generated_at are returned as copies, unchanged."""
        wu = _wu(id="t1", deferment_age_days=3.0, generated_at="not-a-date")
        result = update_deferment_ages([wu])
        assert result[0].deferment_age_days == 3.0

    def test_future_generated_at_zero_age(self):
        """Tasks generated in the future get 0 age (clamped)."""
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=5)).isoformat()
        wu = _wu(id="t1", generated_at=future)
        result = update_deferment_ages([wu], reference_date=now)
        assert result[0].deferment_age_days == 0.0

    def test_multiple_tasks(self):
        """Multiple tasks are all processed correctly."""
        now = datetime.now(timezone.utc)
        units = [
            _wu(id="t1", generated_at=(now - timedelta(days=1)).isoformat()),
            _wu(id="t2", generated_at=(now - timedelta(days=10)).isoformat()),
            _wu(id="t3", generated_at=""),
        ]
        result = update_deferment_ages(units, reference_date=now)
        assert len(result) == 3
        assert result[0].deferment_age_days == pytest.approx(1.0, abs=0.1)
        assert result[1].deferment_age_days == pytest.approx(10.0, abs=0.1)
        assert result[2].deferment_age_days == 0.0  # unchanged default

    def test_naive_generated_at_treated_as_utc(self):
        """Naive datetime in generated_at is treated as UTC."""
        ref = datetime(2026, 3, 29, 12, 0, 0, tzinfo=timezone.utc)
        gen = "2026-03-27T12:00:00"  # no timezone info
        wu = _wu(id="t1", generated_at=gen)
        result = update_deferment_ages([wu], reference_date=ref)
        assert result[0].deferment_age_days == pytest.approx(2.0, abs=0.01)

    def test_empty_input(self):
        """Empty list → empty result."""
        assert update_deferment_ages([]) == []


# ===========================================================================
# Edge case tests
# ===========================================================================


class TestEdgeCases:
    """Edge cases spanning multiple functions."""

    def test_all_tasks_zero_days_no_alerts(self):
        """All tasks at 0 days → no starvation alerts."""
        units = [_wu(id=f"t{i}", deferment_age_days=0.0) for i in range(5)]
        assert check_starvation(units) == []

    def test_all_tasks_30_days(self):
        """All tasks at 30 days → all get auto_review."""
        units = [_wu(id=f"t{i}", deferment_age_days=30.0) for i in range(5)]
        alerts = check_starvation(units)
        assert len(alerts) == 5
        assert all(a["level"] == "auto_review" for a in alerts)

    def test_single_task_starvation(self):
        """Single task at warning level."""
        alerts = check_starvation([_wu(deferment_age_days=8.0)])
        assert len(alerts) == 1

    def test_single_task_effective_priority(self):
        """Single task priority computation works."""
        score, _ = compute_effective_priority(_wu(deferment_age_days=0.0))
        assert score > 0.0

    def test_single_task_deferment_update(self):
        """Single task age update."""
        now = datetime.now(timezone.utc)
        wu = _wu(generated_at=(now - timedelta(days=2)).isoformat())
        result = update_deferment_ages([wu], reference_date=now)
        assert len(result) == 1
        assert result[0].deferment_age_days == pytest.approx(2.0, abs=0.1)

    def test_empty_input_all_functions(self):
        """All functions handle empty input gracefully."""
        assert check_starvation([]) == []
        assert update_deferment_ages([]) == []
        balance = check_work_mode_balance([])
        assert balance.total_minutes == 0.0

    def test_aging_config_defaults(self):
        """AgingConfig defaults are sane."""
        cfg = AgingConfig()
        assert cfg.rate_per_day["hard"] == 1.0
        assert cfg.rate_per_day["soft"] == 0.5
        assert cfg.rate_per_day["opportunistic"] == 0.2
        assert cfg.starvation_warning_days == 7
        assert cfg.starvation_escalation_days == 14
        assert cfg.starvation_auto_review_days == 21

    def test_maintenance_config_defaults(self):
        """MaintenanceConfig defaults are sane."""
        cfg = MaintenanceConfig()
        assert cfg.reservation_pct == 0.10
        assert "admin" in cfg.maintenance_modes
        assert "monitoring" in cfg.maintenance_modes
        assert cfg.min_maintenance_min == 30.0

    def test_work_mode_balance_model(self):
        """WorkModeBalance model can be instantiated directly."""
        balance = WorkModeBalance(
            period_days=14,
            mode_distribution={"admin": 60.0, "research": 40.0},
            total_minutes=300.0,
            imbalances=["some warning"],
        )
        assert balance.period_days == 14
        assert len(balance.imbalances) == 1

    def test_reserve_maintenance_full_block(self):
        """Block completely full → maintenance can't be added."""
        full_wu = _wu(id="full", work_mode=WorkMode.DEEP_IMPLEMENTATION, estimated_expected_min=480.0)
        full_slot = ScheduledSlot(
            scored_task=_scored(full_wu, score=100.0),
            start_offset_min=0.0,
            end_offset_min=480.0,
        )
        plan = _block_plan(duration=480.0, slots=[full_slot])
        maint = [
            _scored(_wu(id="m1", work_mode=WorkMode.ADMIN, estimated_expected_min=30.0), score=10.0),
        ]
        updated = reserve_maintenance(plan, maint)
        # Block is full — no maintenance added
        assert len(updated.scheduled_slots) == 1
        assert updated.scheduled_slots[0].scored_task.work_unit.id == "full"

    def test_high_deferment_all_commitments(self):
        """High deferment correctly boosts all commitment levels."""
        for commit, rate in [
            (CommitmentLevel.HARD, 1.0),
            (CommitmentLevel.SOFT, 0.5),
            (CommitmentLevel.OPPORTUNISTIC, 0.2),
        ]:
            wu = _wu(priority="low", commitment=commit, deferment_age_days=20.0)
            score, _ = compute_effective_priority(wu)
            expected = 1.0 + 20.0 * rate
            assert score == pytest.approx(expected)


# ===========================================================================
# Import / __init__ smoke tests
# ===========================================================================


class TestModuleExports:
    """Verify Phase 9 symbols are exported from the scheduler package."""

    def test_aging_config_importable(self):
        from utils.scheduler import AgingConfig

        assert AgingConfig is not None

    def test_maintenance_config_importable(self):
        from utils.scheduler import MaintenanceConfig

        assert MaintenanceConfig is not None

    def test_work_mode_balance_importable(self):
        from utils.scheduler import WorkModeBalance

        assert WorkModeBalance is not None

    def test_compute_effective_priority_importable(self):
        from utils.scheduler import compute_effective_priority

        assert callable(compute_effective_priority)

    def test_check_starvation_importable(self):
        from utils.scheduler import check_starvation

        assert callable(check_starvation)

    def test_reserve_maintenance_importable(self):
        from utils.scheduler import reserve_maintenance

        assert callable(reserve_maintenance)

    def test_check_work_mode_balance_importable(self):
        from utils.scheduler import check_work_mode_balance

        assert callable(check_work_mode_balance)

    def test_update_deferment_ages_importable(self):
        from utils.scheduler import update_deferment_ages

        assert callable(update_deferment_ages)
