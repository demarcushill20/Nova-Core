"""Enhanced tests for watcher.py — core NovaCore runtime coverage.

Covers task discovery, scheduling, contract validation, and error paths.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from watcher import (
    _check_contract,
    _check_scheduled,
    _extract_keywords,
    _find_recent_output,
    _is_retry_task,
    _original_stem,
    _quick_contract_check,
    _task_stem,
    get_pending_tasks,
    is_task_ready,
    parse_frontmatter,
    verify_artifacts,
)


class TestTaskDiscovery:
    """Test task discovery and filtering logic."""

    def test_extract_keywords_basic(self):
        """Extract keywords from task text."""
        text = """
        # Task: Deploy Application

        Deploy the web application to production server.
        Update configuration files and restart services.
        """
        keywords = _extract_keywords(text, max_keywords=5)

        assert "deploy" in keywords
        assert "application" in keywords
        assert "production" in keywords
        assert len(keywords) <= 5

    def test_extract_keywords_empty_text(self):
        """Handle empty or whitespace-only text."""
        keywords = _extract_keywords("", max_keywords=10)
        assert keywords == []

        keywords = _extract_keywords("   \n\t  ", max_keywords=10)
        assert keywords == []

    def test_task_stem_basic(self):
        """Extract stem from task filename."""
        assert _task_stem("task_001_deploy.md") == "task_001_deploy"
        assert _task_stem("0123_fix_bug.inprogress") == "0123_fix_bug"
        assert _task_stem("simple_task.done") == "simple_task.done"  # .done not stripped

    def test_task_stem_no_extension(self):
        """Handle filename without extension."""
        assert _task_stem("task_name") == "task_name"

    def test_find_recent_output_existing(self, tmp_path):
        """Find most recent output file for task stem."""
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        # Create output files with timestamps
        old_output = output_dir / "task_001__20260101_120000.md"
        new_output = output_dir / "task_001__20260102_120000.md"
        old_output.write_text("old output")
        new_output.write_text("new output")

        with patch("watcher.OUTPUT_DIR", output_dir):
            result = _find_recent_output("task_001")
            assert result == new_output

    def test_find_recent_output_none(self, tmp_path):
        """Return None when no output files exist."""
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        with patch("watcher.OUTPUT_DIR", output_dir):
            result = _find_recent_output("nonexistent_task")
            assert result is None


class TestFrontmatterParsing:
    """Test YAML frontmatter parsing."""

    def test_parse_frontmatter_valid(self, tmp_path):
        """Parse valid YAML frontmatter."""
        task_file = tmp_path / "test_task.md"
        content = """---
scheduled_at: 2026-03-19T15:00:00
priority: high
type: deployment
---

# Task Content
Do something important.
"""
        task_file.write_text(content)

        metadata = parse_frontmatter(task_file)
        from datetime import datetime as dt

        assert metadata["scheduled_at"] == dt(2026, 3, 19, 15, 0, 0)
        assert metadata["priority"] == "high"
        assert metadata["type"] == "deployment"

    def test_parse_frontmatter_no_frontmatter(self, tmp_path):
        """Handle file without frontmatter."""
        task_file = tmp_path / "no_frontmatter.md"
        content = "# Simple Task\nNo frontmatter here."
        task_file.write_text(content)

        metadata = parse_frontmatter(task_file)
        assert metadata == {}

    def test_parse_frontmatter_invalid_yaml(self, tmp_path):
        """Handle malformed YAML frontmatter."""
        task_file = tmp_path / "bad_yaml.md"
        content = """---
invalid: yaml: structure:
missing_quotes: this is bad
---
Content here.
"""
        task_file.write_text(content)

        metadata = parse_frontmatter(task_file)
        assert metadata == {}  # Should return empty dict on parse error

    def test_parse_frontmatter_nonexistent_file(self, tmp_path):
        """Handle nonexistent file gracefully."""
        nonexistent = tmp_path / "missing.md"

        metadata = parse_frontmatter(nonexistent)
        assert metadata == {}


class TestTaskScheduling:
    """Test task scheduling logic."""

    def test_is_task_ready_no_schedule(self, tmp_path):
        """Task without schedule should be ready."""
        task_file = tmp_path / "immediate_task.md"
        content = "# Immediate Task\nRun now."
        task_file.write_text(content)

        assert is_task_ready(task_file) is True

    def test_is_task_ready_past_schedule(self, tmp_path):
        """Task scheduled in past should be ready."""
        task_file = tmp_path / "past_task.md"
        past_time = datetime.now() - timedelta(hours=1)
        content = f"""---
scheduled_at: {past_time.isoformat()}
---
Past task content.
"""
        task_file.write_text(content)

        assert is_task_ready(task_file) is True

    def test_is_task_ready_future_schedule(self, tmp_path):
        """Task scheduled in future should not be ready."""
        task_file = tmp_path / "future_task.md"
        future_time = datetime.now() + timedelta(hours=1)
        content = f"""---
scheduled_at: {future_time.isoformat()}
---
Future task content.
"""
        task_file.write_text(content)

        assert is_task_ready(task_file) is False

    def test_check_scheduled_valid_iso_past(self):
        """Check scheduled time in past."""
        past_time = (datetime.now() - timedelta(hours=1)).isoformat()
        assert _check_scheduled(past_time) is True

    def test_check_scheduled_valid_iso_future(self):
        """Check scheduled time in future."""
        future_time = (datetime.now() + timedelta(hours=1)).isoformat()
        assert _check_scheduled(future_time) is False

    def test_check_scheduled_invalid_format(self):
        """Invalid datetime format should return True (fail-open)."""
        assert _check_scheduled("not-a-date") is True
        assert _check_scheduled("2026-13-99T25:99:99") is True

    def test_check_scheduled_none_or_empty(self):
        """None or empty schedule should return True."""
        assert _check_scheduled(None) is True
        assert _check_scheduled("") is True
        assert _check_scheduled("  ") is True


class TestRetryLogic:
    """Test retry task detection and handling."""

    def test_is_retry_task_true(self):
        """Detect retry task stems (only __retry1 pattern)."""
        assert _is_retry_task("task_001__retry1") is True
        assert _is_retry_task("deploy_fix__retry1") is True
        assert _is_retry_task("0123_bugfix__retry1_extra") is True

    def test_is_retry_task_false(self):
        """Non-retry task stems should return False."""
        assert _is_retry_task("task_001") is False
        assert _is_retry_task("normal_task") is False
        assert _is_retry_task("task_with_retry_in_name") is False

    def test_original_stem_from_retry(self):
        """Extract original stem from retry task."""
        assert _original_stem("task_001__retry1") == "task_001"
        assert _original_stem("complex_task_name__retry1") == "complex_task_name"

    def test_original_stem_from_non_retry(self):
        """Original stem of non-retry task is itself."""
        assert _original_stem("task_001") == "task_001"
        assert _original_stem("normal_task") == "normal_task"


class TestContractValidation:
    """Test contract validation logic."""

    def test_check_contract_valid(self, tmp_path):
        """Valid contract should pass validation."""
        output_file = tmp_path / "output.md"
        content = """
# Task Results

Work completed successfully.

## CONTRACT
summary: Task completed successfully
files_changed: file1.txt, file2.txt
verification: ran tests and confirmed output
confidence: high
"""
        output_file.write_text(content)

        valid, messages = _check_contract(output_file)
        assert valid is True
        # May include informational messages; no error messages expected
        assert all("missing" not in m.lower() and "fail" not in m.lower() for m in messages)

    def test_check_contract_missing_section(self, tmp_path):
        """Missing CONTRACT section should fail."""
        output_file = tmp_path / "output.md"
        content = """
# Task Results

Work completed but no contract section.
"""
        output_file.write_text(content)

        valid, messages = _check_contract(output_file)
        assert valid is False
        assert any("CONTRACT section" in msg for msg in messages)

    def test_check_contract_missing_fields(self, tmp_path):
        """Missing required fields should fail."""
        output_file = tmp_path / "output.md"
        content = """
# Task Results

## CONTRACT
summary: Task completed
# Missing other required fields
"""
        output_file.write_text(content)

        valid, messages = _check_contract(output_file)
        assert valid is False
        assert any("files_changed" in msg for msg in messages)
        assert any("verification" in msg for msg in messages)
        assert any("confidence" in msg for msg in messages)

    def test_check_contract_nonexistent_file(self, tmp_path):
        """Nonexistent file should raise FileNotFoundError."""
        nonexistent = tmp_path / "missing.md"

        with pytest.raises(FileNotFoundError):
            _check_contract(nonexistent)

    def test_quick_contract_check_success(self, tmp_path):
        """Quick check should validate basic contract presence."""
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        output_file = output_dir / "test_task__20260319_120000.md"
        content = """
## CONTRACT
summary: Quick test
files_changed: none
verification: quick check
confidence: high
"""
        output_file.write_text(content)

        with patch("watcher.OUTPUT_DIR", output_dir):
            valid, messages = _quick_contract_check("test_task")
            assert valid is True

    def test_quick_contract_check_no_output(self, tmp_path):
        """Quick check with no output file should fail."""
        output_dir = tmp_path / "OUTPUT"
        output_dir.mkdir()

        with patch("watcher.OUTPUT_DIR", output_dir):
            valid, messages = _quick_contract_check("nonexistent_task")
            assert valid is False


class TestArtifactVerification:
    """Test artifact verification logic."""

    def test_verify_artifacts_success(self, tmp_path):
        """Successful artifact verification."""
        output_dir = tmp_path / "OUTPUT"
        logs_dir = tmp_path / "LOGS"
        state_dir = tmp_path / "STATE"
        output_dir.mkdir()
        logs_dir.mkdir()
        state_dir.mkdir()

        # Create valid output file
        output_file = output_dir / "test_task__20260319_120000.md"
        content = """
## CONTRACT
summary: Test completed
files_changed: none
verification: confirmed
confidence: high
"""
        output_file.write_text(content)

        # Create log file
        log_file = logs_dir / "worker_test_task.log"
        log_file.write_text("Task executed successfully")

        with (
            patch("watcher.OUTPUT_DIR", output_dir),
            patch("watcher.LOGS_DIR", logs_dir),
            patch("watcher.STATE_DIR", state_dir),
        ):
            valid, messages = verify_artifacts("test_task")
            assert valid is True

    def test_verify_artifacts_missing_output(self, tmp_path):
        """Missing output file should fail verification."""
        output_dir = tmp_path / "OUTPUT"
        state_dir = tmp_path / "STATE"
        output_dir.mkdir()
        state_dir.mkdir()

        with patch("watcher.OUTPUT_DIR", output_dir), patch("watcher.STATE_DIR", state_dir):
            valid, errors = verify_artifacts("missing_task")
            assert valid is False
            assert any("OUTPUT missing" in msg for msg in errors)

    def test_verify_artifacts_invalid_contract(self, tmp_path):
        """Invalid contract should fail verification."""
        output_dir = tmp_path / "OUTPUT"
        state_dir = tmp_path / "STATE"
        tasks_dir = tmp_path / "TASKS"
        output_dir.mkdir()
        state_dir.mkdir()
        tasks_dir.mkdir()

        # Create output file with invalid contract
        output_file = output_dir / "test_task__20260319_120000.md"
        content = """
# Task Results
Missing contract section.
"""
        output_file.write_text(content)

        with (
            patch("watcher.OUTPUT_DIR", output_dir),
            patch("watcher.STATE_DIR", state_dir),
            patch("watcher.TASKS_DIR", tasks_dir),
        ):
            valid, errors = verify_artifacts("test_task")
            assert valid is False
            assert len(errors) > 0


class TestGetPendingTasks:
    """Test pending task discovery."""

    def test_get_pending_tasks_mixed_states(self, tmp_path):
        """Get only .md files, not .inprogress or .done."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        # Create tasks in various states
        (tasks_dir / "pending_task.md").write_text("pending")
        (tasks_dir / "in_progress.inprogress").write_text("in progress")
        (tasks_dir / "completed.done").write_text("completed")
        (tasks_dir / "failed.failed").write_text("failed")

        with patch("watcher.TASKS_DIR", tasks_dir):
            pending = get_pending_tasks()

            # Should only return .md files
            assert len(pending) == 1
            assert pending[0].name == "pending_task.md"

    def test_get_pending_tasks_scheduled_filtering(self, tmp_path):
        """Filter out tasks not ready for execution."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        # Ready task
        ready_task = tasks_dir / "ready_task.md"
        ready_task.write_text("# Ready Task")

        # Future scheduled task
        future_task = tasks_dir / "future_task.md"
        future_time = datetime.now() + timedelta(hours=1)
        future_content = f"""---
scheduled_at: {future_time.isoformat()}
---
# Future Task
"""
        future_task.write_text(future_content)

        with patch("watcher.TASKS_DIR", tasks_dir):
            pending = get_pending_tasks()

            # Should only return ready task
            task_names = [t.name for t in pending]
            assert "ready_task.md" in task_names
            assert "future_task.md" not in task_names

    def test_get_pending_tasks_empty_directory(self, tmp_path):
        """Empty TASKS directory should return empty list."""
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        with patch("watcher.TASKS_DIR", tasks_dir):
            pending = get_pending_tasks()
            assert pending == []
