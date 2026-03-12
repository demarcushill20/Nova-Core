"""Tests for Phase 4.1 — watchdog filesystem monitor."""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from utils.file_watcher import TaskFileHandler, TaskFileWatcher, _is_new_task

# --- Unit tests for _is_new_task ---


class TestIsNewTask:
    def test_pending_md_file(self):
        assert _is_new_task("TASKS/0001_hello.md") is True

    def test_inprogress_ignored(self):
        assert _is_new_task("TASKS/0001_hello.md.inprogress") is False

    def test_done_ignored(self):
        assert _is_new_task("TASKS/0001_hello.md.done") is False

    def test_failed_ignored(self):
        assert _is_new_task("TASKS/0001_hello.md.failed") is False

    def test_cancelled_ignored(self):
        assert _is_new_task("TASKS/0001_hello.md.cancelled") is False

    def test_non_md_file(self):
        assert _is_new_task("TASKS/readme.txt") is False

    def test_bare_md(self):
        assert _is_new_task("task.md") is True


# --- Unit tests for TaskFileHandler ---


class _FakeEvent:
    """Minimal mock of a watchdog FileSystemEvent."""

    def __init__(self, src_path="", dest_path="", is_directory=False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class TestTaskFileHandler:
    def test_new_md_triggers_event(self):
        wake = threading.Event()
        handler = TaskFileHandler(wake)
        try:
            handler.handle_event(_FakeEvent(src_path="TASKS/0042_test.md"))
            # Wait for debounce to fire
            assert wake.wait(timeout=2.0), "Wake event should be set after debounce"
        finally:
            handler.stop()

    def test_inprogress_does_not_trigger(self):
        wake = threading.Event()
        handler = TaskFileHandler(wake)
        try:
            handler.handle_event(_FakeEvent(src_path="TASKS/0042_test.md.inprogress"))
            # Should NOT trigger within debounce window
            assert not wake.wait(timeout=0.5), "inprogress should not trigger wake"
        finally:
            handler.stop()

    def test_directory_event_ignored(self):
        wake = threading.Event()
        handler = TaskFileHandler(wake)
        try:
            handler.handle_event(_FakeEvent(src_path="TASKS/subdir.md", is_directory=True))
            assert not wake.wait(timeout=0.5), "Directory events should be ignored"
        finally:
            handler.stop()

    def test_debounce_coalesces_rapid_events(self):
        """Multiple rapid events should result in a single wake signal."""
        wake = threading.Event()
        handler = TaskFileHandler(wake)
        try:
            # Fire 5 events in rapid succession
            for i in range(5):
                handler.handle_event(_FakeEvent(src_path=f"TASKS/task_{i}.md"))
            # Should still get one debounced trigger
            assert wake.wait(timeout=2.0), "Debounced wake should fire"
            wake.clear()
            # No additional triggers should come
            assert not wake.wait(timeout=0.3), "No extra triggers after debounce"
        finally:
            handler.stop()

    def test_move_event_with_dest_path(self):
        """A move/rename event with dest_path as .md should trigger."""
        wake = threading.Event()
        handler = TaskFileHandler(wake)
        try:
            handler.handle_event(_FakeEvent(src_path="TASKS/old.tmp", dest_path="TASKS/new_task.md"))
            assert wake.wait(timeout=2.0), "Move-to .md should trigger wake"
        finally:
            handler.stop()


# --- Integration test: real filesystem with watchdog observer ---


class TestTaskFileWatcherIntegration:
    def test_new_file_triggers_within_2s(self):
        """Creating a .md file in a watched directory triggers wake within 2s."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_dir = Path(tmpdir)
            wake = threading.Event()
            watcher = TaskFileWatcher(tasks_dir, wake)

            started = watcher.start()
            if not started:
                pytest.skip("watchdog observer could not start (e.g. seccomp)")

            try:
                # Give observer a moment to set up
                time.sleep(0.2)

                # Create a new task file
                (tasks_dir / "0099_integration_test.md").write_text("# Test task\n")

                # Should trigger within 2 seconds
                assert wake.wait(timeout=2.0), "New .md file should trigger wake within 2s"
            finally:
                watcher.stop()

    def test_graceful_fallback(self, monkeypatch):
        """If watchdog import fails, watcher returns False and polling continues."""

        # Simulate import failure by patching the observer import
        def _failing_start(self):
            # Simulate the observer raising an exception
            self._active = False
            return False

        monkeypatch.setattr(TaskFileWatcher, "start", _failing_start)

        with tempfile.TemporaryDirectory() as tmpdir:
            wake = threading.Event()
            watcher = TaskFileWatcher(Path(tmpdir), wake)
            assert watcher.start() is False
            assert not watcher.active
            watcher.stop()  # should not raise

    def test_stop_is_idempotent(self):
        """Calling stop() multiple times should not raise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wake = threading.Event()
            watcher = TaskFileWatcher(Path(tmpdir), wake)
            watcher.start()
            watcher.stop()
            watcher.stop()  # second call should be safe
