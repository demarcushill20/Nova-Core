"""Tests for Phase 8: Interrupt Handling & Preemption.

55+ tests covering classification, handle_interrupt logic, preemption budgets,
resume helpers, state persistence, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from utils.scheduler.block import BlockPlan, ScheduledSlot
from utils.scheduler.interrupt_handler import (
    InterruptEvent,
    InterruptLevel,
    InterruptPolicy,
    InterruptState,
    can_resume_paused,
    classify_interrupt,
    get_resume_task_id,
    handle_interrupt,
    load_interrupt_state,
    save_interrupt_state,
)
from utils.scheduler.replanner import BlockState
from utils.scheduler.scorer import ScoredTask, score_task
from utils.scheduler.work_unit import CommitmentLevel, WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 29, 14, 0, 0, tzinfo=timezone.utc)


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


def _make_block_state(
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


def _make_event(
    event_type: str = "health_red",
    level: InterruptLevel = InterruptLevel.CRITICAL,
    title: str = "Test interrupt",
    source: str = "heartbeat",
    **kwargs,
) -> InterruptEvent:
    return InterruptEvent(
        source=source,
        event_type=event_type,
        level=level,
        title=title,
        **kwargs,
    )


# ===================================================================
# InterruptLevel enum tests
# ===================================================================


class TestInterruptLevel:
    def test_values(self):
        assert InterruptLevel.CRITICAL == "critical"
        assert InterruptLevel.HIGH_INSERT == "high_insert"
        assert InterruptLevel.DEFER == "defer"
        assert InterruptLevel.LOG_ONLY == "log_only"

    def test_is_str_enum(self):
        assert isinstance(InterruptLevel.CRITICAL, str)

    def test_all_levels_exist(self):
        assert len(InterruptLevel) == 4


# ===================================================================
# InterruptEvent model tests
# ===================================================================


class TestInterruptEvent:
    def test_create_minimal(self):
        ev = InterruptEvent(
            source="heartbeat",
            event_type="health_red",
            level=InterruptLevel.CRITICAL,
            title="Service down",
        )
        assert ev.source == "heartbeat"
        assert ev.event_type == "health_red"
        assert ev.level == InterruptLevel.CRITICAL
        assert ev.title == "Service down"
        assert ev.detail == ""
        assert ev.task_id is None
        assert ev.event_id.startswith("int_")
        assert ev.timestamp  # auto-generated

    def test_create_full(self):
        ev = InterruptEvent(
            event_id="int_custom_001",
            source="telegram",
            event_type="operator_message",
            level=InterruptLevel.HIGH_INSERT,
            title="Operator says fix now",
            detail="Details about the issue",
            timestamp="2026-03-29T14:00:00+00:00",
            task_id="task_42",
        )
        assert ev.event_id == "int_custom_001"
        assert ev.task_id == "task_42"
        assert ev.detail == "Details about the issue"

    def test_serialization_round_trip(self):
        ev = _make_event()
        data = ev.model_dump_json()
        restored = InterruptEvent.model_validate_json(data)
        assert restored.event_type == ev.event_type
        assert restored.level == ev.level

    def test_extra_fields_forbidden(self):
        with pytest.raises((ValueError, TypeError)):
            InterruptEvent(
                source="x",
                event_type="x",
                level=InterruptLevel.DEFER,
                title="x",
                unknown_field="bad",
            )


# ===================================================================
# InterruptPolicy model tests
# ===================================================================


class TestInterruptPolicy:
    def test_defaults(self):
        pol = InterruptPolicy()
        assert pol.max_preemptions_per_block == 2
        assert "health_red" in pol.auto_classify_rules
        assert pol.auto_classify_rules["health_red"] == "critical"

    def test_custom_policy(self):
        pol = InterruptPolicy(
            max_preemptions_per_block=5,
            auto_classify_rules={"custom_event": "critical"},
        )
        assert pol.max_preemptions_per_block == 5
        assert pol.auto_classify_rules["custom_event"] == "critical"

    def test_all_default_rules_present(self):
        pol = InterruptPolicy()
        expected_keys = {
            "health_red",
            "health_yellow",
            "health_green",
            "operator_critical",
            "operator_message",
            "operator_low",
            "operator_defer",
            "new_task_high",
            "new_task_medium",
            "new_task_low",
            "budget_alert",
            "error_spike",
            "system_info",
        }
        assert set(pol.auto_classify_rules.keys()) == expected_keys


# ===================================================================
# InterruptState model tests
# ===================================================================


class TestInterruptState:
    def test_empty_state(self):
        state = InterruptState()
        assert state.preemption_count == 0
        assert state.interrupt_log == []
        assert state.paused_task_id is None
        assert state.paused_task_checkpoint is None

    def test_serialization_round_trip(self):
        state = InterruptState(
            preemption_count=1,
            interrupt_log=[_make_event()],
            paused_task_id="task_7",
            paused_task_checkpoint="STATE/checkpoints/task_7_interrupt.json",
        )
        data = state.model_dump_json()
        restored = InterruptState.model_validate_json(data)
        assert restored.preemption_count == 1
        assert len(restored.interrupt_log) == 1
        assert restored.paused_task_id == "task_7"


# ===================================================================
# Classification tests
# ===================================================================


class TestClassifyInterrupt:
    """Test classify_interrupt for each event_type and edge cases."""

    def test_health_red_is_critical(self):
        assert classify_interrupt("health_red") == InterruptLevel.CRITICAL

    def test_health_yellow_is_high_insert(self):
        assert classify_interrupt("health_yellow") == InterruptLevel.HIGH_INSERT

    def test_health_green_is_log_only(self):
        assert classify_interrupt("health_green") == InterruptLevel.LOG_ONLY

    def test_operator_critical(self):
        assert classify_interrupt("operator_critical") == InterruptLevel.CRITICAL

    def test_operator_message_is_high_insert(self):
        assert classify_interrupt("operator_message") == InterruptLevel.HIGH_INSERT

    def test_operator_low_is_defer(self):
        assert classify_interrupt("operator_low") == InterruptLevel.DEFER

    def test_operator_defer_is_defer(self):
        assert classify_interrupt("operator_defer") == InterruptLevel.DEFER

    def test_new_task_high_is_high_insert(self):
        assert classify_interrupt("new_task_high") == InterruptLevel.HIGH_INSERT

    def test_new_task_medium_is_defer(self):
        assert classify_interrupt("new_task_medium") == InterruptLevel.DEFER

    def test_new_task_low_is_log_only(self):
        assert classify_interrupt("new_task_low") == InterruptLevel.LOG_ONLY

    def test_budget_alert_is_high_insert(self):
        assert classify_interrupt("budget_alert") == InterruptLevel.HIGH_INSERT

    def test_error_spike_is_high_insert(self):
        assert classify_interrupt("error_spike") == InterruptLevel.HIGH_INSERT

    def test_system_info_is_log_only(self):
        assert classify_interrupt("system_info") == InterruptLevel.LOG_ONLY

    def test_unknown_event_type_defaults_to_defer(self):
        assert classify_interrupt("totally_unknown") == InterruptLevel.DEFER

    def test_empty_string_event_type_defaults_to_defer(self):
        assert classify_interrupt("") == InterruptLevel.DEFER

    # --- Operator prefix parsing ---

    def test_operator_prefix_critical(self):
        result = classify_interrupt("operator_message", operator_text="!critical server is down")
        assert result == InterruptLevel.CRITICAL

    def test_operator_prefix_low(self):
        result = classify_interrupt("operator_message", operator_text="!low just a note")
        assert result == InterruptLevel.DEFER

    def test_operator_prefix_defer(self):
        result = classify_interrupt("operator_message", operator_text="!defer handle later")
        assert result == InterruptLevel.DEFER

    def test_operator_prefix_case_insensitive(self):
        result = classify_interrupt("operator_message", operator_text="!CRITICAL URGENT")
        assert result == InterruptLevel.CRITICAL

    def test_operator_prefix_with_leading_whitespace(self):
        result = classify_interrupt("operator_message", operator_text="  !critical oops")
        assert result == InterruptLevel.CRITICAL

    def test_operator_prefix_overrides_event_type(self):
        # event_type says log_only but operator prefix says critical
        result = classify_interrupt("system_info", operator_text="!critical override")
        assert result == InterruptLevel.CRITICAL

    def test_no_operator_prefix_uses_event_type(self):
        result = classify_interrupt("health_red", operator_text="just a normal message")
        assert result == InterruptLevel.CRITICAL  # health_red -> critical

    def test_empty_operator_text_uses_event_type(self):
        result = classify_interrupt("health_yellow", operator_text="")
        assert result == InterruptLevel.HIGH_INSERT

    # --- Custom policy ---

    def test_custom_policy_overrides_defaults(self):
        custom = InterruptPolicy(
            auto_classify_rules={"health_red": "log_only"},
        )
        assert classify_interrupt("health_red", policy=custom) == InterruptLevel.LOG_ONLY

    def test_custom_policy_new_event_type(self):
        custom = InterruptPolicy(
            auto_classify_rules={"my_custom_event": "critical"},
        )
        assert classify_interrupt("my_custom_event", policy=custom) == InterruptLevel.CRITICAL

    def test_custom_policy_missing_event_type_defaults_to_defer(self):
        custom = InterruptPolicy(auto_classify_rules={})
        assert classify_interrupt("health_red", policy=custom) == InterruptLevel.DEFER


# ===================================================================
# Handle interrupt tests
# ===================================================================


class TestHandleInterrupt:
    """Test handle_interrupt for each level and preemption logic."""

    def test_log_only_no_replan(self):
        ev = _make_event(level=InterruptLevel.LOG_ONLY, event_type="system_info")
        bs = _make_block_state()
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "logged"
        assert result["should_replan"] is False
        assert len(ist.interrupt_log) == 1

    def test_defer_no_replan(self):
        ev = _make_event(level=InterruptLevel.DEFER, event_type="new_task_medium")
        bs = _make_block_state()
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "deferred"
        assert result["should_replan"] is False
        assert result["detail"] == "queued for next replan"

    def test_high_insert_preempts_and_triggers_replan(self):
        ev = _make_event(level=InterruptLevel.HIGH_INSERT, title="Yellow alert")
        bs = _make_block_state(in_progress_task_id="task_1")
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "preempted"
        assert result["should_replan"] is True
        assert result["paused_task"] == "task_1"
        assert "Yellow alert" in result["detail"]
        assert ist.preemption_count == 1

    def test_critical_always_preempts(self):
        ev = _make_event(level=InterruptLevel.CRITICAL, title="Outage")
        bs = _make_block_state(in_progress_task_id="task_5")
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "preempted"
        assert result["should_replan"] is True
        assert result["paused_task"] == "task_5"

    def test_preemption_tracks_paused_task(self):
        ev = _make_event(level=InterruptLevel.CRITICAL)
        bs = _make_block_state(in_progress_task_id="task_99")
        ist = InterruptState()

        handle_interrupt(ev, bs, ist)

        assert ist.paused_task_id == "task_99"
        assert ist.paused_task_checkpoint == "STATE/checkpoints/task_99_interrupt.json"

    def test_preemption_increments_count(self):
        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        bs = _make_block_state()
        ist = InterruptState()

        handle_interrupt(ev, bs, ist)
        assert ist.preemption_count == 1

        ev2 = _make_event(level=InterruptLevel.HIGH_INSERT, title="Second")
        handle_interrupt(ev2, bs, ist)
        assert ist.preemption_count == 2

    def test_event_always_logged(self):
        """Every level logs the event to interrupt_state.interrupt_log."""
        bs = _make_block_state()

        for level in InterruptLevel:
            ist = InterruptState()
            ev = _make_event(level=level)
            handle_interrupt(ev, bs, ist)
            assert len(ist.interrupt_log) == 1
            assert ist.interrupt_log[0].level == level

    def test_high_insert_detail_contains_title(self):
        ev = _make_event(level=InterruptLevel.HIGH_INSERT, title="Budget exceeded")
        bs = _make_block_state()
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)
        assert "Budget exceeded" in result["detail"]


# ===================================================================
# Preemption budget tests
# ===================================================================


class TestPreemptionBudget:
    def test_fresh_state_allows_high_insert(self):
        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        bs = _make_block_state()
        ist = InterruptState()
        pol = InterruptPolicy(max_preemptions_per_block=2)

        result = handle_interrupt(ev, bs, ist, policy=pol)
        assert result["action"] == "preempted"

    def test_budget_exhausted_downgrades_high_insert(self):
        """Third HIGH_INSERT is downgraded to DEFER when budget is 2."""
        bs = _make_block_state()
        ist = InterruptState(preemption_count=2)
        pol = InterruptPolicy(max_preemptions_per_block=2)

        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        result = handle_interrupt(ev, bs, ist, policy=pol)

        assert result["action"] == "deferred"
        assert "budget exhausted" in result["detail"]
        assert result["should_replan"] is False
        # Count should NOT increment since we didn't actually preempt
        assert ist.preemption_count == 2

    def test_critical_bypasses_budget(self):
        """CRITICAL always preempts, even when budget is exhausted."""
        bs = _make_block_state(in_progress_task_id="task_1")
        ist = InterruptState(preemption_count=10)
        pol = InterruptPolicy(max_preemptions_per_block=2)

        ev = _make_event(level=InterruptLevel.CRITICAL)
        result = handle_interrupt(ev, bs, ist, policy=pol)

        assert result["action"] == "preempted"
        assert result["should_replan"] is True
        assert ist.preemption_count == 11

    def test_budget_zero_blocks_all_high_insert(self):
        """With budget=0, even the first HIGH_INSERT is deferred."""
        bs = _make_block_state()
        ist = InterruptState()
        pol = InterruptPolicy(max_preemptions_per_block=0)

        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        result = handle_interrupt(ev, bs, ist, policy=pol)

        assert result["action"] == "deferred"
        assert "budget exhausted" in result["detail"]

    def test_budget_zero_critical_still_works(self):
        bs = _make_block_state(in_progress_task_id="task_1")
        ist = InterruptState()
        pol = InterruptPolicy(max_preemptions_per_block=0)

        ev = _make_event(level=InterruptLevel.CRITICAL)
        result = handle_interrupt(ev, bs, ist, policy=pol)

        assert result["action"] == "preempted"

    def test_exactly_at_budget_limit_blocks(self):
        """Preemption count == max blocks HIGH_INSERT."""
        bs = _make_block_state()
        ist = InterruptState(preemption_count=3)
        pol = InterruptPolicy(max_preemptions_per_block=3)

        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        result = handle_interrupt(ev, bs, ist, policy=pol)

        assert result["action"] == "deferred"

    def test_one_below_budget_allows(self):
        """Preemption count == max-1 still allows."""
        bs = _make_block_state()
        ist = InterruptState(preemption_count=2)
        pol = InterruptPolicy(max_preemptions_per_block=3)

        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        result = handle_interrupt(ev, bs, ist, policy=pol)

        assert result["action"] == "preempted"
        assert ist.preemption_count == 3

    def test_high_budget_allows_many(self):
        bs = _make_block_state()
        pol = InterruptPolicy(max_preemptions_per_block=100)
        ist = InterruptState()

        for i in range(50):
            ev = _make_event(level=InterruptLevel.HIGH_INSERT, title=f"Event {i}")
            result = handle_interrupt(ev, bs, ist, policy=pol)
            assert result["action"] == "preempted"

        assert ist.preemption_count == 50


# ===================================================================
# Resume tests
# ===================================================================


class TestResume:
    def test_can_resume_after_preemption(self):
        ist = InterruptState(paused_task_id="task_7")
        assert can_resume_paused(ist) is True

    def test_cannot_resume_no_paused_task(self):
        ist = InterruptState()
        assert can_resume_paused(ist) is False

    def test_get_resume_task_id_returns_correct_id(self):
        ist = InterruptState(paused_task_id="task_42")
        assert get_resume_task_id(ist) == "task_42"

    def test_get_resume_task_id_returns_none_when_empty(self):
        ist = InterruptState()
        assert get_resume_task_id(ist) is None

    def test_preemption_sets_resume_data(self):
        ev = _make_event(level=InterruptLevel.CRITICAL)
        bs = _make_block_state(in_progress_task_id="task_11")
        ist = InterruptState()

        handle_interrupt(ev, bs, ist)

        assert can_resume_paused(ist)
        assert get_resume_task_id(ist) == "task_11"
        assert ist.paused_task_checkpoint == "STATE/checkpoints/task_11_interrupt.json"


# ===================================================================
# State persistence tests
# ===================================================================


class TestStatePersistence:
    def test_save_and_load_round_trip(self, tmp_path: Path):
        state = InterruptState(
            preemption_count=2,
            interrupt_log=[
                _make_event(title="Event A"),
                _make_event(title="Event B"),
            ],
            paused_task_id="task_9",
            paused_task_checkpoint="STATE/checkpoints/task_9_interrupt.json",
        )
        fpath = tmp_path / "interrupt_state.json"
        saved = save_interrupt_state(state, path=fpath)

        assert saved == fpath
        assert fpath.exists()

        loaded = load_interrupt_state(path=fpath)
        assert loaded.preemption_count == 2
        assert len(loaded.interrupt_log) == 2
        assert loaded.paused_task_id == "task_9"
        assert loaded.paused_task_checkpoint == "STATE/checkpoints/task_9_interrupt.json"

    def test_load_missing_file_returns_empty_state(self, tmp_path: Path):
        fpath = tmp_path / "nonexistent.json"
        loaded = load_interrupt_state(path=fpath)

        assert loaded.preemption_count == 0
        assert loaded.interrupt_log == []
        assert loaded.paused_task_id is None

    def test_load_corrupt_file_returns_empty_state(self, tmp_path: Path):
        fpath = tmp_path / "corrupt.json"
        fpath.write_text("{invalid json content!!!")

        loaded = load_interrupt_state(path=fpath)
        assert loaded.preemption_count == 0

    def test_interrupt_log_preserved_across_save_load(self, tmp_path: Path):
        events = [_make_event(event_type="health_red", level=InterruptLevel.CRITICAL, title=f"E{i}") for i in range(5)]
        state = InterruptState(interrupt_log=events)
        fpath = tmp_path / "state.json"
        save_interrupt_state(state, path=fpath)

        loaded = load_interrupt_state(path=fpath)
        assert len(loaded.interrupt_log) == 5
        assert loaded.interrupt_log[3].title == "E3"

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        deep_path = tmp_path / "a" / "b" / "c" / "state.json"
        save_interrupt_state(InterruptState(), path=deep_path)
        assert deep_path.exists()

    def test_save_overwrites_existing(self, tmp_path: Path):
        fpath = tmp_path / "state.json"
        save_interrupt_state(InterruptState(preemption_count=1), path=fpath)
        save_interrupt_state(InterruptState(preemption_count=7), path=fpath)

        loaded = load_interrupt_state(path=fpath)
        assert loaded.preemption_count == 7


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    def test_no_current_task_preempt_with_no_pause(self):
        """Preemption when no task is in progress: action=preempted, paused_task=None."""
        ev = _make_event(level=InterruptLevel.CRITICAL)
        bs = _make_block_state()  # no in_progress_task_id
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "preempted"
        assert result["paused_task"] is None
        assert result["should_replan"] is True
        assert ist.paused_task_id is None
        assert ist.preemption_count == 1

    def test_multiple_rapid_interrupts(self):
        """Multiple interrupts in quick succession are all logged."""
        bs = _make_block_state(in_progress_task_id="task_1")
        ist = InterruptState()
        pol = InterruptPolicy(max_preemptions_per_block=2)

        events = [
            _make_event(level=InterruptLevel.HIGH_INSERT, title="Interrupt 1"),
            _make_event(level=InterruptLevel.HIGH_INSERT, title="Interrupt 2"),
            _make_event(level=InterruptLevel.HIGH_INSERT, title="Interrupt 3"),
            _make_event(level=InterruptLevel.CRITICAL, title="Interrupt 4"),
        ]

        results = [handle_interrupt(ev, bs, ist, policy=pol) for ev in events]

        # First two preempt
        assert results[0]["action"] == "preempted"
        assert results[1]["action"] == "preempted"
        # Third deferred (budget exhausted)
        assert results[2]["action"] == "deferred"
        # Fourth preempts (CRITICAL bypasses budget)
        assert results[3]["action"] == "preempted"
        # All four logged
        assert len(ist.interrupt_log) == 4
        # Preemption count = 3 (two HIGH_INSERT + one CRITICAL)
        assert ist.preemption_count == 3

    def test_empty_policy_uses_defaults(self):
        """Passing None policy uses InterruptPolicy defaults."""
        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        bs = _make_block_state()
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist, policy=None)
        assert result["action"] == "preempted"

    def test_default_policy_classify_all_known_types(self):
        """Every default rule in InterruptPolicy produces a valid InterruptLevel."""
        pol = InterruptPolicy()
        for event_type, _raw_level in pol.auto_classify_rules.items():
            level = classify_interrupt(event_type, pol)
            assert isinstance(level, InterruptLevel), f"Failed for {event_type}"

    def test_preemption_updates_paused_on_second_preempt(self):
        """Second preemption overwrites the paused task."""
        bs = _make_block_state(in_progress_task_id="task_A")
        ist = InterruptState()

        ev1 = _make_event(level=InterruptLevel.CRITICAL)
        handle_interrupt(ev1, bs, ist)
        assert ist.paused_task_id == "task_A"

        # Simulate a different task now in progress
        bs.in_progress_task_id = "task_B"
        ev2 = _make_event(level=InterruptLevel.CRITICAL, title="Second")
        handle_interrupt(ev2, bs, ist)
        assert ist.paused_task_id == "task_B"

    def test_log_only_does_not_increment_preemption_count(self):
        ev = _make_event(level=InterruptLevel.LOG_ONLY)
        bs = _make_block_state()
        ist = InterruptState()

        handle_interrupt(ev, bs, ist)
        assert ist.preemption_count == 0

    def test_defer_does_not_increment_preemption_count(self):
        ev = _make_event(level=InterruptLevel.DEFER)
        bs = _make_block_state()
        ist = InterruptState()

        handle_interrupt(ev, bs, ist)
        assert ist.preemption_count == 0

    def test_deferred_event_still_logged(self):
        """Even when HIGH_INSERT is downgraded to deferred, the event is logged."""
        ev = _make_event(level=InterruptLevel.HIGH_INSERT)
        bs = _make_block_state()
        ist = InterruptState(preemption_count=5)
        pol = InterruptPolicy(max_preemptions_per_block=2)

        handle_interrupt(ev, bs, ist, policy=pol)
        assert len(ist.interrupt_log) == 1
        assert ist.interrupt_log[0].level == InterruptLevel.HIGH_INSERT

    def test_classify_with_invalid_level_in_policy_defaults_to_defer(self):
        """If auto_classify_rules has a bogus level string, default to DEFER."""
        pol = InterruptPolicy(auto_classify_rules={"bad_event": "nonexistent_level"})
        result = classify_interrupt("bad_event", policy=pol)
        assert result == InterruptLevel.DEFER


# ===================================================================
# Integration: classify then handle
# ===================================================================


class TestClassifyThenHandle:
    """End-to-end: classify event_type, build InterruptEvent, handle it."""

    def test_health_red_end_to_end(self):
        level = classify_interrupt("health_red")
        ev = InterruptEvent(
            source="heartbeat",
            event_type="health_red",
            level=level,
            title="Redis down",
        )
        bs = _make_block_state(in_progress_task_id="task_redis_fix")
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "preempted"
        assert result["should_replan"] is True
        assert ist.paused_task_id == "task_redis_fix"

    def test_system_info_end_to_end(self):
        level = classify_interrupt("system_info")
        ev = InterruptEvent(
            source="system",
            event_type="system_info",
            level=level,
            title="Disk usage 60%",
        )
        bs = _make_block_state()
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "logged"
        assert result["should_replan"] is False

    def test_operator_with_prefix_end_to_end(self):
        level = classify_interrupt("operator_message", operator_text="!critical deploy hotfix")
        ev = InterruptEvent(
            source="telegram",
            event_type="operator_message",
            level=level,
            title="!critical deploy hotfix",
        )
        bs = _make_block_state(in_progress_task_id="task_routine")
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "preempted"
        assert ist.paused_task_id == "task_routine"

    def test_new_task_medium_end_to_end(self):
        level = classify_interrupt("new_task_medium")
        ev = InterruptEvent(
            source="watcher",
            event_type="new_task_medium",
            level=level,
            title="New task filed",
        )
        bs = _make_block_state()
        ist = InterruptState()

        result = handle_interrupt(ev, bs, ist)

        assert result["action"] == "deferred"
        assert result["should_replan"] is False


# ===================================================================
# Import from __init__.py
# ===================================================================


class TestPublicAPI:
    """Verify that all Phase 8 symbols are importable from utils.scheduler."""

    def test_imports_from_package(self):
        from utils.scheduler import (
            InterruptLevel,
            can_resume_paused,
            classify_interrupt,
            get_resume_task_id,
            handle_interrupt,
            load_interrupt_state,
            save_interrupt_state,
        )

        # Smoke check: they are the right objects
        assert InterruptLevel.CRITICAL == "critical"
        assert callable(classify_interrupt)
        assert callable(handle_interrupt)
        assert callable(can_resume_paused)
        assert callable(get_resume_task_id)
        assert callable(save_interrupt_state)
        assert callable(load_interrupt_state)
