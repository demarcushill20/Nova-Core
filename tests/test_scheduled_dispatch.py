"""Tests for scheduled task dispatch (scheduled_at frontmatter filtering)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_task(tmp_path: Path, name: str, content: str) -> Path:
    """Write a task file with the given content and return its path."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir(exist_ok=True)
    p = tasks_dir / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_no_frontmatter(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(tmp_path, "task.md", "# Just a heading\nSome text")
        assert parse_frontmatter(p) == {}

    def test_empty_frontmatter(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(tmp_path, "task.md", "---\n---\n# Body")
        assert parse_frontmatter(p) == {}

    def test_priority_only(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(tmp_path, "task.md", "---\npriority: high\n---\n# Body")
        fm = parse_frontmatter(p)
        assert fm["priority"] == "high"

    def test_scheduled_at_field(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(
            tmp_path,
            "task.md",
            '---\npriority: CRITICAL\nscheduled_at: "2026-03-20T10:00:00"\n---\n# Body',
        )
        fm = parse_frontmatter(p)
        assert fm["priority"] == "CRITICAL"
        assert fm["scheduled_at"] == "2026-03-20T10:00:00"

    def test_invalid_yaml(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(tmp_path, "task.md", "---\n: :\n  - [invalid\n---\n# Body")
        assert parse_frontmatter(p) == {}

    def test_missing_closing_fence(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(tmp_path, "task.md", "---\npriority: high\n# Body")
        # Only one '---' → split produces < 3 parts
        assert parse_frontmatter(p) == {}

    def test_nonexistent_file(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = tmp_path / "does_not_exist.md"
        assert parse_frontmatter(p) == {}


# ---------------------------------------------------------------------------
# is_task_ready
# ---------------------------------------------------------------------------


class TestIsTaskReady:
    def test_no_frontmatter_is_ready(self, tmp_path: Path):
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", "# Just a task\nDo the thing")
        assert is_task_ready(p) is True

    def test_priority_only_no_scheduled_at_is_ready(self, tmp_path: Path):
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", "---\npriority: high\n---\n# Task")
        assert is_task_ready(p) is True

    def test_scheduled_at_in_past_is_ready(self, tmp_path: Path):
        from watcher import is_task_ready

        past = (datetime.now() - timedelta(hours=1)).isoformat()
        p = _write_task(tmp_path, "task.md", f'---\nscheduled_at: "{past}"\n---\n# Task')
        assert is_task_ready(p) is True

    def test_scheduled_at_in_future_is_skipped(self, tmp_path: Path):
        from watcher import is_task_ready

        future = (datetime.now() + timedelta(hours=1)).isoformat()
        p = _write_task(tmp_path, "task.md", f'---\nscheduled_at: "{future}"\n---\n# Task')
        assert is_task_ready(p) is False

    def test_scheduled_at_now_is_ready(self, tmp_path: Path):
        """A task scheduled at exactly now (or very slightly in the past) should be ready."""
        from watcher import is_task_ready

        # Use 1 second in the past to avoid race conditions
        now = (datetime.now() - timedelta(seconds=1)).isoformat()
        p = _write_task(tmp_path, "task.md", f'---\nscheduled_at: "{now}"\n---\n# Task')
        assert is_task_ready(p) is True

    def test_invalid_scheduled_at_value_is_ready(self, tmp_path: Path):
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", '---\nscheduled_at: "not-a-date"\n---\n# Task')
        assert is_task_ready(p) is True

    def test_numeric_scheduled_at_is_ready(self, tmp_path: Path):
        """A numeric (non-string, non-datetime) value should fall through to ready."""
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", "---\nscheduled_at: 12345\n---\n# Task")
        assert is_task_ready(p) is True

    def test_boolean_scheduled_at_is_ready(self, tmp_path: Path):
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", "---\nscheduled_at: true\n---\n# Task")
        assert is_task_ready(p) is True

    def test_yaml_native_datetime_in_past(self, tmp_path: Path):
        """yaml.safe_load can parse bare datetimes as datetime objects."""
        from watcher import is_task_ready

        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        p = _write_task(tmp_path, "task.md", f"---\nscheduled_at: {past}\n---\n# Task")
        assert is_task_ready(p) is True

    def test_yaml_native_datetime_in_future(self, tmp_path: Path):
        from watcher import is_task_ready

        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        p = _write_task(tmp_path, "task.md", f"---\nscheduled_at: {future}\n---\n# Task")
        assert is_task_ready(p) is False

    def test_yaml_bare_date_in_future(self, tmp_path: Path):
        """yaml.safe_load parses bare dates (2026-12-31) as datetime.date objects."""
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", "---\nscheduled_at: 2099-12-31\n---\n# Task")
        assert is_task_ready(p) is False

    def test_yaml_bare_date_in_past(self, tmp_path: Path):
        from watcher import is_task_ready

        p = _write_task(tmp_path, "task.md", "---\nscheduled_at: 2020-01-01\n---\n# Task")
        assert is_task_ready(p) is True


# ---------------------------------------------------------------------------
# Integration: parse_frontmatter preserves existing fields
# ---------------------------------------------------------------------------


class TestFrontmatterIntegration:
    def test_preserves_priority_with_scheduled_at(self, tmp_path: Path):
        from watcher import parse_frontmatter

        p = _write_task(
            tmp_path,
            "task.md",
            '---\npriority: CRITICAL\nscheduled_at: "2026-12-31T23:59:59"\n---\n# Task',
        )
        fm = parse_frontmatter(p)
        assert fm["priority"] == "CRITICAL"
        assert fm["scheduled_at"] == "2026-12-31T23:59:59"

    def test_multiple_fields_preserved(self, tmp_path: Path):
        from watcher import parse_frontmatter

        content = '---\npriority: high\nscheduled_at: "2026-06-01T08:00:00"\ntags: [deploy, urgent]\n---\n# Deploy task'
        p = _write_task(tmp_path, "task.md", content)
        fm = parse_frontmatter(p)
        assert fm["priority"] == "high"
        assert fm["scheduled_at"] == "2026-06-01T08:00:00"
        assert fm["tags"] == ["deploy", "urgent"]
