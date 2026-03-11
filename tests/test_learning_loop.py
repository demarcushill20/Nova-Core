"""Tests for learning loop — memory capture from direct tasks + retrieval injection.

Covers:
- Direct task memory capture (non-orchestrator path)
- Keyword extraction for memory retrieval
- Memory retrieval injection into task prompts
- Round-trip: capture → retrieve cycle
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

_here = str(Path(__file__).resolve().parent.parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

from agents.memory_engine import (  # noqa: E402
    capture_direct_task_memory,
    format_retrieval_for_planner,
    retrieve_related_patterns,
)


class TestDirectTaskMemoryCapture(unittest.TestCase):
    """Test memory capture from direct worker tasks."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._base = Path(self._tmpdir) / "MEMORY"
        (self._base / "workflow_learnings").mkdir(parents=True)
        (self._base / "agent_patterns").mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_captures_successful_task(self):
        contract = {
            "summary": "Implemented user authentication with JWT",
            "files_changed": "auth.py, tests/test_auth.py",
            "verification": "ran tests: 5/5 pass",
            "confidence": "high",
        }
        path = capture_direct_task_memory(
            task_stem="0200_auth",
            task_class="code_impl",
            contract=contract,
            success=True,
            base=self._base,
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data["task_class"], "code_impl")
        self.assertIn("JWT", data["task_summary"])
        self.assertEqual(data["verification_outcome"], "approved")
        self.assertEqual(data["confidence"], "high")
        self.assertEqual(data["roles_involved"], ["direct_worker"])

    def test_captures_failed_task(self):
        contract = {
            "summary": "Attempted database migration",
            "files_changed": "none",
            "verification": "not run",
            "confidence": "low",
        }
        path = capture_direct_task_memory(
            task_stem="0201_migrate",
            task_class="code_impl",
            contract=contract,
            success=False,
            base=self._base,
        )
        self.assertIsNotNone(path)
        data = json.loads(path.read_text())
        self.assertEqual(data["verification_outcome"], "rejected")
        self.assertTrue(len(data["failure_patterns"]) > 0)
        self.assertEqual(len(data["successful_patterns"]), 0)

    def test_skips_empty_summary(self):
        contract = {"summary": "", "files_changed": "none"}
        path = capture_direct_task_memory(
            task_stem="0202_empty",
            task_class="simple",
            contract=contract,
            success=True,
            base=self._base,
        )
        self.assertIsNone(path)

    def test_handles_unknown_task_class(self):
        contract = {
            "summary": "Did something",
            "files_changed": "none",
            "verification": "not run",
            "confidence": "medium",
        }
        path = capture_direct_task_memory(
            task_stem="0203_unknown",
            task_class="invalid_class",
            contract=contract,
            success=True,
            base=self._base,
        )
        self.assertIsNotNone(path)
        data = json.loads(path.read_text())
        self.assertEqual(data["task_class"], "unknown")

    def test_handles_invalid_confidence(self):
        contract = {
            "summary": "Did something",
            "files_changed": "none",
            "verification": "ok",
            "confidence": "super_high",
        }
        path = capture_direct_task_memory(
            task_stem="0204_conf",
            task_class="research",
            contract=contract,
            success=True,
            base=self._base,
        )
        self.assertIsNotNone(path)
        data = json.loads(path.read_text())
        self.assertEqual(data["confidence"], "medium")

    def test_no_duplicate_capture(self):
        contract = {
            "summary": "Task A",
            "files_changed": "none",
            "verification": "ok",
            "confidence": "medium",
        }
        path1 = capture_direct_task_memory(
            task_stem="0205_dup",
            task_class="research",
            contract=contract,
            success=True,
            base=self._base,
        )
        # Second capture with same stem should return None (artifact exists)
        path2 = capture_direct_task_memory(
            task_stem="0205_dup",
            task_class="research",
            contract=contract,
            success=True,
            base=self._base,
        )
        self.assertIsNotNone(path1)
        self.assertIsNone(path2)


class TestKeywordExtraction(unittest.TestCase):
    """Test keyword extraction for memory retrieval."""

    def test_extracts_meaningful_words(self):
        # Import from watcher
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import importlib

        import watcher
        importlib.reload(watcher)

        keywords = watcher._extract_keywords(
            "Implement user authentication with JWT tokens and OAuth2 flow"
        )
        self.assertIn("implement", keywords)
        self.assertIn("authentication", keywords)
        self.assertIn("tokens", keywords)

    def test_filters_stopwords(self):
        import watcher
        keywords = watcher._extract_keywords(
            "Create a new function for the user authentication"
        )
        self.assertNotIn("the", keywords)
        self.assertNotIn("for", keywords)
        self.assertNotIn("create", keywords)

    def test_caps_at_max(self):
        import watcher
        keywords = watcher._extract_keywords(
            "word1 word2 word3 word4 word5 word6 word7 word8 word9 word10 "
            "word11 word12 word13 word14 word15",
            max_keywords=5,
        )
        self.assertEqual(len(keywords), 5)

    def test_empty_text(self):
        import watcher
        keywords = watcher._extract_keywords("")
        self.assertEqual(keywords, [])


class TestRoundTripLearningLoop(unittest.TestCase):
    """Test the full capture → retrieve cycle."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._base = Path(self._tmpdir) / "MEMORY"
        (self._base / "workflow_learnings").mkdir(parents=True)
        (self._base / "agent_patterns").mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_captured_artifact_is_retrievable(self):
        contract = {
            "summary": "Built REST API endpoints for user management",
            "files_changed": "api/users.py, api/routes.py",
            "verification": "ran tests: 8/8 pass",
            "confidence": "high",
        }
        capture_direct_task_memory(
            task_stem="0300_api",
            task_class="code_impl",
            contract=contract,
            success=True,
            base=self._base,
        )

        patterns = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["api", "endpoints", "user"],
            base=self._base,
        )
        self.assertEqual(len(patterns), 1)
        self.assertIn("REST API", patterns[0]["task_summary"])

    def test_retrieval_ranked_by_relevance(self):
        # Create two artifacts with different relevance
        for i, (stem, summary, cls) in enumerate([
            ("0301_auth", "Set up OAuth2 authentication", "code_impl"),
            ("0302_docs", "Wrote API documentation", "research"),
        ]):
            contract = {
                "summary": summary,
                "files_changed": "none",
                "verification": "ok",
                "confidence": "medium",
            }
            capture_direct_task_memory(
                task_stem=stem, task_class=cls,
                contract=contract, success=True, base=self._base,
            )
            time.sleep(0.01)  # ensure unique timestamps

        # Search for code_impl + auth keywords
        patterns = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["authentication", "oauth"],
            base=self._base,
        )
        self.assertGreater(len(patterns), 0)
        # The code_impl auth artifact should rank higher
        self.assertIn("OAuth2", patterns[0]["task_summary"])

    def test_format_for_planner_output(self):
        contract = {
            "summary": "Implemented caching layer with Redis",
            "files_changed": "cache.py",
            "verification": "ran tests: 3/3 pass",
            "confidence": "high",
        }
        capture_direct_task_memory(
            task_stem="0303_cache",
            task_class="code_impl",
            contract=contract,
            success=True,
            base=self._base,
        )

        patterns = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["cache", "redis"],
            base=self._base,
        )
        formatted = format_retrieval_for_planner(patterns)
        self.assertIn("Prior Related Patterns", formatted)
        self.assertIn("caching layer", formatted)
        self.assertIn("Advisory only", formatted)

    def test_no_patterns_returns_advisory(self):
        formatted = format_retrieval_for_planner([])
        self.assertIn("No prior related patterns", formatted)

    def test_failed_task_surfaces_in_retrieval(self):
        contract = {
            "summary": "Database migration failed due to schema conflict",
            "files_changed": "none",
            "verification": "not run",
            "confidence": "low",
        }
        capture_direct_task_memory(
            task_stem="0304_migrate",
            task_class="code_impl",
            contract=contract,
            success=False,
            base=self._base,
        )

        patterns = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["database", "migration"],
            base=self._base,
        )
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["verification_outcome"], "rejected")
        self.assertTrue(len(patterns[0]["failure_patterns"]) > 0)


if __name__ == "__main__":
    unittest.main()
