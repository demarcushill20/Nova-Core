# mypy: ignore-errors
"""Tests for tools/skill_optimizer/global_smoke.py — multi-shot smoke evaluation.

Validates that smoke testing uses the same multi-shot majority-vote
methodology as the rest of the skill optimizer pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tools.skill_optimizer.global_smoke import (
    _load_eval_config,
    load_smoke_tests,
    run_smoke_test,
)

_MOCK_QUERY = "tools.skill_optimizer.global_smoke.run_single_query"
_MOCK_ROOT = "tools.skill_optimizer.global_smoke.find_project_root"
_MOCK_CFG = "tools.skill_optimizer.global_smoke.THRESHOLDS_PATH"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def smoke_file(tmp_path: Path) -> Path:
    """Create a temporary smoke_test.jsonl with known queries."""
    p = tmp_path / "smoke_test.jsonl"
    p.write_text(
        json.dumps({"query": "fetch https://example.com/api", "should_trigger": True, "skill": "http-fetch"})
        + "\n"
        + json.dumps({"query": "search for Python tutorials", "should_trigger": False, "skill": "http-fetch"})
        + "\n"
        + json.dumps({"query": "get docs for React", "should_trigger": True, "skill": "context7-docs"})
        + "\n"
    )
    return p


@pytest.fixture
def thresholds_file(tmp_path: Path) -> Path:
    """Create a temporary thresholds.yaml."""
    p = tmp_path / "thresholds.yaml"
    p.write_text("eval:\n  runs_per_query: 3\n  trigger_threshold: 0.5\n  timeout_per_query: 30\n")
    return p


# ---------------------------------------------------------------------------
# TestLoadSmokeTests
# ---------------------------------------------------------------------------


class TestLoadSmokeTests:
    def test_load_from_file(self, smoke_file: Path):
        records = load_smoke_tests(smoke_file)
        assert len(records) == 3

    def test_load_missing_file(self, tmp_path: Path):
        records = load_smoke_tests(tmp_path / "nonexistent.jsonl")
        assert records == []

    def test_skip_comments_and_blanks(self, tmp_path: Path):
        p = tmp_path / "smoke.jsonl"
        p.write_text("# comment\n\n" + json.dumps({"query": "test", "skill": "a"}) + "\n")
        records = load_smoke_tests(p)
        assert len(records) == 1


# ---------------------------------------------------------------------------
# TestLoadEvalConfig
# ---------------------------------------------------------------------------


class TestLoadEvalConfig:
    def test_reads_from_yaml(self, thresholds_file: Path):
        with mock.patch(_MOCK_CFG, thresholds_file):
            cfg = _load_eval_config()
        assert cfg["runs_per_query"] == 3
        assert cfg["trigger_threshold"] == 0.5

    def test_missing_file_returns_empty(self, tmp_path: Path):
        with mock.patch(_MOCK_CFG, tmp_path / "nope.yaml"):
            cfg = _load_eval_config()
        assert cfg == {}


# ---------------------------------------------------------------------------
# TestMultiShotSmoke — core fix validation
# ---------------------------------------------------------------------------


class TestMultiShotSmoke:
    """Validate multi-shot majority-vote smoke evaluation."""

    def test_deterministic_pass_when_majority_triggers(self, smoke_file: Path):
        """2/3 triggers -> trigger_rate=0.67 >= 0.5 -> voted True -> pass."""
        call_count = {"n": 0}

        def fake_query(**kwargs):
            call_count["n"] += 1
            return call_count["n"] % 3 != 0  # True, True, False

        with (
            mock.patch(_MOCK_QUERY, side_effect=fake_query),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        http_results = [r for r in report.results if r.skill == "http-fetch"]
        pos_result = [r for r in http_results if r.expected_trigger][0]
        assert pos_result.passed is True
        assert pos_result.trigger_rate >= 0.5
        assert len(pos_result.run_outcomes) == 3

    def test_deterministic_fail_when_majority_does_not_trigger(self, smoke_file: Path):
        """0/3 triggers -> trigger_rate=0.0 < 0.5 -> voted False -> fail for should_trigger=True."""
        with (
            mock.patch(_MOCK_QUERY, return_value=False),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        http_results = [r for r in report.results if r.skill == "http-fetch"]
        pos_result = [r for r in http_results if r.expected_trigger][0]
        assert pos_result.passed is False
        assert pos_result.trigger_rate == 0.0
        assert report.has_regressions

    def test_all_runs_trigger_is_pass(self, smoke_file: Path):
        """3/3 triggers -> pass."""
        with (
            mock.patch(_MOCK_QUERY, return_value=True),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        http_results = [r for r in report.results if r.skill == "http-fetch"]
        pos_result = [r for r in http_results if r.expected_trigger][0]
        assert pos_result.passed is True
        assert pos_result.trigger_count == 3
        assert pos_result.trigger_rate == 1.0

    def test_should_not_trigger_query_passes_when_no_triggers(self, smoke_file: Path):
        """should_trigger=False + 0/3 triggers -> pass."""
        with (
            mock.patch(_MOCK_QUERY, return_value=False),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        http_results = [r for r in report.results if r.skill == "http-fetch"]
        neg_result = [r for r in http_results if not r.expected_trigger][0]
        assert neg_result.passed is True

    def test_exception_counts_as_no_trigger(self, smoke_file: Path):
        """Exceptions in individual runs count as non-trigger, not total failure."""
        call_count = {"n": 0}

        def fake_query(**kwargs):
            call_count["n"] += 1
            if call_count["n"] % 3 == 1:
                raise TimeoutError("timeout")
            return True  # 2/3 succeed and trigger

        with (
            mock.patch(_MOCK_QUERY, side_effect=fake_query),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        http_results = [r for r in report.results if r.skill == "http-fetch"]
        pos_result = [r for r in http_results if r.expected_trigger][0]
        # 2/3 triggered (1 exception = False), rate = 0.6667 >= 0.5 -> pass
        assert pos_result.passed is True
        assert pos_result.trigger_count == 2

    def test_runs_per_query_actually_executes_n_times(self, smoke_file: Path):
        """Verify that run_single_query is called runs_per_query times per smoke query."""
        call_count = {"n": 0}

        def fake_query(**kwargs):
            call_count["n"] += 1
            return True

        queries = [q for q in load_smoke_tests(smoke_file) if q.get("skill") == "http-fetch"]
        with (
            mock.patch(_MOCK_QUERY, side_effect=fake_query),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=queries,
                runs_per_query=5,
                trigger_threshold=0.5,
            )

        # 2 http-fetch queries x 5 runs each = 10 calls
        assert call_count["n"] == len(queries) * 5


# ---------------------------------------------------------------------------
# TestConfigIntegration
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    """Verify config values are actually read from thresholds.yaml."""

    def test_defaults_from_config(self, smoke_file: Path, thresholds_file: Path):
        """When no explicit params, reads runs_per_query=3 from config."""
        call_count = {"n": 0}

        def fake_query(**kwargs):
            call_count["n"] += 1
            return True

        queries = [q for q in load_smoke_tests(smoke_file) if q.get("skill") == "http-fetch"]
        with (
            mock.patch(_MOCK_CFG, thresholds_file),
            mock.patch(_MOCK_QUERY, side_effect=fake_query),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=queries,
            )

        # Config says runs_per_query=3 -> 2 queries x 3 = 6 calls
        assert call_count["n"] == len(queries) * 3
        assert report.runs_per_query == 3
        assert report.trigger_threshold == 0.5

    def test_explicit_params_override_config(self, smoke_file: Path, thresholds_file: Path):
        """Explicit runs_per_query/trigger_threshold override config."""
        call_count = {"n": 0}

        def fake_query(**kwargs):
            call_count["n"] += 1
            return True

        queries = [q for q in load_smoke_tests(smoke_file) if q.get("skill") == "http-fetch"]
        with (
            mock.patch(_MOCK_CFG, thresholds_file),
            mock.patch(_MOCK_QUERY, side_effect=fake_query),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=queries,
                runs_per_query=7,
                trigger_threshold=0.8,
            )

        assert call_count["n"] == len(queries) * 7
        assert report.runs_per_query == 7
        assert report.trigger_threshold == 0.8


# ---------------------------------------------------------------------------
# TestArtifactPersistence
# ---------------------------------------------------------------------------


class TestArtifactPersistence:
    """Verify per-run artifacts in SmokeReport.to_dict()."""

    def test_to_dict_includes_per_run_outcomes(self, smoke_file: Path):
        call_count = {"n": 0}

        def fake_query(**kwargs):
            call_count["n"] += 1
            return call_count["n"] % 2 == 0

        with (
            mock.patch(_MOCK_QUERY, side_effect=fake_query),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        d = report.to_dict()
        assert "results" in d
        assert "runs_per_query" in d
        assert "trigger_threshold" in d

        for result_entry in d["results"]:
            assert "run_outcomes" in result_entry
            assert "trigger_rate" in result_entry
            assert "runs" in result_entry
            assert "trigger_count" in result_entry
            assert len(result_entry["run_outcomes"]) == 3

    def test_regression_entries_include_run_detail(self, smoke_file: Path):
        with (
            mock.patch(_MOCK_QUERY, return_value=False),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test desc",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        d = report.to_dict()
        assert len(d["regressions"]) >= 1
        for reg in d["regressions"]:
            assert "trigger_rate" in reg
            assert "run_outcomes" in reg


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_queries(self):
        report = run_smoke_test(
            modified_skill="http-fetch",
            modified_description="test",
            smoke_queries=[],
            runs_per_query=3,
            trigger_threshold=0.5,
        )
        assert report.total_tests == 0
        assert report.passed == 0
        assert not report.has_regressions

    def test_no_matching_skill(self, smoke_file: Path):
        with (
            mock.patch(_MOCK_QUERY, return_value=True),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="nonexistent-skill",
                modified_description="test",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=3,
                trigger_threshold=0.5,
            )

        assert report.total_tests == 0

    def test_report_includes_elapsed_time(self, smoke_file: Path):
        with (
            mock.patch(_MOCK_QUERY, return_value=True),
            mock.patch(_MOCK_ROOT, return_value="/tmp"),
        ):
            report = run_smoke_test(
                modified_skill="http-fetch",
                modified_description="test",
                smoke_queries=load_smoke_tests(smoke_file),
                runs_per_query=1,
                trigger_threshold=0.5,
            )

        assert report.elapsed_seconds >= 0
