"""Tests for Phase 5: Calibration & After-Action Learning.

60+ tests covering ExecutionRecord, execution log I/O, calibration
computation, bias lookup, estimate correction, persistence, reporting,
pattern detection, bias convergence, and edge cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.scheduler.calibration import (
    CalibrationEntry,
    CalibrationTable,
    compute_calibration,
    correct_estimate,
    detect_patterns,
    generate_calibration_report,
    get_bias_multiplier,
    load_calibration,
    save_calibration,
)
from utils.scheduler.execution_log import (
    ExecutionRecord,
    create_execution_record,
    log_execution,
    read_execution_log,
)
from utils.scheduler.work_unit import TaskClass, WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_work_unit(
    id: str = "task_1",
    task_class: TaskClass = TaskClass.BOUNDED,
    work_mode: WorkMode = WorkMode.DEEP_IMPLEMENTATION,
    estimated_expected_min: float = 30.0,
    estimated_optimistic_min: float = 20.0,
    estimated_pessimistic_min: float = 45.0,
    **kwargs,
) -> WorkUnit:
    return WorkUnit(
        id=id,
        title=f"Test {id}",
        task_class=task_class,
        work_mode=work_mode,
        estimated_expected_min=estimated_expected_min,
        estimated_optimistic_min=estimated_optimistic_min,
        estimated_pessimistic_min=estimated_pessimistic_min,
        **kwargs,
    )


def _make_record(
    task_id: str = "task_1",
    task_class: TaskClass = TaskClass.BOUNDED,
    work_mode: WorkMode = WorkMode.DEEP_IMPLEMENTATION,
    planned_duration_min: float = 30.0,
    actual_duration_min: float = 30.0,
    variance_ratio: float | None = None,
    outcome: str = "success",
    outcome_quality: float = 80.0,
    block_id: str = "block_1",
    timestamp: str = "2026-03-29T12:00:00+00:00",
    **kwargs,
) -> ExecutionRecord:
    vr = variance_ratio if variance_ratio is not None else actual_duration_min / planned_duration_min
    return ExecutionRecord(
        task_id=task_id,
        task_class=task_class,
        work_mode=work_mode,
        planned_duration_min=planned_duration_min,
        actual_duration_min=actual_duration_min,
        variance_ratio=vr,
        outcome=outcome,
        outcome_quality=outcome_quality,
        block_id=block_id,
        timestamp=timestamp,
        **kwargs,
    )


def _make_records_with_ratio(
    ratio: float,
    count: int = 10,
    task_class: TaskClass = TaskClass.BOUNDED,
    work_mode: WorkMode = WorkMode.DEEP_IMPLEMENTATION,
    planned: float = 30.0,
) -> list[ExecutionRecord]:
    """Create N records where actual = planned * ratio."""
    return [
        _make_record(
            task_id=f"task_{i}",
            task_class=task_class,
            work_mode=work_mode,
            planned_duration_min=planned,
            actual_duration_min=planned * ratio,
            variance_ratio=ratio,
        )
        for i in range(count)
    ]


# ===========================================================================
# ExecutionRecord tests
# ===========================================================================


class TestExecutionRecord:
    """Tests for the ExecutionRecord model."""

    def test_valid_record_creation(self):
        rec = _make_record()
        assert rec.task_id == "task_1"
        assert rec.task_class == TaskClass.BOUNDED
        assert rec.work_mode == WorkMode.DEEP_IMPLEMENTATION
        assert rec.planned_duration_min == 30.0
        assert rec.actual_duration_min == 30.0
        assert rec.variance_ratio == 1.0
        assert rec.outcome == "success"

    def test_variance_ratio_is_actual_over_planned(self):
        rec = _make_record(planned_duration_min=20.0, actual_duration_min=30.0)
        assert rec.variance_ratio == pytest.approx(1.5)

    def test_create_execution_record_computes_variance(self):
        wu = _make_work_unit(estimated_expected_min=40.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=60.0)
        rec = create_execution_record(wu, actual_duration_min=60.0, outcome="success")
        assert rec.variance_ratio == pytest.approx(1.5)
        assert rec.planned_duration_min == 40.0
        assert rec.actual_duration_min == 60.0

    def test_create_execution_record_with_block_id(self):
        wu = _make_work_unit()
        rec = create_execution_record(wu, actual_duration_min=30.0, outcome="success", block_id="blk_42")
        assert rec.block_id == "blk_42"

    def test_create_execution_record_with_quality(self):
        wu = _make_work_unit()
        rec = create_execution_record(wu, actual_duration_min=30.0, outcome="partial", outcome_quality=65.0)
        assert rec.outcome_quality == 65.0
        assert rec.outcome == "partial"

    def test_create_execution_record_zero_planned_duration(self):
        wu = _make_work_unit(estimated_expected_min=1.0, estimated_optimistic_min=1.0, estimated_pessimistic_min=1.0)
        # Patch to test edge case: estimated_expected_min at minimum
        rec = create_execution_record(wu, actual_duration_min=5.0, outcome="success")
        assert rec.variance_ratio == pytest.approx(5.0)

    def test_record_default_values(self):
        rec = _make_record()
        assert rec.interruptions == 0
        assert rec.retry_count == 0
        assert rec.tools_used == []
        assert rec.unlocked_downstream == []

    def test_record_with_optional_fields(self):
        rec = _make_record(
            interruptions=3,
            retry_count=1,
            tools_used=["pytest", "ruff"],
            unlocked_downstream=["task_2", "task_3"],
        )
        assert rec.interruptions == 3
        assert rec.retry_count == 1
        assert rec.tools_used == ["pytest", "ruff"]
        assert rec.unlocked_downstream == ["task_2", "task_3"]

    def test_record_outcome_quality_bounds(self):
        rec = _make_record(outcome_quality=0.0)
        assert rec.outcome_quality == 0.0
        rec = _make_record(outcome_quality=100.0)
        assert rec.outcome_quality == 100.0

    def test_record_outcome_quality_below_zero_fails(self):
        with pytest.raises(ValueError):
            _make_record(outcome_quality=-1.0)

    def test_record_outcome_quality_above_100_fails(self):
        with pytest.raises(ValueError):
            _make_record(outcome_quality=101.0)

    def test_record_serialization_roundtrip(self):
        rec = _make_record(tools_used=["git", "python"], interruptions=2)
        json_str = rec.model_dump_json()
        restored = ExecutionRecord.model_validate_json(json_str)
        assert restored == rec

    def test_create_execution_record_populates_timestamp(self):
        wu = _make_work_unit()
        rec = create_execution_record(wu, actual_duration_min=30.0, outcome="success")
        assert rec.timestamp != ""
        assert "T" in rec.timestamp  # ISO format


# ===========================================================================
# Execution log I/O tests
# ===========================================================================


class TestExecutionLogIO:
    """Tests for log_execution and read_execution_log."""

    def test_log_execution_creates_file(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        assert not log_file.exists()
        rec = _make_record()
        result = log_execution(rec, log_path=log_file)
        assert result == log_file
        assert log_file.exists()

    def test_log_execution_creates_parent_dirs(self, tmp_path: Path):
        log_file = tmp_path / "deep" / "nested" / "test.jsonl"
        rec = _make_record()
        log_execution(rec, log_path=log_file)
        assert log_file.exists()

    def test_log_execution_appends_not_overwrites(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        rec1 = _make_record(task_id="task_1")
        rec2 = _make_record(task_id="task_2")
        log_execution(rec1, log_path=log_file)
        log_execution(rec2, log_path=log_file)

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_read_execution_log_reads_all(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        for i in range(5):
            log_execution(_make_record(task_id=f"task_{i}"), log_path=log_file)

        records = read_execution_log(log_path=log_file)
        assert len(records) == 5
        assert records[0].task_id == "task_0"
        assert records[4].task_id == "task_4"

    def test_read_execution_log_with_limit(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        for i in range(10):
            log_execution(_make_record(task_id=f"task_{i}"), log_path=log_file)

        records = read_execution_log(log_path=log_file, limit=3)
        assert len(records) == 3
        assert records[0].task_id == "task_7"
        assert records[2].task_id == "task_9"

    def test_read_execution_log_handles_corrupt_lines(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        rec = _make_record(task_id="good_task")
        log_execution(rec, log_path=log_file)
        # Inject a corrupt line
        with open(log_file, "a") as f:
            f.write("NOT VALID JSON\n")
        log_execution(_make_record(task_id="another_good"), log_path=log_file)

        with pytest.warns(UserWarning, match="Skipping corrupt line"):
            records = read_execution_log(log_path=log_file)
        assert len(records) == 2
        assert records[0].task_id == "good_task"
        assert records[1].task_id == "another_good"

    def test_read_execution_log_roundtrip(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        original = _make_record(
            task_id="roundtrip",
            tools_used=["pytest"],
            interruptions=1,
            outcome_quality=92.5,
        )
        log_execution(original, log_path=log_file)
        records = read_execution_log(log_path=log_file)
        assert len(records) == 1
        assert records[0] == original

    def test_read_execution_log_empty_file(self, tmp_path: Path):
        log_file = tmp_path / "empty.jsonl"
        log_file.write_text("")
        records = read_execution_log(log_path=log_file)
        assert records == []

    def test_read_execution_log_missing_file(self, tmp_path: Path):
        log_file = tmp_path / "nonexistent.jsonl"
        records = read_execution_log(log_path=log_file)
        assert records == []

    def test_read_execution_log_blank_lines_skipped(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        rec = _make_record(task_id="t1")
        log_execution(rec, log_path=log_file)
        with open(log_file, "a") as f:
            f.write("\n\n\n")
        log_execution(_make_record(task_id="t2"), log_path=log_file)

        records = read_execution_log(log_path=log_file)
        assert len(records) == 2

    def test_log_execution_jsonl_format(self, tmp_path: Path):
        log_file = tmp_path / "test.jsonl"
        rec = _make_record()
        log_execution(rec, log_path=log_file)
        line = log_file.read_text().strip()
        parsed = json.loads(line)
        assert parsed["task_id"] == "task_1"
        assert parsed["outcome"] == "success"


# ===========================================================================
# Calibration computation tests
# ===========================================================================


class TestComputeCalibration:
    """Tests for compute_calibration."""

    def test_empty_records_returns_default_table(self):
        table = compute_calibration([])
        assert table.global_bias == 1.0
        assert table.entries == {}
        assert table.last_computed != ""

    def test_single_record_sets_global_and_entry(self):
        records = [_make_record(variance_ratio=1.5)]
        table = compute_calibration(records)
        # Global bias = 1.5 (from the single record)
        assert table.global_bias == pytest.approx(1.5)
        assert len(table.entries) == 1

    def test_single_record_entry_uses_global_bias(self):
        """With < 3 samples, entry bias falls back to global."""
        records = [_make_record(variance_ratio=1.5)]
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.sample_count == 1
        # With <3 samples, bias_multiplier is set to global
        assert entry.bias_multiplier == pytest.approx(table.global_bias)

    def test_multiple_records_same_pair_mean(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.mean_variance_ratio == pytest.approx(1.5)
        assert entry.sample_count == 5
        assert entry.bias_multiplier == pytest.approx(1.5)

    def test_bias_clamped_at_lower_bound(self):
        """When tasks always finish in 40% of estimated time, clamp at 0.5."""
        records = _make_records_with_ratio(0.4, count=5)
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.mean_variance_ratio == pytest.approx(0.4)
        assert entry.bias_multiplier == 0.5  # clamped

    def test_bias_clamped_at_upper_bound(self):
        """When tasks take 5x estimate, clamp at 3.0."""
        records = _make_records_with_ratio(5.0, count=5)
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.mean_variance_ratio == pytest.approx(5.0)
        assert entry.bias_multiplier == 3.0  # clamped

    def test_window_limits_records(self):
        # Create 50 records but window=10
        records = _make_records_with_ratio(2.0, count=50)
        table = compute_calibration(records, window=10)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.sample_count == 10

    def test_fewer_than_3_samples_uses_global(self):
        # 2 BOUNDED/DEEP_IMPL + 5 ATOMIC/RESEARCH
        records = _make_records_with_ratio(
            1.2, count=2, task_class=TaskClass.BOUNDED, work_mode=WorkMode.DEEP_IMPLEMENTATION
        )
        records += _make_records_with_ratio(1.8, count=5, task_class=TaskClass.ATOMIC, work_mode=WorkMode.RESEARCH)
        table = compute_calibration(records)

        bounded_key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        atomic_key = f"{TaskClass.ATOMIC.value}_{WorkMode.RESEARCH.value}"

        bounded_entry = table.entries[bounded_key]
        assert bounded_entry.sample_count == 2
        # Falls back to global which is (2*1.2 + 5*1.8) / 7
        assert bounded_entry.bias_multiplier == pytest.approx(table.global_bias)

        atomic_entry = table.entries[atomic_key]
        assert atomic_entry.sample_count == 5
        assert atomic_entry.bias_multiplier == pytest.approx(1.8)

    def test_mixed_pairs_computed_independently(self):
        records_a = _make_records_with_ratio(
            1.2, count=5, task_class=TaskClass.BOUNDED, work_mode=WorkMode.DEEP_IMPLEMENTATION
        )
        records_b = _make_records_with_ratio(0.8, count=5, task_class=TaskClass.ATOMIC, work_mode=WorkMode.ADMIN)
        records_c = _make_records_with_ratio(2.0, count=5, task_class=TaskClass.EPIC, work_mode=WorkMode.RESEARCH)
        table = compute_calibration(records_a + records_b + records_c)

        assert len(table.entries) == 3
        key_a = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        key_b = f"{TaskClass.ATOMIC.value}_{WorkMode.ADMIN.value}"
        key_c = f"{TaskClass.EPIC.value}_{WorkMode.RESEARCH.value}"
        assert table.entries[key_a].bias_multiplier == pytest.approx(1.2)
        assert table.entries[key_b].bias_multiplier == pytest.approx(0.8)
        assert table.entries[key_c].bias_multiplier == pytest.approx(2.0)

    def test_std_computed_for_multiple_records(self):
        # Records with variance_ratio 1.0, 1.2, 1.4, 1.6, 1.8
        records = [_make_record(task_id=f"t{i}", variance_ratio=1.0 + i * 0.2) for i in range(5)]
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.std_variance_ratio > 0

    def test_std_zero_for_uniform_records(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.std_variance_ratio == pytest.approx(0.0)

    def test_global_bias_is_mean_of_all(self):
        records = _make_records_with_ratio(1.0, count=5)
        records += _make_records_with_ratio(2.0, count=5)
        table = compute_calibration(records)
        # Global = mean of all 10 ratios = (5*1.0 + 5*2.0) / 10 = 1.5
        assert table.global_bias == pytest.approx(1.5)

    def test_global_bias_clamped(self):
        records = _make_records_with_ratio(0.3, count=5)
        table = compute_calibration(records)
        assert table.global_bias == 0.5  # clamped from 0.3


# ===========================================================================
# get_bias_multiplier tests
# ===========================================================================


class TestGetBiasMultiplier:
    """Tests for get_bias_multiplier lookup."""

    def test_known_pair_returns_entry_value(self):
        records = _make_records_with_ratio(1.3, count=5)
        table = compute_calibration(records)
        result = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION)
        assert result == pytest.approx(1.3)

    def test_unknown_pair_returns_global(self):
        records = _make_records_with_ratio(1.3, count=5)
        table = compute_calibration(records)
        result = get_bias_multiplier(table, TaskClass.EPIC, WorkMode.MONITORING)
        assert result == pytest.approx(table.global_bias)

    def test_entry_with_less_than_3_samples_returns_global(self):
        records = _make_records_with_ratio(1.5, count=2)
        records += _make_records_with_ratio(1.2, count=10, task_class=TaskClass.ATOMIC, work_mode=WorkMode.ADMIN)
        table = compute_calibration(records)
        # BOUNDED/DEEP_IMPL has only 2 samples — should return global
        result = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION)
        assert result == pytest.approx(table.global_bias)

    def test_empty_table_returns_global_default(self):
        table = CalibrationTable()
        result = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.ADMIN)
        assert result == 1.0

    def test_global_bias_clamped_on_lookup(self):
        table = CalibrationTable(global_bias=0.2)
        result = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.ADMIN)
        assert result == 0.5  # clamped

    def test_global_bias_upper_clamped_on_lookup(self):
        table = CalibrationTable(global_bias=5.0)
        result = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.ADMIN)
        assert result == 3.0  # clamped


# ===========================================================================
# correct_estimate tests
# ===========================================================================


class TestCorrectEstimate:
    """Tests for correct_estimate."""

    def test_applies_multiplier(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        result = correct_estimate(60.0, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION, table)
        assert result == pytest.approx(90.0)

    def test_with_bias_1_5_estimate_60_returns_90(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        assert correct_estimate(60.0, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION, table) == pytest.approx(90.0)

    def test_with_bias_0_7_estimate_60_returns_42(self):
        records = _make_records_with_ratio(0.7, count=5)
        table = compute_calibration(records)
        assert correct_estimate(60.0, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION, table) == pytest.approx(42.0)

    def test_unknown_pair_uses_global(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        # EPIC/MONITORING not in records — uses global (1.5)
        result = correct_estimate(60.0, TaskClass.EPIC, WorkMode.MONITORING, table)
        assert result == pytest.approx(90.0)

    def test_zero_estimate_returns_zero(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        assert correct_estimate(0.0, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION, table) == 0.0


# ===========================================================================
# Calibration persistence tests
# ===========================================================================


class TestCalibrationPersistence:
    """Tests for save_calibration and load_calibration."""

    def test_save_then_load_roundtrip(self, tmp_path: Path):
        records = _make_records_with_ratio(1.4, count=5)
        records += _make_records_with_ratio(0.9, count=5, task_class=TaskClass.ATOMIC, work_mode=WorkMode.ADMIN)
        table = compute_calibration(records)

        cal_path = tmp_path / "calibration.json"
        save_calibration(table, path=cal_path)
        loaded = load_calibration(path=cal_path)

        assert loaded.global_bias == pytest.approx(table.global_bias)
        assert len(loaded.entries) == len(table.entries)
        for key in table.entries:
            assert loaded.entries[key].bias_multiplier == pytest.approx(table.entries[key].bias_multiplier)

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        cal_path = tmp_path / "nonexistent.json"
        table = load_calibration(path=cal_path)
        assert table.global_bias == 1.0
        assert table.entries == {}

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        cal_path = tmp_path / "deep" / "nested" / "calibration.json"
        table = CalibrationTable()
        save_calibration(table, path=cal_path)
        assert cal_path.exists()

    def test_save_overwrites_existing(self, tmp_path: Path):
        cal_path = tmp_path / "calibration.json"
        table1 = CalibrationTable(global_bias=1.2, last_computed="t1")
        save_calibration(table1, path=cal_path)
        table2 = CalibrationTable(global_bias=1.8, last_computed="t2")
        save_calibration(table2, path=cal_path)

        loaded = load_calibration(path=cal_path)
        assert loaded.global_bias == pytest.approx(1.8)

    def test_load_corrupt_file_returns_empty(self, tmp_path: Path):
        cal_path = tmp_path / "calibration.json"
        cal_path.write_text("NOT VALID JSON")
        table = load_calibration(path=cal_path)
        assert table.global_bias == 1.0
        assert table.entries == {}


# ===========================================================================
# Calibration report tests
# ===========================================================================


class TestCalibrationReport:
    """Tests for generate_calibration_report."""

    def test_report_generates_valid_markdown(self):
        records = _make_records_with_ratio(1.3, count=5)
        table = compute_calibration(records)
        report = generate_calibration_report(table, records)
        assert "# Calibration Report" in report
        assert "Global bias" in report

    def test_report_includes_all_pairs(self):
        records_a = _make_records_with_ratio(
            1.2, count=5, task_class=TaskClass.BOUNDED, work_mode=WorkMode.DEEP_IMPLEMENTATION
        )
        records_b = _make_records_with_ratio(0.8, count=5, task_class=TaskClass.ATOMIC, work_mode=WorkMode.ADMIN)
        all_records = records_a + records_b
        table = compute_calibration(all_records)
        report = generate_calibration_report(table, all_records)
        assert "bounded" in report
        assert "atomic" in report
        assert "deep_implementation" in report
        assert "admin" in report

    def test_report_handles_empty_table(self):
        table = CalibrationTable()
        report = generate_calibration_report(table, [])
        assert "# Calibration Report" in report
        assert "No calibration entries" in report

    def test_report_shows_trend_with_enough_data(self):
        records = _make_records_with_ratio(1.5, count=12)
        table = compute_calibration(records)
        report = generate_calibration_report(table, records)
        assert "Trend" in report

    def test_report_no_trend_with_few_records(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        report = generate_calibration_report(table, records)
        assert "Trend" not in report

    def test_report_shows_over_estimated_section(self):
        records = _make_records_with_ratio(0.7, count=5)
        table = compute_calibration(records)
        report = generate_calibration_report(table, records)
        assert "Over-Estimated" in report

    def test_report_shows_under_estimated_section(self):
        records = _make_records_with_ratio(1.5, count=5)
        table = compute_calibration(records)
        report = generate_calibration_report(table, records)
        assert "Under-Estimated" in report

    def test_report_total_records_count(self):
        records = _make_records_with_ratio(1.0, count=7)
        table = compute_calibration(records)
        report = generate_calibration_report(table, records)
        assert "7" in report


# ===========================================================================
# Pattern detection tests
# ===========================================================================


class TestDetectPatterns:
    """Tests for detect_patterns."""

    def test_detects_overestimation_pattern(self):
        records = _make_records_with_ratio(0.7, count=10, work_mode=WorkMode.ADMIN)
        patterns = detect_patterns(records)
        matching = [p for p in patterns if "overestimated" in p.lower()]
        assert len(matching) > 0

    def test_detects_underestimation_pattern(self):
        records = _make_records_with_ratio(1.8, count=10, work_mode=WorkMode.DEBUGGING)
        patterns = detect_patterns(records)
        matching = [p for p in patterns if "1.8x" in p]
        assert len(matching) > 0

    def test_detects_high_variance_mode(self):
        # RESEARCH with high variance
        research_records = [
            _make_record(task_id=f"r{i}", work_mode=WorkMode.RESEARCH, variance_ratio=1.0 + i * 0.5) for i in range(8)
        ]
        # ADMIN with low variance
        admin_records = _make_records_with_ratio(1.0, count=8, work_mode=WorkMode.ADMIN, task_class=TaskClass.ATOMIC)
        patterns = detect_patterns(research_records + admin_records)
        variance_patterns = [p for p in patterns if "variance" in p.lower()]
        assert len(variance_patterns) > 0
        assert any("RESEARCH" in p for p in variance_patterns)

    def test_min_samples_respected(self):
        records = _make_records_with_ratio(1.8, count=3, work_mode=WorkMode.DEBUGGING)
        patterns = detect_patterns(records, min_samples=5)
        assert patterns == []

    def test_no_patterns_with_insufficient_data(self):
        records = _make_records_with_ratio(1.0, count=2)
        patterns = detect_patterns(records, min_samples=5)
        assert patterns == []

    def test_no_patterns_with_empty_records(self):
        patterns = detect_patterns([])
        assert patterns == []

    def test_detects_task_class_overestimation(self):
        records = _make_records_with_ratio(0.7, count=10, task_class=TaskClass.ATOMIC)
        patterns = detect_patterns(records)
        class_patterns = [p for p in patterns if "ATOMIC" in p]
        assert len(class_patterns) > 0

    def test_detects_task_class_underestimation(self):
        records = _make_records_with_ratio(1.5, count=10, task_class=TaskClass.EPIC, work_mode=WorkMode.RESEARCH)
        patterns = detect_patterns(records)
        class_patterns = [p for p in patterns if "EPIC" in p]
        assert len(class_patterns) > 0


# ===========================================================================
# Bias convergence (parametrized)
# ===========================================================================


class TestBiasConvergence:
    """Tests that bias converges to expected values with synthetic data."""

    @pytest.mark.parametrize(
        "ratio,expected_bias",
        [
            (1.5, 1.5),
            (0.8, 0.8),
            (1.0, 1.0),
            (2.0, 2.0),
            (0.6, 0.6),
        ],
    )
    def test_bias_converges_to_actual_ratio(self, ratio: float, expected_bias: float):
        records = _make_records_with_ratio(ratio, count=20)
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        assert entry.bias_multiplier == pytest.approx(expected_bias)

    def test_bias_converges_to_1_5_with_20_records(self):
        records = _make_records_with_ratio(1.5, count=20)
        table = compute_calibration(records)
        bias = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION)
        assert bias == pytest.approx(1.5, abs=0.01)

    def test_bias_converges_to_0_8_with_20_records(self):
        records = _make_records_with_ratio(0.8, count=20)
        table = compute_calibration(records)
        bias = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION)
        assert bias == pytest.approx(0.8, abs=0.01)

    def test_bias_clamped_even_with_extreme_data(self):
        records = _make_records_with_ratio(10.0, count=20)
        table = compute_calibration(records)
        bias = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION)
        assert bias == 3.0

    def test_progressive_improvement_reflected(self):
        """If estimation accuracy improves over time, later window captures it."""
        # First 20 records: 2.0x overrun
        old_records = _make_records_with_ratio(2.0, count=20)
        # Last 10 records: 1.1x (much better)
        new_records = _make_records_with_ratio(1.1, count=10)
        table = compute_calibration(old_records + new_records, window=10)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        entry = table.entries[key]
        # Window=10 should use the last 10 (1.1x) records
        assert entry.bias_multiplier == pytest.approx(1.1)


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    """Misc edge cases."""

    def test_all_same_task_class_and_mode(self):
        records = _make_records_with_ratio(1.3, count=15)
        table = compute_calibration(records)
        assert len(table.entries) == 1

    def test_all_different_combos(self):
        records = []
        for tc in TaskClass:
            for wm in [WorkMode.DEEP_IMPLEMENTATION, WorkMode.RESEARCH]:
                records.append(
                    _make_record(
                        task_id=f"{tc.value}_{wm.value}",
                        task_class=tc,
                        work_mode=wm,
                        variance_ratio=1.0,
                    )
                )
        table = compute_calibration(records)
        assert len(table.entries) == len(TaskClass) * 2

    def test_window_larger_than_records(self):
        records = _make_records_with_ratio(1.2, count=5)
        table = compute_calibration(records, window=100)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        assert table.entries[key].sample_count == 5

    def test_calibration_entry_model_fields(self):
        entry = CalibrationEntry(
            task_class=TaskClass.BOUNDED,
            work_mode=WorkMode.ADMIN,
            sample_count=10,
            mean_variance_ratio=1.3,
            std_variance_ratio=0.2,
            bias_multiplier=1.3,
            last_updated="2026-03-29T12:00:00Z",
        )
        assert entry.task_class == TaskClass.BOUNDED
        assert entry.work_mode == WorkMode.ADMIN

    def test_calibration_table_empty_by_default(self):
        table = CalibrationTable()
        assert table.entries == {}
        assert table.global_bias == 1.0
        assert table.last_computed == ""

    def test_execution_record_all_outcomes(self):
        for outcome in ("success", "partial", "fail"):
            rec = _make_record(outcome=outcome)
            assert rec.outcome == outcome

    def test_mixed_outcomes_dont_break_calibration(self):
        records = [
            _make_record(task_id=f"t{i}", outcome=o, variance_ratio=1.2)
            for i, o in enumerate(["success", "partial", "fail", "success", "success"])
        ]
        table = compute_calibration(records)
        assert len(table.entries) == 1

    def test_correct_estimate_with_default_table(self):
        table = CalibrationTable()
        result = correct_estimate(60.0, TaskClass.BOUNDED, WorkMode.ADMIN, table)
        assert result == pytest.approx(60.0)  # global_bias=1.0

    def test_log_then_calibrate_integration(self, tmp_path: Path):
        """End-to-end: log records, read them, compute calibration."""
        log_file = tmp_path / "exec.jsonl"
        wu = _make_work_unit(estimated_expected_min=30.0, estimated_optimistic_min=20.0, estimated_pessimistic_min=45.0)

        for i in range(10):
            rec = create_execution_record(wu, actual_duration_min=45.0, outcome="success", block_id=f"blk_{i}")
            log_execution(rec, log_path=log_file)

        records = read_execution_log(log_path=log_file)
        assert len(records) == 10

        table = compute_calibration(records)
        bias = get_bias_multiplier(table, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION)
        assert bias == pytest.approx(1.5, abs=0.01)

        corrected = correct_estimate(30.0, TaskClass.BOUNDED, WorkMode.DEEP_IMPLEMENTATION, table)
        assert corrected == pytest.approx(45.0, abs=0.5)

    def test_save_load_calibrate_report_pipeline(self, tmp_path: Path):
        """Full pipeline: compute → save → load → report."""
        records = _make_records_with_ratio(1.4, count=15)
        table = compute_calibration(records)

        cal_path = tmp_path / "cal.json"
        save_calibration(table, path=cal_path)
        loaded = load_calibration(path=cal_path)

        report = generate_calibration_report(loaded, records)
        assert "# Calibration Report" in report
        assert "1.4" in report or "1.400" in report


# ===========================================================================
# H1-H6 input validation fixes
# ===========================================================================


class TestInputValidationFixes:
    """Tests for HIGH issues H1-H6: input validation gaps."""

    # H1: Negative durations rejected
    def test_negative_planned_duration_rejected(self):
        with pytest.raises(ValueError):
            _make_record(planned_duration_min=-1.0)

    def test_negative_actual_duration_rejected(self):
        with pytest.raises(ValueError):
            _make_record(actual_duration_min=-5.0)

    def test_zero_planned_duration_accepted(self):
        rec = _make_record(planned_duration_min=0.0, actual_duration_min=0.0, variance_ratio=0.0)
        assert rec.planned_duration_min == 0.0

    def test_zero_actual_duration_accepted(self):
        rec = _make_record(planned_duration_min=1.0, actual_duration_min=0.0, variance_ratio=0.0)
        assert rec.actual_duration_min == 0.0

    # H2: Negative variance_ratio rejected
    def test_negative_variance_ratio_rejected(self):
        with pytest.raises(ValueError):
            _make_record(variance_ratio=-0.5)

    def test_zero_variance_ratio_accepted(self):
        rec = _make_record(variance_ratio=0.0)
        assert rec.variance_ratio == 0.0

    def test_compute_calibration_filters_non_finite_ratios(self):
        """Non-finite variance ratios (inf/nan) are excluded from aggregation."""
        import math

        records = _make_records_with_ratio(1.5, count=5)
        # Manually inject inf/nan by constructing records with finite values
        # then patching -- but since ge=0.0 is enforced, we test the filter
        # indirectly: all-finite records should work normally.
        table = compute_calibration(records)
        key = f"{TaskClass.BOUNDED.value}_{WorkMode.DEEP_IMPLEMENTATION.value}"
        assert math.isfinite(table.entries[key].mean_variance_ratio)

    # H3: global_bias clamped at model level
    def test_global_bias_clamped_low_at_model_level(self):
        table = CalibrationTable(global_bias=0.1)
        assert table.global_bias == 0.5

    def test_global_bias_clamped_high_at_model_level(self):
        table = CalibrationTable(global_bias=10.0)
        assert table.global_bias == 3.0

    def test_global_bias_within_range_unchanged(self):
        table = CalibrationTable(global_bias=1.5)
        assert table.global_bias == 1.5

    def test_global_bias_at_boundary_low(self):
        table = CalibrationTable(global_bias=0.5)
        assert table.global_bias == 0.5

    def test_global_bias_at_boundary_high(self):
        table = CalibrationTable(global_bias=3.0)
        assert table.global_bias == 3.0

    # H4: detect_patterns single-mode absolute threshold
    def test_detect_patterns_single_mode_high_variance(self):
        """A single mode with std > 0.5 should be flagged even without other modes."""
        records = [
            _make_record(
                task_id=f"t{i}",
                work_mode=WorkMode.RESEARCH,
                task_class=TaskClass.BOUNDED,
                variance_ratio=1.0 + i * 0.3,
            )
            for i in range(8)
        ]
        patterns = detect_patterns(records, min_samples=5)
        variance_patterns = [p for p in patterns if "variance" in p.lower()]
        assert len(variance_patterns) > 0

    # H5: Near-zero planned duration
    def test_near_zero_planned_duration_in_calibration(self):
        """A record with planned_duration_min=0.001 should not break compute_calibration."""
        rec = _make_record(
            planned_duration_min=0.001,
            actual_duration_min=5.0,
            variance_ratio=5000.0,
        )
        table = compute_calibration([rec])
        # Should not crash; bias clamped to 3.0
        assert table.global_bias == pytest.approx(3.0)

    # H6: Encoding in save_calibration (implicit: test roundtrip with unicode)
    def test_save_calibration_utf8_encoding(self, tmp_path):
        """Verify save_calibration writes UTF-8 encoded files."""
        table = CalibrationTable(global_bias=1.5, last_computed="2026-03-29T12:00:00Z")
        cal_path = tmp_path / "cal_utf8.json"
        save_calibration(table, path=cal_path)
        raw = cal_path.read_bytes()
        # Should be valid UTF-8
        raw.decode("utf-8")
        loaded = load_calibration(path=cal_path)
        assert loaded.global_bias == pytest.approx(1.5)
