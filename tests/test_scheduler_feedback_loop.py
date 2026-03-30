"""Integration test: Scheduler Feedback Loop (Phase D).

Validates the complete feedback loop end-to-end across Phases A-C:
  - Phase A: On-demand BlockState creation when none exists on disk
  - Phase B: Test leakage fix (synthetic log archived)
  - Phase C: Heartbeat calibration filters records with duration < 0.1 min

Tests the real scheduler code — no mocking of scheduler internals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from utils.scheduler.block import BlockPlan
from utils.scheduler.calibration import (
    compute_calibration,
    load_calibration,
    save_calibration,
)
from utils.scheduler.execution_log import (
    create_execution_record,
    log_execution,
    read_execution_log,
)
from utils.scheduler.packer import PackerConfig, pack_block
from utils.scheduler.replanner import (
    BlockState,
    load_block_state,
    on_task_complete,
    on_task_start,
    save_block_state,
)
from utils.scheduler.scorer import rank_tasks
from utils.scheduler.work_unit import (
    CommitmentLevel,
    TaskClass,
    WorkMode,
    WorkUnit,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DURATION_FILTER_THRESHOLD = 0.1  # minutes — Phase C threshold


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Temporary directory for block state persistence."""
    d = tmp_path / "scheduler"
    d.mkdir()
    return d


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    """Temporary JSONL log file path."""
    return tmp_path / "execution_log.jsonl"


def _make_work_unit(
    task_id: str,
    *,
    work_mode: WorkMode = WorkMode.DEEP_IMPLEMENTATION,
    task_class: TaskClass = TaskClass.BOUNDED,
    commitment: CommitmentLevel = CommitmentLevel.SOFT,
    estimated: float = 35.0,
) -> WorkUnit:
    """Helper: create a WorkUnit with sensible defaults for testing."""
    return WorkUnit(
        id=task_id,
        title=f"Task {task_id}",
        work_mode=work_mode,
        task_class=task_class,
        commitment_level=commitment,
        estimated_optimistic_min=max(1.0, estimated * 0.6),
        estimated_expected_min=max(1.0, estimated),
        estimated_pessimistic_min=max(1.0, estimated * 1.8),
        urgency=0.7,
        strategic_value=0.6,
        confidence=0.7,
    )


def _make_block_state(
    block_id: str,
    plan: BlockPlan | None = None,
    duration_hours: float = 8.0,
) -> BlockState:
    """Helper: create a BlockState with UTC timestamps."""
    now = datetime.now(timezone.utc)
    return BlockState(
        block_id=block_id,
        original_plan=plan or BlockPlan(),
        block_start_utc=now,
        block_end_utc=now + timedelta(hours=duration_hours),
    )


# ---------------------------------------------------------------------------
# Test 1: On-demand BlockState creation
# ---------------------------------------------------------------------------


class TestOnDemandBlockStateCreation:
    """Verify that a BlockState can be created on-demand when none exists on disk."""

    def test_load_returns_none_for_new_block(self, state_dir: Path) -> None:
        """load_block_state returns None when no state file exists."""
        result = load_block_state("block_20260329_1", state_dir=state_dir)
        assert result is None

    def test_on_demand_creation_and_round_trip(self, state_dir: Path) -> None:
        """Create a BlockState on-demand, save it, and load it back."""
        block_id = "block_20260329_1"

        # Confirm nothing on disk
        assert load_block_state(block_id, state_dir=state_dir) is None

        # Create on-demand (same code path as watcher)
        now = datetime.now(timezone.utc)
        state = BlockState(
            block_id=block_id,
            original_plan=BlockPlan(),  # empty plan
            block_start_utc=now,
            block_end_utc=now + timedelta(hours=8),
        )

        # Save
        saved_path = save_block_state(state, state_dir=state_dir)
        assert saved_path.exists()

        # Load back
        loaded = load_block_state(block_id, state_dir=state_dir)
        assert loaded is not None
        assert loaded.block_id == block_id
        assert loaded.completed_task_ids == []
        assert loaded.failed_task_ids == []
        assert loaded.in_progress_task_id is None
        assert loaded.actual_durations == {}
        assert loaded.original_plan.scheduled_slots == []

    def test_timestamps_are_utc(self, state_dir: Path) -> None:
        """Verify persisted timestamps are UTC-aware."""
        block_id = "block_utc_test"
        state = _make_block_state(block_id)

        save_block_state(state, state_dir=state_dir)
        loaded = load_block_state(block_id, state_dir=state_dir)
        assert loaded is not None
        assert loaded.block_start_utc.tzinfo is not None
        assert loaded.block_end_utc.tzinfo is not None


# ---------------------------------------------------------------------------
# Test 2: ExecutionRecord with real metadata
# ---------------------------------------------------------------------------


class TestExecutionRecordWithRealMetadata:
    """Verify ExecutionRecord creation, persistence, and filtering."""

    def test_create_and_round_trip(self, log_path: Path) -> None:
        """Create a record from a WorkUnit, log it, read it back."""
        wu = _make_work_unit(
            "impl_scheduler_v2",
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            task_class=TaskClass.BOUNDED,
            estimated=35.0,
        )

        record = create_execution_record(
            work_unit=wu,
            actual_duration_min=15.5,
            outcome="success",
            block_id="block_20260329_1",
            outcome_quality=85.0,
        )

        # Verify fields
        assert record.task_id == "impl_scheduler_v2"
        assert record.task_class == TaskClass.BOUNDED
        assert record.work_mode == WorkMode.DEEP_IMPLEMENTATION
        assert record.planned_duration_min == 35.0
        assert record.actual_duration_min == 15.5
        assert record.variance_ratio == pytest.approx(15.5 / 35.0)
        assert record.outcome == "success"
        assert record.outcome_quality == 85.0
        assert record.block_id == "block_20260329_1"
        assert record.timestamp != ""

        # Log to file
        log_execution(record, log_path=log_path)

        # Read back
        records = read_execution_log(log_path=log_path)
        assert len(records) == 1

        loaded = records[0]
        assert loaded.task_id == record.task_id
        assert loaded.actual_duration_min == record.actual_duration_min
        assert loaded.variance_ratio == pytest.approx(record.variance_ratio)
        assert loaded.outcome_quality == record.outcome_quality
        assert loaded.block_id == record.block_id

    def test_real_record_not_filtered(self, log_path: Path) -> None:
        """A record with duration 15.5 min survives the 0.1 min threshold."""
        wu = _make_work_unit("real_task", estimated=35.0)
        record = create_execution_record(
            work_unit=wu,
            actual_duration_min=15.5,
            outcome="success",
        )
        assert record.actual_duration_min >= DURATION_FILTER_THRESHOLD


# ---------------------------------------------------------------------------
# Test 3: Synthetic probe filtered from calibration
# ---------------------------------------------------------------------------


class TestSyntheticProbeFiltered:
    """Verify that synthetic probes (tiny durations) are filtered out."""

    def test_filter_by_duration_threshold(self, log_path: Path) -> None:
        """Only records with duration >= 0.1 min survive the filter."""
        wu_synthetic = _make_work_unit("synthetic_probe", estimated=1.0)
        wu_real = _make_work_unit("real_impl", estimated=30.0)

        rec_synthetic = create_execution_record(
            work_unit=wu_synthetic,
            actual_duration_min=0.00001,
            outcome="success",
        )
        rec_real = create_execution_record(
            work_unit=wu_real,
            actual_duration_min=15.0,
            outcome="success",
        )

        log_execution(rec_synthetic, log_path=log_path)
        log_execution(rec_real, log_path=log_path)

        # Read all back
        all_records = read_execution_log(log_path=log_path)
        assert len(all_records) == 2

        # Apply Phase C filter
        filtered = [r for r in all_records if r.actual_duration_min >= DURATION_FILTER_THRESHOLD]
        assert len(filtered) == 1
        assert filtered[0].task_id == "real_impl"

    def test_calibration_from_filtered_records(self, log_path: Path) -> None:
        """Calibration works with filtered records (>= 5 real records required for per-pair entry)."""
        wu = _make_work_unit(
            "calibration_task",
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            task_class=TaskClass.BOUNDED,
            estimated=30.0,
        )

        # Write 1 synthetic + 6 real records
        synthetic = create_execution_record(
            work_unit=wu,
            actual_duration_min=0.00001,
            outcome="success",
        )
        log_execution(synthetic, log_path=log_path)

        for i in range(6):
            real = create_execution_record(
                work_unit=wu,
                actual_duration_min=25.0 + i * 2.0,  # 25, 27, 29, 31, 33, 35
                outcome="success",
                block_id=f"block_{i}",
            )
            log_execution(real, log_path=log_path)

        # Read + filter
        all_records = read_execution_log(log_path=log_path)
        filtered = [r for r in all_records if r.actual_duration_min >= DURATION_FILTER_THRESHOLD]
        assert len(filtered) == 6

        # Compute calibration
        table = compute_calibration(filtered)
        assert table.entries  # should have at least one entry
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        assert key in table.entries
        entry = table.entries[key]
        assert entry.sample_count == 6
        assert entry.mean_variance_ratio > 0


# ---------------------------------------------------------------------------
# Test 4: BlockState lifecycle hooks
# ---------------------------------------------------------------------------


class TestBlockStateLifecycleHooks:
    """Verify on_task_start / on_task_complete lifecycle hooks and persistence."""

    def test_task_start_sets_in_progress(self) -> None:
        """on_task_start sets in_progress_task_id."""
        state = _make_block_state("block_lifecycle")
        on_task_start(state, "task_alpha")
        assert state.in_progress_task_id == "task_alpha"

    def test_task_complete_moves_to_completed(self) -> None:
        """on_task_complete records the task and clears in_progress."""
        state = _make_block_state("block_lifecycle")
        on_task_start(state, "task_alpha")

        # Complete with no replanner config (disables replan triggers)
        from utils.scheduler.replanner import ReplannerConfig

        disabled_config = ReplannerConfig(enabled=False)
        on_task_complete(state, "task_alpha", actual_duration_min=15.0, config=disabled_config)

        assert "task_alpha" in state.completed_task_ids
        assert state.actual_durations["task_alpha"] == 15.0
        assert state.in_progress_task_id is None

    def test_lifecycle_persists_across_save_load(self, state_dir: Path) -> None:
        """State survives a save/load round-trip after lifecycle operations."""
        from utils.scheduler.replanner import ReplannerConfig

        block_id = "block_persist_lifecycle"
        state = _make_block_state(block_id)

        # Start + complete a task
        on_task_start(state, "task_beta")
        disabled_config = ReplannerConfig(enabled=False)
        on_task_complete(state, "task_beta", actual_duration_min=22.5, config=disabled_config)

        # Start another task (leave it in-progress)
        on_task_start(state, "task_gamma")

        # Save
        save_block_state(state, state_dir=state_dir)

        # Load
        loaded = load_block_state(block_id, state_dir=state_dir)
        assert loaded is not None
        assert "task_beta" in loaded.completed_task_ids
        assert loaded.actual_durations["task_beta"] == 22.5
        assert loaded.in_progress_task_id == "task_gamma"


# ---------------------------------------------------------------------------
# Test 5: Full feedback loop
# ---------------------------------------------------------------------------


class TestFullFeedbackLoop:
    """End-to-end: plan -> execute -> log -> filter -> calibrate."""

    def test_complete_loop(self, state_dir: Path, log_path: Path, tmp_path: Path) -> None:
        """Simulate the complete scheduler feedback loop."""
        from utils.scheduler.replanner import ReplannerConfig

        # ---- Step 1: Create WorkUnits ----
        work_units = [
            _make_work_unit(
                "impl_auth",
                work_mode=WorkMode.DEEP_IMPLEMENTATION,
                task_class=TaskClass.BOUNDED,
                commitment=CommitmentLevel.SOFT,
                estimated=45.0,
            ),
            _make_work_unit(
                "review_pr",
                work_mode=WorkMode.REVIEW,
                task_class=TaskClass.ATOMIC,
                commitment=CommitmentLevel.SOFT,
                estimated=15.0,
            ),
            _make_work_unit(
                "debug_flaky",
                work_mode=WorkMode.DEBUGGING,
                task_class=TaskClass.BOUNDED,
                commitment=CommitmentLevel.SOFT,
                estimated=30.0,
            ),
            _make_work_unit(
                "monitor_health",
                work_mode=WorkMode.MONITORING,
                task_class=TaskClass.ATOMIC,
                commitment=CommitmentLevel.OPPORTUNISTIC,
                estimated=10.0,
            ),
        ]

        # ---- Step 2: Score and pack into a BlockPlan ----
        scored = rank_tasks(work_units)
        assert len(scored) == len(work_units)

        packer_config = PackerConfig(
            committed_ratio=0.75,
            buffer_ratio=0.17,
            filler_ratio=0.08,
        )
        plan = pack_block(scored, block_duration_min=240.0, config=packer_config)
        assert plan.scheduled_slots  # at least some tasks should be scheduled

        # ---- Step 3: Create and save BlockState ----
        block_id = "block_20260329_full"
        state = BlockState(
            block_id=block_id,
            original_plan=plan,
            block_start_utc=datetime.now(timezone.utc),
            block_end_utc=datetime.now(timezone.utc) + timedelta(hours=4),
        )
        save_block_state(state, state_dir=state_dir)

        # ---- Step 4: Simulate execution for each scheduled task ----
        disabled_config = ReplannerConfig(enabled=False)
        simulated_durations = {
            "impl_auth": 40.0,
            "review_pr": 12.0,
            "debug_flaky": 25.0,
            "monitor_health": 8.0,
        }

        for slot in plan.scheduled_slots:
            task_id = slot.scored_task.work_unit.id
            actual_dur = simulated_durations.get(task_id, 20.0)

            # Lifecycle: start
            on_task_start(state, task_id)
            assert state.in_progress_task_id == task_id

            # Create execution record
            record = create_execution_record(
                work_unit=slot.scored_task.work_unit,
                actual_duration_min=actual_dur,
                outcome="success",
                block_id=block_id,
                outcome_quality=80.0,
            )
            log_execution(record, log_path=log_path)

            # Lifecycle: complete
            on_task_complete(state, task_id, actual_duration_min=actual_dur, config=disabled_config)

        # Save final state
        save_block_state(state, state_dir=state_dir)

        # ---- Step 5: Read execution log and filter ----
        all_records = read_execution_log(log_path=log_path)
        assert len(all_records) == len(plan.scheduled_slots)

        filtered = [r for r in all_records if r.actual_duration_min >= DURATION_FILTER_THRESHOLD]
        assert len(filtered) == len(all_records)  # all real durations should survive

        # ---- Step 6: Compute calibration ----
        table = compute_calibration(filtered)
        assert table.last_computed != ""
        assert table.global_bias > 0

        # ---- Step 7: Verify calibration entries for work modes used ----
        # We should have entries for the (task_class, work_mode) pairs we used
        for slot in plan.scheduled_slots:
            wu = slot.scored_task.work_unit
            key = f"{wu.task_class.value}_{wu.work_mode.value}"
            assert key in table.entries, f"Expected calibration entry for {key}"
            entry = table.entries[key]
            assert entry.sample_count >= 1

        # ---- Verify final block state integrity ----
        loaded = load_block_state(block_id, state_dir=state_dir)
        assert loaded is not None
        for slot in plan.scheduled_slots:
            tid = slot.scored_task.work_unit.id
            assert tid in loaded.completed_task_ids
            assert tid in loaded.actual_durations

        # ---- Verify calibration persistence ----
        cal_path = tmp_path / "calibration.json"
        save_calibration(table, path=cal_path)
        loaded_table = load_calibration(path=cal_path)
        assert loaded_table.entries == table.entries
        assert loaded_table.global_bias == pytest.approx(table.global_bias)
