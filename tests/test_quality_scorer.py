"""Tests for utils.quality_scorer — LLM-as-Judge quality scoring.

Phase 7, Step 7.7 of Enhancement Plan v32 (2026-03-18).
"""

from __future__ import annotations

import os
import textwrap

import pytest

from utils.quality_scorer import (
    RUBRIC,
    THRESHOLDS,
    QualityScore,
    _compute_rating,
    score_output,
    score_output_deterministic,
    score_output_llm,
)

# ======================================================================
# Rubric & threshold completeness
# ======================================================================


class TestRubricAndThresholds:
    """Every category has both rubric and thresholds defined."""

    def test_all_rubric_categories_have_thresholds(self):
        for cat in RUBRIC:
            assert cat in THRESHOLDS, f"Missing thresholds for {cat}"

    def test_all_threshold_categories_have_rubric(self):
        for cat in THRESHOLDS:
            assert cat in RUBRIC, f"Missing rubric for {cat}"

    def test_each_rubric_has_at_least_one_criterion(self):
        for cat, criteria in RUBRIC.items():
            assert len(criteria) >= 1, f"Empty rubric for {cat}"

    def test_threshold_values_ordered(self):
        for cat, t in THRESHOLDS.items():
            assert t["green"] > t["yellow"], f"{cat}: green <= yellow"
            assert t["yellow"] > t["red"], f"{cat}: yellow <= red"
            assert t["red"] == 0.0, f"{cat}: red != 0.0"

    def test_expected_categories_present(self):
        expected = {"heartbeat", "codegen", "research", "plan", "bugfix", "novatrade"}
        assert expected == set(RUBRIC.keys())

    def test_rubric_criteria_are_strings(self):
        for cat, criteria in RUBRIC.items():
            for c in criteria:
                assert isinstance(c, str), f"{cat}: criterion is not str"
                assert len(c) > 0, f"{cat}: empty criterion string"


# ======================================================================
# QualityScore dataclass
# ======================================================================


class TestQualityScore:
    """QualityScore data structure."""

    def test_fields(self):
        qs = QualityScore(
            category="heartbeat",
            score=0.9,
            rating="green",
            criteria_met=["a"],
            criteria_missed=["b"],
            method="deterministic",
        )
        assert qs.category == "heartbeat"
        assert qs.score == 0.9
        assert qs.rating == "green"
        assert qs.criteria_met == ["a"]
        assert qs.criteria_missed == ["b"]
        assert qs.method == "deterministic"

    def test_defaults(self):
        qs = QualityScore(category="test", score=0.5, rating="yellow")
        assert qs.criteria_met == []
        assert qs.criteria_missed == []
        assert qs.method == "deterministic"


# ======================================================================
# Rating computation
# ======================================================================


class TestComputeRating:
    """Threshold boundary tests."""

    def test_green_at_exact_threshold(self):
        assert _compute_rating(0.8, "heartbeat") == "green"

    def test_green_above_threshold(self):
        assert _compute_rating(1.0, "heartbeat") == "green"

    def test_yellow_at_exact_threshold(self):
        assert _compute_rating(0.6, "heartbeat") == "yellow"

    def test_yellow_between_thresholds(self):
        assert _compute_rating(0.7, "heartbeat") == "yellow"

    def test_red_below_yellow(self):
        assert _compute_rating(0.5, "heartbeat") == "red"

    def test_red_at_zero(self):
        assert _compute_rating(0.0, "heartbeat") == "red"

    def test_codegen_thresholds(self):
        # codegen: green=0.85, yellow=0.7
        assert _compute_rating(0.85, "codegen") == "green"
        assert _compute_rating(0.84, "codegen") == "yellow"
        assert _compute_rating(0.7, "codegen") == "yellow"
        assert _compute_rating(0.69, "codegen") == "red"

    def test_unknown_category_uses_defaults(self):
        # Falls back to green=0.8, yellow=0.6
        assert _compute_rating(0.8, "unknown_cat") == "green"
        assert _compute_rating(0.6, "unknown_cat") == "yellow"
        assert _compute_rating(0.3, "unknown_cat") == "red"


# ======================================================================
# Deterministic scoring — heartbeat
# ======================================================================


class TestDeterministicHeartbeat:
    """Heartbeat category deterministic scoring."""

    def test_good_heartbeat_output_green(self):
        output = textwrap.dedent("""\
            ## System Health Report
            - CPU: 45.2% | Memory: 67.8% | Disk: 32.1%
            - Uptime: 14 days, 3 hours
            - Latency: 120ms average

            ## Actionable Items
            - TODO: Rotate log files (disk usage increasing)
            - FIXME: Redis connection pool nearing limit

            ## Trends
            - Memory usage trending upward (+2.3% over 7 days)
            - Error rate declining since last patch
        """)
        result = score_output_deterministic("heartbeat", "Generate heartbeat", output)
        assert result.score >= 0.8
        assert result.rating == "green"
        assert result.method == "deterministic"
        assert len(result.criteria_met) == 3

    def test_partial_heartbeat_yellow(self):
        output = textwrap.dedent("""\
            System looks OK.
            CPU at 50%, memory usage normal.
            No issues to report.
        """)
        result = score_output_deterministic("heartbeat", "Generate heartbeat", output)
        # Has metrics but no actionable items or trends
        assert 0.0 < result.score < 1.0
        assert len(result.criteria_met) >= 1

    def test_empty_heartbeat_red(self):
        result = score_output_deterministic("heartbeat", "Generate heartbeat", "")
        assert result.score == 0.0
        assert result.rating == "red"
        assert len(result.criteria_missed) == 3

    def test_whitespace_only_red(self):
        result = score_output_deterministic("heartbeat", "input", "   \n\n  ")
        assert result.score == 0.0
        assert result.rating == "red"


# ======================================================================
# Deterministic scoring — codegen
# ======================================================================


class TestDeterministicCodegen:
    """Codegen category deterministic scoring."""

    def test_good_codegen_output_green(self):
        output = textwrap.dedent("""\
            ```python
            import os
            from dataclasses import dataclass

            @dataclass
            class Config:
                \"\"\"Application configuration.\"\"\"
                name: str
                debug: bool = False

            def load_config(path: str) -> Config:
                return Config(name="test")
            ```

            ## Tests

            ```python
            def test_load_config():
                cfg = load_config("test.yaml")
                assert cfg.name == "test"
                assert cfg.debug is False
            ```
        """)
        result = score_output_deterministic("codegen", "Write a config loader", output)
        assert result.score >= 0.85
        assert result.rating == "green"
        assert "Code is syntactically valid Python" in result.criteria_met

    def test_invalid_python_misses_syntax_criterion(self):
        output = textwrap.dedent("""\
            ```python
            def broken(
            ```
        """)
        result = score_output_deterministic("codegen", "Write code", output)
        assert "Code is syntactically valid Python" in result.criteria_missed

    def test_empty_codegen_red(self):
        result = score_output_deterministic("codegen", "Write code", "")
        assert result.score == 0.0
        assert result.rating == "red"


# ======================================================================
# Deterministic scoring — research
# ======================================================================


class TestDeterministicResearch:
    """Research category deterministic scoring."""

    def test_good_research_output_green(self):
        output = textwrap.dedent("""\
            # Research Findings: Database Comparison

            ## Key Findings
            - PostgreSQL offers superior JSON support (source: https://www.postgresql.org/docs/)
            - Redis excels for caching use cases [1]

            ## Recommendations
            - We recommend PostgreSQL for primary storage
            - Consider Redis as a caching layer
            - Should implement connection pooling

            ## References
            [1] Redis documentation, https://redis.io/docs
        """)
        result = score_output_deterministic("research", "Compare databases", output)
        assert result.score >= 0.75
        assert result.rating == "green"
        assert len(result.criteria_met) == 3

    def test_empty_research_red(self):
        result = score_output_deterministic("research", "Research topic", "")
        assert result.score == 0.0
        assert result.rating == "red"


# ======================================================================
# Deterministic scoring — plan
# ======================================================================


class TestDeterministicPlan:
    """Plan category deterministic scoring."""

    def test_good_plan_output_green(self):
        output = textwrap.dedent("""\
            # Migration Plan

            ## Phase 1: Assessment (2 days)
            - Audit current schema
            - Identify dependencies between services

            ## Phase 2: Implementation (5 days)
            - Requires Phase 1 completion
            - Migrate core tables first

            ## Phase 3: Validation (3 days)
            - After Phase 2, run integration tests
            - Estimated 3 hours for full test suite
        """)
        result = score_output_deterministic("plan", "Create migration plan", output)
        assert result.score >= 0.8
        assert result.rating == "green"
        assert len(result.criteria_met) == 3

    def test_empty_plan_red(self):
        result = score_output_deterministic("plan", "Create plan", "")
        assert result.score == 0.0
        assert result.rating == "red"

    def test_plan_without_estimates_partial(self):
        output = textwrap.dedent("""\
            ## Phase 1: Setup
            - Install dependencies
            - Requires database access

            ## Phase 2: Implementation
            - Depends on Phase 1
        """)
        result = score_output_deterministic("plan", "Create plan", output)
        # Has phases and dependencies, but no time estimates
        assert "Phases are clearly defined" in result.criteria_met
        assert "Dependencies are identified" in result.criteria_met


# ======================================================================
# Deterministic scoring — bugfix
# ======================================================================


class TestDeterministicBugfix:
    """Bugfix category deterministic scoring."""

    def test_good_bugfix_output_green(self):
        output = textwrap.dedent("""\
            ## Root Cause
            The bug was caused by a race condition in the connection pool.
            When two threads acquired connections simultaneously, the pool
            counter was not properly synchronized.

            ## Fix
            Changed the pool counter to use an atomic operation. Updated
            the lock scope to cover both acquisition and counter update.

            ```python
            def acquire(self):
                with self._lock:
                    self._count += 1
                    return self._pool.get()
            ```

            ## Tests
            ```python
            def test_concurrent_acquisition():
                pool = ConnectionPool(max_size=5)
                # Test thread safety
                assert pool.available == 5
            ```
        """)
        result = score_output_deterministic("bugfix", "Fix connection pool", output)
        assert result.score >= 0.8
        assert result.rating == "green"
        assert len(result.criteria_met) == 3

    def test_empty_bugfix_red(self):
        result = score_output_deterministic("bugfix", "Fix bug", "")
        assert result.score == 0.0
        assert result.rating == "red"


# ======================================================================
# Deterministic scoring — novatrade
# ======================================================================


class TestDeterministicNovatrade:
    """NovaTrade category deterministic scoring."""

    def test_good_novatrade_output_green(self):
        output = textwrap.dedent("""\
            # Strategy: EMA Crossover

            ## Entry Rules
            - Long entry when EMA(20) crosses above EMA(50)
            - Short entry when EMA(20) crosses below EMA(50)
            - RSI confirmation required (above 50 for longs)

            ## Exit Rules
            - Take profit at 2:1 risk-reward ratio
            - Stop-loss at 1.5% below entry

            ## Risk Parameters
            - Position size: 1% risk per trade
            - Max drawdown: 5% daily limit
            - Maximum 3 concurrent positions

            ## FTMO Compliance
            - Daily loss limit: 5% (FTMO max: 5%)
            - Max drawdown: 10% (FTMO max: 10%)
            - Minimum 10 trading days in challenge period
        """)
        result = score_output_deterministic("novatrade", "Design trading strategy", output)
        assert result.score >= 0.85
        assert result.rating == "green"
        assert len(result.criteria_met) == 3

    def test_empty_novatrade_red(self):
        result = score_output_deterministic("novatrade", "Design strategy", "")
        assert result.score == 0.0
        assert result.rating == "red"


# ======================================================================
# Edge cases & error handling
# ======================================================================


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_unknown_category_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown category"):
            score_output_deterministic("nonexistent", "input", "output")

    def test_score_always_between_0_and_1(self):
        for cat in RUBRIC:
            good = "This has metrics 50% CPU memory healthy trend declining TODO fix test_foo"
            result = score_output_deterministic(cat, "input", good)
            assert 0.0 <= result.score <= 1.0

    def test_score_always_between_0_and_1_empty(self):
        for cat in RUBRIC:
            result = score_output_deterministic(cat, "input", "")
            assert result.score == 0.0

    def test_all_categories_return_quality_score(self):
        for cat in RUBRIC:
            result = score_output_deterministic(cat, "input", "some text output")
            assert isinstance(result, QualityScore)
            assert result.category == cat
            assert result.method == "deterministic"

    def test_criteria_met_plus_missed_equals_rubric(self):
        for cat in RUBRIC:
            result = score_output_deterministic(cat, "input", "some output text")
            total = len(result.criteria_met) + len(result.criteria_missed)
            assert total == len(RUBRIC[cat]), f"{cat}: met + missed = {total}, rubric has {len(RUBRIC[cat])}"

    def test_empty_input_text_still_works(self):
        result = score_output_deterministic("heartbeat", "", "CPU: 50% | Memory: 60% | healthy trend")
        assert isinstance(result, QualityScore)
        assert result.score >= 0.0

    def test_very_long_output_does_not_crash(self):
        long_output = "healthy CPU 50% degrading trend TODO fix\n" * 10000
        result = score_output_deterministic("heartbeat", "input", long_output)
        assert isinstance(result, QualityScore)

    def test_unicode_output(self):
        output = "CPU: 50% | Memory: 60% | trend ▲ improving | TODO: fix"
        result = score_output_deterministic("heartbeat", "input", output)
        assert isinstance(result, QualityScore)

    def test_none_like_empty_output(self):
        """Empty string treated as red for all categories."""
        for cat in RUBRIC:
            result = score_output_deterministic(cat, "input", "")
            assert result.score == 0.0
            assert result.rating == "red"
            assert result.criteria_met == []
            assert result.criteria_missed == RUBRIC[cat]


# ======================================================================
# score_output (main entry point)
# ======================================================================


class TestScoreOutput:
    """Main entry point score_output()."""

    def test_deterministic_by_default(self):
        result = score_output("heartbeat", "input", "CPU: 50% | Memory: 60% | healthy | trend up | TODO fix")
        assert result.method == "deterministic"

    def test_unknown_category_raises(self):
        with pytest.raises(ValueError):
            score_output("nope", "input", "output")

    def test_use_llm_false_uses_deterministic(self):
        result = score_output(
            "heartbeat",
            "input",
            "CPU: 50% | healthy | TODO fix | trend up",
            use_llm=False,
        )
        assert result.method == "deterministic"

    def test_use_llm_true_falls_back_without_api_key(self):
        """When use_llm=True but no API key, falls back to deterministic."""
        # Temporarily remove API key if present
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            result = score_output(
                "heartbeat",
                "input",
                "CPU: 50% | Memory: 60% | healthy | trend up | TODO fix",
                use_llm=True,
            )
            # Should fall back gracefully
            assert result.method == "deterministic"
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old


# ======================================================================
# LLM scoring (requires API key)
# ======================================================================


@pytest.mark.eval_llm
class TestLLMScoring:
    """LLM-based scoring tests. Skipped without OPENAI_API_KEY."""

    @pytest.fixture(autouse=True)
    def _require_api_key(self):
        if not os.environ.get("OPENAI_API_KEY"):
            pytest.skip("OPENAI_API_KEY not set")

    @pytest.mark.asyncio
    async def test_llm_scoring_returns_quality_score(self):
        result = await score_output_llm(
            "heartbeat",
            "Generate system health report",
            "CPU: 50%, Memory: 60%, Disk: 30%. TODO: rotate logs. Trend: memory increasing.",
        )
        assert isinstance(result, QualityScore)
        assert result.method == "llm"
        assert 0.0 <= result.score <= 1.0
        assert result.rating in ("green", "yellow", "red")

    @pytest.mark.asyncio
    async def test_llm_empty_output_red(self):
        result = await score_output_llm("heartbeat", "Generate report", "")
        assert result.score == 0.0
        assert result.rating == "red"

    @pytest.mark.asyncio
    async def test_llm_unknown_category_raises(self):
        with pytest.raises(ValueError, match="Unknown category"):
            await score_output_llm("fake_category", "input", "output")

    @pytest.mark.asyncio
    async def test_llm_scoring_no_api_key_raises(self):
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                await score_output_llm("heartbeat", "input", "output")
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old


# ======================================================================
# Cross-category consistency
# ======================================================================


class TestCrossCategoryConsistency:
    """Ensure scoring is consistent across categories."""

    def test_perfect_output_scores_higher_than_empty(self):
        for cat in RUBRIC:
            empty = score_output_deterministic(cat, "input", "")
            nonempty = score_output_deterministic(cat, "input", "some text with content")
            assert nonempty.score >= empty.score, f"{cat}: non-empty <= empty"

    def test_good_output_beats_garbage(self):
        good_heartbeat = "CPU: 50% | Memory: 60% | Disk: 30% | healthy | TODO: rotate logs | trend: memory increasing"
        garbage = "asdf jkl; qwerty uiop random nonsense 12345"
        good_result = score_output_deterministic("heartbeat", "input", good_heartbeat)
        bad_result = score_output_deterministic("heartbeat", "input", garbage)
        assert good_result.score > bad_result.score

    def test_all_categories_empty_is_zero(self):
        for cat in RUBRIC:
            result = score_output_deterministic(cat, "", "")
            assert result.score == 0.0
            assert result.rating == "red"
