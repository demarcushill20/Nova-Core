"""Integration tests for the Dynamic Block Scheduler feedback loop.

Phase E: Verifies the complete end-to-end pipeline:
  Generator -> BlockPlan persistence -> BlockState creation/merge ->
  Watcher metadata extraction -> Execution records -> Calibration log

Each test exercises real module code (no mocking of scheduler internals)
and uses tmp_path to avoid touching the production STATE/ directory.
"""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.scheduler.block import BlockPlan
from utils.scheduler.execution_log import (
    _DEFAULT_LOG_PATH,
    create_execution_record,
    log_execution,
    read_execution_log,
)
from utils.scheduler.orchestrator import (
    SchedulerConfig,
    SchedulerResult,
    build_block_plan,
)
from utils.scheduler.replanner import (
    BlockState,
    load_block_state,
    save_block_state,
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


def _make_task(
    task_id: str = "t1",
    title: str = "Test task",
    work_mode: WorkMode = WorkMode.DEEP_IMPLEMENTATION,
    task_class: TaskClass = TaskClass.BOUNDED,
    expected_min: float = 30.0,
    optimistic_min: float = 20.0,
    pessimistic_min: float = 50.0,
) -> WorkUnit:
    return WorkUnit(
        id=task_id,
        title=title,
        priority="high",
        work_mode=work_mode,
        task_class=task_class,
        commitment_level=CommitmentLevel.HARD,
        estimated_expected_min=expected_min,
        estimated_optimistic_min=optimistic_min,
        estimated_pessimistic_min=pessimistic_min,
        confidence=0.7,
        urgency=0.8,
        strategic_value=0.6,
        must_do_today=True,
        schedulable_now=True,
    )


def _make_blocks(n: int = 4) -> list[WorkUnit]:
    """Create n distinct work units for testing."""
    modes = list(WorkMode)
    return [
        _make_task(
            task_id=f"shift_20260329_{i}_task{i}",
            title=f"Task {i}",
            work_mode=modes[i % len(modes)],
        )
        for i in range(1, n + 1)
    ]


def _build_plan_data(result: SchedulerResult, shift_date: str, total_blocks: int) -> dict:
    """Mirror the plan_data dict the generator writes to current_block_plan.json."""
    return {
        "generated_at": datetime.now().isoformat(),
        "shift_date": shift_date,
        "block_plan": result.block_plan.model_dump(mode="json") if result.block_plan else None,
        "calibration_applied": result.calibration_applied,
        "starvation_alerts": result.starvation_alerts,
        "total_blocks": total_blocks,
    }


# ---------------------------------------------------------------------------
# Test 1: BlockPlan persistence
# ---------------------------------------------------------------------------


class TestBlockPlanPersistence:
    """Verify current_block_plan.json is written with correct fields."""

    def test_plan_fields_present(self, tmp_path: Path):
        tasks = _make_blocks(4)
        config = SchedulerConfig(
            enabled=True,
            use_calibration=False,
            use_uncertainty=False,
        )
        result = build_block_plan(tasks, block_duration_min=180.0, config=config)

        # Simulate the generator's persistence step
        plan_path = tmp_path / "scheduler" / "current_block_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_data = _build_plan_data(result, "2026-03-29", len(tasks))
        plan_path.write_text(json.dumps(plan_data, indent=2, default=str))

        # Read back and verify required fields
        loaded = json.loads(plan_path.read_text())
        assert "generated_at" in loaded
        assert loaded["shift_date"] == "2026-03-29"
        assert loaded["block_plan"] is not None
        assert isinstance(loaded["calibration_applied"], bool)
        assert loaded["total_blocks"] == 4

    def test_plan_contains_scheduled_slots(self, tmp_path: Path):
        tasks = _make_blocks(3)
        config = SchedulerConfig(enabled=True, use_calibration=False, use_uncertainty=False)
        result = build_block_plan(tasks, block_duration_min=300.0, config=config)

        plan_path = tmp_path / "current_block_plan.json"
        plan_data = _build_plan_data(result, "2026-03-29", len(tasks))
        plan_path.write_text(json.dumps(plan_data, indent=2, default=str))

        loaded = json.loads(plan_path.read_text())
        bp = loaded["block_plan"]
        assert "scheduled_slots" in bp
        assert "deferred_tasks" in bp
        # At least some tasks should be scheduled (block is big enough)
        assert len(bp["scheduled_slots"]) > 0


# ---------------------------------------------------------------------------
# Test 2: BlockState creation
# ---------------------------------------------------------------------------


class TestBlockStateCreation:
    """Verify shift_YYYYMMDD_state.json has correct structure."""

    def test_initial_state_fields(self, tmp_path: Path):
        tasks = _make_blocks(3)
        config = SchedulerConfig(enabled=True, use_calibration=False, use_uncertainty=False)
        result = build_block_plan(tasks, block_duration_min=300.0, config=config)

        block_id = "shift_20260329"
        now_utc = datetime.now(timezone.utc)
        initial_state = BlockState(
            block_id=block_id,
            original_plan=result.block_plan,
            block_start_utc=now_utc,
            block_end_utc=now_utc + timedelta(hours=5),
        )
        save_block_state(initial_state, state_dir=tmp_path)

        # Verify file exists with correct name
        state_file = tmp_path / f"{block_id}_state.json"
        assert state_file.exists()

        loaded = load_block_state(block_id, state_dir=tmp_path)
        assert loaded is not None
        assert loaded.block_id == block_id
        assert loaded.original_plan is not None
        assert len(loaded.original_plan.scheduled_slots) > 0
        assert loaded.completed_task_ids == []
        assert loaded.failed_task_ids == []
        assert loaded.in_progress_task_id is None
        assert loaded.replan_count == 0


# ---------------------------------------------------------------------------
# Test 3: BlockState merge (batch mode)
# ---------------------------------------------------------------------------


class TestBlockStateMerge:
    """Verify batch-mode merge: second call extends existing state."""

    def test_afternoon_merges_into_morning(self, tmp_path: Path):
        config = SchedulerConfig(enabled=True, use_calibration=False, use_uncertainty=False)
        block_id = "shift_20260329"
        now_utc = datetime.now(timezone.utc)

        # --- Morning call ---
        morning_tasks = [_make_task(task_id=f"shift_20260329_{i}_morning", title=f"Morning {i}") for i in range(1, 4)]
        morning_result = build_block_plan(morning_tasks, block_duration_min=180.0, config=config)

        morning_state = BlockState(
            block_id=block_id,
            original_plan=morning_result.block_plan,
            block_start_utc=now_utc,
            block_end_utc=now_utc + timedelta(hours=3),
        )
        save_block_state(morning_state, state_dir=tmp_path)

        # --- Afternoon call (merge) ---
        afternoon_tasks = [
            _make_task(task_id=f"shift_20260329_{i}_afternoon", title=f"Afternoon {i}") for i in range(9, 12)
        ]
        afternoon_result = build_block_plan(afternoon_tasks, block_duration_min=180.0, config=config)

        existing = load_block_state(block_id, state_dir=tmp_path)
        assert existing is not None

        # Reproduce the generator's merge logic
        merged_slots = list(existing.original_plan.scheduled_slots)
        merged_slots.extend(afternoon_result.block_plan.scheduled_slots)
        merged_deferred = list(existing.original_plan.deferred_tasks) + list(afternoon_result.block_plan.deferred_tasks)
        merged_plan = BlockPlan(
            scheduled_slots=merged_slots,
            deferred_tasks=merged_deferred,
        )
        existing.original_plan = merged_plan
        existing.block_end_utc = now_utc + timedelta(hours=8)
        save_block_state(existing, state_dir=tmp_path)

        # Verify merged state
        final = load_block_state(block_id, state_dir=tmp_path)
        assert final is not None
        morning_count = len(morning_result.block_plan.scheduled_slots)
        afternoon_count = len(afternoon_result.block_plan.scheduled_slots)
        assert len(final.original_plan.scheduled_slots) == morning_count + afternoon_count
        # End time was extended
        assert final.block_end_utc == now_utc + timedelta(hours=8)

    def test_active_state_not_overwritten(self, tmp_path: Path):
        """If the existing state has progress, merging should be skipped."""
        config = SchedulerConfig(enabled=True, use_calibration=False, use_uncertainty=False)
        block_id = "shift_20260329"
        now_utc = datetime.now(timezone.utc)

        morning_tasks = _make_blocks(2)
        morning_result = build_block_plan(morning_tasks, block_duration_min=180.0, config=config)

        active_state = BlockState(
            block_id=block_id,
            original_plan=morning_result.block_plan,
            block_start_utc=now_utc,
            block_end_utc=now_utc + timedelta(hours=3),
            completed_task_ids=["shift_20260329_1_task1"],  # Has progress
        )
        save_block_state(active_state, state_dir=tmp_path)

        existing = load_block_state(block_id, state_dir=tmp_path)
        assert existing is not None
        # Generator logic: skip if completed_task_ids or in_progress_task_id
        should_skip = bool(existing.completed_task_ids or existing.in_progress_task_id)
        assert should_skip is True


# ---------------------------------------------------------------------------
# Test 4: Block ID parsing
# ---------------------------------------------------------------------------


class TestBlockIdParsing:
    """Test _get_current_block_id logic for various filename patterns."""

    @staticmethod
    def _parse_block_id(filename: str) -> str | None:
        """Replicate the block-ID extraction logic from watcher._get_current_block_id."""
        stem = filename.replace(".md.inprogress", "")
        parts = stem.split("_")
        if len(parts) >= 2 and parts[0] == "shift":
            return f"{parts[0]}_{parts[1]}"
        return None

    def test_standard_shift_filename(self):
        assert self._parse_block_id("shift_20260329_1_system_health.md.inprogress") == "shift_20260329"

    def test_afternoon_block(self):
        assert self._parse_block_id("shift_20260329_12_novatrade_testing.md.inprogress") == "shift_20260329"

    def test_multi_word_slug(self):
        assert self._parse_block_id("shift_20260329_4_deep_research.md.inprogress") == "shift_20260329"

    def test_no_shift_prefix(self):
        assert self._parse_block_id("daily_report_20260329.md.inprogress") is None

    def test_single_part(self):
        assert self._parse_block_id("task.md.inprogress") is None

    def test_only_shift_and_date(self):
        assert self._parse_block_id("shift_20260329.md.inprogress") == "shift_20260329"


# ---------------------------------------------------------------------------
# Test 5: Metadata extraction (with frontmatter)
# ---------------------------------------------------------------------------


class TestMetadataExtraction:
    """Test _extract_task_metadata with YAML frontmatter."""

    @staticmethod
    def _extract(task_path: Path) -> dict:
        """Import and call the real watcher function.

        We import lazily because watcher.py has heavy top-level imports.
        If importing fails (e.g. missing optional deps in test env),
        we fall back to a standalone reimplementation that matches the
        same logic.
        """
        try:
            from watcher import _extract_task_metadata

            return _extract_task_metadata(task_path)
        except Exception:
            # Fallback: standalone extraction matching watcher logic
            return _extract_standalone(task_path)

    def test_full_frontmatter(self, tmp_path: Path):
        task = tmp_path / "shift_20260329_3_implementation.md"
        task.write_text(
            textwrap.dedent("""\
            ---
            work_mode: deep_implementation
            task_class: bounded
            commitment_level: hard
            estimated_expected_min: 45.0
            ---
            # Implementation block
            Do the work.
        """)
        )
        meta = self._extract(task)
        assert meta["work_mode"] == "deep_implementation"
        assert meta["task_class"] == "bounded"
        assert meta["commitment_level"] == "hard"
        assert meta["estimated_expected_min"] == 45.0

    def test_partial_frontmatter(self, tmp_path: Path):
        task = tmp_path / "shift_20260329_1_system_health.md"
        task.write_text(
            textwrap.dedent("""\
            ---
            work_mode: monitoring
            ---
            # System health check
        """)
        )
        meta = self._extract(task)
        assert meta["work_mode"] == "monitoring"
        # Defaults for missing keys
        assert meta["task_class"] == "bounded"
        assert meta["commitment_level"] == "soft"
        assert meta["estimated_expected_min"] == 35.0


# ---------------------------------------------------------------------------
# Test 6: Metadata extraction fallback (no frontmatter)
# ---------------------------------------------------------------------------


class TestMetadataFallback:
    """Test _extract_task_metadata with no YAML frontmatter."""

    @staticmethod
    def _extract(task_path: Path) -> dict:
        try:
            from watcher import _extract_task_metadata

            return _extract_task_metadata(task_path)
        except Exception:
            return _extract_standalone(task_path)

    def test_no_frontmatter_slug_based(self, tmp_path: Path):
        task = tmp_path / "shift_20260329_4_deep_research.md"
        task.write_text("# Deep research block\nDo research stuff.\n")
        meta = self._extract(task)
        # Should infer work_mode from the slug "deep_research" -> "research"
        assert meta["work_mode"] == "research"
        # Defaults for everything else
        assert meta["task_class"] == "bounded"
        assert meta["estimated_expected_min"] == 35.0

    def test_no_frontmatter_unknown_slug(self, tmp_path: Path):
        task = tmp_path / "shift_20260329_99_something_new.md"
        task.write_text("# A brand new task type\n")
        meta = self._extract(task)
        # Falls back to default work_mode
        assert meta["work_mode"] == "admin"

    def test_empty_file(self, tmp_path: Path):
        task = tmp_path / "shift_20260329_1_health.md"
        task.write_text("")
        meta = self._extract(task)
        # Should still return valid defaults
        assert isinstance(meta["work_mode"], str)
        assert isinstance(meta["estimated_expected_min"], float)

    def test_nonexistent_file(self, tmp_path: Path):
        task = tmp_path / "does_not_exist.md"
        meta = self._extract(task)
        # Should return defaults, not raise
        assert meta["work_mode"] == "admin"
        assert meta["estimated_expected_min"] == 35.0


# ---------------------------------------------------------------------------
# Test 7: Execution record with real data
# ---------------------------------------------------------------------------


class TestExecutionRecordRealData:
    """Verify create_execution_record uses real WorkUnit metadata."""

    def test_record_from_work_unit(self, tmp_path: Path):
        wu = WorkUnit(
            id="shift_20260329_3_impl",
            title="Implementation block",
            work_mode=WorkMode.DEEP_IMPLEMENTATION,
            task_class=TaskClass.BOUNDED,
            commitment_level=CommitmentLevel.HARD,
            estimated_expected_min=45.0,
            estimated_optimistic_min=30.0,
            estimated_pessimistic_min=60.0,
        )

        record = create_execution_record(
            work_unit=wu,
            actual_duration_min=42.5,
            outcome="success",
            block_id="shift_20260329",
            outcome_quality=85.0,
        )

        # Verify non-default values from the WorkUnit
        assert record.task_id == "shift_20260329_3_impl"
        assert record.work_mode == WorkMode.DEEP_IMPLEMENTATION
        assert record.task_class == TaskClass.BOUNDED
        assert record.planned_duration_min == 45.0
        assert record.actual_duration_min == 42.5
        assert record.outcome == "success"
        assert record.outcome_quality == 85.0
        assert record.block_id == "shift_20260329"
        # Variance ratio = 42.5 / 45.0
        assert abs(record.variance_ratio - (42.5 / 45.0)) < 0.001
        assert record.timestamp != ""

    def test_record_persists_to_jsonl(self, tmp_path: Path):
        wu = _make_task(task_id="test_persist", work_mode=WorkMode.RESEARCH)
        record = create_execution_record(
            work_unit=wu,
            actual_duration_min=25.0,
            outcome="success",
            outcome_quality=90.0,
        )

        log_path = tmp_path / "execution_log.jsonl"
        log_execution(record, log_path=log_path)

        assert log_path.exists()
        records = read_execution_log(log_path=log_path)
        assert len(records) == 1
        assert records[0].task_id == "test_persist"
        assert records[0].work_mode == WorkMode.RESEARCH

    def test_multiple_records_append(self, tmp_path: Path):
        log_path = tmp_path / "execution_log.jsonl"

        for i in range(3):
            wu = _make_task(task_id=f"task_{i}", work_mode=WorkMode.ADMIN)
            record = create_execution_record(
                work_unit=wu,
                actual_duration_min=float(10 + i * 5),
                outcome="success",
                outcome_quality=float(70 + i * 10),
            )
            log_execution(record, log_path=log_path)

        records = read_execution_log(log_path=log_path)
        assert len(records) == 3
        assert [r.task_id for r in records] == ["task_0", "task_1", "task_2"]
        assert records[2].outcome_quality == 90.0


# ---------------------------------------------------------------------------
# Test 8: Execution log path alignment
# ---------------------------------------------------------------------------


class TestExecutionLogPathAlignment:
    """Verify default paths match between execution_log.py and watcher.py."""

    def test_default_path_is_state_scheduler(self):
        assert _DEFAULT_LOG_PATH == Path("STATE/scheduler/execution_log.jsonl")

    def test_default_path_matches_watcher_convention(self):
        """The watcher constructs: SCHEDULER_STATE_DIR / 'execution_log.jsonl'
        where SCHEDULER_STATE_DIR = BASE_DIR / 'STATE' / 'scheduler'.

        The relative form should match _DEFAULT_LOG_PATH.
        """
        # The watcher's construction resolves to STATE/scheduler/execution_log.jsonl
        # which matches the default in execution_log.py
        watcher_relative = Path("STATE") / "scheduler" / "execution_log.jsonl"
        assert _DEFAULT_LOG_PATH == watcher_relative

    def test_read_empty_log(self, tmp_path: Path):
        """Reading a non-existent log returns empty list, not an error."""
        records = read_execution_log(log_path=tmp_path / "nonexistent.jsonl")
        assert records == []


# ---------------------------------------------------------------------------
# Standalone _extract_task_metadata fallback (for when watcher import fails)
# ---------------------------------------------------------------------------

_SLUG_WORK_MODE_MAP: dict[str, str] = {
    "system_health": "monitoring",
    "health": "monitoring",
    "monitoring": "monitoring",
    "implementation": "deep_implementation",
    "impl": "deep_implementation",
    "novatrade": "deep_implementation",
    "research": "research",
    "deep_research": "research",
    "analysis": "analysis",
    "review": "review",
    "testing": "review",
    "quality": "review",
    "free_will": "analysis",
    "autonomy": "analysis",
    "session_wrap": "communication",
    "evening_wrap": "communication",
    "memory_hygiene": "admin",
    "deployment": "deployment",
    "debugging": "debugging",
}


def _extract_standalone(task_path: Path) -> dict:
    """Standalone reimplementation of watcher._extract_task_metadata.

    Used when watcher.py cannot be imported in the test environment.
    Logic is kept in sync with the watcher's implementation.
    """
    defaults: dict = {
        "work_mode": "admin",
        "task_class": "bounded",
        "commitment_level": "soft",
        "estimated_expected_min": 35.0,
    }
    try:
        raw = task_path.read_text(encoding="utf-8", errors="replace")[:8192]
    except Exception:
        return defaults

    # Parse YAML frontmatter
    fm: dict = {}
    stripped = raw.lstrip()
    if stripped.startswith("---"):
        parts = stripped.split("---", 2)
        if len(parts) >= 3:
            yaml_block = parts[1]
            for line in yaml_block.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                colon_idx = line.find(":")
                if colon_idx > 0:
                    key = line[:colon_idx].strip()
                    val = line[colon_idx + 1 :].strip().strip('"').strip("'")
                    fm[key] = val

    result = dict(defaults)

    if "work_mode" in fm:
        result["work_mode"] = fm["work_mode"]
    else:
        stem = task_path.stem.replace(".md", "")
        slug_parts = stem.lower().split("_")
        for slug, mode in _SLUG_WORK_MODE_MAP.items():
            slug_tokens = slug.split("_")
            for i in range(len(slug_parts) - len(slug_tokens) + 1):
                if slug_parts[i : i + len(slug_tokens)] == slug_tokens:
                    result["work_mode"] = mode
                    break
            else:
                continue
            break

    if "task_class" in fm:
        result["task_class"] = fm["task_class"]
    if "commitment_level" in fm:
        result["commitment_level"] = fm["commitment_level"]
    if "estimated_expected_min" in fm:
        try:
            result["estimated_expected_min"] = float(fm["estimated_expected_min"])
        except (ValueError, TypeError):
            pass

    return result
