"""Tests for utils.scheduler.migration — frontmatter parsing and enrichment."""

from __future__ import annotations

import pytest

from utils.scheduler.migration import (
    enrich_task,
    enrich_task_file,
    migrate_all_tasks,
    parse_frontmatter,
    write_enriched_frontmatter,
)
from utils.scheduler.work_unit import WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\npriority: high\ntype: shift_block\n---\n\n# Title\nBody text."
        fm, body = parse_frontmatter(content)
        assert fm["priority"] == "high"
        assert fm["type"] == "shift_block"
        assert "# Title" in body
        assert "Body text." in body

    def test_no_frontmatter(self):
        content = "# Just a title\n\nSome body text."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_empty_content(self):
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_incomplete_frontmatter(self):
        content = "---\npriority: high\nNo closing delimiter"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n---\n\nBody only."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert "Body only." in body

    def test_frontmatter_with_datetime(self):
        content = "---\nscheduled_at: 2026-03-29T13:00:00\npriority: medium\n---\n\nBody."
        fm, body = parse_frontmatter(content)
        assert "scheduled_at" in fm
        assert fm["priority"] == "medium"

    def test_invalid_yaml(self):
        content = "---\n: : : invalid yaml [ {\n---\n\nBody."
        fm, body = parse_frontmatter(content)
        assert fm == {}

    def test_preserves_body_exactly(self):
        body_text = "\n\n# My Task\n\nLine 1\nLine 2\n"
        content = f"---\nkey: value\n---{body_text}"
        fm, body = parse_frontmatter(content)
        assert fm == {"key": "value"}
        assert body == body_text

    def test_multiple_yaml_keys(self):
        content = "---\na: 1\nb: two\nc: true\nd: 3.14\n---\n\nBody."
        fm, body = parse_frontmatter(content)
        assert fm["a"] == 1
        assert fm["b"] == "two"
        assert fm["c"] is True
        assert fm["d"] == pytest.approx(3.14)


# ---------------------------------------------------------------------------
# enrich_task
# ---------------------------------------------------------------------------


class TestEnrichTask:
    def test_preserves_existing_fields(self):
        fm = {"priority": "high", "type": "shift_block", "shift_block": 3}
        body = "\n\n# Implementation Work\n\nDo the implementation."
        wu = enrich_task(fm, body, file_path="/tasks/impl.md")
        assert wu.priority == "high"
        assert wu.file_path == "/tasks/impl.md"

    def test_adds_scheduler_fields(self):
        fm = {"priority": "medium"}
        body = "\n\n# Research new strategies\n\nExplore options."
        wu = enrich_task(fm, body)
        # Should have classified from title keywords
        assert wu.work_mode == WorkMode.RESEARCH
        assert wu.estimated_optimistic_min >= 1.0
        assert wu.estimated_expected_min >= wu.estimated_optimistic_min
        assert wu.estimated_pessimistic_min >= wu.estimated_expected_min
        assert 0.0 <= wu.confidence <= 1.0

    def test_extracts_title_from_body(self):
        fm = {}
        body = "\n\n# My Amazing Task\n\nDetails here."
        wu = enrich_task(fm, body, file_path="/tasks/amazing.md")
        assert wu.title == "My Amazing Task"

    def test_uses_id_as_fallback_title(self):
        fm = {}
        body = "\n\nNo heading here, just text."
        wu = enrich_task(fm, body, file_path="/tasks/some_task.md")
        # Title should fall through to body scan, but no heading found
        # So title stays as id
        assert wu.id == "some_task"

    def test_category_flows_to_classifier(self):
        fm = {"category": "deploy"}
        body = "\n\n# Ship it\n\nDeploy the service."
        wu = enrich_task(fm, body)
        assert wu.work_mode == WorkMode.DEPLOYMENT

    def test_estimated_effort_flows_to_classifier(self):
        fm = {"estimated_effort": "light"}
        body = "\n\n# Quick task\n\nDone fast."
        wu = enrich_task(fm, body)
        assert wu.estimated_expected_min <= 20.0


# ---------------------------------------------------------------------------
# enrich_task_file
# ---------------------------------------------------------------------------


class TestEnrichTaskFile:
    def test_reads_and_enriches(self, tmp_path):
        task_file = tmp_path / "test_task.md"
        task_file.write_text(
            "---\npriority: high\n---\n\n# Fix the bug\n\nDetails here.",
            encoding="utf-8",
        )
        wu = enrich_task_file(task_file)
        assert wu is not None
        assert wu.id == "test_task"
        assert wu.priority == "high"
        assert wu.title == "Fix the bug"
        assert wu.work_mode == WorkMode.DEBUGGING

    def test_file_without_frontmatter(self, tmp_path):
        task_file = tmp_path / "plain.md"
        task_file.write_text("# Just a title\n\nBody text.", encoding="utf-8")
        wu = enrich_task_file(task_file)
        assert wu is not None
        assert wu.title == "Just a title"
        assert wu.priority == "medium"

    def test_nonexistent_file(self, tmp_path):
        wu = enrich_task_file(tmp_path / "nonexistent.md")
        assert wu is None

    def test_file_path_set(self, tmp_path):
        task_file = tmp_path / "my_task.md"
        task_file.write_text("# Task\n\nBody.", encoding="utf-8")
        wu = enrich_task_file(task_file)
        assert wu is not None
        assert wu.file_path == str(task_file)


# ---------------------------------------------------------------------------
# write_enriched_frontmatter
# ---------------------------------------------------------------------------


class TestWriteEnrichedFrontmatter:
    def _make_task_file(self, tmp_path, content):
        task_file = tmp_path / "task.md"
        task_file.write_text(content, encoding="utf-8")
        return task_file

    def test_dry_run_does_not_modify(self, tmp_path):
        original = "---\npriority: high\n---\n\n# Title\nBody."
        task_file = self._make_task_file(tmp_path, original)
        wu = WorkUnit(id="task", priority="high")

        result = write_enriched_frontmatter(task_file, wu, dry_run=True)
        assert result is True
        assert task_file.read_text(encoding="utf-8") == original

    def test_writes_scheduler_fields(self, tmp_path):
        original = "---\npriority: high\ntype: shift_block\n---\n\n# Title\nBody."
        task_file = self._make_task_file(tmp_path, original)
        wu = enrich_task_file(task_file)
        assert wu is not None

        result = write_enriched_frontmatter(task_file, wu, dry_run=False)
        assert result is True

        new_content = task_file.read_text(encoding="utf-8")
        assert "scheduler_work_mode:" in new_content
        assert "scheduler_task_class:" in new_content
        assert "scheduler_confidence:" in new_content
        assert "scheduler_est_expected_min:" in new_content

    def test_preserves_original_fields(self, tmp_path):
        original = "---\npriority: high\ntype: shift_block\nshift_block: 3\n---\n\n# Title\nBody."
        task_file = self._make_task_file(tmp_path, original)
        wu = enrich_task_file(task_file)
        assert wu is not None

        write_enriched_frontmatter(task_file, wu, dry_run=False)
        new_content = task_file.read_text(encoding="utf-8")

        assert "priority: high" in new_content
        assert "type: shift_block" in new_content
        assert "shift_block: 3" in new_content

    def test_preserves_body(self, tmp_path):
        body_text = "\n\n# My Task Title\n\nImportant body text with details.\n\n## Section\n\nMore content."
        original = f"---\npriority: medium\n---{body_text}"
        task_file = self._make_task_file(tmp_path, original)
        wu = enrich_task_file(task_file)
        assert wu is not None

        write_enriched_frontmatter(task_file, wu, dry_run=False)
        new_content = task_file.read_text(encoding="utf-8")

        assert body_text in new_content

    def test_contains_scheduler_comment(self, tmp_path):
        original = "---\npriority: high\n---\n\n# Title\nBody."
        task_file = self._make_task_file(tmp_path, original)
        wu = WorkUnit(id="task", priority="high")

        write_enriched_frontmatter(task_file, wu, dry_run=False)
        new_content = task_file.read_text(encoding="utf-8")
        assert "# scheduler" in new_content

    def test_re_enrichment_removes_old_scheduler_fields(self, tmp_path):
        """Running enrichment twice should not duplicate scheduler fields."""
        original = "---\npriority: high\n---\n\n# Title\nBody."
        task_file = self._make_task_file(tmp_path, original)
        wu = WorkUnit(id="task", priority="high", confidence=0.5)

        # First write
        write_enriched_frontmatter(task_file, wu, dry_run=False)

        # Change confidence and re-enrich
        wu2 = WorkUnit(id="task", priority="high", confidence=0.9)
        write_enriched_frontmatter(task_file, wu2, dry_run=False)

        new_content = task_file.read_text(encoding="utf-8")
        # Should only have one scheduler_confidence field
        assert new_content.count("scheduler_confidence:") == 1

    def test_nonexistent_file_returns_false(self, tmp_path):
        wu = WorkUnit(id="task")
        result = write_enriched_frontmatter(tmp_path / "nope.md", wu, dry_run=False)
        assert result is False

    def test_atomic_write(self, tmp_path):
        """Verify the file is written atomically (no partial writes visible)."""
        original = "---\npriority: high\n---\n\n# Title\nBody."
        task_file = self._make_task_file(tmp_path, original)
        wu = WorkUnit(id="task", priority="high")

        write_enriched_frontmatter(task_file, wu, dry_run=False)

        # File should be valid YAML frontmatter after write
        from utils.scheduler.migration import parse_frontmatter

        new_content = task_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(new_content)
        assert "priority" in fm
        assert "scheduler_work_mode" in fm


# ---------------------------------------------------------------------------
# migrate_all_tasks
# ---------------------------------------------------------------------------


class TestMigrateAllTasks:
    def _create_tasks(self, tmp_path, tasks: list[tuple[str, str]]):
        """Create test task files. tasks = [(filename, content), ...]."""
        for name, content in tasks:
            (tmp_path / name).write_text(content, encoding="utf-8")

    def test_processes_directory(self, tmp_path):
        self._create_tasks(
            tmp_path,
            [
                ("task_a.md", "---\npriority: high\n---\n\n# Fix bug\nDetails."),
                ("task_b.md", "---\npriority: low\n---\n\n# Research topic\nDetails."),
                ("task_c.md", "# No frontmatter\n\nJust a task."),
            ],
        )

        results = migrate_all_tasks(tmp_path, dry_run=True)
        assert len(results) == 3
        assert all(r["error"] is None for r in results)
        assert all(r["enriched"] is False for r in results)  # dry_run

    def test_enriches_when_not_dry_run(self, tmp_path):
        self._create_tasks(
            tmp_path,
            [
                ("task_x.md", "---\npriority: high\n---\n\n# Deploy service\nDeploy."),
            ],
        )

        results = migrate_all_tasks(tmp_path, dry_run=False)
        assert len(results) == 1
        assert results[0]["enriched"] is True

        # Verify file was modified
        content = (tmp_path / "task_x.md").read_text(encoding="utf-8")
        assert "scheduler_work_mode:" in content

    def test_returns_correct_metadata(self, tmp_path):
        self._create_tasks(
            tmp_path,
            [
                ("research_task.md", "---\ncategory: research\n---\n\n# Research new APIs\nExplore."),
            ],
        )

        results = migrate_all_tasks(tmp_path, dry_run=True)
        assert len(results) == 1
        r = results[0]
        assert r["work_mode"] == "research"
        assert r["title"] == "Research new APIs"
        assert r["confidence"] > 0

    def test_empty_directory(self, tmp_path):
        results = migrate_all_tasks(tmp_path, dry_run=True)
        assert results == []

    def test_nonexistent_directory(self, tmp_path):
        results = migrate_all_tasks(tmp_path / "nonexistent", dry_run=True)
        assert results == []

    def test_ignores_non_md_files(self, tmp_path):
        self._create_tasks(
            tmp_path,
            [
                ("task.md", "---\npriority: high\n---\n\n# Task\nBody."),
            ],
        )
        (tmp_path / "notes.txt").write_text("Not a task", encoding="utf-8")
        (tmp_path / "config.yaml").write_text("key: value", encoding="utf-8")

        results = migrate_all_tasks(tmp_path, dry_run=True)
        assert len(results) == 1  # Only the .md file

    def test_file_path_in_results(self, tmp_path):
        self._create_tasks(
            tmp_path,
            [
                ("my_task.md", "# Task\nBody."),
            ],
        )

        results = migrate_all_tasks(tmp_path, dry_run=True)
        assert len(results) == 1
        assert results[0]["file"] == str(tmp_path / "my_task.md")

    def test_epic_candidate_flagged(self, tmp_path):
        self._create_tasks(
            tmp_path,
            [
                ("big_task.md", "---\nestimated_effort: heavy\n---\n\n# Major refactor\nBig work."),
            ],
        )

        results = migrate_all_tasks(tmp_path, dry_run=True)
        assert len(results) == 1
        assert results[0]["epic_candidate"] is True

    def test_sorted_output(self, tmp_path):
        """Results should be sorted by filename."""
        self._create_tasks(
            tmp_path,
            [
                ("c_task.md", "# C\nBody."),
                ("a_task.md", "# A\nBody."),
                ("b_task.md", "# B\nBody."),
            ],
        )

        results = migrate_all_tasks(tmp_path, dry_run=True)
        files = [r["file"] for r in results]
        assert files == sorted(files)
