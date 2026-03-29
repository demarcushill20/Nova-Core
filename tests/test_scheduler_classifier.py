"""Tests for utils.scheduler.classifier — heuristic task classification."""

from __future__ import annotations

import pytest

from utils.scheduler.classifier import classify_task
from utils.scheduler.work_unit import TaskClass, WorkMode

# ---------------------------------------------------------------------------
# Category -> WorkMode mapping
# ---------------------------------------------------------------------------


class TestCategoryMapping:
    @pytest.mark.parametrize(
        "category,expected_mode",
        [
            ("research", WorkMode.RESEARCH),
            ("plan", WorkMode.ANALYSIS),
            ("planning", WorkMode.ANALYSIS),
            ("execute", WorkMode.DEEP_IMPLEMENTATION),
            ("implementation", WorkMode.DEEP_IMPLEMENTATION),
            ("repair", WorkMode.DEBUGGING),
            ("fix", WorkMode.DEBUGGING),
            ("validate", WorkMode.REVIEW),
            ("review", WorkMode.REVIEW),
            ("monitor", WorkMode.MONITORING),
            ("monitoring", WorkMode.MONITORING),
            ("deploy", WorkMode.DEPLOYMENT),
            ("deployment", WorkMode.DEPLOYMENT),
            ("communicate", WorkMode.COMMUNICATION),
            ("communication", WorkMode.COMMUNICATION),
            ("", WorkMode.ADMIN),
            ("unknown_category", WorkMode.ADMIN),
        ],
    )
    def test_category_to_work_mode(self, category, expected_mode):
        result = classify_task(title="Some task", category=category)
        # Note: keyword matching in title may override; use neutral title
        # Actually "Some task" is neutral enough
        assert result["work_mode"] == expected_mode


# ---------------------------------------------------------------------------
# Effort -> TaskClass + duration
# ---------------------------------------------------------------------------


class TestEffortMapping:
    def test_light_effort(self):
        result = classify_task(title="Quick update", estimated_effort="light")
        assert result["task_class"] == TaskClass.ATOMIC
        assert result["estimated_optimistic_min"] == 5.0
        assert result["estimated_expected_min"] == 12.0
        assert result["estimated_pessimistic_min"] == 20.0
        assert result["epic_candidate"] is False

    def test_medium_effort(self):
        result = classify_task(title="Moderate work", estimated_effort="medium")
        assert result["task_class"] == TaskClass.BOUNDED
        assert result["estimated_optimistic_min"] == 20.0
        assert result["estimated_expected_min"] == 45.0
        assert result["estimated_pessimistic_min"] == 90.0
        assert result["epic_candidate"] is False

    def test_heavy_effort(self):
        result = classify_task(title="Major overhaul", estimated_effort="heavy")
        assert result["task_class"] == TaskClass.EPIC
        assert result["estimated_optimistic_min"] == 60.0
        assert result["estimated_expected_min"] == 120.0
        assert result["estimated_pessimistic_min"] == 240.0
        assert result["epic_candidate"] is True

    def test_default_effort(self):
        result = classify_task(title="Unknown effort task")
        assert result["task_class"] == TaskClass.BOUNDED
        assert result["estimated_optimistic_min"] == 15.0
        assert result["estimated_expected_min"] == 30.0
        assert result["estimated_pessimistic_min"] == 60.0

    def test_unknown_effort_string(self):
        result = classify_task(title="Task", estimated_effort="extra_large")
        assert result["task_class"] == TaskClass.BOUNDED
        assert result["estimated_expected_min"] == 30.0


# ---------------------------------------------------------------------------
# Keyword detection in title
# ---------------------------------------------------------------------------


class TestKeywordDetection:
    @pytest.mark.parametrize(
        "title,expected_mode",
        [
            ("Fix the login bug", WorkMode.DEBUGGING),
            ("Bug in payment processing", WorkMode.DEBUGGING),
            ("Broken webhook handler", WorkMode.DEBUGGING),
            ("Error handling for MetaApi", WorkMode.DEBUGGING),
            ("Research new prop firms", WorkMode.RESEARCH),
            ("Investigate memory leak", WorkMode.RESEARCH),
            ("Explore alternative architectures", WorkMode.RESEARCH),
            ("Deploy new IRB strategy", WorkMode.DEPLOYMENT),
            ("Release v2.1.0", WorkMode.DEPLOYMENT),
            ("Rollout signal monitor", WorkMode.DEPLOYMENT),
            ("Review PR #42", WorkMode.REVIEW),
            ("Audit security config", WorkMode.REVIEW),
            ("Check test coverage", WorkMode.REVIEW),
            ("Cleanup old task files", WorkMode.ADMIN),
            ("Memory hygiene pass", WorkMode.ADMIN),
            ("Archive completed tasks", WorkMode.ADMIN),
            ("Monitor system health", WorkMode.MONITORING),
            ("Health check dashboard", WorkMode.MONITORING),
            ("Status page update", WorkMode.MONITORING),
            ("Implement new scheduler", WorkMode.DEEP_IMPLEMENTATION),
            ("Build data pipeline", WorkMode.DEEP_IMPLEMENTATION),
            ("Create migration tool", WorkMode.DEEP_IMPLEMENTATION),
            ("Add retry logic", WorkMode.DEEP_IMPLEMENTATION),
        ],
    )
    def test_keyword_overrides_category(self, title, expected_mode):
        # Even with a different category, keyword in title should win
        result = classify_task(title=title, category="")
        assert result["work_mode"] == expected_mode

    def test_keyword_overrides_category_explicitly(self):
        """Title keyword 'fix' overrides category 'research'."""
        result = classify_task(title="Fix the broken parser", category="research")
        assert result["work_mode"] == WorkMode.DEBUGGING

    def test_neutral_title_uses_category(self):
        """Title with no keywords falls back to category mapping."""
        result = classify_task(title="Some task", category="research")
        assert result["work_mode"] == WorkMode.RESEARCH


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------


class TestConfidenceLevels:
    def test_all_fields_provided(self):
        result = classify_task(
            title="Full task",
            body="With a body",
            category="execute",
            estimated_effort="medium",
        )
        assert result["confidence"] == 0.7

    def test_missing_body(self):
        result = classify_task(
            title="Task with effort",
            body="",
            category="execute",
            estimated_effort="medium",
        )
        assert result["confidence"] == 0.5

    def test_missing_effort(self):
        result = classify_task(
            title="Task with body",
            body="Some description here",
            category="execute",
            estimated_effort="",
        )
        assert result["confidence"] == 0.5

    def test_title_and_category_only(self):
        result = classify_task(
            title="Task",
            body="",
            category="research",
            estimated_effort="",
        )
        assert result["confidence"] == 0.5

    def test_title_only(self):
        result = classify_task(title="Just a title")
        assert result["confidence"] == 0.3

    def test_empty_title(self):
        result = classify_task(title="")
        assert result["confidence"] == 0.2

    def test_no_inputs(self):
        result = classify_task(title="", body="", category="", estimated_effort="")
        assert result["confidence"] == 0.2


# ---------------------------------------------------------------------------
# Return structure
# ---------------------------------------------------------------------------


class TestReturnStructure:
    def test_all_keys_present(self):
        result = classify_task(title="Test")
        expected_keys = {
            "work_mode",
            "task_class",
            "estimated_optimistic_min",
            "estimated_expected_min",
            "estimated_pessimistic_min",
            "confidence",
            "epic_candidate",
            "keyword_matched",
        }
        assert set(result.keys()) == expected_keys

    def test_work_mode_is_enum(self):
        result = classify_task(title="Test")
        assert isinstance(result["work_mode"], WorkMode)

    def test_task_class_is_enum(self):
        result = classify_task(title="Test")
        assert isinstance(result["task_class"], TaskClass)

    def test_estimates_are_ordered(self):
        result = classify_task(title="Test", estimated_effort="medium")
        assert result["estimated_optimistic_min"] <= result["estimated_expected_min"]
        assert result["estimated_expected_min"] <= result["estimated_pessimistic_min"]


# ---------------------------------------------------------------------------
# Parametrized sample tasks (20+ cases)
# ---------------------------------------------------------------------------


_SAMPLE_TASKS = [
    # (title, body, category, effort, expected_mode, expected_class)
    ("Fix login crash", "", "", "", WorkMode.DEBUGGING, TaskClass.BOUNDED),
    ("Research FTMO rules", "", "research", "light", WorkMode.RESEARCH, TaskClass.ATOMIC),
    ("Deploy IRB v2.1", "", "deploy", "medium", WorkMode.DEPLOYMENT, TaskClass.BOUNDED),
    ("Review PR changes", "Check for bugs", "review", "light", WorkMode.REVIEW, TaskClass.ATOMIC),
    ("System Health Check", "", "monitor", "light", WorkMode.MONITORING, TaskClass.ATOMIC),
    ("Cleanup old logs", "", "", "light", WorkMode.ADMIN, TaskClass.ATOMIC),
    (
        "Implement block scheduler",
        "Phase 1 data model",
        "execute",
        "heavy",
        WorkMode.DEEP_IMPLEMENTATION,
        TaskClass.EPIC,
    ),
    ("Archive completed tasks", "", "admin", "light", WorkMode.ADMIN, TaskClass.ATOMIC),
    ("Investigate memory leak", "Memory usage growing", "repair", "medium", WorkMode.RESEARCH, TaskClass.BOUNDED),
    ("Build retry handler", "", "execute", "medium", WorkMode.DEEP_IMPLEMENTATION, TaskClass.BOUNDED),
    ("Audit security config", "", "", "medium", WorkMode.REVIEW, TaskClass.BOUNDED),
    ("Monitor disk usage", "", "monitor", "light", WorkMode.MONITORING, TaskClass.ATOMIC),
    ("Ship v3 release", "", "", "heavy", WorkMode.DEPLOYMENT, TaskClass.EPIC),
    ("Explore new data sources", "", "research", "heavy", WorkMode.RESEARCH, TaskClass.EPIC),
    ("Bug in trade execution", "", "repair", "medium", WorkMode.DEBUGGING, TaskClass.BOUNDED),
    ("Plan Q2 roadmap", "", "plan", "heavy", WorkMode.ANALYSIS, TaskClass.EPIC),
    ("Quick status check", "", "", "light", WorkMode.MONITORING, TaskClass.ATOMIC),
    ("Add logging to pipeline", "", "execute", "light", WorkMode.DEEP_IMPLEMENTATION, TaskClass.ATOMIC),
    ("Broken webhook handler", "", "", "medium", WorkMode.DEBUGGING, TaskClass.BOUNDED),
    ("Prune stale branches", "", "", "light", WorkMode.ADMIN, TaskClass.ATOMIC),
    ("Create API endpoint", "", "execute", "medium", WorkMode.DEEP_IMPLEMENTATION, TaskClass.BOUNDED),
    ("Rollout feature flag", "", "deploy", "light", WorkMode.DEPLOYMENT, TaskClass.ATOMIC),
    ("Memory hygiene pass", "", "", "light", WorkMode.ADMIN, TaskClass.ATOMIC),
    ("Error rate alert firing", "", "repair", "medium", WorkMode.DEBUGGING, TaskClass.BOUNDED),
    ("Study Python asyncio patterns", "", "research", "medium", WorkMode.RESEARCH, TaskClass.BOUNDED),
]


class TestSampleTasks:
    @pytest.mark.parametrize(
        "title,body,category,effort,expected_mode,expected_class",
        _SAMPLE_TASKS,
        ids=[t[0] for t in _SAMPLE_TASKS],
    )
    def test_sample_classification(self, title, body, category, effort, expected_mode, expected_class):
        result = classify_task(
            title=title,
            body=body,
            category=category,
            estimated_effort=effort,
        )
        assert result["work_mode"] == expected_mode, (
            f"Expected {expected_mode} for '{title}', got {result['work_mode']}"
        )
        assert result["task_class"] == expected_class, (
            f"Expected {expected_class} for '{title}', got {result['task_class']}"
        )
