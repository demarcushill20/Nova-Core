"""Tests for Phase 2: Execution Feedback Loop.

Covers:
- ExecutionAnalysis schema validation (SkillJudgment, EvolutionSuggestion, ExecutionAnalysis, SkillHealth)
- ExecutionAnalyzer deterministic and LLM paths
- SkillVersionStore analysis storage and health computation
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from skills.execution_analysis import (
    EvolutionSuggestion,
    ExecutionAnalysis,
    SkillHealth,
    SkillJudgment,
)
from skills.execution_analyzer import ExecutionAnalyzer
from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
)
from skills.version_store import SkillVersionStore

# ===================================================================
# Helpers
# ===================================================================


def _make_skill_version(
    name: str = "test-skill",
    skill_id: str = "test-skill__imp_abc12345",
    selections: int = 0,
    executions: int = 0,
    completions: int = 0,
    failures: int = 0,
    fallbacks: int = 0,
    is_active: bool = True,
) -> SkillVersion:
    """Create a SkillVersion for testing."""
    return SkillVersion(
        skill_id=skill_id,
        name=name,
        description="Test skill",
        path="/skills/test-skill",
        is_active=is_active,
        lineage=SkillLineage(origin=Origin.IMPORTED),
        stats=ExecutionStats(
            selections=selections,
            executions=executions,
            completions=completions,
            failures=failures,
            fallbacks=fallbacks,
        ),
    )


def _make_store(tmp_path: Path) -> SkillVersionStore:
    """Create a SkillVersionStore with a temp database."""
    return SkillVersionStore(db_path=tmp_path / "test_skills.db")


# ===================================================================
# TestExecutionAnalysisSchema
# ===================================================================


class TestExecutionAnalysisSchema:
    """Test Pydantic schema validation for execution analysis models."""

    def test_skill_judgment_defaults(self):
        """SkillJudgment should have sensible defaults."""
        j = SkillJudgment(skill_name="my-skill")
        assert j.skill_name == "my-skill"
        assert j.skill_id == ""
        assert j.applied is False
        assert j.completed is False
        assert j.failure_reason == ""
        assert j.quality_score == 0.5
        assert j.tool_issues == []

    def test_skill_judgment_full(self):
        """SkillJudgment should accept all fields."""
        j = SkillJudgment(
            skill_name="debug-skill",
            skill_id="debug-skill__v_1_abc",
            applied=True,
            completed=True,
            failure_reason="",
            quality_score=0.95,
            tool_issues=["slow response"],
        )
        assert j.applied is True
        assert j.completed is True
        assert j.quality_score == 0.95
        assert j.tool_issues == ["slow response"]

    def test_skill_judgment_quality_score_bounds(self):
        """quality_score should be clamped to [0.0, 1.0]."""
        j = SkillJudgment(skill_name="x", quality_score=0.0)
        assert j.quality_score == 0.0
        j = SkillJudgment(skill_name="x", quality_score=1.0)
        assert j.quality_score == 1.0

        with pytest.raises(ValidationError):
            SkillJudgment(skill_name="x", quality_score=-0.1)
        with pytest.raises(ValidationError):
            SkillJudgment(skill_name="x", quality_score=1.1)

    def test_evolution_suggestion_fix(self):
        """EvolutionSuggestion with type FIX."""
        s = EvolutionSuggestion(
            type="FIX",
            target_skill_name="broken-skill",
            target_skill_id="broken__v_1_abc",
            direction="Fix the error handling",
            priority=1,
        )
        assert s.type == "FIX"
        assert s.priority == 1

    def test_evolution_suggestion_derived(self):
        """EvolutionSuggestion with type DERIVED."""
        s = EvolutionSuggestion(
            type="DERIVED",
            target_skill_name="base-skill",
            direction="Specialize for Python projects",
        )
        assert s.type == "DERIVED"
        assert s.target_skill_id == ""
        assert s.priority == 3  # default

    def test_evolution_suggestion_captured(self):
        """EvolutionSuggestion with type CAPTURED."""
        s = EvolutionSuggestion(
            type="CAPTURED",
            target_skill_name="new-pattern",
            direction="Capture the debugging workflow",
            priority=4,
        )
        assert s.type == "CAPTURED"

    def test_evolution_suggestion_priority_bounds(self):
        """priority should be clamped to [1, 5]."""
        with pytest.raises(ValidationError):
            EvolutionSuggestion(type="FIX", target_skill_name="x", direction="d", priority=0)
        with pytest.raises(ValidationError):
            EvolutionSuggestion(type="FIX", target_skill_name="x", direction="d", priority=6)

    def test_evolution_suggestion_invalid_type(self):
        """Invalid type should raise validation error."""
        with pytest.raises(ValidationError):
            EvolutionSuggestion(type="INVALID", target_skill_name="x", direction="d")

    def test_execution_analysis_minimal(self):
        """ExecutionAnalysis with only required fields."""
        a = ExecutionAnalysis(task_id="task-001")
        assert a.task_id == "task-001"
        assert a.task_description == ""
        assert a.skill_judgments == []
        assert a.evolution_suggestions == []
        assert a.overall_quality == 0.5
        assert a.analysis_timestamp == ""
        assert a.raw_response == ""

    def test_execution_analysis_full(self):
        """ExecutionAnalysis with all fields populated."""
        a = ExecutionAnalysis(
            task_id="task-002",
            task_description="Fix the bug",
            skill_judgments=[SkillJudgment(skill_name="debug", applied=True, completed=True)],
            evolution_suggestions=[
                EvolutionSuggestion(
                    type="DERIVED",
                    target_skill_name="debug",
                    direction="Add Python-specific steps",
                )
            ],
            overall_quality=0.9,
            analysis_timestamp="2026-03-29T12:00:00Z",
            raw_response='{"some": "json"}',
        )
        assert len(a.skill_judgments) == 1
        assert len(a.evolution_suggestions) == 1
        assert a.overall_quality == 0.9

    def test_execution_analysis_overall_quality_bounds(self):
        """overall_quality should be clamped to [0.0, 1.0]."""
        with pytest.raises(ValidationError):
            ExecutionAnalysis(task_id="t", overall_quality=-0.5)
        with pytest.raises(ValidationError):
            ExecutionAnalysis(task_id="t", overall_quality=1.5)

    def test_skill_health_defaults(self):
        """SkillHealth should have sensible defaults."""
        h = SkillHealth(skill_name="s", skill_id="id")
        assert h.selections == 0
        assert h.is_healthy is True
        assert h.health_issues == []
        assert h.avg_quality_score == 0.0

    def test_skill_health_unhealthy(self):
        """SkillHealth can represent an unhealthy skill."""
        h = SkillHealth(
            skill_name="bad-skill",
            skill_id="bad-id",
            selections=10,
            executions=10,
            completions=2,
            failures=8,
            completion_rate=0.2,
            is_healthy=False,
            health_issues=["Low completion rate: 0.20 (threshold: 0.40)"],
        )
        assert h.is_healthy is False
        assert len(h.health_issues) == 1


# ===================================================================
# TestExecutionAnalyzer
# ===================================================================


class TestExecutionAnalyzer:
    """Test ExecutionAnalyzer deterministic path and helpers."""

    def test_deterministic_success(self):
        """Deterministic analysis with successful outcome."""
        analyzer = ExecutionAnalyzer()
        result = analyzer._deterministic_analyze(
            task_id="t1",
            task_description="Deploy service",
            selected_skills=["deploy-skill"],
            outcome={"success": True, "exit_code": 0},
        )
        assert result.task_id == "t1"
        assert result.overall_quality == 0.8
        assert len(result.skill_judgments) == 1
        j = result.skill_judgments[0]
        assert j.skill_name == "deploy-skill"
        assert j.applied is True
        assert j.completed is True
        assert j.quality_score == 0.8
        assert j.failure_reason == ""
        assert result.evolution_suggestions == []

    def test_deterministic_failure(self):
        """Deterministic analysis with failed outcome."""
        analyzer = ExecutionAnalyzer()
        result = analyzer._deterministic_analyze(
            task_id="t2",
            task_description="Build project",
            selected_skills=["build-skill"],
            outcome={"success": False, "exit_code": 1},
        )
        assert result.overall_quality == 0.2
        j = result.skill_judgments[0]
        assert j.completed is False
        assert j.quality_score == 0.2
        assert "exit_code=1" in j.failure_reason
        # No FIX suggestion because skill_id is empty (no version store)
        assert len(result.evolution_suggestions) == 0

    def test_deterministic_failure_with_version_store(self, tmp_path):
        """Deterministic analysis should suggest FIX when skill_id is known."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store)
        result = analyzer._deterministic_analyze(
            task_id="t3",
            task_description="Run tests",
            selected_skills=["test-skill"],
            outcome={"success": False, "exit_code": 2},
        )
        assert len(result.evolution_suggestions) == 1
        s = result.evolution_suggestions[0]
        assert s.type == "FIX"
        assert s.target_skill_id == sv.skill_id
        assert s.priority == 2

    def test_deterministic_no_skills(self):
        """Deterministic analysis with empty skill list."""
        analyzer = ExecutionAnalyzer()
        result = analyzer._deterministic_analyze(
            task_id="t4",
            task_description="Empty task",
            selected_skills=[],
            outcome={"success": True, "exit_code": 0},
        )
        assert result.skill_judgments == []
        assert result.evolution_suggestions == []
        assert result.overall_quality == 0.8

    def test_deterministic_multiple_skills(self):
        """Deterministic analysis with multiple skills."""
        analyzer = ExecutionAnalyzer()
        result = analyzer._deterministic_analyze(
            task_id="t5",
            task_description="Complex task",
            selected_skills=["skill-a", "skill-b", "skill-c"],
            outcome={"success": True, "exit_code": 0},
        )
        assert len(result.skill_judgments) == 3
        for j in result.skill_judgments:
            assert j.applied is True
            assert j.completed is True

    def test_deterministic_sets_timestamp(self):
        """Deterministic analysis should set analysis_timestamp."""
        analyzer = ExecutionAnalyzer()
        result = analyzer._deterministic_analyze(
            task_id="t6",
            task_description="Timed",
            selected_skills=["s"],
            outcome={"success": True, "exit_code": 0},
        )
        assert result.analysis_timestamp != ""
        # Should be a valid ISO 8601 timestamp
        from datetime import datetime

        datetime.fromisoformat(result.analysis_timestamp)

    def test_analyze_falls_back_to_deterministic(self):
        """analyze() should fall back to deterministic when LLM fails."""
        analyzer = ExecutionAnalyzer()
        # Force LLM path to fail by mocking the import
        with patch.object(analyzer, "_llm_analyze", side_effect=RuntimeError("no LLM")):
            result = analyzer.analyze(
                task_id="t7",
                task_description="Fallback test",
                selected_skills=["skill-x"],
                execution_trace="some trace",
                outcome={"success": True, "exit_code": 0},
            )
        assert result.task_id == "t7"
        assert result.overall_quality == 0.8

    def test_update_stats_increments_counters(self, tmp_path):
        """update_stats should increment version store counters."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store)
        analysis = ExecutionAnalysis(
            task_id="t8",
            skill_judgments=[
                SkillJudgment(
                    skill_name="test-skill",
                    skill_id=sv.skill_id,
                    applied=True,
                    completed=True,
                ),
            ],
        )
        analyzer.update_stats(analysis)

        updated = store.get_skill(sv.skill_id)
        assert updated.stats.executions == 1
        assert updated.stats.completions == 1
        assert updated.stats.failures == 0

    def test_update_stats_failure_counters(self, tmp_path):
        """update_stats should increment failures for failed skills."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store)
        analysis = ExecutionAnalysis(
            task_id="t9",
            skill_judgments=[
                SkillJudgment(
                    skill_name="test-skill",
                    skill_id=sv.skill_id,
                    applied=True,
                    completed=False,
                ),
            ],
        )
        analyzer.update_stats(analysis)

        updated = store.get_skill(sv.skill_id)
        assert updated.stats.executions == 1
        assert updated.stats.completions == 0
        assert updated.stats.failures == 1

    def test_update_stats_fallback_counters(self, tmp_path):
        """update_stats should increment fallbacks for unapplied skills."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store)
        analysis = ExecutionAnalysis(
            task_id="t10",
            skill_judgments=[
                SkillJudgment(
                    skill_name="test-skill",
                    skill_id=sv.skill_id,
                    applied=False,
                    completed=False,
                ),
            ],
        )
        analyzer.update_stats(analysis)

        updated = store.get_skill(sv.skill_id)
        assert updated.stats.executions == 0
        assert updated.stats.fallbacks == 1

    def test_update_stats_skips_empty_skill_id(self, tmp_path):
        """update_stats should skip judgments without skill_id."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store)
        analysis = ExecutionAnalysis(
            task_id="t11",
            skill_judgments=[
                SkillJudgment(
                    skill_name="unknown-skill",
                    skill_id="",  # no skill_id
                    applied=True,
                    completed=True,
                ),
            ],
        )
        # Should not raise
        analyzer.update_stats(analysis)

        # Original skill should be unchanged
        updated = store.get_skill(sv.skill_id)
        assert updated.stats.executions == 0

    def test_update_stats_no_version_store(self):
        """update_stats with version_store=None should be a no-op."""
        analyzer = ExecutionAnalyzer(version_store=None)
        analysis = ExecutionAnalysis(
            task_id="t12",
            skill_judgments=[
                SkillJudgment(
                    skill_name="s",
                    skill_id="some-id",
                    applied=True,
                    completed=True,
                ),
            ],
        )
        # Should not raise
        analyzer.update_stats(analysis)

    def test_build_user_prompt_truncation(self):
        """_build_user_prompt should truncate long traces from the tail (M2)."""
        analyzer = ExecutionAnalyzer()
        # Build a trace where start and end are distinguishable
        long_trace = "START" + "x" * 10000 + "END_MARKER"
        prompt = analyzer._build_user_prompt(
            task_description="Test",
            selected_skills=["s1", "s2"],
            execution_trace=long_trace,
            outcome={"success": True},
        )
        # Trace should be truncated to last 4000 chars (tail)
        assert "x" * 4001 not in prompt
        assert "s1, s2" in prompt
        assert "Test" in prompt
        # M2: Should contain the end, not the start
        assert "END_MARKER" in prompt
        assert "START" not in prompt

    def test_build_user_prompt_empty_trace(self):
        """_build_user_prompt should handle empty trace."""
        analyzer = ExecutionAnalyzer()
        prompt = analyzer._build_user_prompt(
            task_description="Test",
            selected_skills=[],
            execution_trace="",
            outcome={"success": True},
        )
        assert "(no trace)" in prompt
        assert "(none)" in prompt

    def test_build_system_prompt(self):
        """_build_system_prompt should return a non-empty prompt."""
        analyzer = ExecutionAnalyzer()
        prompt = analyzer._build_system_prompt()
        assert "execution analyzer" in prompt.lower()
        assert "FIX" in prompt
        assert "DERIVED" in prompt
        assert "CAPTURED" in prompt

    def test_llm_analyze_mocked(self, tmp_path):
        """Test LLM analysis path with mocked dependencies."""
        store = _make_store(tmp_path)
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True)

        mock_analysis = ExecutionAnalysis(
            task_id="placeholder",
            skill_judgments=[
                SkillJudgment(
                    skill_name="test-skill",
                    applied=True,
                    completed=True,
                    quality_score=0.9,
                )
            ],
            overall_quality=0.9,
        )

        llm_response_json = mock_analysis.model_dump_json()

        mock_cache_result = {"response": llm_response_json}

        with (
            patch("utils.llm_cache.cached_llm_call", return_value=mock_cache_result),
            patch("utils.structured_output.parse_and_validate", return_value=mock_analysis),
        ):
            result = analyzer._llm_analyze(
                task_id="llm-task-1",
                task_description="Test LLM path",
                selected_skills=["test-skill"],
                execution_trace="some trace output",
                outcome={"success": True, "exit_code": 0},
            )

        assert result.task_id == "llm-task-1"
        assert result.task_description == "Test LLM path"
        assert result.analysis_timestamp != ""

    def test_system_prompt_included_in_llm_call(self, tmp_path):
        """C1 FIX: System prompt must be included in the subprocess call."""
        store = _make_store(tmp_path)
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True)

        mock_analysis = ExecutionAnalysis(
            task_id="placeholder",
            skill_judgments=[SkillJudgment(skill_name="s", applied=True, completed=True)],
            overall_quality=0.9,
        )

        captured_call_fn = {}

        def capture_cached_llm_call(call_fn, **kwargs):
            captured_call_fn["fn"] = call_fn
            return {"response": mock_analysis.model_dump_json()}

        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = json.dumps({"result": "ok"})

        with (
            patch("utils.llm_cache.cached_llm_call", side_effect=capture_cached_llm_call),
            patch("utils.structured_output.parse_and_validate", return_value=mock_analysis),
            patch("subprocess.run", return_value=mock_subprocess_result) as mock_run,
        ):
            analyzer._llm_analyze(
                task_id="c1-test",
                task_description="System prompt test",
                selected_skills=["s"],
                execution_trace="trace",
                outcome={"success": True, "exit_code": 0},
            )
            # Call the captured call_fn to trigger subprocess.run
            captured_call_fn["fn"]()

        # Verify subprocess.run was called with the full prompt that contains the system prompt
        call_args = mock_run.call_args
        prompt_arg = call_args[0][0][3]  # ["claude", "--print", "-p", <prompt>, ...]
        system_prompt = analyzer._build_system_prompt()
        assert system_prompt in prompt_arg, "System prompt must be embedded in the LLM call prompt"
        assert "---" in prompt_arg, "Separator between system and user prompt expected"

    def test_use_cache_false_bypasses_cache(self, tmp_path):
        """H2 FIX: use_cache=False should call LLM directly, not cached_llm_call."""
        store = _make_store(tmp_path)
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=False)

        mock_analysis = ExecutionAnalysis(
            task_id="placeholder",
            skill_judgments=[SkillJudgment(skill_name="s", applied=True, completed=True)],
            overall_quality=0.9,
        )

        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = mock_analysis.model_dump_json()

        with (
            patch("subprocess.run", return_value=mock_subprocess_result),
            patch("utils.structured_output.parse_and_validate", return_value=mock_analysis),
        ):
            result = analyzer._llm_analyze(
                task_id="h2-test",
                task_description="No cache test",
                selected_skills=["s"],
                execution_trace="trace",
                outcome={"success": True, "exit_code": 0},
            )

        assert result.task_id == "h2-test"
        # cached_llm_call was never imported/called (use_cache=False)

    def test_use_cache_false_does_not_import_llm_cache(self, tmp_path):
        """H2 FIX: use_cache=False should not call cached_llm_call."""
        store = _make_store(tmp_path)
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=False)

        mock_analysis = ExecutionAnalysis(
            task_id="placeholder",
            skill_judgments=[SkillJudgment(skill_name="s", applied=True, completed=True)],
            overall_quality=0.9,
        )

        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = mock_analysis.model_dump_json()

        with (
            patch("utils.llm_cache.cached_llm_call") as mock_cache,
            patch("subprocess.run", return_value=mock_subprocess_result),
            patch("utils.structured_output.parse_and_validate", return_value=mock_analysis),
        ):
            analyzer._llm_analyze(
                task_id="h2-test-2",
                task_description="No cache verify",
                selected_skills=["s"],
                execution_trace="trace",
                outcome={"success": True, "exit_code": 0},
            )

        mock_cache.assert_not_called()

    def test_llm_analyze_parse_failure_raises_for_outer_fallback(self, tmp_path):
        """LLM path should raise ValueError when parse fails (H4 fix).

        The outer analyze() method catches this and falls back to deterministic.
        Previously _llm_analyze had its own inner fallback, creating two paths.
        """
        store = _make_store(tmp_path)
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True)

        mock_cache_result = {"response": "not valid json at all"}

        with (
            patch("utils.llm_cache.cached_llm_call", return_value=mock_cache_result),
            patch("utils.structured_output.parse_and_validate", return_value=None),
            pytest.raises(ValueError, match="Structured extraction returned None"),
        ):
            analyzer._llm_analyze(
                task_id="llm-task-2",
                task_description="Parse failure test",
                selected_skills=["skill-a"],
                execution_trace="trace",
                outcome={"success": False, "exit_code": 1},
            )

    def test_analyze_falls_back_on_parse_failure(self, tmp_path):
        """analyze() should fall back to deterministic when parse_and_validate returns None."""
        store = _make_store(tmp_path)
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True)

        mock_cache_result = {"response": "not valid json at all"}

        with (
            patch("utils.llm_cache.cached_llm_call", return_value=mock_cache_result),
            patch("utils.structured_output.parse_and_validate", return_value=None),
        ):
            result = analyzer.analyze(
                task_id="llm-task-2",
                task_description="Parse failure test",
                selected_skills=["skill-a"],
                execution_trace="trace",
                outcome={"success": False, "exit_code": 1},
            )

        # Should have fallen back to deterministic via outer try/except
        assert result.task_id == "llm-task-2"
        assert result.overall_quality == 0.2  # deterministic failure score


# ===================================================================
# TestVersionStoreAnalyses
# ===================================================================


class TestVersionStoreAnalyses:
    """Test execution analysis storage and health computation in SkillVersionStore."""

    def test_store_analysis_single_judgment(self, tmp_path):
        """store_analysis should save a single judgment to the database."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-100",
            analysis_timestamp="2026-03-29T10:00:00Z",
            skill_judgments=[
                SkillJudgment(
                    skill_name="deploy",
                    skill_id="deploy__imp_abc",
                    applied=True,
                    completed=True,
                    quality_score=0.85,
                ),
            ],
        )
        store.store_analysis(analysis)

        # Verify in database
        results = store.get_analyses_for_skill("deploy__imp_abc")
        assert len(results) == 1
        r = results[0]
        assert r["task_id"] == "task-100"
        assert r["skill_name"] == "deploy"
        assert r["applied"] is True
        assert r["completed"] is True
        assert r["quality_score"] == 0.85

    def test_store_analysis_multiple_judgments(self, tmp_path):
        """store_analysis should save multiple judgments from one analysis."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-101",
            analysis_timestamp="2026-03-29T11:00:00Z",
            skill_judgments=[
                SkillJudgment(skill_name="s1", skill_id="id1", applied=True, completed=True),
                SkillJudgment(skill_name="s2", skill_id="id2", applied=True, completed=False),
                SkillJudgment(skill_name="s3", skill_id="id3", applied=False, completed=False),
            ],
        )
        store.store_analysis(analysis)

        assert len(store.get_analyses_for_skill("id1")) == 1
        assert len(store.get_analyses_for_skill("id2")) == 1
        assert len(store.get_analyses_for_skill("id3")) == 1

    def test_store_analysis_with_evolution_suggestion(self, tmp_path):
        """store_analysis should store matching evolution suggestions."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-102",
            analysis_timestamp="2026-03-29T12:00:00Z",
            skill_judgments=[
                SkillJudgment(
                    skill_name="buggy-skill",
                    skill_id="buggy__v_1",
                    applied=True,
                    completed=False,
                    failure_reason="Timeout",
                ),
            ],
            evolution_suggestions=[
                EvolutionSuggestion(
                    type="FIX",
                    target_skill_name="buggy-skill",
                    target_skill_id="buggy__v_1",
                    direction="Add timeout handling",
                    priority=1,
                ),
            ],
        )
        store.store_analysis(analysis)

        results = store.get_analyses_for_skill("buggy__v_1")
        assert len(results) == 1
        r = results[0]
        assert r["evolution_type"] == "FIX"
        assert r["evolution_direction"] == "Add timeout handling"
        assert r["evolution_priority"] == 1

    def test_store_analysis_empty_skill_id(self, tmp_path):
        """store_analysis should handle empty skill_id."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-103",
            analysis_timestamp="2026-03-29T13:00:00Z",
            skill_judgments=[
                SkillJudgment(skill_name="unknown", skill_id="", applied=True, completed=True),
            ],
        )
        # Should not raise
        store.store_analysis(analysis)
        results = store.get_analyses_for_skill("")
        assert len(results) == 1

    def test_get_analyses_for_skill_respects_limit(self, tmp_path):
        """get_analyses_for_skill should respect the limit parameter."""
        store = _make_store(tmp_path)
        for i in range(10):
            analysis = ExecutionAnalysis(
                task_id=f"task-{i}",
                analysis_timestamp=f"2026-03-29T{10 + i:02d}:00:00Z",
                skill_judgments=[
                    SkillJudgment(skill_name="s", skill_id="skill-x", applied=True, completed=True),
                ],
            )
            store.store_analysis(analysis)

        # Default limit
        results = store.get_analyses_for_skill("skill-x")
        assert len(results) == 10

        # Custom limit
        results = store.get_analyses_for_skill("skill-x", limit=3)
        assert len(results) == 3

    def test_get_analyses_for_skill_order(self, tmp_path):
        """get_analyses_for_skill should return results in reverse chronological order."""
        store = _make_store(tmp_path)
        for i in range(3):
            analysis = ExecutionAnalysis(
                task_id=f"task-{i}",
                analysis_timestamp=f"2026-03-29T{10 + i:02d}:00:00Z",
                skill_judgments=[
                    SkillJudgment(skill_name="s", skill_id="skill-y", applied=True, completed=True),
                ],
            )
            store.store_analysis(analysis)

        results = store.get_analyses_for_skill("skill-y")
        assert results[0]["analyzed_at"] > results[1]["analyzed_at"]
        assert results[1]["analyzed_at"] > results[2]["analyzed_at"]

    def test_get_analyses_for_nonexistent_skill(self, tmp_path):
        """get_analyses_for_skill should return empty list for unknown skill."""
        store = _make_store(tmp_path)
        results = store.get_analyses_for_skill("nonexistent")
        assert results == []

    def test_get_skill_health_basic(self, tmp_path):
        """get_skill_health should compute rates correctly."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(
            selections=10,
            executions=8,
            completions=6,
            failures=2,
            fallbacks=2,
        )
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 1
        h = health[0]
        assert h.skill_name == "test-skill"
        assert h.selections == 10
        assert h.applied_rate == 0.8  # 8/10
        assert h.completion_rate == 0.75  # 6/8
        assert h.effective_rate == 0.6  # 6/10
        assert h.fallback_rate == 0.2  # 2/10
        assert h.is_healthy is True

    def test_get_skill_health_zero_selections(self, tmp_path):
        """get_skill_health should skip skills below min_selections."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(selections=2)
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 0

    def test_get_skill_health_zero_selections_threshold(self, tmp_path):
        """get_skill_health with min_selections=0 should include all skills."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(selections=0)
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=0)
        assert len(health) == 1
        h = health[0]
        # All rates should be 0 (no division error)
        assert h.applied_rate == 0.0
        assert h.completion_rate == 0.0
        assert h.effective_rate == 0.0
        assert h.fallback_rate == 0.0

    def test_get_skill_health_flags_unhealthy_completion(self, tmp_path):
        """get_skill_health should flag low completion rate."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(
            selections=10,
            executions=10,
            completions=3,  # 30% completion rate < 40% threshold
            failures=7,
        )
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 1
        h = health[0]
        assert h.is_healthy is False
        assert any("completion rate" in issue.lower() for issue in h.health_issues)

    def test_get_skill_health_flags_unhealthy_fallback(self, tmp_path):
        """get_skill_health should flag high fallback rate."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(
            selections=10,
            executions=4,
            completions=4,
            fallbacks=6,  # 60% fallback rate > 50% threshold
        )
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 1
        h = health[0]
        assert h.is_healthy is False
        assert any("fallback rate" in issue.lower() for issue in h.health_issues)

    def test_get_skill_health_with_avg_quality(self, tmp_path):
        """get_skill_health should include avg_quality_score from analyses."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(selections=10, executions=10, completions=10)
        store.register_skill(sv)

        # Store some analyses with varying quality scores
        for qs in [0.6, 0.8, 1.0]:
            analysis = ExecutionAnalysis(
                task_id=f"task-q-{qs}",
                analysis_timestamp="2026-03-29T10:00:00Z",
                skill_judgments=[
                    SkillJudgment(
                        skill_name="test-skill",
                        skill_id=sv.skill_id,
                        applied=True,
                        completed=True,
                        quality_score=qs,
                    ),
                ],
            )
            store.store_analysis(analysis)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 1
        h = health[0]
        assert 0.79 <= h.avg_quality_score <= 0.81  # avg of 0.6, 0.8, 1.0 = 0.8

    def test_get_skill_health_no_analyses(self, tmp_path):
        """get_skill_health should return avg_quality=0 when no analyses exist."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(selections=10, executions=10, completions=10)
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 1
        h = health[0]
        assert h.avg_quality_score == 0.0

    def test_get_skill_health_excludes_inactive(self, tmp_path):
        """get_skill_health should only include active skills."""
        store = _make_store(tmp_path)
        sv = _make_skill_version(selections=10, executions=10, completions=10, is_active=False)
        store.register_skill(sv)

        health = store.get_skill_health(min_selections=5)
        assert len(health) == 0

    def test_execution_analyses_table_exists(self, tmp_path):
        """The execution_analyses table should be created on init."""
        _make_store(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='execution_analyses'")
            tables = cursor.fetchall()
            assert len(tables) == 1
        finally:
            conn.close()

    def test_schema_migration_v1_to_v2(self, tmp_path):
        """C2 FIX: Opening an existing v1 database should migrate to v2."""
        db_path = tmp_path / "migrate_test.db"

        # Create a v1 database manually (only skill_versions, no execution_analyses)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS skill_versions (
            skill_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            path TEXT,
            is_active INTEGER DEFAULT 1,
            version TEXT DEFAULT '1.0.0',
            origin TEXT NOT NULL,
            generation INTEGER DEFAULT 0,
            change_summary TEXT,
            content_diff TEXT,
            selections INTEGER DEFAULT 0,
            executions INTEGER DEFAULT 0,
            completions INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            fallbacks INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            created_by TEXT DEFAULT 'system'
        )""")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        # Now open with SkillVersionStore — should migrate to v2
        SkillVersionStore(db_path=db_path)

        # Verify execution_analyses table was created
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='execution_analyses'")
        tables = cursor.fetchall()
        assert len(tables) == 1, "execution_analyses table should exist after migration"

        # Verify schema version is now current (v3 with pattern_hash)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3
        conn.close()

    def test_store_analysis_multiple_evolution_suggestions_per_skill(self, tmp_path):
        """H1 FIX: store_analysis should store ALL evolution suggestions per skill, not just the first."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-h1",
            analysis_timestamp="2026-03-29T15:00:00Z",
            skill_judgments=[
                SkillJudgment(
                    skill_name="multi-evo-skill",
                    skill_id="multi__v_1",
                    applied=True,
                    completed=False,
                    failure_reason="Multiple issues",
                ),
            ],
            evolution_suggestions=[
                EvolutionSuggestion(
                    type="FIX",
                    target_skill_name="multi-evo-skill",
                    target_skill_id="multi__v_1",
                    direction="Fix timeout handling",
                    priority=1,
                ),
                EvolutionSuggestion(
                    type="FIX",
                    target_skill_name="multi-evo-skill",
                    target_skill_id="multi__v_1",
                    direction="Fix error recovery",
                    priority=2,
                ),
                EvolutionSuggestion(
                    type="DERIVED",
                    target_skill_name="multi-evo-skill",
                    target_skill_id="multi__v_1",
                    direction="Specialize for async tasks",
                    priority=3,
                ),
            ],
        )
        store.store_analysis(analysis)

        results = store.get_analyses_for_skill("multi__v_1")
        assert len(results) == 1
        r = results[0]
        # Should have all 3 suggestions accessible
        assert "evolution_suggestions" in r
        assert len(r["evolution_suggestions"]) == 3
        types = [s["type"] for s in r["evolution_suggestions"]]
        assert "FIX" in types
        assert "DERIVED" in types
        # Legacy field should still have the first suggestion's type
        assert r["evolution_type"] == "FIX"

    def test_store_analysis_single_suggestion_no_json_array(self, tmp_path):
        """Single suggestion should be stored as plain string in evolution_direction."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-h1-single",
            analysis_timestamp="2026-03-29T15:30:00Z",
            skill_judgments=[
                SkillJudgment(
                    skill_name="single-evo",
                    skill_id="single__v_1",
                    applied=True,
                    completed=False,
                ),
            ],
            evolution_suggestions=[
                EvolutionSuggestion(
                    type="FIX",
                    target_skill_name="single-evo",
                    target_skill_id="single__v_1",
                    direction="Fix the bug",
                    priority=1,
                ),
            ],
        )
        store.store_analysis(analysis)

        results = store.get_analyses_for_skill("single__v_1")
        assert len(results) == 1
        r = results[0]
        assert r["evolution_type"] == "FIX"
        assert r["evolution_direction"] == "Fix the bug"
        assert r["evolution_priority"] == 1
        # Single suggestion: evolution_suggestions should be empty list
        # (plain string is not JSON array)
        assert r["evolution_suggestions"] == []

    def test_store_analysis_tool_issues_serialized(self, tmp_path):
        """tool_issues should be serialized as JSON in the database."""
        store = _make_store(tmp_path)
        analysis = ExecutionAnalysis(
            task_id="task-tools",
            analysis_timestamp="2026-03-29T14:00:00Z",
            skill_judgments=[
                SkillJudgment(
                    skill_name="s",
                    skill_id="s-id",
                    applied=True,
                    completed=False,
                    tool_issues=["timeout on API call", "missing dependency"],
                ),
            ],
        )
        store.store_analysis(analysis)

        results = store.get_analyses_for_skill("s-id")
        assert len(results) == 1
        assert results[0]["tool_issues"] == ["timeout on API call", "missing dependency"]
