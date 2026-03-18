"""Tests for utils/task_checkpoint.py — Phase 6B.14 Task Checkpointing."""

import json
import threading

import pytest

from utils.task_checkpoint import (
    MAX_TASK_RETRIES,
    TaskCheckpoint,
    _now_iso,
    clear_checkpoint,
    increment_retry,
    list_incomplete_checkpoints,
    load_checkpoint,
    save_checkpoint,
    update_status,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_checkpoint_dir(tmp_path, monkeypatch):
    """Redirect CHECKPOINT_DIR to a temp directory for every test."""
    import utils.task_checkpoint as mod

    monkeypatch.setattr(mod, "CHECKPOINT_DIR", tmp_path / "checkpoints")


def _make_checkpoint(task_id: str = "0042_test", **overrides) -> TaskCheckpoint:
    """Helper to build a TaskCheckpoint with sane defaults."""
    defaults = {
        "task_id": task_id,
        "task_file": f"{task_id}.md",
        "status": "dispatched",
        "started_at": _now_iso(),
        "last_updated": _now_iso(),
        "partial_output": None,
        "retry_count": 0,
    }
    defaults.update(overrides)
    return TaskCheckpoint(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveCheckpoint:
    def test_creates_correct_json_file(self):
        """save_checkpoint creates a valid JSON file at the expected path."""
        cp = _make_checkpoint()
        result = save_checkpoint(cp)

        assert result.exists()
        assert result.name == "0042_test.json"
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["task_id"] == "0042_test"
        assert data["status"] == "dispatched"
        assert data["task_file"] == "0042_test.md"
        assert data["retry_count"] == 0

    def test_checkpoint_dir_created_if_missing(self, tmp_path, monkeypatch):
        """CHECKPOINT_DIR is auto-created when it doesn't exist."""
        import utils.task_checkpoint as mod

        cp_dir = mod.CHECKPOINT_DIR
        assert not cp_dir.exists()

        cp = _make_checkpoint()
        save_checkpoint(cp)

        assert cp_dir.exists()
        assert cp_dir.is_dir()

    def test_overwrite_existing(self):
        """Saving a checkpoint with the same task_id overwrites the old one."""
        cp1 = _make_checkpoint(status="dispatched")
        save_checkpoint(cp1)

        cp2 = _make_checkpoint(status="in_progress")
        save_checkpoint(cp2)

        loaded = load_checkpoint("0042_test")
        assert loaded is not None
        assert loaded.status == "in_progress"


class TestLoadCheckpoint:
    def test_reads_back_correctly(self):
        """load_checkpoint returns the same data that was saved."""
        cp = _make_checkpoint(
            partial_output="some partial result",
            retry_count=2,
        )
        save_checkpoint(cp)

        loaded = load_checkpoint("0042_test")
        assert loaded is not None
        assert loaded.task_id == cp.task_id
        assert loaded.task_file == cp.task_file
        assert loaded.status == cp.status
        assert loaded.started_at == cp.started_at
        assert loaded.partial_output == "some partial result"
        assert loaded.retry_count == 2

    def test_returns_none_for_nonexistent(self):
        """load_checkpoint returns None when no checkpoint file exists."""
        result = load_checkpoint("nonexistent_task")
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path, monkeypatch):
        """Malformed checkpoint file returns None gracefully."""
        import utils.task_checkpoint as mod

        cp_dir = mod.CHECKPOINT_DIR
        cp_dir.mkdir(parents=True, exist_ok=True)

        malformed_path = cp_dir / "bad_task.json"
        malformed_path.write_text("{this is not valid json!!!", encoding="utf-8")

        result = load_checkpoint("bad_task")
        assert result is None

    def test_returns_none_for_missing_fields(self, tmp_path, monkeypatch):
        """Checkpoint with missing required fields returns None."""
        import utils.task_checkpoint as mod

        cp_dir = mod.CHECKPOINT_DIR
        cp_dir.mkdir(parents=True, exist_ok=True)

        incomplete_path = cp_dir / "incomplete_task.json"
        incomplete_path.write_text(
            json.dumps({"task_id": "incomplete_task"}),
            encoding="utf-8",
        )

        result = load_checkpoint("incomplete_task")
        assert result is None


class TestClearCheckpoint:
    def test_removes_file(self):
        """clear_checkpoint removes the checkpoint file."""
        cp = _make_checkpoint()
        path = save_checkpoint(cp)
        assert path.exists()

        clear_checkpoint("0042_test")
        assert not path.exists()

    def test_no_error_for_nonexistent(self):
        """clear_checkpoint does not raise when the file doesn't exist."""
        clear_checkpoint("nonexistent_task")  # should not raise


class TestListIncompleteCheckpoints:
    def test_finds_saved_checkpoints(self):
        """list_incomplete_checkpoints returns all saved checkpoints."""
        save_checkpoint(_make_checkpoint("task_a"))
        save_checkpoint(_make_checkpoint("task_b"))
        save_checkpoint(_make_checkpoint("task_c"))

        results = list_incomplete_checkpoints()
        task_ids = {cp.task_id for cp in results}
        assert task_ids == {"task_a", "task_b", "task_c"}

    def test_ignores_cleared_checkpoints(self):
        """Cleared checkpoints are not returned."""
        save_checkpoint(_make_checkpoint("task_keep"))
        save_checkpoint(_make_checkpoint("task_clear"))
        clear_checkpoint("task_clear")

        results = list_incomplete_checkpoints()
        task_ids = {cp.task_id for cp in results}
        assert task_ids == {"task_keep"}

    def test_max_retries_respected(self):
        """Tasks beyond MAX_TASK_RETRIES are not returned."""
        save_checkpoint(_make_checkpoint("task_ok", retry_count=0))
        save_checkpoint(_make_checkpoint("task_retried", retry_count=MAX_TASK_RETRIES - 1))
        save_checkpoint(_make_checkpoint("task_exhausted", retry_count=MAX_TASK_RETRIES))
        save_checkpoint(_make_checkpoint("task_over", retry_count=MAX_TASK_RETRIES + 1))

        results = list_incomplete_checkpoints()
        task_ids = {cp.task_id for cp in results}
        assert "task_ok" in task_ids
        assert "task_retried" in task_ids
        assert "task_exhausted" not in task_ids
        assert "task_over" not in task_ids

    def test_empty_when_no_dir(self, tmp_path, monkeypatch):
        """Returns empty list when CHECKPOINT_DIR doesn't exist."""
        import utils.task_checkpoint as mod

        monkeypatch.setattr(mod, "CHECKPOINT_DIR", tmp_path / "does_not_exist")
        results = list_incomplete_checkpoints()
        assert results == []

    def test_skips_malformed_files(self, tmp_path, monkeypatch):
        """Malformed files in the directory are skipped without crashing."""
        import utils.task_checkpoint as mod

        cp_dir = mod.CHECKPOINT_DIR
        cp_dir.mkdir(parents=True, exist_ok=True)

        # One good checkpoint
        save_checkpoint(_make_checkpoint("task_good"))

        # One malformed file
        (cp_dir / "task_bad.json").write_text("NOT JSON", encoding="utf-8")

        results = list_incomplete_checkpoints()
        assert len(results) == 1
        assert results[0].task_id == "task_good"


class TestRetryCount:
    def test_increment_retry(self):
        """increment_retry bumps retry_count by 1."""
        save_checkpoint(_make_checkpoint("task_retry", retry_count=0))

        result = increment_retry("task_retry")
        assert result is not None
        assert result.retry_count == 1

        # Verify persisted
        loaded = load_checkpoint("task_retry")
        assert loaded is not None
        assert loaded.retry_count == 1

    def test_increment_retry_nonexistent(self):
        """increment_retry returns None for nonexistent task."""
        result = increment_retry("no_such_task")
        assert result is None

    def test_multiple_increments(self):
        """Multiple increments accumulate correctly."""
        save_checkpoint(_make_checkpoint("task_multi", retry_count=0))

        increment_retry("task_multi")
        increment_retry("task_multi")
        result = increment_retry("task_multi")

        assert result is not None
        assert result.retry_count == 3


class TestUpdateStatus:
    def test_update_status(self):
        """update_status changes the status field."""
        save_checkpoint(_make_checkpoint("task_update", status="dispatched"))

        update_status("task_update", "in_progress")
        loaded = load_checkpoint("task_update")
        assert loaded is not None
        assert loaded.status == "in_progress"

    def test_update_status_with_partial_output(self):
        """update_status can set partial_output."""
        save_checkpoint(_make_checkpoint("task_partial", status="dispatched"))

        update_status("task_partial", "partial_output", partial_output="some output so far")
        loaded = load_checkpoint("task_partial")
        assert loaded is not None
        assert loaded.status == "partial_output"
        assert loaded.partial_output == "some output so far"

    def test_update_nonexistent_returns_none(self):
        """update_status returns None when no checkpoint exists."""
        result = update_status("no_such_task", "in_progress")
        assert result is None


class TestThreadSafety:
    def test_concurrent_saves(self):
        """Concurrent save operations from multiple threads don't corrupt data."""
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def _save_worker(idx: int):
            try:
                barrier.wait(timeout=5)
                cp = _make_checkpoint(f"concurrent_{idx}", status="in_progress")
                save_checkpoint(cp)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_save_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"

        # Verify all checkpoints were written correctly
        results = list_incomplete_checkpoints()
        assert len(results) == 10
        task_ids = {cp.task_id for cp in results}
        assert task_ids == {f"concurrent_{i}" for i in range(10)}

    def test_concurrent_save_and_clear(self):
        """Concurrent save + clear operations don't raise exceptions."""
        errors: list[Exception] = []

        # Pre-create some checkpoints
        for i in range(5):
            save_checkpoint(_make_checkpoint(f"race_{i}"))

        barrier = threading.Barrier(10)

        def _worker(idx: int):
            try:
                barrier.wait(timeout=5)
                if idx % 2 == 0:
                    save_checkpoint(_make_checkpoint(f"race_{idx}", status="in_progress"))
                else:
                    clear_checkpoint(f"race_{idx}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
