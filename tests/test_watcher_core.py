"""Tests for critical watcher.py helper functions.

Covers: _task_stem, _find_recent_output, parse_frontmatter, _check_scheduled,
get_pending_tasks, _check_contract, _quick_contract_check, _is_retry_task.
"""

import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Patch heavy imports before importing watcher so module-level side-effects
# (logging setup, service connections, etc.) do not interfere with tests.
# We only need the pure helper functions; everything else can be stubbed.
# ---------------------------------------------------------------------------
import watcher
from watcher import (
    _check_contract,
    _check_scheduled,
    _find_recent_output,
    _is_retry_task,
    _quick_contract_check,
    _task_stem,
    get_pending_tasks,
    parse_frontmatter,
)


class TestTaskStem(unittest.TestCase):
    """Tests for _task_stem: strip .md and .inprogress suffixes."""

    def test_plain_md(self):
        self.assertEqual(_task_stem("0004_foo.md"), "0004_foo")

    def test_inprogress(self):
        self.assertEqual(_task_stem("0004_foo.md.inprogress"), "0004_foo")

    def test_no_suffix(self):
        self.assertEqual(_task_stem("0004_foo"), "0004_foo")

    def test_double_md(self):
        # Only strips once from the right for each suffix
        self.assertEqual(_task_stem("task.md.md"), "task.md")

    def test_only_inprogress_suffix(self):
        # .inprogress stripped first, then .md would be checked
        self.assertEqual(_task_stem("task.inprogress"), "task")

    def test_empty_string(self):
        self.assertEqual(_task_stem(""), "")

    def test_complex_stem(self):
        self.assertEqual(_task_stem("0012_long_task_name__retry1.md"), "0012_long_task_name__retry1")


class TestIsRetryTask(unittest.TestCase):
    """Tests for _is_retry_task: checks for __retry1 in stem."""

    def test_retry_task(self):
        self.assertTrue(_is_retry_task("0012_broken__retry1"))

    def test_not_retry_task(self):
        self.assertFalse(_is_retry_task("0012_broken"))

    def test_retry_in_middle(self):
        self.assertTrue(_is_retry_task("0012__retry1_extra"))

    def test_empty(self):
        self.assertFalse(_is_retry_task(""))

    def test_partial_match(self):
        self.assertFalse(_is_retry_task("0012__retry"))

    def test_retry2_not_matched(self):
        # Only __retry1 is checked
        self.assertFalse(_is_retry_task("0012__retry2"))


class TestParseFrontmatter(unittest.TestCase):
    """Tests for parse_frontmatter: YAML frontmatter extraction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name, content):
        p = Path(self.tmpdir) / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_valid_frontmatter(self):
        p = self._write(
            "task.md",
            "---\ntitle: Test Task\npriority: high\n---\n\nBody content here.",
        )
        result = parse_frontmatter(p)
        self.assertEqual(result["title"], "Test Task")
        self.assertEqual(result["priority"], "high")

    def test_no_frontmatter(self):
        p = self._write("task.md", "No frontmatter at all.\nJust body.")
        self.assertEqual(parse_frontmatter(p), {})

    def test_empty_file(self):
        p = self._write("task.md", "")
        self.assertEqual(parse_frontmatter(p), {})

    def test_only_opening_fence(self):
        p = self._write("task.md", "---\ntitle: Test\n")
        # Only one --- → split produces < 3 parts
        self.assertEqual(parse_frontmatter(p), {})

    def test_malformed_yaml(self):
        p = self._write("task.md", "---\n: invalid: [yaml:: broken\n---\nBody.")
        self.assertEqual(parse_frontmatter(p), {})

    def test_empty_frontmatter_block(self):
        p = self._write("task.md", "---\n---\nBody.")
        # yaml.safe_load("") returns None → function returns {}
        self.assertEqual(parse_frontmatter(p), {})

    def test_frontmatter_with_scheduled_at(self):
        p = self._write(
            "task.md",
            "---\nscheduled_at: 2026-04-01T10:00:00\n---\nBody.",
        )
        result = parse_frontmatter(p)
        # yaml.safe_load parses ISO datetimes as datetime objects
        self.assertIn("scheduled_at", result)

    def test_nonexistent_file(self):
        p = Path(self.tmpdir) / "nonexistent.md"
        self.assertEqual(parse_frontmatter(p), {})

    def test_frontmatter_with_special_characters(self):
        p = self._write(
            "task.md",
            '---\ntitle: "Task: with [special] chars"\n---\nBody.',
        )
        result = parse_frontmatter(p)
        self.assertEqual(result["title"], "Task: with [special] chars")


class TestCheckScheduled(unittest.TestCase):
    """Tests for _check_scheduled: scheduling gate logic."""

    def test_none_is_ready(self):
        self.assertTrue(_check_scheduled(None))

    def test_past_datetime_is_ready(self):
        past = datetime.now() - timedelta(hours=1)
        self.assertTrue(_check_scheduled(past))

    def test_future_datetime_is_not_ready(self):
        future = datetime.now() + timedelta(hours=1)
        self.assertFalse(_check_scheduled(future))

    def test_past_date_object_is_ready(self):
        past_date = date.today() - timedelta(days=1)
        self.assertTrue(_check_scheduled(past_date))

    def test_future_date_object_is_not_ready(self):
        future_date = date.today() + timedelta(days=2)
        self.assertFalse(_check_scheduled(future_date))

    def test_today_date_is_ready(self):
        # date(today) is converted to datetime(today, 00:00) which is <= now
        self.assertTrue(_check_scheduled(date.today()))

    def test_past_iso_string_is_ready(self):
        past = (datetime.now() - timedelta(hours=2)).isoformat()
        self.assertTrue(_check_scheduled(past))

    def test_future_iso_string_is_not_ready(self):
        future = (datetime.now() + timedelta(hours=2)).isoformat()
        self.assertFalse(_check_scheduled(future))

    def test_invalid_string_is_ready(self):
        # Unparseable → ready immediately
        self.assertTrue(_check_scheduled("not-a-date"))

    def test_unexpected_type_is_ready(self):
        # Unexpected type (int, list, etc.) → ready immediately
        self.assertTrue(_check_scheduled(12345))
        self.assertTrue(_check_scheduled([1, 2, 3]))
        self.assertTrue(_check_scheduled({"key": "value"}))

    def test_timezone_aware_past(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        self.assertTrue(_check_scheduled(past))

    def test_timezone_aware_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertFalse(_check_scheduled(future))

    def test_iso_string_with_timezone(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(_check_scheduled(past))

    def test_empty_string_is_ready(self):
        # Empty string is unparseable → ready immediately
        self.assertTrue(_check_scheduled(""))


class TestFindRecentOutput(unittest.TestCase):
    """Tests for _find_recent_output: locating recent OUTPUT artifacts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_dir = Path(self.tmpdir) / "OUTPUT"

    def test_no_output_dir(self):
        """When OUTPUT directory doesn't exist, returns None."""
        fake_dir = Path(self.tmpdir) / "nonexistent_OUTPUT"
        with patch.object(watcher, "OUTPUT_DIR", fake_dir):
            self.assertIsNone(_find_recent_output("0001_test"))

    def test_output_dir_empty(self):
        """When OUTPUT directory exists but is empty, returns None."""
        self.output_dir.mkdir(parents=True)
        with patch.object(watcher, "OUTPUT_DIR", self.output_dir):
            self.assertIsNone(_find_recent_output("0001_test"))

    def test_no_matching_files(self):
        """When OUTPUT has files but none match the stem, returns None."""
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "0099_other.md").write_text("other")
        with patch.object(watcher, "OUTPUT_DIR", self.output_dir):
            self.assertIsNone(_find_recent_output("0001_test"))

    def test_recent_matching_file(self):
        """When a matching file was recently modified, returns it."""
        self.output_dir.mkdir(parents=True)
        f = self.output_dir / "0001_test_output.md"
        f.write_text("output content")
        with (
            patch.object(watcher, "OUTPUT_DIR", self.output_dir),
            patch.object(watcher, "ARTIFACT_WINDOW", 600),
        ):
            result = _find_recent_output("0001_test")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "0001_test_output.md")

    def test_stale_matching_file(self):
        """When the matching file is older than ARTIFACT_WINDOW, returns None."""
        self.output_dir.mkdir(parents=True)
        f = self.output_dir / "0001_test_output.md"
        f.write_text("output content")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(f, (old_time, old_time))
        with (
            patch.object(watcher, "OUTPUT_DIR", self.output_dir),
            patch.object(watcher, "ARTIFACT_WINDOW", 600),
        ):
            self.assertIsNone(_find_recent_output("0001_test"))

    def test_newest_of_multiple_matches(self):
        """When multiple files match, returns the newest one."""
        self.output_dir.mkdir(parents=True)
        f1 = self.output_dir / "0001_test_v1.md"
        f1.write_text("first")
        # Make f1 slightly older
        old_time = time.time() - 10
        os.utime(f1, (old_time, old_time))

        f2 = self.output_dir / "0001_test_v2.md"
        f2.write_text("second")

        with (
            patch.object(watcher, "OUTPUT_DIR", self.output_dir),
            patch.object(watcher, "ARTIFACT_WINDOW", 600),
        ):
            result = _find_recent_output("0001_test")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "0001_test_v2.md")

    def test_non_md_files_ignored(self):
        """Non-.md files should not be matched."""
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "0001_test.txt").write_text("text file")
        (self.output_dir / "0001_test.json").write_text("{}")
        with (
            patch.object(watcher, "OUTPUT_DIR", self.output_dir),
            patch.object(watcher, "ARTIFACT_WINDOW", 600),
        ):
            self.assertIsNone(_find_recent_output("0001_test"))

    def test_large_artifact_window(self):
        """With a very large window, even old files are found."""
        self.output_dir.mkdir(parents=True)
        f = self.output_dir / "0001_test_output.md"
        f.write_text("output content")
        old_time = time.time() - 3600  # 1 hour old
        os.utime(f, (old_time, old_time))
        with (
            patch.object(watcher, "OUTPUT_DIR", self.output_dir),
            patch.object(watcher, "ARTIFACT_WINDOW", 86400),  # 24 hours
        ):
            result = _find_recent_output("0001_test")
            self.assertIsNotNone(result)


class TestGetPendingTasks(unittest.TestCase):
    """Tests for get_pending_tasks: filtering and sorting task files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_dir = Path(self.tmpdir) / "TASKS"

    def test_no_tasks_dir(self):
        """When TASKS directory doesn't exist, returns empty list."""
        fake_dir = Path(self.tmpdir) / "nonexistent_TASKS"
        with patch.object(watcher, "TASKS_DIR", fake_dir):
            self.assertEqual(get_pending_tasks(), [])

    def test_empty_tasks_dir(self):
        """When TASKS directory is empty, returns empty list."""
        self.tasks_dir.mkdir(parents=True)
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            self.assertEqual(get_pending_tasks(), [])

    def test_non_md_files_excluded(self):
        """Files without .md suffix are ignored."""
        self.tasks_dir.mkdir(parents=True)
        (self.tasks_dir / "task.txt").write_text("not a task")
        (self.tasks_dir / "task.json").write_text("{}")
        (self.tasks_dir / "task.md.done").write_text("done")
        (self.tasks_dir / "task.md.inprogress").write_text("in progress")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            self.assertEqual(get_pending_tasks(), [])

    def test_md_files_returned(self):
        """Valid .md files are returned."""
        self.tasks_dir.mkdir(parents=True)
        (self.tasks_dir / "0001_task.md").write_text("Do something")
        (self.tasks_dir / "0002_task.md").write_text("Do more")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            result = get_pending_tasks()
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].name, "0001_task.md")
            self.assertEqual(result[1].name, "0002_task.md")

    def test_sorted_order(self):
        """Tasks are returned in sorted order."""
        self.tasks_dir.mkdir(parents=True)
        (self.tasks_dir / "0003_c.md").write_text("c")
        (self.tasks_dir / "0001_a.md").write_text("a")
        (self.tasks_dir / "0002_b.md").write_text("b")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            result = get_pending_tasks()
            names = [p.name for p in result]
            self.assertEqual(names, ["0001_a.md", "0002_b.md", "0003_c.md"])

    def test_future_scheduled_excluded(self):
        """Tasks with future scheduled_at are deferred."""
        self.tasks_dir.mkdir(parents=True)
        future = (datetime.now() + timedelta(hours=2)).isoformat()
        (self.tasks_dir / "0001_future.md").write_text(f"---\nscheduled_at: '{future}'\n---\nFuture task")
        (self.tasks_dir / "0002_ready.md").write_text("Ready task, no frontmatter")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            result = get_pending_tasks()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "0002_ready.md")

    def test_past_scheduled_included(self):
        """Tasks with past scheduled_at are ready."""
        self.tasks_dir.mkdir(parents=True)
        past = (datetime.now() - timedelta(hours=2)).isoformat()
        (self.tasks_dir / "0001_past.md").write_text(f"---\nscheduled_at: '{past}'\n---\nPast task")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            result = get_pending_tasks()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "0001_past.md")

    def test_no_frontmatter_included(self):
        """Tasks with no frontmatter are always ready."""
        self.tasks_dir.mkdir(parents=True)
        (self.tasks_dir / "0001_plain.md").write_text("Just a plain task file.")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            result = get_pending_tasks()
            self.assertEqual(len(result), 1)

    def test_mixed_ready_and_deferred(self):
        """Only ready tasks are returned from a mix."""
        self.tasks_dir.mkdir(parents=True)
        future = (datetime.now() + timedelta(days=1)).isoformat()
        past = (datetime.now() - timedelta(days=1)).isoformat()
        (self.tasks_dir / "0001_deferred.md").write_text(f"---\nscheduled_at: '{future}'\n---\nDeferred")
        (self.tasks_dir / "0002_ready.md").write_text(f"---\nscheduled_at: '{past}'\n---\nReady")
        (self.tasks_dir / "0003_plain.md").write_text("Plain task")
        with patch.object(watcher, "TASKS_DIR", self.tasks_dir):
            result = get_pending_tasks()
            names = [p.name for p in result]
            self.assertIn("0002_ready.md", names)
            self.assertIn("0003_plain.md", names)
            self.assertNotIn("0001_deferred.md", names)
            self.assertEqual(len(result), 2)


class TestCheckContract(unittest.TestCase):
    """Tests for _check_contract: output file contract validation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_output(self, name, content):
        p = Path(self.tmpdir) / name
        p.write_text(content, encoding="utf-8")
        return p

    @patch("watcher.validate_contract")
    def test_valid_contract(self, mock_validate):
        """Valid contract returns (True, messages) without modifying file."""
        mock_validate.return_value = {
            "valid": True,
            "errors": [],
            "warnings": [],
        }
        f = self._write_output("output.md", "# Output\n\n## CONTRACT\nsummary: done\n")
        original_content = f.read_text()
        ok, msgs = _check_contract(f)
        self.assertTrue(ok)
        self.assertTrue(any("validated" in m for m in msgs))
        # File should not be modified
        self.assertEqual(f.read_text(), original_content)

    @patch("watcher.validate_contract")
    def test_invalid_contract_appends_failure(self, mock_validate):
        """Invalid contract appends failure section and returns (False, messages)."""
        mock_validate.return_value = {
            "valid": False,
            "errors": ["missing summary", "missing confidence"],
            "warnings": ["low quality"],
        }
        f = self._write_output("output.md", "# Output\n\nSome content.\n")
        ok, msgs = _check_contract(f)
        self.assertFalse(ok)
        self.assertTrue(any("CONTRACT FAILED" in m for m in msgs))
        self.assertTrue(any("missing summary" in m for m in msgs))
        self.assertTrue(any("low quality" in m for m in msgs))
        # File should have failure section appended
        text = f.read_text()
        self.assertIn("CONTRACT VALIDATION FAILED", text)
        self.assertIn("missing summary", text)
        self.assertIn("missing confidence", text)
        self.assertIn("low quality", text)
        self.assertIn("Suggestion", text)

    @patch("watcher.validate_contract")
    def test_invalid_contract_no_warnings(self, mock_validate):
        """Invalid contract with errors but no warnings."""
        mock_validate.return_value = {
            "valid": False,
            "errors": ["missing summary"],
            "warnings": [],
        }
        f = self._write_output("output.md", "# Output\n")
        ok, msgs = _check_contract(f)
        self.assertFalse(ok)
        text = f.read_text()
        self.assertIn("missing summary", text)
        # No warnings section header when warnings list is empty
        self.assertNotIn("**Warnings:**", text)

    @patch("watcher.validate_contract")
    def test_invalid_contract_multiple_errors(self, mock_validate):
        """All errors are listed in the appended failure section."""
        mock_validate.return_value = {
            "valid": False,
            "errors": ["err1", "err2", "err3"],
            "warnings": ["warn1"],
        }
        f = self._write_output("output.md", "content")
        ok, msgs = _check_contract(f)
        self.assertFalse(ok)
        text = f.read_text()
        self.assertIn("- err1", text)
        self.assertIn("- err2", text)
        self.assertIn("- err3", text)
        self.assertIn("- warn1", text)
        # Messages list includes all errors and warnings
        error_msgs = [m for m in msgs if "contract error" in m]
        warning_msgs = [m for m in msgs if "contract warning" in m]
        self.assertEqual(len(error_msgs), 3)
        self.assertEqual(len(warning_msgs), 1)


class TestQuickContractCheck(unittest.TestCase):
    """Tests for _quick_contract_check: non-destructive contract validation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_dir = Path(self.tmpdir) / "OUTPUT"
        self.output_dir.mkdir()

    @patch("watcher.validate_contract")
    @patch("watcher._find_recent_output")
    def test_no_recent_output(self, mock_find, mock_validate):
        """When no recent output file exists, returns (False, error message)."""
        mock_find.return_value = None
        ok, errors = _quick_contract_check("0001_test")
        self.assertFalse(ok)
        self.assertEqual(len(errors), 1)
        self.assertIn("No recent output", errors[0])
        mock_validate.assert_not_called()

    @patch("watcher.validate_contract")
    @patch("watcher._find_recent_output")
    def test_valid_contract(self, mock_find, mock_validate):
        """When contract is valid, returns (True, [])."""
        f = self.output_dir / "0001_test_output.md"
        f.write_text("# Output\n## CONTRACT\nsummary: done\n")
        mock_find.return_value = f
        mock_validate.return_value = {"valid": True, "errors": [], "warnings": []}
        ok, errors = _quick_contract_check("0001_test")
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    @patch("watcher.validate_contract")
    @patch("watcher._find_recent_output")
    def test_invalid_contract_no_file_modification(self, mock_find, mock_validate):
        """Invalid contract returns errors but does NOT modify the file."""
        f = self.output_dir / "0001_test_output.md"
        original = "# Output\nno contract\n"
        f.write_text(original)
        mock_find.return_value = f
        mock_validate.return_value = {
            "valid": False,
            "errors": ["missing summary", "missing confidence"],
            "warnings": ["low quality"],
        }
        ok, errors = _quick_contract_check("0001_test")
        self.assertFalse(ok)
        self.assertEqual(errors, ["missing summary", "missing confidence"])
        # File must NOT be modified (unlike _check_contract)
        self.assertEqual(f.read_text(), original)

    @patch("watcher.validate_contract")
    @patch("watcher._find_recent_output")
    def test_invalid_contract_missing_errors_key(self, mock_find, mock_validate):
        """When validate_contract result has no 'errors' key, falls back gracefully."""
        f = self.output_dir / "0001_test_output.md"
        f.write_text("content")
        mock_find.return_value = f
        mock_validate.return_value = {"valid": False}
        ok, errors = _quick_contract_check("0001_test")
        self.assertFalse(ok)
        self.assertEqual(errors, [])


class TestReapStaleTasks(unittest.TestCase):
    """Tests for reap_stale_tasks: auto-mark old pending .md tasks as .done."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_tasks = watcher.TASKS_DIR
        watcher.TASKS_DIR = Path(self.tmpdir)
        # Reset reaper timer so it always runs
        watcher._last_reap_time = 0.0

    def tearDown(self):
        watcher.TASKS_DIR = self.orig_tasks
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reaps_old_task(self):
        """Task older than threshold gets renamed to .done."""
        task = Path(self.tmpdir) / "0001_old.md"
        task.write_text("old task")
        # Set mtime to 72 hours ago
        old_time = time.time() - (72 * 3600)
        os.utime(task, (old_time, old_time))

        reaped = watcher.reap_stale_tasks(force=True)
        self.assertEqual(reaped, ["0001_old.md"])
        self.assertFalse(task.exists())
        self.assertTrue(Path(self.tmpdir, "0001_old.done").exists())

    def test_keeps_recent_task(self):
        """Task younger than threshold is left alone."""
        task = Path(self.tmpdir) / "0002_recent.md"
        task.write_text("recent task")
        # mtime is now — well within threshold

        reaped = watcher.reap_stale_tasks(force=True)
        self.assertEqual(reaped, [])
        self.assertTrue(task.exists())

    def test_skips_inprogress_task(self):
        """Task with .inprogress sibling is not reaped even if old."""
        task = Path(self.tmpdir) / "0003_busy.md"
        task.write_text("busy task")
        inprog = Path(self.tmpdir) / "0003_busy.md.inprogress"
        inprog.write_text("in progress")
        old_time = time.time() - (72 * 3600)
        os.utime(task, (old_time, old_time))

        reaped = watcher.reap_stale_tasks(force=True)
        self.assertEqual(reaped, [])
        self.assertTrue(task.exists())

    def test_respects_interval_without_force(self):
        """Without force, reaper skips if called within STALE_REAP_INTERVAL."""
        task = Path(self.tmpdir) / "0004_stale.md"
        task.write_text("stale")
        old_time = time.time() - (72 * 3600)
        os.utime(task, (old_time, old_time))

        # First call — runs
        watcher._last_reap_time = 0.0
        reaped1 = watcher.reap_stale_tasks(force=False)
        self.assertEqual(len(reaped1), 1)

        # Second call immediately — skipped
        task2 = Path(self.tmpdir) / "0005_stale2.md"
        task2.write_text("stale2")
        os.utime(task2, (old_time, old_time))
        reaped2 = watcher.reap_stale_tasks(force=False)
        self.assertEqual(reaped2, [])

    def test_ignores_non_md_files(self):
        """Only .md files are candidates for reaping."""
        done = Path(self.tmpdir) / "0006_done.done"
        done.write_text("done")
        old_time = time.time() - (72 * 3600)
        os.utime(done, (old_time, old_time))

        reaped = watcher.reap_stale_tasks(force=True)
        self.assertEqual(reaped, [])


if __name__ == "__main__":
    unittest.main()
