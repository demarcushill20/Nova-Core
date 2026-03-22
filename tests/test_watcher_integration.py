"""Integration tests for the watcher → dispatch → contract → lifecycle pipeline.

Tests cross-module interactions end-to-end with realistic scenarios.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestDispatchContractLifecycle(unittest.TestCase):
    """Test the watcher dispatch → contract validation → lifecycle transitions."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._tasks = self._tmpdir / "TASKS"
        self._output = self._tmpdir / "OUTPUT"
        self._logs = self._tmpdir / "LOGS"
        self._state = self._tmpdir / "STATE"
        self._cancel = self._state / "cancel"
        for d in [self._tasks, self._output, self._logs, self._state, self._cancel]:
            d.mkdir(parents=True, exist_ok=True)

        # Patch watcher module-level dirs
        import watcher

        self._orig_tasks = watcher.TASKS_DIR
        self._orig_output = watcher.OUTPUT_DIR
        self._orig_logs = watcher.LOGS_DIR
        self._orig_state = watcher.STATE_DIR
        self._orig_cancel = watcher.CANCEL_DIR
        watcher.TASKS_DIR = self._tasks
        watcher.OUTPUT_DIR = self._output
        watcher.LOGS_DIR = self._logs
        watcher.STATE_DIR = self._state
        watcher.CANCEL_DIR = self._cancel

    def tearDown(self):
        import watcher

        watcher.TASKS_DIR = self._orig_tasks
        watcher.OUTPUT_DIR = self._orig_output
        watcher.LOGS_DIR = self._orig_logs
        watcher.STATE_DIR = self._orig_state
        watcher.CANCEL_DIR = self._orig_cancel
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_get_pending_filters_non_md(self):
        """get_pending_tasks only returns .md files."""
        from watcher import get_pending_tasks

        (self._tasks / "0001_test.md").write_text("# Task 1\n")
        (self._tasks / "0001_test.md.done").write_text("done\n")
        (self._tasks / "0002_test.md.inprogress").write_text("wip\n")
        (self._tasks / "0003_test.md.failed").write_text("fail\n")

        result = get_pending_tasks()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "0001_test.md")

    def test_get_pending_empty_dir(self):
        """get_pending_tasks returns empty list for empty directory."""
        from watcher import get_pending_tasks

        result = get_pending_tasks()
        self.assertEqual(result, [])

    def test_get_pending_missing_dir(self):
        """get_pending_tasks returns empty list when TASKS_DIR doesn't exist."""
        import watcher

        watcher.TASKS_DIR = self._tmpdir / "nonexistent"
        from watcher import get_pending_tasks

        result = get_pending_tasks()
        self.assertEqual(result, [])

    def test_get_pending_respects_scheduled_at(self):
        """get_pending_tasks skips tasks scheduled for the future."""
        from watcher import get_pending_tasks

        # Past scheduled — should be included
        (self._tasks / "0001_past.md").write_text("---\nscheduled_at: 2020-01-01T00:00:00\n---\n# Past task\n")
        # Future scheduled — should be skipped
        (self._tasks / "0002_future.md").write_text("---\nscheduled_at: 2099-12-31T23:59:59\n---\n# Future task\n")
        # No frontmatter — should be included
        (self._tasks / "0003_none.md").write_text("# No frontmatter\n")

        result = get_pending_tasks()
        names = [p.name for p in result]
        self.assertIn("0001_past.md", names)
        self.assertIn("0003_none.md", names)
        self.assertNotIn("0002_future.md", names)

    def test_contract_check_valid(self):
        """_check_contract returns True for valid contract."""
        from watcher import _check_contract

        output_file = self._output / "test_output.md"
        output_file.write_text(
            "# Output\n\n"
            "## CONTRACT\n"
            "summary: did the thing\n"
            "files_changed: foo.py\n"
            "verification: ran tests\n"
            "confidence: high\n"
        )

        ok, messages = _check_contract(output_file)
        self.assertTrue(ok)
        self.assertTrue(any("validated" in m.lower() for m in messages))

    def test_contract_check_invalid_appends_failure(self):
        """_check_contract appends failure section for invalid contract."""
        from watcher import _check_contract

        output_file = self._output / "test_output.md"
        output_file.write_text("# Output\nNo contract here\n")

        ok, messages = _check_contract(output_file)
        self.assertFalse(ok)
        self.assertTrue(any("FAILED" in m for m in messages))

        # Check failure section was appended
        text = output_file.read_text()
        self.assertIn("CONTRACT VALIDATION FAILED", text)
        self.assertIn("Suggestion:", text)

    def test_quick_contract_check_no_output(self):
        """_quick_contract_check returns False when no output file exists."""
        from watcher import _quick_contract_check

        ok, errors = _quick_contract_check("nonexistent_task")
        self.assertFalse(ok)
        self.assertTrue(len(errors) > 0)

    def test_find_recent_output_within_window(self):
        """_find_recent_output finds files within ARTIFACT_WINDOW."""
        import time

        from watcher import _find_recent_output

        out = self._output / "0001_test__20260318-120000.md"
        out.write_text("# Output\n")
        # Touch to ensure recent mtime
        os.utime(out, (time.time(), time.time()))

        result = _find_recent_output("0001_test")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, out.name)

    def test_find_recent_output_stale(self):
        """_find_recent_output returns None for stale files."""
        import time

        from watcher import ARTIFACT_WINDOW, _find_recent_output

        out = self._output / "0001_test__20200101-000000.md"
        out.write_text("# Output\n")
        # Set mtime to far in the past
        old_time = time.time() - (ARTIFACT_WINDOW + 3600)
        os.utime(out, (old_time, old_time))

        result = _find_recent_output("0001_test")
        self.assertIsNone(result)

    def test_find_recent_output_no_dir(self):
        """_find_recent_output returns None when OUTPUT_DIR doesn't exist."""
        import watcher

        watcher.OUTPUT_DIR = self._tmpdir / "nonexistent_output"
        from watcher import _find_recent_output

        result = _find_recent_output("any_task")
        self.assertIsNone(result)

    def test_find_recent_output_no_matches(self):
        """_find_recent_output returns None when no matching files."""
        from watcher import _find_recent_output

        (self._output / "other_task.md").write_text("# Other\n")
        result = _find_recent_output("0001_test")
        self.assertIsNone(result)

    def test_cancel_detection_flow(self):
        """_dispatch_inner detects cancel marker and writes cancellation report."""
        from watcher import _dispatch_inner

        stem = "0099_cancel_test"
        task = self._tasks / f"{stem}.md"
        task.write_text("# Cancel me\n")

        # Create cancel marker
        (self._cancel / f"{stem}.cancel").write_text("")

        # Mock kill switch, budget, checkpoint, and audit
        with (
            patch("watcher.check_kill_switch", return_value="run"),
            patch("watcher.MODE_RUN", "run"),
            patch("watcher.save_checkpoint"),
            patch("watcher._cp_now_iso", return_value="2026-01-01T00:00:00Z"),
            patch("watcher.clear_checkpoint"),
            patch("watcher._audit"),
            patch("watcher.slog"),
            patch("watcher.TraceContext"),
            patch("watcher.validate_task_content", return_value={"blocked": False, "risk_score": 0, "risks": []}),
            patch("watcher.dlp") as mock_dlp,
        ):
            mock_dlp.scan.return_value = MagicMock(action="pass", findings=[], redacted_text="")
            asyncio.run(_dispatch_inner(task))

        # Task should be renamed to .cancelled
        self.assertTrue((self._tasks / f"{stem}.md.cancelled").exists())
        # Cancel report should exist in OUTPUT
        reports = list(self._output.glob(f"{stem}__*.md"))
        self.assertEqual(len(reports), 1)
        content = reports[0].read_text()
        self.assertIn("Cancelled", content)
        # Cancel marker should be cleaned up
        self.assertFalse((self._cancel / f"{stem}.cancel").exists())


class TestTaskStemHelpers(unittest.TestCase):
    """Test _task_stem and _is_retry_task helpers."""

    def test_task_stem_md(self):
        from watcher import _task_stem

        self.assertEqual(_task_stem("0004_foo.md"), "0004_foo")

    def test_task_stem_inprogress(self):
        from watcher import _task_stem

        self.assertEqual(_task_stem("0004_foo.md.inprogress"), "0004_foo")

    def test_task_stem_plain(self):
        from watcher import _task_stem

        self.assertEqual(_task_stem("0004_foo"), "0004_foo")

    def test_is_retry_task(self):
        from watcher import _is_retry_task

        self.assertTrue(_is_retry_task("0004_foo__retry1"))
        self.assertFalse(_is_retry_task("0004_foo"))

    def test_original_stem(self):
        from watcher import _original_stem

        self.assertEqual(_original_stem("0004_foo__retry1"), "0004_foo")
        self.assertEqual(_original_stem("0004_foo"), "0004_foo")


class TestVerifyArtifacts(unittest.TestCase):
    """Test verify_artifacts end-to-end."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._output = self._tmpdir / "OUTPUT"
        self._output.mkdir()

        import watcher

        self._orig_output = watcher.OUTPUT_DIR
        watcher.OUTPUT_DIR = self._output

    def tearDown(self):
        import watcher

        watcher.OUTPUT_DIR = self._orig_output
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_verify_no_output_file(self):
        """verify_artifacts returns False when no output file exists."""
        from watcher import verify_artifacts

        passed, messages = verify_artifacts("nonexistent_stem")
        self.assertFalse(passed)
        self.assertTrue(any("not found" in m.lower() or "no recent" in m.lower() for m in messages))

    def test_verify_valid_contract(self):
        """verify_artifacts passes with valid contract."""
        import time

        from watcher import verify_artifacts

        out = self._output / "0001_test__20260318-120000.md"
        out.write_text(
            "# Output\n\n"
            "## CONTRACT\n"
            "summary: did the thing\n"
            "files_changed: foo.py\n"
            "verification: ran tests\n"
            "confidence: high\n"
        )
        os.utime(out, (time.time(), time.time()))

        passed, messages = verify_artifacts("0001_test")
        self.assertTrue(passed)


class TestParseScheduledAt(unittest.TestCase):
    """Test _check_scheduled with various input types."""

    def test_none_is_ready(self):
        from watcher import _check_scheduled

        self.assertTrue(_check_scheduled(None))

    def test_past_datetime_is_ready(self):
        from watcher import _check_scheduled

        self.assertTrue(_check_scheduled(datetime(2020, 1, 1)))

    def test_future_datetime_not_ready(self):
        from watcher import _check_scheduled

        self.assertFalse(_check_scheduled(datetime(2099, 12, 31)))

    def test_past_string_is_ready(self):
        from watcher import _check_scheduled

        self.assertTrue(_check_scheduled("2020-01-01T00:00:00"))

    def test_future_string_not_ready(self):
        from watcher import _check_scheduled

        self.assertFalse(_check_scheduled("2099-12-31T23:59:59"))

    def test_invalid_string_is_ready(self):
        from watcher import _check_scheduled

        self.assertTrue(_check_scheduled("not-a-date"))

    def test_unexpected_type_is_ready(self):
        from watcher import _check_scheduled

        self.assertTrue(_check_scheduled(42))
        self.assertTrue(_check_scheduled([]))

    def test_bare_date_past(self):
        """yaml.safe_load produces date objects for bare dates."""
        from datetime import date

        from watcher import _check_scheduled

        self.assertTrue(_check_scheduled(date(2020, 1, 1)))

    def test_bare_date_future(self):
        from datetime import date

        from watcher import _check_scheduled

        self.assertFalse(_check_scheduled(date(2099, 12, 31)))


class TestParseFrontmatter(unittest.TestCase):
    """Test parse_frontmatter with various file contents."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_valid_frontmatter(self):
        from watcher import parse_frontmatter

        f = self._tmpdir / "task.md"
        f.write_text("---\nscheduled_at: 2026-01-01\npriority: high\n---\n# Task\n")
        result = parse_frontmatter(f)
        self.assertEqual(result.get("priority"), "high")

    def test_no_frontmatter(self):
        from watcher import parse_frontmatter

        f = self._tmpdir / "task.md"
        f.write_text("# Task with no frontmatter\n")
        result = parse_frontmatter(f)
        self.assertEqual(result, {})

    def test_malformed_yaml(self):
        from watcher import parse_frontmatter

        f = self._tmpdir / "task.md"
        f.write_text("---\n: : invalid yaml\n---\n")
        result = parse_frontmatter(f)
        self.assertEqual(result, {})

    def test_missing_closing_delimiter(self):
        from watcher import parse_frontmatter

        f = self._tmpdir / "task.md"
        f.write_text("---\npriority: high\n")
        result = parse_frontmatter(f)
        self.assertEqual(result, {})

    def test_empty_frontmatter(self):
        from watcher import parse_frontmatter

        f = self._tmpdir / "task.md"
        f.write_text("---\n---\n# Task\n")
        result = parse_frontmatter(f)
        self.assertEqual(result, {})

    def test_nonexistent_file(self):
        from watcher import parse_frontmatter

        f = self._tmpdir / "nonexistent.md"
        result = parse_frontmatter(f)
        self.assertEqual(result, {})


class TestKillSwitchIntegration(unittest.TestCase):
    """Test kill switch check at the start of _dispatch_inner."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._tasks = self._tmpdir / "TASKS"
        self._tasks.mkdir()

        import watcher

        self._orig_tasks = watcher.TASKS_DIR
        watcher.TASKS_DIR = self._tasks

    def tearDown(self):
        import watcher

        watcher.TASKS_DIR = self._orig_tasks
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_kill_switch_blocks_dispatch(self):
        """When kill switch is not 'run', dispatch should skip the task and defer it."""
        from watcher import _deferred, _dispatch_inner, _dispatched

        task = self._tasks / "0001_blocked.md"
        task.write_text("# Blocked task\n")
        _dispatched.add("0001_blocked.md")
        _deferred.pop("0001_blocked.md", None)

        with patch("watcher.check_kill_switch", return_value="pause"):
            asyncio.run(_dispatch_inner(task))

        # Task should still exist (not renamed)
        self.assertTrue(task.exists())
        # Should be deferred so it won't be re-dispatched until cooldown expires
        self.assertIn("0001_blocked.md", _deferred)
        # Cleanup
        _deferred.pop("0001_blocked.md", None)
        _dispatched.discard("0001_blocked.md")


if __name__ == "__main__":
    unittest.main()
