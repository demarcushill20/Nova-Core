"""Tests for scripts/task_archive.py — task archival utility."""

# Import the module under test
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.task_archive import archive_tasks, consolidate_completed, get_task_stats


@pytest.fixture
def tasks_dir(tmp_path, monkeypatch):
    """Create a temporary TASKS/ directory with test files."""
    tasks = tmp_path / "TASKS"
    tasks.mkdir()
    archive = tasks / "archive"
    archive.mkdir()

    # Patch the module's paths
    import scripts.task_archive as mod

    monkeypatch.setattr(mod, "BASE", tmp_path)
    monkeypatch.setattr(mod, "TASKS", tasks)
    monkeypatch.setattr(mod, "ARCHIVE", archive)

    return tasks


def _make_old_file(path: Path, age_days: int = 5) -> None:
    """Create a file and set its mtime to `age_days` ago."""
    path.write_text("test content")
    old_time = time.time() - (age_days * 86400)
    import os

    os.utime(path, (old_time, old_time))


def _make_recent_file(path: Path) -> None:
    """Create a file with current timestamp."""
    path.write_text("test content")


class TestGetTaskStats:
    def test_empty_dir(self, tasks_dir):
        stats = get_task_stats()
        assert stats["total"] == 0
        assert stats["done"] == 0

    def test_counts_by_state(self, tasks_dir):
        (tasks_dir / "t1.md").write_text("pending")
        (tasks_dir / "t2.md.inprogress").write_text("working")
        (tasks_dir / "t3.md.done").write_text("done")
        (tasks_dir / "t4.md.failed").write_text("failed")
        (tasks_dir / "t5.md.cancelled").write_text("cancelled")

        stats = get_task_stats()
        assert stats["pending (.md)"] == 1
        assert stats["in-progress"] == 1
        assert stats["done"] == 1
        assert stats["failed"] == 1
        assert stats["cancelled"] == 1
        assert stats["total"] == 5

    def test_counts_archived(self, tasks_dir):
        archive = tasks_dir / "archive"
        (archive / "old1.md.done").write_text("archived")
        (archive / "old2.md.failed").write_text("archived")

        stats = get_task_stats()
        assert stats["archived"] == 2


class TestArchiveTasks:
    def test_dry_run_does_not_move(self, tasks_dir):
        _make_old_file(tasks_dir / "t1.md.done")
        result = archive_tasks(max_age_days=3, apply=False)

        assert result.archived == 1
        assert (tasks_dir / "t1.md.done").exists()  # still in place

    def test_apply_moves_old_done(self, tasks_dir):
        _make_old_file(tasks_dir / "t1.md.done", age_days=5)
        result = archive_tasks(max_age_days=3, apply=True)

        assert result.archived == 1
        assert not (tasks_dir / "t1.md.done").exists()
        assert (tasks_dir / "archive" / "t1.md.done").exists()

    def test_apply_moves_old_failed(self, tasks_dir):
        _make_old_file(tasks_dir / "t1.md.failed", age_days=5)
        result = archive_tasks(max_age_days=3, apply=True)

        assert result.archived == 1
        assert (tasks_dir / "archive" / "t1.md.failed").exists()

    def test_skips_recent_files(self, tasks_dir):
        _make_recent_file(tasks_dir / "t1.md.done")
        result = archive_tasks(max_age_days=3, apply=True)

        assert result.archived == 0
        assert (tasks_dir / "t1.md.done").exists()

    def test_skips_pending_and_inprogress(self, tasks_dir):
        _make_old_file(tasks_dir / "t1.md", age_days=10)
        _make_old_file(tasks_dir / "t2.md.inprogress", age_days=10)
        result = archive_tasks(max_age_days=3, apply=True)

        assert result.archived == 0
        assert (tasks_dir / "t1.md").exists()
        assert (tasks_dir / "t2.md.inprogress").exists()

    def test_by_suffix_tracking(self, tasks_dir):
        _make_old_file(tasks_dir / "a.md.done", age_days=5)
        _make_old_file(tasks_dir / "b.md.done", age_days=5)
        _make_old_file(tasks_dir / "c.md.failed", age_days=5)
        result = archive_tasks(max_age_days=3, apply=True)

        assert result.by_suffix[".done"] == 2
        assert result.by_suffix[".failed"] == 1

    def test_handles_collision_in_archive(self, tasks_dir):
        # Pre-existing file in archive with same name
        (tasks_dir / "archive" / "t1.md.done").write_text("old archived")
        _make_old_file(tasks_dir / "t1.md.done", age_days=5)

        result = archive_tasks(max_age_days=3, apply=True)
        assert result.archived == 1
        # Should have appended counter
        assert (tasks_dir / "archive" / "t1.md_1.done").exists()


class TestConsolidateCompleted:
    def test_moves_from_completed_to_archive(self, tasks_dir, monkeypatch):
        completed = tasks_dir / "_completed"
        completed.mkdir()
        (completed / "old_shift.md.done").write_text("done")

        # _completed is not used by consolidate — it checks TASKS/_completed
        # which is already patched via TASKS

        moved = consolidate_completed(apply=True)
        assert moved == 1
        assert (tasks_dir / "archive" / "old_shift.md.done").exists()
        assert not completed.exists()  # empty dir removed

    def test_dry_run_counts_without_moving(self, tasks_dir):
        completed = tasks_dir / "_completed"
        completed.mkdir()
        (completed / "s1.done").write_text("done")
        (completed / "s2.done").write_text("done")

        moved = consolidate_completed(apply=False)
        assert moved == 2
        assert (completed / "s1.done").exists()  # still in place

    def test_no_completed_dir(self, tasks_dir):
        moved = consolidate_completed(apply=True)
        assert moved == 0


class TestIntegration:
    def test_full_workflow(self, tasks_dir):
        """Simulate a realistic task directory and archive."""
        # Old done tasks
        for i in range(10):
            _make_old_file(tasks_dir / f"task_{i:04d}.md.done", age_days=7)

        # Old failed tasks
        for i in range(5):
            _make_old_file(tasks_dir / f"failed_{i:04d}.md.failed", age_days=7)

        # Recent done (should stay)
        for i in range(3):
            _make_recent_file(tasks_dir / f"recent_{i:04d}.md.done")

        # Pending and in-progress (should stay)
        _make_recent_file(tasks_dir / "active.md")
        _make_old_file(tasks_dir / "working.md.inprogress", age_days=1)

        # Run archive
        result = archive_tasks(max_age_days=3, apply=True)

        assert result.archived == 15  # 10 done + 5 failed
        assert result.scanned == 20  # 15 archived + 3 recent + 2 active

        # Verify post-state
        remaining = [p.name for p in tasks_dir.iterdir() if p.is_file()]
        assert len(remaining) == 5  # 3 recent done + 1 pending + 1 inprogress
        assert "active.md" in remaining
        assert "working.md.inprogress" in remaining

        archived = list((tasks_dir / "archive").iterdir())
        assert len(archived) == 15
