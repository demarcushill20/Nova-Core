"""Tests for Phase 9 — task reconciliation lifecycle.

Covers:
  - Frontmatter parsing (valid, missing fields)
  - Success metric checking (met, unmet, invalid format)
  - Expiry checking (in-window, out-of-window)
  - Full reconcile workflow (.done scanning, filtering, sub-goal update)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novatrade.autonomy.schemas import DimensionScore, ProgressReport
from novatrade.autonomy.task_reconciler import TaskReconciler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(**dim_scores: float) -> ProgressReport:
    """Build a minimal ProgressReport with the given dimension scores."""
    dimensions = {}
    for name, score in dim_scores.items():
        dimensions[name] = DimensionScore(name=name, score=score)
    overall = sum(dim_scores.values()) / max(len(dim_scores), 1)
    return ProgressReport(overall_score=overall, dimensions=dimensions)


def _write_done_file(
    tasks_dir: Path,
    filename: str,
    *,
    decision_id: str = "",
    sub_goal_id: str = "",
    success_metric: str = "",
    expiry_minutes: int = 120,
    generated_at: str = "",
    extra_fm: str = "",
    mtime: float | None = None,
) -> Path:
    """Write a .done file with YAML frontmatter into the tasks dir."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / filename

    fm_lines = [
        "---",
        "priority: high",
        "category: repair",
        "target_dimension: system_health",
    ]
    if decision_id:
        fm_lines.append(f"decision_id: {decision_id}")
    if sub_goal_id:
        fm_lines.append(f"sub_goal_id: {sub_goal_id}")
    if success_metric:
        fm_lines.append(f'success_metric: "{success_metric}"')
    fm_lines.append(f"expiry_minutes: {expiry_minutes}")
    if generated_at:
        fm_lines.append(f'generated_at: "{generated_at}"')
    if extra_fm:
        fm_lines.append(extra_fm)
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append("# Test task")

    path.write_text("\n".join(fm_lines))

    if mtime is not None:
        os.utime(str(path), (mtime, mtime))

    return path


# ---------------------------------------------------------------------------
# Tests: _parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "TASKS"
        path = _write_done_file(
            tasks_dir,
            "0001_test.md.done",
            decision_id="REPAIR_system_health_20260330T120000",
            sub_goal_id="sg_sys_health_1",
            success_metric="system_health >= 70",
            generated_at="2026-03-30T12:00:00+00:00",
        )

        reconciler = TaskReconciler(base_path=str(tmp_path))
        fm = reconciler._parse_frontmatter(path)

        assert fm["decision_id"] == "REPAIR_system_health_20260330T120000"
        assert fm["sub_goal_id"] == "sg_sys_health_1"
        assert fm["success_metric"] == "system_health >= 70"
        assert fm["priority"] == "high"
        assert fm["generated_at"] == "2026-03-30T12:00:00+00:00"

    def test_missing_fields(self, tmp_path: Path) -> None:
        """Frontmatter with no decision_id or success_metric."""
        tasks_dir = tmp_path / "TASKS"
        path = _write_done_file(tasks_dir, "0002_plain.md.done")

        reconciler = TaskReconciler(base_path=str(tmp_path))
        fm = reconciler._parse_frontmatter(path)

        assert fm.get("decision_id", "") == ""
        assert fm.get("success_metric", "") == ""
        assert fm["priority"] == "high"  # still parses other fields

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        """File with no --- delimiters returns empty dict."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / "0003_nofm.md.done"
        path.write_text("# Just a heading\nNo frontmatter here.\n")

        reconciler = TaskReconciler(base_path=str(tmp_path))
        fm = reconciler._parse_frontmatter(path)
        assert fm == {}


# ---------------------------------------------------------------------------
# Tests: _check_success_metric
# ---------------------------------------------------------------------------


class TestCheckSuccessMetric:
    def test_metric_met(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=85.0)
        assert reconciler._check_success_metric("system_health >= 70", report) is True

    def test_metric_not_met(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=55.0)
        assert reconciler._check_success_metric("system_health >= 70", report) is False

    def test_metric_with_score_keyword(self, tmp_path: Path) -> None:
        """Format: 'dimension score >= N within M hours'."""
        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(execution_pipeline=80.0)
        assert reconciler._check_success_metric("execution_pipeline score >= 75 within 2 hours", report) is True

    def test_metric_invalid_format(self, tmp_path: Path) -> None:
        """Gracefully returns False for unparseable metrics."""
        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=85.0)
        assert reconciler._check_success_metric("gibberish no numbers", report) is False

    def test_metric_unknown_dimension(self, tmp_path: Path) -> None:
        """Dimension not in report returns False."""
        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=85.0)
        assert reconciler._check_success_metric("nonexistent_dim >= 50", report) is False


# ---------------------------------------------------------------------------
# Tests: _check_expiry
# ---------------------------------------------------------------------------


class TestCheckExpiry:
    def test_in_window(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        gen_at = datetime.now(timezone.utc).isoformat()
        completed = time.time() + 60  # completed 1 min later
        assert reconciler._check_expiry(gen_at, 120, completed) is False

    def test_out_of_window(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        # Generated 3 hours ago
        gen_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        completed = time.time()  # completed now
        assert reconciler._check_expiry(gen_at, 120, completed) is True  # 120 min window, but 180 min elapsed

    def test_invalid_timestamp(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        assert reconciler._check_expiry("not-a-date", 120, time.time()) is False

    def test_zero_expiry(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        gen_at = datetime.now(timezone.utc).isoformat()
        assert reconciler._check_expiry(gen_at, 0, time.time()) is False


# ---------------------------------------------------------------------------
# Tests: reconcile (full flow)
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_finds_done_files_with_decision_id(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "TASKS"
        _write_done_file(
            tasks_dir,
            "0010_repair_system_health.md.done",
            decision_id="REPAIR_system_health_20260330",
            success_metric="system_health >= 70",
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=80.0)
        results = reconciler.reconcile(report)

        assert len(results) == 1
        assert results[0].decision_id == "REPAIR_system_health_20260330"
        assert results[0].metric_met is True

    def test_skips_old_done_files(self, tmp_path: Path) -> None:
        """Done files older than 24h should be skipped."""
        tasks_dir = tmp_path / "TASKS"
        old_mtime = time.time() - (25 * 3600)  # 25 hours ago
        _write_done_file(
            tasks_dir,
            "0011_old_task.md.done",
            decision_id="REPAIR_old_20260329",
            success_metric="system_health >= 70",
            mtime=old_mtime,
        )

        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=80.0)
        results = reconciler.reconcile(report)

        assert len(results) == 0

    def test_skips_tasks_without_decision_id(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "TASKS"
        _write_done_file(
            tasks_dir,
            "0012_no_decision.md.done",
            # No decision_id
        )

        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=80.0)
        results = reconciler.reconcile(report)

        assert len(results) == 0

    def test_reconcile_empty_tasks_dir(self, tmp_path: Path) -> None:
        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=80.0)
        results = reconciler.reconcile(report)
        assert results == []

    def test_reconcile_no_tasks_dir(self, tmp_path: Path) -> None:
        """TASKS/ directory doesn't exist at all."""
        reconciler = TaskReconciler(base_path=str(tmp_path / "nonexistent"))
        report = _make_report(system_health=80.0)
        results = reconciler.reconcile(report)
        assert results == []

    def test_reconcile_with_expired_task(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "TASKS"
        # Generated 3 hours ago, 60-minute expiry window
        gen_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        _write_done_file(
            tasks_dir,
            "0013_expired.md.done",
            decision_id="EXECUTE_strat_20260330",
            success_metric="strategy_validity >= 70",
            expiry_minutes=60,
            generated_at=gen_at,
        )

        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(strategy_validity=80.0)
        results = reconciler.reconcile(report)

        assert len(results) == 1
        assert results[0].expired is True
        assert "expiry" in results[0].notes.lower()

    def test_reconcile_metric_not_met(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "TASKS"
        _write_done_file(
            tasks_dir,
            "0014_fail.md.done",
            decision_id="REPAIR_exec_20260330",
            success_metric="execution_pipeline >= 80",
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(execution_pipeline=60.0)
        results = reconciler.reconcile(report)

        assert len(results) == 1
        assert results[0].metric_met is False
        assert "NOT met" in results[0].notes

    def test_reconcile_ignores_non_done_files(self, tmp_path: Path) -> None:
        """Only .done files are considered, not .md or .inprogress."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        # Write an active task (not .done)
        active = tasks_dir / "0015_active.md"
        active.write_text("---\ndecision_id: REPAIR_active\n---\n# Active\n")

        reconciler = TaskReconciler(base_path=str(tmp_path))
        report = _make_report(system_health=80.0)
        results = reconciler.reconcile(report)

        assert len(results) == 0
