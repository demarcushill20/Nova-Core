"""Tests for skills.skill_cache — Warm Reuse and Token Efficiency.

~25 tests covering:
- TokenStats creation, savings calculations, serialization
- SkillCache CRUD, persistence, aggregate savings
- Cache key generation (determinism, uniqueness, format)
- Progressive disclosure header formatting
- Execution template building and truncation
"""

from __future__ import annotations

from pathlib import Path

from skills.skill_cache import (
    DEFAULT_CACHE_TTL,
    SKILL_CACHE_TTL,
    SkillCache,
    TokenStats,
)

# ---------------------------------------------------------------------------
# TokenStats
# ---------------------------------------------------------------------------


class TestTokenStats:
    """Tests for the TokenStats dataclass."""

    def test_creation_defaults(self):
        """TokenStats initialises with zero counters."""
        ts = TokenStats(skill_name="test-skill")
        assert ts.skill_name == "test-skill"
        assert ts.cold_tokens == 0
        assert ts.warm_tokens == 0
        assert ts.cold_count == 0
        assert ts.warm_count == 0

    def test_savings_ratio_normal(self):
        """savings_ratio computes correctly when both cold and warm exist."""
        ts = TokenStats(
            skill_name="s",
            cold_tokens=1000,
            cold_count=10,  # cold avg = 100
            warm_tokens=300,
            warm_count=10,  # warm avg = 30
        )
        # 1 - 30/100 = 0.7
        assert abs(ts.savings_ratio - 0.7) < 1e-9

    def test_savings_ratio_zero_cold_count(self):
        """savings_ratio is 0.0 when there are no cold executions."""
        ts = TokenStats(
            skill_name="s",
            cold_tokens=0,
            cold_count=0,
            warm_tokens=100,
            warm_count=5,
        )
        assert ts.savings_ratio == 0.0

    def test_savings_ratio_zero_warm_count(self):
        """savings_ratio is 0.0 when there are no warm executions."""
        ts = TokenStats(
            skill_name="s",
            cold_tokens=500,
            cold_count=5,
            warm_tokens=0,
            warm_count=0,
        )
        assert ts.savings_ratio == 0.0

    def test_savings_ratio_zero_cold_avg(self):
        """savings_ratio is 0.0 when cold average is zero (pathological)."""
        ts = TokenStats(
            skill_name="s",
            cold_tokens=0,
            cold_count=5,
            warm_tokens=100,
            warm_count=5,
        )
        assert ts.savings_ratio == 0.0

    def test_savings_ratio_warm_greater_than_cold(self):
        """savings_ratio is clamped to 0.0 when warm avg exceeds cold avg."""
        ts = TokenStats(
            skill_name="s",
            cold_tokens=100,
            cold_count=10,  # cold avg = 10
            warm_tokens=500,
            warm_count=10,  # warm avg = 50
        )
        # 1 - 50/10 = -4.0 → clamped to 0.0
        assert ts.savings_ratio == 0.0

    def test_total_tokens_saved(self):
        """total_tokens_saved estimates correctly."""
        ts = TokenStats(
            skill_name="s",
            cold_tokens=1000,
            cold_count=10,  # cold avg = 100
            warm_tokens=200,
            warm_count=5,  # would have cost 5 * 100 = 500
        )
        # saved = 500 - 200 = 300
        assert ts.total_tokens_saved == 300

    def test_total_tokens_saved_no_data(self):
        """total_tokens_saved is 0 when either count is zero."""
        ts = TokenStats(skill_name="s")
        assert ts.total_tokens_saved == 0

    def test_to_dict_serialization(self):
        """to_dict includes all fields and computed properties."""
        ts = TokenStats(
            skill_name="analyzer",
            cold_tokens=2000,
            cold_count=20,
            warm_tokens=600,
            warm_count=20,
        )
        d = ts.to_dict()
        assert d["skill_name"] == "analyzer"
        assert d["cold_tokens"] == 2000
        assert d["warm_tokens"] == 600
        assert d["cold_count"] == 20
        assert d["warm_count"] == 20
        assert "savings_ratio" in d
        assert "total_tokens_saved" in d
        # savings_ratio: 1 - (30/100) = 0.7
        assert abs(d["savings_ratio"] - 0.7) < 1e-4
        # total_tokens_saved: 20*100 - 600 = 1400
        assert d["total_tokens_saved"] == 1400


# ---------------------------------------------------------------------------
# SkillCache — recording and retrieval
# ---------------------------------------------------------------------------


class TestSkillCache:
    """Tests for SkillCache token stats tracking."""

    def test_record_usage_cold(self, tmp_path: Path):
        """Recording a cold (uncached) execution increments cold counters."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        sc.record_usage("my-skill", tokens_used=150, was_cached=False)
        stats = sc.get_stats("my-skill")
        assert stats is not None
        assert stats.cold_tokens == 150
        assert stats.cold_count == 1
        assert stats.warm_tokens == 0
        assert stats.warm_count == 0

    def test_record_usage_warm(self, tmp_path: Path):
        """Recording a warm (cached) execution increments warm counters."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        sc.record_usage("my-skill", tokens_used=30, was_cached=True)
        stats = sc.get_stats("my-skill")
        assert stats is not None
        assert stats.warm_tokens == 30
        assert stats.warm_count == 1
        assert stats.cold_tokens == 0
        assert stats.cold_count == 0

    def test_record_usage_creates_new_entry(self, tmp_path: Path):
        """Recording usage for an unknown skill creates a new stats entry."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        assert sc.get_stats("new-skill") is None
        sc.record_usage("new-skill", tokens_used=100, was_cached=False)
        assert sc.get_stats("new-skill") is not None

    def test_record_usage_accumulates(self, tmp_path: Path):
        """Multiple recordings accumulate correctly."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        sc.record_usage("s", tokens_used=100, was_cached=False)
        sc.record_usage("s", tokens_used=200, was_cached=False)
        sc.record_usage("s", tokens_used=50, was_cached=True)
        stats = sc.get_stats("s")
        assert stats is not None
        assert stats.cold_tokens == 300
        assert stats.cold_count == 2
        assert stats.warm_tokens == 50
        assert stats.warm_count == 1

    def test_get_stats_missing_returns_none(self, tmp_path: Path):
        """get_stats returns None for an untracked skill."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        assert sc.get_stats("nonexistent") is None

    def test_get_all_stats(self, tmp_path: Path):
        """get_all_stats returns a copy of all tracked skills."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        sc.record_usage("a", tokens_used=10, was_cached=False)
        sc.record_usage("b", tokens_used=20, was_cached=True)
        all_stats = sc.get_all_stats()
        assert set(all_stats.keys()) == {"a", "b"}
        assert isinstance(all_stats["a"], TokenStats)

    def test_get_total_savings(self, tmp_path: Path):
        """get_total_savings aggregates across all skills."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        sc.record_usage("x", tokens_used=500, was_cached=False)
        sc.record_usage("x", tokens_used=100, was_cached=True)
        sc.record_usage("y", tokens_used=300, was_cached=False)
        sc.record_usage("y", tokens_used=50, was_cached=True)

        totals = sc.get_total_savings()
        assert totals["total_cold_tokens"] == 800
        assert totals["total_warm_tokens"] == 150
        assert totals["total_cold_executions"] == 2
        assert totals["total_warm_executions"] == 2
        assert totals["total_tokens_saved"] > 0

    def test_get_total_savings_empty(self, tmp_path: Path):
        """get_total_savings returns zeroes when no stats exist."""
        sc = SkillCache(stats_path=str(tmp_path / "stats.json"))
        totals = sc.get_total_savings()
        assert totals["total_cold_tokens"] == 0
        assert totals["total_warm_tokens"] == 0
        assert totals["total_tokens_saved"] == 0
        assert totals["overall_savings_ratio"] == 0.0

    def test_persistence_round_trip(self, tmp_path: Path):
        """Stats survive save + load cycle."""
        stats_file = str(tmp_path / "stats.json")
        sc1 = SkillCache(stats_path=stats_file)
        sc1.record_usage("skill-a", tokens_used=500, was_cached=False)
        sc1.record_usage("skill-a", tokens_used=100, was_cached=True)

        # Load from same file
        sc2 = SkillCache(stats_path=stats_file)
        stats = sc2.get_stats("skill-a")
        assert stats is not None
        assert stats.cold_tokens == 500
        assert stats.cold_count == 1
        assert stats.warm_tokens == 100
        assert stats.warm_count == 1

    def test_persistence_corrupt_file(self, tmp_path: Path):
        """Corrupt stats file is handled gracefully."""
        stats_file = tmp_path / "stats.json"
        stats_file.write_text("NOT VALID JSON {{{")
        sc = SkillCache(stats_path=str(stats_file))
        # Should start fresh, not crash
        assert sc.get_all_stats() == {}

    def test_persistence_missing_file(self, tmp_path: Path):
        """Missing stats file starts with empty stats."""
        sc = SkillCache(stats_path=str(tmp_path / "nonexistent.json"))
        assert sc.get_all_stats() == {}

    def test_persistence_creates_parent_dirs(self, tmp_path: Path):
        """Stats file creation creates parent directories as needed."""
        stats_file = str(tmp_path / "deep" / "nested" / "stats.json")
        sc = SkillCache(stats_path=stats_file)
        sc.record_usage("s", tokens_used=10, was_cached=False)
        assert Path(stats_file).exists()


# ---------------------------------------------------------------------------
# Cache key generation
# ---------------------------------------------------------------------------


class TestCacheKeys:
    """Tests for make_skill_cache_key."""

    def test_deterministic(self):
        """Same inputs produce the same key."""
        k1 = SkillCache.make_skill_cache_key("id1", "pattern1", "model1")
        k2 = SkillCache.make_skill_cache_key("id1", "pattern1", "model1")
        assert k1 == k2

    def test_different_for_different_inputs(self):
        """Different inputs produce different keys."""
        k1 = SkillCache.make_skill_cache_key("id1", "pattern1", "model1")
        k2 = SkillCache.make_skill_cache_key("id2", "pattern1", "model1")
        k3 = SkillCache.make_skill_cache_key("id1", "pattern2", "model1")
        k4 = SkillCache.make_skill_cache_key("id1", "pattern1", "model2")
        assert len({k1, k2, k3, k4}) == 4

    def test_prefix_format(self):
        """Key starts with 'skill_cache:' prefix."""
        key = SkillCache.make_skill_cache_key("abc", "def", "ghi")
        assert key.startswith("skill_cache:")

    def test_key_is_sha256_hex(self):
        """Key suffix is a valid SHA-256 hex digest (64 chars)."""
        key = SkillCache.make_skill_cache_key("abc", "def")
        suffix = key.removeprefix("skill_cache:")
        assert len(suffix) == 64
        int(suffix, 16)  # validates hex


# ---------------------------------------------------------------------------
# Progressive disclosure
# ---------------------------------------------------------------------------


class TestProgressiveDisclosure:
    """Tests for progressive_disclosure_header."""

    def test_header_format(self):
        """Header contains skill name, description, and scores."""
        h = SkillCache.progressive_disclosure_header("my-skill", "Does useful things", 0.85, 0.92)
        assert "[my-skill]" in h
        assert "Does useful things" in h
        assert "quality=0.85" in h
        assert "completion=0.92" in h

    def test_header_zero_scores(self):
        """Header renders zero scores correctly."""
        h = SkillCache.progressive_disclosure_header("s", "desc")
        assert "quality=0.00" in h
        assert "completion=0.00" in h

    def test_header_long_description(self):
        """Header handles long descriptions without truncation."""
        long_desc = "A" * 500
        h = SkillCache.progressive_disclosure_header("s", long_desc, 0.5, 0.5)
        assert long_desc in h


# ---------------------------------------------------------------------------
# Execution templates
# ---------------------------------------------------------------------------


class TestExecutionTemplate:
    """Tests for build_execution_template."""

    def test_template_structure(self):
        """Template has all required keys."""
        t = SkillCache.build_execution_template("skill-x", "do a thing", "it worked great")
        assert t["skill_name"] == "skill-x"
        assert t["task_pattern"] == "do a thing"
        assert t["execution_template"] == "it worked great"
        assert "created_at" in t
        assert "template_key" in t

    def test_template_truncation_task(self):
        """Task pattern is truncated to 500 chars."""
        long_task = "X" * 1000
        t = SkillCache.build_execution_template("s", long_task, "summary")
        assert len(t["task_pattern"]) == 500

    def test_template_truncation_summary(self):
        """Execution summary is truncated to 2000 chars."""
        long_summary = "Y" * 5000
        t = SkillCache.build_execution_template("s", "task", long_summary)
        assert len(t["execution_template"]) == 2000

    def test_template_key_deterministic(self):
        """Same skill + task produce the same template key."""
        t1 = SkillCache.build_execution_template("s", "task", "summary1")
        t2 = SkillCache.build_execution_template("s", "task", "summary2")
        assert t1["template_key"] == t2["template_key"]

    def test_template_key_different_inputs(self):
        """Different skill or task produce different template keys."""
        t1 = SkillCache.build_execution_template("s1", "task", "summary")
        t2 = SkillCache.build_execution_template("s2", "task", "summary")
        t3 = SkillCache.build_execution_template("s1", "other", "summary")
        assert len({t1["template_key"], t2["template_key"], t3["template_key"]}) == 3

    def test_template_key_length(self):
        """Template key is 16 hex chars (truncated SHA-256)."""
        t = SkillCache.build_execution_template("s", "t", "e")
        assert len(t["template_key"]) == 16
        int(t["template_key"], 16)  # validates hex

    def test_template_created_at_format(self):
        """created_at is ISO 8601 UTC format."""
        t = SkillCache.build_execution_template("s", "t", "e")
        assert t["created_at"].endswith("Z")
        assert "T" in t["created_at"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify TTL constants are set correctly."""

    def test_skill_cache_ttl(self):
        assert SKILL_CACHE_TTL == 259200  # 72h

    def test_default_cache_ttl(self):
        assert DEFAULT_CACHE_TTL == 86400  # 24h

    def test_skill_ttl_longer_than_default(self):
        assert SKILL_CACHE_TTL > DEFAULT_CACHE_TTL
