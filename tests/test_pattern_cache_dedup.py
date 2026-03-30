"""Tests for pattern-level dedup in execution analysis.

Covers:
- Pattern hash computation (determinism, order-independence, stability)
- Pattern cache storage and retrieval in SkillVersionStore
- End-to-end dedup flow in ExecutionAnalyzer
- Schema v3 migration (pattern_hash column)
- Edge cases: empty skills, expired cache, no version store
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from skills.execution_analysis import (
    EvolutionSuggestion,
    ExecutionAnalysis,
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
    return SkillVersionStore(db_path=tmp_path / "test_skills.db")


def _make_analysis(
    task_id: str = "task-001",
    skill_name: str = "test-skill",
    skill_id: str = "test-skill__imp_abc12345",
    applied: bool = True,
    completed: bool = True,
    quality: float = 0.8,
    suggestions: list | None = None,
) -> ExecutionAnalysis:
    judgments = [
        SkillJudgment(
            skill_name=skill_name,
            skill_id=skill_id,
            applied=applied,
            completed=completed,
            quality_score=quality,
        )
    ]
    return ExecutionAnalysis(
        task_id=task_id,
        task_description="Test task",
        skill_judgments=judgments,
        evolution_suggestions=suggestions or [],
        overall_quality=quality,
        analysis_timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ===================================================================
# TestPatternHashComputation
# ===================================================================


class TestPatternHashComputation:
    """Test compute_pattern_hash() determinism and properties."""

    def test_deterministic_same_inputs(self):
        """Same inputs produce same hash."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(
            ["skill-a", "skill-b"], {"success": True, "exit_code": 0}, "trace output"
        )
        h2 = ExecutionAnalyzer.compute_pattern_hash(
            ["skill-a", "skill-b"], {"success": True, "exit_code": 0}, "trace output"
        )
        assert h1 == h2

    def test_order_independent_skills(self):
        """Skill order does not affect hash."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-b", "skill-a"], {"success": True, "exit_code": 0})
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a", "skill-b"], {"success": True, "exit_code": 0})
        assert h1 == h2

    def test_different_outcome_different_hash(self):
        """Different success/failure produces different hash."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0})
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": False, "exit_code": 1})
        assert h1 != h2

    def test_different_exit_code_different_hash(self):
        """Different exit codes produce different hashes."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": False, "exit_code": 1})
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": False, "exit_code": 2})
        assert h1 != h2

    def test_different_skills_different_hash(self):
        """Different skill sets produce different hashes."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0})
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-b"], {"success": True, "exit_code": 0})
        assert h1 != h2

    def test_hash_length_is_16(self):
        """Hash is truncated to 16 hex chars."""
        h = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0})
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_whitespace_normalized_trace(self):
        """Traces differing only in whitespace produce same hash."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(
            ["skill-a"], {"success": True, "exit_code": 0}, "error   at  line  5"
        )
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0}, "error at line 5")
        assert h1 == h2

    def test_empty_skills(self):
        """Empty skill list produces a valid hash."""
        h = ExecutionAnalyzer.compute_pattern_hash([], {"success": True, "exit_code": 0})
        assert len(h) == 16

    def test_empty_trace(self):
        """Empty trace produces a valid hash."""
        h = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0}, "")
        assert len(h) == 16

    def test_trace_uses_last_512_chars(self):
        """Only the last 512 chars of trace are used for hashing."""
        common_tail = "X" * 512
        trace1 = "AAA" + common_tail
        trace2 = "BBB" + common_tail
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0}, trace1)
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": True, "exit_code": 0}, trace2)
        assert h1 == h2

    def test_trace_tail_difference_changes_hash(self):
        """Different trace tails produce different hashes."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": False, "exit_code": 1}, "error: segfault")
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": False, "exit_code": 1}, "error: timeout")
        assert h1 != h2

    def test_missing_outcome_keys_use_defaults(self):
        """Missing 'success' defaults to False, 'exit_code' to 1."""
        h1 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {})
        h2 = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": False, "exit_code": 1})
        assert h1 == h2


# ===================================================================
# TestPatternCacheStorage
# ===================================================================


class TestPatternCacheStorage:
    """Test SkillVersionStore pattern_hash storage and retrieval."""

    def test_store_and_retrieve_by_pattern(self, tmp_path):
        """Stored analysis can be retrieved by pattern hash."""
        store = _make_store(tmp_path)
        analysis = _make_analysis(task_id="t1")

        store.store_analysis(analysis, pattern_hash="abc123def4567890")

        cached = store.get_cached_analysis_by_pattern("abc123def4567890")
        assert cached is not None
        assert cached.task_id == "t1"
        assert len(cached.skill_judgments) == 1
        assert cached.skill_judgments[0].skill_name == "test-skill"
        assert cached.skill_judgments[0].completed is True

    def test_pattern_cache_miss_returns_none(self, tmp_path):
        """Non-existent pattern hash returns None."""
        store = _make_store(tmp_path)
        cached = store.get_cached_analysis_by_pattern("nonexistent1234567")
        assert cached is None

    def test_empty_pattern_hash_returns_none(self, tmp_path):
        """Empty pattern hash returns None."""
        store = _make_store(tmp_path)
        cached = store.get_cached_analysis_by_pattern("")
        assert cached is None

    def test_expired_pattern_returns_none(self, tmp_path):
        """Analysis older than max_age_hours is not returned."""
        store = _make_store(tmp_path)
        # Store analysis with old timestamp
        analysis = _make_analysis(task_id="t-old")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
        analysis.analysis_timestamp = old_time

        store.store_analysis(analysis, pattern_hash="expired12345678")

        # With default 6h max age, should miss
        cached = store.get_cached_analysis_by_pattern("expired12345678", max_age_hours=6.0)
        assert cached is None

    def test_recent_pattern_is_returned(self, tmp_path):
        """Analysis within max_age_hours is returned."""
        store = _make_store(tmp_path)
        analysis = _make_analysis(task_id="t-recent")
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        analysis.analysis_timestamp = recent_time

        store.store_analysis(analysis, pattern_hash="recent1234567890")

        cached = store.get_cached_analysis_by_pattern("recent1234567890", max_age_hours=6.0)
        assert cached is not None
        assert cached.task_id == "t-recent"

    def test_multiple_analyses_returns_most_recent(self, tmp_path):
        """When multiple analyses match, the most recent is returned."""
        store = _make_store(tmp_path)
        phash = "multi12345678901"

        # Store older analysis
        a1 = _make_analysis(task_id="t-old", quality=0.3)
        a1.analysis_timestamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        store.store_analysis(a1, pattern_hash=phash)

        # Store newer analysis
        a2 = _make_analysis(task_id="t-new", quality=0.9)
        a2.analysis_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        store.store_analysis(a2, pattern_hash=phash)

        cached = store.get_cached_analysis_by_pattern(phash)
        assert cached is not None
        assert cached.task_id == "t-new"

    def test_pattern_hash_stored_in_db(self, tmp_path):
        """Pattern hash is persisted in the execution_analyses table."""
        store = _make_store(tmp_path)
        analysis = _make_analysis(task_id="t-check")
        store.store_analysis(analysis, pattern_hash="dbcheck1234567890")

        # Direct SQL check
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pattern_hash FROM execution_analyses WHERE task_id = ?",
            ("t-check",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["pattern_hash"] == "dbcheck1234567890"

    def test_evolution_suggestions_roundtrip(self, tmp_path):
        """Evolution suggestions survive store → retrieve cycle."""
        store = _make_store(tmp_path)
        analysis = _make_analysis(
            task_id="t-evo",
            suggestions=[
                EvolutionSuggestion(
                    type="FIX",
                    target_skill_name="test-skill",
                    target_skill_id="test-skill__imp_abc12345",
                    direction="Fix timeout handling",
                    priority=1,
                )
            ],
        )
        store.store_analysis(analysis, pattern_hash="evo1234567890123")

        cached = store.get_cached_analysis_by_pattern("evo1234567890123")
        assert cached is not None
        assert len(cached.evolution_suggestions) == 1
        assert cached.evolution_suggestions[0].type == "FIX"
        assert cached.evolution_suggestions[0].direction == "Fix timeout handling"


# ===================================================================
# TestPatternCacheInAnalyzer
# ===================================================================


class TestPatternCacheInAnalyzer:
    """Test end-to-end pattern dedup in ExecutionAnalyzer."""

    def test_pattern_hash_stored_after_analyze(self, tmp_path):
        """After analyze(), _last_pattern_hash is set."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store, use_cache=False)
        analyzer.analyze(
            task_id="t1",
            task_description="Test",
            selected_skills=["test-skill"],
            execution_trace="some trace",
            outcome={"success": True, "exit_code": 0},
        )
        assert analyzer._last_pattern_hash != ""
        assert len(analyzer._last_pattern_hash) == 16

    def test_pattern_cache_hit_skips_llm(self, tmp_path):
        """When pattern cache hits, LLM is not called."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        # First: store an analysis with a known pattern hash
        skills = ["test-skill"]
        outcome = {"success": True, "exit_code": 0}
        trace = "all tests passed"
        phash = ExecutionAnalyzer.compute_pattern_hash(skills, outcome, trace)

        first_analysis = _make_analysis(task_id="t-first")
        store.store_analysis(first_analysis, pattern_hash=phash)

        # Second: analyze with same pattern — should hit cache
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True)
        with patch.object(analyzer, "_llm_analyze") as mock_llm:
            result = analyzer.analyze(
                task_id="t-second",
                task_description="Second run",
                selected_skills=skills,
                execution_trace=trace,
                outcome=outcome,
            )
            mock_llm.assert_not_called()
        assert result.task_id == "t-second"
        assert result.task_description == "Second run"

    def test_cache_disabled_skips_pattern_lookup(self, tmp_path):
        """When use_cache=False, pattern cache is not checked."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store, use_cache=False)
        # Deterministic fallback will run since no LLM
        result = analyzer.analyze(
            task_id="t-nocache",
            task_description="No cache",
            selected_skills=["test-skill"],
            execution_trace="trace",
            outcome={"success": True, "exit_code": 0},
        )
        assert result.task_id == "t-nocache"
        assert result.raw_response != "[pattern-cache-hit]"

    def test_no_version_store_skips_pattern_lookup(self):
        """When no version_store, pattern cache is not checked."""
        analyzer = ExecutionAnalyzer(version_store=None, use_cache=True)
        result = analyzer.analyze(
            task_id="t-nostore",
            task_description="No store",
            selected_skills=["skill-a"],
            execution_trace="",
            outcome={"success": True, "exit_code": 0},
        )
        assert result.task_id == "t-nostore"

    def test_store_analysis_persists_pattern_hash(self, tmp_path):
        """analyzer.store_analysis() passes pattern_hash to version store."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        analyzer = ExecutionAnalyzer(version_store=store, use_cache=False)
        result = analyzer.analyze(
            task_id="t-persist",
            task_description="Persist test",
            selected_skills=["test-skill"],
            execution_trace="some output",
            outcome={"success": True, "exit_code": 0},
        )

        # Store via analyzer (which includes pattern_hash)
        analyzer.store_analysis(result)

        # Verify pattern_hash is in DB
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pattern_hash FROM execution_analyses WHERE task_id = ?",
            ("t-persist",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["pattern_hash"] == analyzer._last_pattern_hash
        assert len(row["pattern_hash"]) == 16

    def test_expired_pattern_falls_through_to_deterministic(self, tmp_path):
        """Expired pattern cache falls through to deterministic analysis."""
        store = _make_store(tmp_path)
        sv = _make_skill_version()
        store.register_skill(sv)

        skills = ["test-skill"]
        outcome = {"success": False, "exit_code": 1}
        trace = "error: something broke"
        phash = ExecutionAnalyzer.compute_pattern_hash(skills, outcome, trace)

        # Store old analysis
        old_analysis = _make_analysis(task_id="t-old", completed=False, quality=0.2)
        old_analysis.analysis_timestamp = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        store.store_analysis(old_analysis, pattern_hash=phash)

        # Analyze — should miss cache (expired) and fall through
        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True, pattern_cache_max_age_hours=6.0)
        result = analyzer.analyze(
            task_id="t-fresh",
            task_description="Fresh analysis",
            selected_skills=skills,
            execution_trace=trace,
            outcome=outcome,
        )
        # Should be deterministic fallback (no LLM available in tests)
        assert result.task_id == "t-fresh"
        assert result.raw_response != "[pattern-cache-hit]"

    def test_pattern_cache_max_age_configurable(self, tmp_path):
        """pattern_cache_max_age_hours is respected."""
        store = _make_store(tmp_path)

        skills = ["skill-a"]
        outcome = {"success": True, "exit_code": 0}
        phash = ExecutionAnalyzer.compute_pattern_hash(skills, outcome)

        # Store analysis 30 minutes ago
        analysis = _make_analysis(task_id="t-30min")
        analysis.analysis_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        store.store_analysis(analysis, pattern_hash=phash)

        # With 1h max age — should hit
        analyzer1 = ExecutionAnalyzer(version_store=store, use_cache=True, pattern_cache_max_age_hours=1.0)
        with patch.object(analyzer1, "_llm_analyze") as mock_llm:
            analyzer1.analyze(
                task_id="t-hit",
                task_description="",
                selected_skills=skills,
                execution_trace="",
                outcome=outcome,
            )
            mock_llm.assert_not_called()

        # With 0.1h (6 min) max age — should miss (30 min > 6 min)
        analyzer2 = ExecutionAnalyzer(version_store=store, use_cache=True, pattern_cache_max_age_hours=0.1)
        result2 = analyzer2.analyze(
            task_id="t-miss",
            task_description="",
            selected_skills=skills,
            execution_trace="",
            outcome=outcome,
        )
        # Falls through to deterministic (no LLM)
        assert result2.raw_response != "[pattern-cache-hit]"

    def test_store_analysis_without_version_store_is_noop(self):
        """store_analysis with no version_store doesn't raise."""
        analyzer = ExecutionAnalyzer(version_store=None)
        analysis = _make_analysis()
        analyzer.store_analysis(analysis)  # should not raise


# ===================================================================
# TestSchemaV3Migration
# ===================================================================


class TestSchemaV3Migration:
    """Test schema v2 → v3 migration adds pattern_hash column."""

    def test_fresh_db_has_pattern_hash_column(self, tmp_path):
        """Freshly created DB has pattern_hash column."""
        _make_store(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        cursor = conn.execute("PRAGMA table_info(execution_analyses)")
        columns = [row[1] for row in cursor.fetchall()]
        conn.close()
        assert "pattern_hash" in columns

    def test_v2_db_migrated_to_v3(self, tmp_path):
        """A v2 database gets pattern_hash column added on init."""
        db_path = tmp_path / "v2_migrate.db"

        # Create a v2 database manually (without pattern_hash)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS skill_versions (
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
            );
            CREATE TABLE IF NOT EXISTS skill_lineage_parents (
                skill_id TEXT NOT NULL,
                parent_id TEXT NOT NULL,
                PRIMARY KEY (skill_id, parent_id)
            );
            CREATE TABLE IF NOT EXISTS skill_snapshots (
                skill_id TEXT PRIMARY KEY,
                content BLOB NOT NULL,
                snapshot_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_analyses (
                analysis_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT DEFAULT '',
                quality_score REAL DEFAULT 0.5,
                tool_issues TEXT DEFAULT '[]',
                evolution_type TEXT,
                evolution_direction TEXT,
                evolution_priority INTEGER,
                analyzed_at TEXT NOT NULL
            );
        """)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        # Now init SkillVersionStore which should migrate to v3
        SkillVersionStore(db_path=db_path)

        # Verify pattern_hash column exists
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(execution_analyses)")
        columns = [row[1] for row in cursor.fetchall()]
        conn.close()
        assert "pattern_hash" in columns

    def test_schema_version_is_3(self, tmp_path):
        """After init, schema version should be 3."""
        _make_store(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 3

    def test_pattern_hash_index_exists(self, tmp_path):
        """The idx_analyses_pattern index is created."""
        _make_store(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_analyses_pattern'"
        ).fetchall()
        conn.close()
        assert len(indexes) == 1


# ===================================================================
# TestPatternCacheEdgeCases
# ===================================================================


class TestPatternCacheEdgeCases:
    """Edge cases and robustness tests."""

    def test_concurrent_same_pattern_no_conflict(self, tmp_path):
        """Multiple analyses with same pattern_hash stored without conflict."""
        store = _make_store(tmp_path)
        phash = "concurrent12345678"

        for i in range(5):
            a = _make_analysis(task_id=f"t-concurrent-{i}")
            store.store_analysis(a, pattern_hash=phash)

        # All 5 should be stored (INSERT OR IGNORE with unique analysis_id)
        conn = sqlite3.connect(str(tmp_path / "test_skills.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM execution_analyses WHERE pattern_hash = ?",
            (phash,),
        ).fetchone()[0]
        conn.close()
        assert count == 5

    def test_pattern_cache_with_multiple_skills(self, tmp_path):
        """Pattern cache works with multi-skill analyses."""
        store = _make_store(tmp_path)
        phash = "multiskill12345678"

        analysis = ExecutionAnalysis(
            task_id="t-multi",
            skill_judgments=[
                SkillJudgment(
                    skill_name="skill-a", skill_id="a__imp_1", applied=True, completed=True, quality_score=0.9
                ),
                SkillJudgment(
                    skill_name="skill-b", skill_id="b__imp_2", applied=True, completed=False, quality_score=0.3
                ),
            ],
            overall_quality=0.6,
            analysis_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        store.store_analysis(analysis, pattern_hash=phash)

        cached = store.get_cached_analysis_by_pattern(phash)
        assert cached is not None
        assert len(cached.skill_judgments) == 2
        names = {j.skill_name for j in cached.skill_judgments}
        assert names == {"skill-a", "skill-b"}

    def test_pattern_cache_lookup_exception_is_nonfatal(self, tmp_path):
        """If pattern cache lookup raises, analyze falls through gracefully."""
        store = MagicMock()
        store.get_cached_analysis_by_pattern.side_effect = RuntimeError("db locked")
        store.get_active_version.return_value = None

        analyzer = ExecutionAnalyzer(version_store=store, use_cache=True)
        result = analyzer.analyze(
            task_id="t-err",
            task_description="Error test",
            selected_skills=["skill-x"],
            execution_trace="",
            outcome={"success": True, "exit_code": 0},
        )
        # Should fall through to deterministic (no LLM)
        assert result.task_id == "t-err"

    def test_pattern_hash_none_in_outcome(self):
        """Outcome with None values doesn't crash hash computation."""
        h = ExecutionAnalyzer.compute_pattern_hash(["skill-a"], {"success": None, "exit_code": None})
        assert len(h) == 16

    def test_store_analysis_exception_is_nonfatal(self):
        """store_analysis() swallows exceptions gracefully."""
        store = MagicMock()
        store.store_analysis.side_effect = RuntimeError("write failed")
        analyzer = ExecutionAnalyzer(version_store=store)
        analyzer._last_pattern_hash = "test1234567890ab"
        analysis = _make_analysis()
        # Should not raise
        analyzer.store_analysis(analysis)

    def test_cached_analysis_preserves_quality_score(self, tmp_path):
        """Overall quality score survives pattern cache roundtrip."""
        store = _make_store(tmp_path)
        phash = "quality123456789ab"
        analysis = _make_analysis(task_id="t-q", quality=0.73)
        store.store_analysis(analysis, pattern_hash=phash)

        cached = store.get_cached_analysis_by_pattern(phash)
        assert cached is not None
        assert abs(cached.overall_quality - 0.73) < 0.01
