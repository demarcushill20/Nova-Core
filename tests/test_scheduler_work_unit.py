"""Tests for utils.scheduler.work_unit — data model validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from utils.scheduler.work_unit import (
    TASK_CLASS_DURATION_RANGES,
    CommitmentLevel,
    TaskClass,
    WorkMode,
    WorkUnit,
)

# ---------------------------------------------------------------------------
# Enum completeness
# ---------------------------------------------------------------------------


class TestWorkModeEnum:
    def test_all_modes_present(self):
        expected = {
            "deep_implementation",
            "debugging",
            "research",
            "analysis",
            "review",
            "communication",
            "deployment",
            "monitoring",
            "admin",
        }
        assert {m.value for m in WorkMode} == expected

    def test_str_enum(self):
        assert WorkMode.RESEARCH.value == "research"
        assert WorkMode.RESEARCH == "research"

    def test_from_string(self):
        assert WorkMode("research") == WorkMode.RESEARCH


class TestCommitmentLevelEnum:
    def test_all_levels_present(self):
        expected = {"hard", "soft", "opportunistic"}
        assert {c.value for c in CommitmentLevel} == expected


class TestTaskClassEnum:
    def test_all_classes_present(self):
        expected = {"atomic", "bounded", "epic"}
        assert {t.value for t in TaskClass} == expected


# ---------------------------------------------------------------------------
# TASK_CLASS_DURATION_RANGES
# ---------------------------------------------------------------------------


class TestDurationRanges:
    def test_all_task_classes_covered(self):
        for tc in TaskClass:
            assert tc in TASK_CLASS_DURATION_RANGES, f"Missing range for {tc}"

    def test_ranges_are_tuples(self):
        for tc, (lo, hi) in TASK_CLASS_DURATION_RANGES.items():
            assert isinstance(lo, float), f"{tc} lo not float"
            assert isinstance(hi, float), f"{tc} hi not float"
            assert lo < hi, f"{tc}: lo ({lo}) >= hi ({hi})"

    def test_atomic_range(self):
        lo, hi = TASK_CLASS_DURATION_RANGES[TaskClass.ATOMIC]
        assert lo == 5.0
        assert hi == 20.0

    def test_bounded_range(self):
        lo, hi = TASK_CLASS_DURATION_RANGES[TaskClass.BOUNDED]
        assert lo == 20.0
        assert hi == 90.0

    def test_epic_range(self):
        lo, hi = TASK_CLASS_DURATION_RANGES[TaskClass.EPIC]
        assert lo == 90.0


# ---------------------------------------------------------------------------
# WorkUnit construction
# ---------------------------------------------------------------------------


class TestWorkUnitValid:
    def test_defaults(self):
        wu = WorkUnit()
        assert wu.id == ""
        assert wu.priority == "medium"
        assert wu.work_mode == WorkMode.ADMIN
        assert wu.urgency == 0.5
        assert wu.auto_execute is True
        assert wu.schedulable_now is True
        assert wu.commitment_level == CommitmentLevel.SOFT
        assert wu.task_class == TaskClass.BOUNDED

    def test_full_construction(self):
        wu = WorkUnit(
            id="task_001",
            title="Fix login bug",
            goal_id="g_auth",
            parent_task_id="parent_001",
            task_type="repair",
            work_mode=WorkMode.DEBUGGING,
            priority="high",
            urgency=0.9,
            strategic_value=0.7,
            unblock_value=0.5,
            risk_reduction=0.3,
            dependency_ids=["dep_a", "dep_b"],
            estimated_optimistic_min=10.0,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=60.0,
            confidence=0.8,
            success_probability=0.9,
            context_switch_cost_min=3.0,
            deferment_age_days=2.0,
            hard_deadline=datetime(2026, 4, 1, tzinfo=timezone.utc),
            must_do_today=True,
            schedulable_now=True,
            done_condition="Login tests pass",
            fallback_plan="Revert and fix later",
            commitment_level=CommitmentLevel.HARD,
            task_class=TaskClass.ATOMIC,
            auto_execute=False,
            generated_at="2026-03-29T10:00:00",
            source="heartbeat",
            file_path="/home/nova/nova-core/TASKS/task_001.md",
        )
        assert wu.id == "task_001"
        assert wu.work_mode == WorkMode.DEBUGGING
        assert wu.priority == "high"
        assert wu.urgency == 0.9
        assert wu.dependency_ids == ["dep_a", "dep_b"]
        assert wu.hard_deadline is not None
        assert wu.auto_execute is False
        assert wu.commitment_level == CommitmentLevel.HARD

    def test_equal_estimates_valid(self):
        """Optimistic == expected == pessimistic is valid."""
        wu = WorkUnit(
            estimated_optimistic_min=30.0,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=30.0,
        )
        assert wu.estimated_optimistic_min == 30.0


# ---------------------------------------------------------------------------
# WorkUnit validation errors
# ---------------------------------------------------------------------------


class TestWorkUnitInvalid:
    def test_negative_urgency(self):
        with pytest.raises(ValidationError):
            WorkUnit(urgency=-0.1)

    def test_urgency_over_one(self):
        with pytest.raises(ValidationError):
            WorkUnit(urgency=1.5)

    def test_negative_strategic_value(self):
        with pytest.raises(ValidationError):
            WorkUnit(strategic_value=-0.01)

    def test_optimistic_below_minimum(self):
        with pytest.raises(ValidationError):
            WorkUnit(estimated_optimistic_min=0.5)

    def test_pessimistic_less_than_expected(self):
        with pytest.raises(ValidationError, match="pessimistic"):
            WorkUnit(
                estimated_optimistic_min=10.0,
                estimated_expected_min=50.0,
                estimated_pessimistic_min=30.0,
            )

    def test_expected_less_than_optimistic(self):
        with pytest.raises(ValidationError, match="expected"):
            WorkUnit(
                estimated_optimistic_min=40.0,
                estimated_expected_min=20.0,
                estimated_pessimistic_min=60.0,
            )

    def test_negative_deferment(self):
        with pytest.raises(ValidationError):
            WorkUnit(deferment_age_days=-1.0)

    def test_confidence_over_one(self):
        with pytest.raises(ValidationError):
            WorkUnit(confidence=1.1)

    def test_success_probability_negative(self):
        with pytest.raises(ValidationError):
            WorkUnit(success_probability=-0.1)


# ---------------------------------------------------------------------------
# Priority normalization
# ---------------------------------------------------------------------------


class TestPriorityNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("high", "high"),
            ("HIGH", "high"),
            ("critical", "high"),
            ("urgent", "high"),
            ("medium", "medium"),
            ("Medium", "medium"),
            ("normal", "medium"),
            ("low", "low"),
            ("minor", "low"),
            ("LOW", "low"),
            ("unknown", "medium"),
            ("", "medium"),
        ],
    )
    def test_priority_normalization(self, raw, expected):
        wu = WorkUnit(priority=raw)
        assert wu.priority == expected


# ---------------------------------------------------------------------------
# from_frontmatter
# ---------------------------------------------------------------------------


class TestFromFrontmatter:
    def test_full_frontmatter(self):
        fm = {
            "priority": "high",
            "type": "shift_block",
            "shift_block": 3,
            "shift_date": "2026-03-29",
            "scheduled_at": "2026-03-29T13:00:00",
            "auto_execute": True,
            "source": "shift_generator",
            "generated_at": "2026-03-29T06:00:00",
        }
        wu = WorkUnit.from_frontmatter(fm, file_path="/home/nova/nova-core/TASKS/shift_20260329_3_impl.md")
        assert wu.id == "shift_20260329_3_impl"
        assert wu.priority == "high"
        assert wu.urgency == 0.8
        assert wu.work_mode == WorkMode.DEEP_IMPLEMENTATION
        assert wu.must_do_today is True
        assert wu.auto_execute is True
        assert wu.source == "shift_generator"
        assert wu.file_path == "/home/nova/nova-core/TASKS/shift_20260329_3_impl.md"

    def test_minimal_frontmatter(self):
        wu = WorkUnit.from_frontmatter({})
        assert wu.id == ""
        assert wu.priority == "medium"
        assert wu.urgency == 0.5
        assert wu.work_mode == WorkMode.ADMIN
        assert wu.auto_execute is True

    def test_empty_dict(self):
        wu = WorkUnit.from_frontmatter({})
        assert isinstance(wu, WorkUnit)
        assert wu.schedulable_now is True

    def test_none_frontmatter(self):
        wu = WorkUnit.from_frontmatter(None)
        assert isinstance(wu, WorkUnit)

    def test_shift_block_1_maps_to_monitoring(self):
        fm = {"type": "shift_block", "shift_block": 1}
        wu = WorkUnit.from_frontmatter(fm)
        assert wu.work_mode == WorkMode.MONITORING

    def test_shift_block_4_maps_to_research(self):
        fm = {"type": "shift_block", "shift_block": 4}
        wu = WorkUnit.from_frontmatter(fm)
        assert wu.work_mode == WorkMode.RESEARCH

    def test_shift_block_6_maps_to_review(self):
        fm = {"type": "shift_block", "shift_block": 6}
        wu = WorkUnit.from_frontmatter(fm)
        assert wu.work_mode == WorkMode.REVIEW

    def test_shift_block_8_maps_to_communication(self):
        fm = {"type": "shift_block", "shift_block": 8}
        wu = WorkUnit.from_frontmatter(fm)
        assert wu.work_mode == WorkMode.COMMUNICATION

    def test_auto_execute_false_preserved(self):
        fm = {"auto_execute": False}
        wu = WorkUnit.from_frontmatter(fm)
        assert wu.auto_execute is False

    def test_file_path_sets_id(self):
        wu = WorkUnit.from_frontmatter({}, file_path="/tasks/my_task_42.md")
        assert wu.id == "my_task_42"

    def test_category_maps_to_task_type(self):
        fm = {"category": "research"}
        wu = WorkUnit.from_frontmatter(fm)
        assert wu.task_type == "research"


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_model_dump_roundtrip(self):
        wu = WorkUnit(
            id="test",
            title="Test task",
            work_mode=WorkMode.RESEARCH,
            estimated_optimistic_min=5.0,
            estimated_expected_min=15.0,
            estimated_pessimistic_min=30.0,
        )
        data = wu.model_dump()
        wu2 = WorkUnit(**data)
        assert wu2.id == wu.id
        assert wu2.work_mode == wu.work_mode

    def test_model_json_roundtrip(self):
        wu = WorkUnit(id="json_test", title="JSON roundtrip")
        json_str = wu.model_dump_json()
        wu2 = WorkUnit.model_validate_json(json_str)
        assert wu2.id == "json_test"
