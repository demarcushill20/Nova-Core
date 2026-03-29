"""Tests for skills.skill_dashboard — observability dashboard.

Phase 10 of Self-Evolving Skills.
Covers:
- Health summary aggregation (with/without version store)
- Evolution summary (with/without audit entries)
- Token savings (with/without cache)
- Lineage tree construction
- Telegram formatting (health, evolution, savings)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skills.evolution_audit import AuditEntry, EvolutionAudit
from skills.evolution_queue import EvolutionQueue
from skills.execution_analysis import SkillHealth
from skills.skill_cache import SkillCache, TokenStats
from skills.skill_dashboard import SkillDashboard
from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
)
from skills.version_store import SkillVersionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(
    name: str = "test-skill",
    skill_id: str = "test-skill__imp_abc12345",
    is_active: bool = True,
    origin: Origin = Origin.IMPORTED,
    generation: int = 0,
    parent_ids: list[str] | None = None,
    selections: int = 10,
    executions: int = 8,
    completions: int = 7,
    failures: int = 1,
    fallbacks: int = 0,
    change_summary: str = "",
) -> SkillVersion:
    return SkillVersion(
        skill_id=skill_id,
        name=name,
        description=f"Description for {name}",
        path=f"SKILLS/{name}/SKILL.md",
        is_active=is_active,
        version="1.0.0",
        lineage=SkillLineage(
            origin=origin,
            generation=generation,
            parent_ids=parent_ids or [],
            change_summary=change_summary,
        ),
        stats=ExecutionStats(
            selections=selections,
            executions=executions,
            completions=completions,
            failures=failures,
            fallbacks=fallbacks,
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_health(
    name: str = "test-skill",
    skill_id: str = "test-skill__imp_abc12345",
    is_healthy: bool = True,
    selections: int = 10,
    executions: int = 8,
    completions: int = 7,
    failures: int = 1,
    fallbacks: int = 0,
    completion_rate: float = 0.875,
    fallback_rate: float = 0.0,
) -> SkillHealth:
    return SkillHealth(
        skill_name=name,
        skill_id=skill_id,
        selections=selections,
        executions=executions,
        completions=completions,
        failures=failures,
        fallbacks=fallbacks,
        applied_rate=executions / max(selections, 1),
        completion_rate=completion_rate,
        effective_rate=completions / max(selections, 1),
        fallback_rate=fallback_rate,
        avg_quality_score=0.8,
        is_healthy=is_healthy,
        health_issues=[] if is_healthy else [f"Low completion rate: {completion_rate:.2f}"],
    )


def _make_audit_entry(
    evolution_type: str = "FIX",
    result: str = "success",
    skill_name: str = "test-skill",
) -> AuditEntry:
    return AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        skill_id="test-skill__imp_abc12345",
        skill_name=skill_name,
        parent_id="test-skill__imp_parent",
        evolution_type=evolution_type,
        trigger="post_analysis",
        direction="repair",
        result=result,
    )


# ---------------------------------------------------------------------------
# TestSkillDashboard — core aggregation
# ---------------------------------------------------------------------------


class TestSkillDashboard:
    """Test core dashboard aggregation methods."""

    def test_init_all_none(self):
        """Dashboard should work with no subsystems provided."""
        dash = SkillDashboard()
        assert dash.version_store is None
        assert dash.skill_cache is None
        assert dash.evolution_queue is None
        assert dash.evolution_audit is None

    def test_get_health_summary_no_version_store(self):
        """Returns empty health summary when version_store is None."""
        dash = SkillDashboard()
        result = dash.get_health_summary()
        assert result["total_skills"] == 0
        assert result["active_skills"] == 0
        assert result["healthy_skills"] == 0
        assert result["unhealthy_skills"] == 0
        assert result["frozen_skills"] == []
        assert result["skills"] == []
        assert "generated_at" in result

    def test_get_health_summary_with_version_store(self, tmp_path):
        """Returns populated health summary from version store."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")

        # Register two skills: one active, one inactive
        active_skill = _make_skill("skill-a", "skill-a__imp_aaa11111")
        inactive_skill = _make_skill(
            "skill-b", "skill-b__imp_bbb22222", is_active=False
        )
        store.register_skill(active_skill)
        store.register_skill(inactive_skill)

        dash = SkillDashboard(version_store=store)
        result = dash.get_health_summary()

        assert result["total_skills"] == 2
        assert result["active_skills"] == 1

    def test_get_health_summary_with_healthy_and_unhealthy(self, tmp_path):
        """Counts healthy vs unhealthy skills correctly."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")

        # Healthy skill: good completion rate
        healthy = _make_skill(
            "good-skill", "good__imp_aaa11111",
            selections=10, executions=10, completions=9, failures=1, fallbacks=0,
        )
        # Unhealthy skill: low completion rate
        unhealthy = _make_skill(
            "bad-skill", "bad__imp_bbb22222",
            selections=10, executions=10, completions=2, failures=8, fallbacks=6,
        )
        store.register_skill(healthy)
        store.register_skill(unhealthy)

        dash = SkillDashboard(version_store=store)
        result = dash.get_health_summary()

        assert result["healthy_skills"] + result["unhealthy_skills"] == len(result["skills"])

    def test_get_health_summary_frozen_skills(self, tmp_path):
        """Includes frozen skills from evolution queue."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")
        queue = EvolutionQueue(state_dir=str(tmp_path / "state"))
        # Manually freeze a skill
        queue._frozen_skills.add("broken-skill")

        dash = SkillDashboard(version_store=store, evolution_queue=queue)
        result = dash.get_health_summary()
        assert "broken-skill" in result["frozen_skills"]

    def test_get_evolution_summary_no_audit(self):
        """Returns empty evolution summary when audit is None."""
        dash = SkillDashboard()
        result = dash.get_evolution_summary()
        assert result["total_evolutions"] == 0
        assert result["success_rate"] == 0.0
        assert result["by_type"] == {"FIX": 0, "DERIVED": 0, "CAPTURED": 0}
        assert result["recent"] == []
        assert result["queue_status"] == {}

    def test_get_evolution_summary_with_entries(self, tmp_path):
        """Counts evolution types and success rate correctly."""
        audit_path = tmp_path / "audit.jsonl"
        audit = EvolutionAudit(
            audit_path=str(audit_path),
            policy_path=str(tmp_path / "nonexistent.yaml"),
        )

        # Write entries
        entries = [
            _make_audit_entry("FIX", "success"),
            _make_audit_entry("FIX", "failed"),
            _make_audit_entry("DERIVED", "success"),
            _make_audit_entry("CAPTURED", "success"),
        ]
        for e in entries:
            audit.log_evolution(e)

        dash = SkillDashboard(evolution_audit=audit)
        result = dash.get_evolution_summary()

        assert result["total_evolutions"] == 4
        assert result["by_type"]["FIX"] == 2
        assert result["by_type"]["DERIVED"] == 1
        assert result["by_type"]["CAPTURED"] == 1
        assert result["success_rate"] == 0.75  # 3/4

    def test_get_evolution_summary_recent_capped(self, tmp_path):
        """Recent entries are capped at last 10."""
        audit_path = tmp_path / "audit.jsonl"
        audit = EvolutionAudit(
            audit_path=str(audit_path),
            policy_path=str(tmp_path / "nonexistent.yaml"),
        )

        for i in range(15):
            audit.log_evolution(
                _make_audit_entry("FIX", "success", skill_name=f"skill-{i}")
            )

        dash = SkillDashboard(evolution_audit=audit)
        result = dash.get_evolution_summary()
        assert len(result["recent"]) == 10

    def test_get_evolution_summary_with_queue(self, tmp_path):
        """Includes queue status when evolution_queue is provided."""
        queue = EvolutionQueue(state_dir=str(tmp_path / "state"))
        dash = SkillDashboard(evolution_queue=queue)
        result = dash.get_evolution_summary()
        assert "queued" in result["queue_status"]
        assert "active_evolutions" in result["queue_status"]

    def test_get_token_savings_no_cache(self):
        """Returns empty savings when cache is None."""
        dash = SkillDashboard()
        result = dash.get_token_savings()
        assert result["total_tokens_saved"] == 0
        assert result["overall_savings_ratio"] == 0.0
        assert result["per_skill"] == {}

    def test_get_token_savings_with_cache(self, tmp_path):
        """Returns populated savings from skill cache."""
        cache = SkillCache(stats_path=str(tmp_path / "stats.json"))

        # Record some usage
        cache.record_usage("skill-a", 1000, was_cached=False)
        cache.record_usage("skill-a", 200, was_cached=True)
        cache.record_usage("skill-b", 500, was_cached=False)

        dash = SkillDashboard(skill_cache=cache)
        result = dash.get_token_savings()

        assert "total_tokens_saved" in result
        assert "per_skill" in result
        assert "skill-a" in result["per_skill"]
        assert "skill-b" in result["per_skill"]

    def test_get_full_report(self):
        """Full report includes all three sections."""
        dash = SkillDashboard()
        report = dash.get_full_report()
        assert "health" in report
        assert "evolution" in report
        assert "token_savings" in report
        assert "generated_at" in report


# ---------------------------------------------------------------------------
# TestLineageTree
# ---------------------------------------------------------------------------


class TestLineageTree:
    """Test lineage tree construction."""

    def test_lineage_tree_no_version_store(self):
        """Returns empty tree when version_store is None."""
        dash = SkillDashboard()
        result = dash.get_lineage_tree("any-skill")
        assert result == {"root": "", "nodes": [], "edges": []}

    def test_lineage_tree_missing_skill(self, tmp_path):
        """Returns empty tree when skill is not found."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")
        dash = SkillDashboard(version_store=store)
        result = dash.get_lineage_tree("nonexistent-skill")
        assert result == {"root": "", "nodes": [], "edges": []}

    def test_lineage_tree_single_node(self, tmp_path):
        """Tree with a single skill (no lineage parents)."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")
        skill = _make_skill("solo-skill", "solo__imp_aaa11111")
        store.register_skill(skill)

        dash = SkillDashboard(version_store=store)
        result = dash.get_lineage_tree("solo-skill")

        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["skill_id"] == "solo__imp_aaa11111"
        assert result["nodes"][0]["generation"] == 0
        assert result["root"] == "solo__imp_aaa11111"
        assert result["edges"] == []

    def test_lineage_tree_multi_generation(self, tmp_path):
        """Tree with parent -> child lineage."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")

        parent = _make_skill(
            "evolving", "evolving__imp_parent",
            origin=Origin.IMPORTED, generation=0,
        )
        store.register_skill(parent)

        child = _make_skill(
            "evolving", "evolving__v_1_child",
            origin=Origin.FIXED, generation=1,
            parent_ids=["evolving__imp_parent"],
            change_summary="Fixed error handling",
        )
        # Use evolve_skill for proper lineage
        store.evolve_skill("evolving__imp_parent", child, deactivate_parent=True)

        dash = SkillDashboard(version_store=store)
        result = dash.get_lineage_tree("evolving")

        assert len(result["nodes"]) == 2
        assert result["root"] == "evolving__imp_parent"

        # Should have one edge from parent to child
        assert len(result["edges"]) == 1
        assert result["edges"][0]["from"] == "evolving__imp_parent"
        assert result["edges"][0]["to"] == "evolving__v_1_child"

    def test_lineage_tree_node_fields(self, tmp_path):
        """Verify all expected fields are present in nodes."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")
        skill = _make_skill("field-check", "field__imp_aaa11111")
        store.register_skill(skill)

        dash = SkillDashboard(version_store=store)
        result = dash.get_lineage_tree("field-check")

        node = result["nodes"][0]
        expected_fields = {
            "skill_id", "name", "generation", "origin", "is_active",
            "version", "stats", "change_summary", "created_at",
        }
        assert expected_fields.issubset(set(node.keys()))
        assert "selections" in node["stats"]
        assert "executions" in node["stats"]
        assert "completions" in node["stats"]
        assert "failures" in node["stats"]
        assert "fallbacks" in node["stats"]


# ---------------------------------------------------------------------------
# TestTelegramFormatting
# ---------------------------------------------------------------------------


class TestTelegramFormatting:
    """Test Telegram message formatting."""

    def test_format_telegram_health_basic(self):
        """Basic health formatting without issues."""
        store = MagicMock(spec=SkillVersionStore)
        store.list_skills.return_value = [
            _make_skill("a"), _make_skill("b"),
        ]
        store.get_skill_health.return_value = [
            _make_health("a"), _make_health("b"),
        ]

        dash = SkillDashboard(version_store=store)
        text = dash.format_telegram_health()

        assert "*Skill Health Report*" in text
        assert "Active: 2/2" in text
        assert "Healthy: 2" in text
        assert "Unhealthy: 0" in text

    def test_format_telegram_health_with_frozen(self):
        """Health formatting includes frozen skill names."""
        store = MagicMock(spec=SkillVersionStore)
        store.list_skills.return_value = []
        store.get_skill_health.return_value = []

        queue = MagicMock(spec=EvolutionQueue)
        queue.get_frozen_skills.return_value = ["broken-skill"]

        dash = SkillDashboard(version_store=store, evolution_queue=queue)
        text = dash.format_telegram_health()

        assert "Frozen: broken-skill" in text

    def test_format_telegram_health_with_unhealthy(self):
        """Health formatting shows top unhealthy skills."""
        store = MagicMock(spec=SkillVersionStore)
        store.list_skills.return_value = [_make_skill("bad")]
        store.get_skill_health.return_value = [
            _make_health("bad", is_healthy=False, completion_rate=0.2),
        ]

        dash = SkillDashboard(version_store=store)
        text = dash.format_telegram_health()

        assert "Unhealthy: 1" in text
        assert "bad: completion=" in text

    def test_format_telegram_health_limits_unhealthy_to_3(self):
        """Only shows top 3 unhealthy skills."""
        store = MagicMock(spec=SkillVersionStore)
        store.list_skills.return_value = [_make_skill(f"bad-{i}") for i in range(5)]
        unhealthy_list = [
            _make_health(f"bad-{i}", skill_id=f"bad-{i}__imp_{i:08d}",
                         is_healthy=False, completion_rate=0.1)
            for i in range(5)
        ]
        store.get_skill_health.return_value = unhealthy_list

        dash = SkillDashboard(version_store=store)
        text = dash.format_telegram_health()

        # Count warning symbols — should be 3
        assert text.count("\u26a0\ufe0f") == 3

    def test_format_telegram_evolution_basic(self):
        """Basic evolution formatting."""
        audit = MagicMock(spec=EvolutionAudit)
        audit.get_recent_entries.return_value = [
            _make_audit_entry("FIX", "success"),
            _make_audit_entry("DERIVED", "success"),
        ]

        dash = SkillDashboard(evolution_audit=audit)
        text = dash.format_telegram_evolution()

        assert "*Evolution Report*" in text
        assert "Total: 2" in text
        assert "success: 100%" in text
        assert "FIX: 1" in text
        assert "DERIVED: 1" in text

    def test_format_telegram_evolution_with_queue(self):
        """Evolution formatting includes queue status."""
        audit = MagicMock(spec=EvolutionAudit)
        audit.get_recent_entries.return_value = []

        queue = MagicMock(spec=EvolutionQueue)
        queue.get_queue_status.return_value = {
            "queued": 3,
            "active_evolutions": 1,
            "frozen_skills": ["bad-skill"],
            "max_concurrent": 2,
            "max_queue_size": 50,
            "items": [],
        }

        dash = SkillDashboard(evolution_audit=audit, evolution_queue=queue)
        text = dash.format_telegram_evolution()

        assert "Queue: 3 pending, 1 active" in text
        assert "Frozen: bad-skill" in text

    def test_format_telegram_evolution_no_frozen(self):
        """Evolution formatting omits frozen line when no frozen skills."""
        audit = MagicMock(spec=EvolutionAudit)
        audit.get_recent_entries.return_value = []

        queue = MagicMock(spec=EvolutionQueue)
        queue.get_queue_status.return_value = {
            "queued": 0,
            "active_evolutions": 0,
            "frozen_skills": [],
            "max_concurrent": 2,
            "max_queue_size": 50,
            "items": [],
        }

        dash = SkillDashboard(evolution_audit=audit, evolution_queue=queue)
        text = dash.format_telegram_evolution()

        assert "Frozen" not in text

    def test_format_telegram_savings_basic(self, tmp_path):
        """Basic token savings formatting."""
        cache = SkillCache(stats_path=str(tmp_path / "stats.json"))
        cache.record_usage("fast-skill", 1000, was_cached=False)
        cache.record_usage("fast-skill", 200, was_cached=True)

        dash = SkillDashboard(skill_cache=cache)
        text = dash.format_telegram_savings()

        assert "*Token Savings*" in text
        assert "Total saved:" in text
        assert "Overall savings:" in text

    def test_format_telegram_savings_with_top_savers(self, tmp_path):
        """Shows top savers when tokens have been saved."""
        cache = SkillCache(stats_path=str(tmp_path / "stats.json"))

        # Record enough for measurable savings
        cache.record_usage("big-saver", 10000, was_cached=False)
        cache.record_usage("big-saver", 1000, was_cached=True)
        cache.record_usage("big-saver", 1000, was_cached=True)

        dash = SkillDashboard(skill_cache=cache)
        text = dash.format_telegram_savings()

        assert "big-saver" in text
        assert "tokens saved" in text

    def test_format_telegram_savings_empty(self):
        """Savings formatting with no cache returns defaults."""
        dash = SkillDashboard()
        text = dash.format_telegram_savings()

        assert "*Token Savings*" in text
        assert "Total saved: 0 tokens" in text
        assert "Overall savings: 0.0%" in text

    def test_format_telegram_savings_limits_top_savers(self, tmp_path):
        """Only shows top 3 savers even if more exist."""
        cache = SkillCache(stats_path=str(tmp_path / "stats.json"))

        for i in range(5):
            name = f"saver-{i}"
            cache.record_usage(name, 1000 * (i + 1), was_cached=False)
            cache.record_usage(name, 100, was_cached=True)

        dash = SkillDashboard(skill_cache=cache)
        text = dash.format_telegram_savings()

        # Count sparkle emojis — should be max 3
        assert text.count("\u2728") <= 3


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and graceful degradation."""

    def test_evolution_summary_all_failures(self, tmp_path):
        """Success rate is 0 when all evolutions failed."""
        audit_path = tmp_path / "audit.jsonl"
        audit = EvolutionAudit(
            audit_path=str(audit_path),
            policy_path=str(tmp_path / "nope.yaml"),
        )
        for _ in range(3):
            audit.log_evolution(_make_audit_entry("FIX", "failed"))

        dash = SkillDashboard(evolution_audit=audit)
        result = dash.get_evolution_summary()
        assert result["success_rate"] == 0.0

    def test_evolution_summary_unknown_type_ignored(self, tmp_path):
        """Unknown evolution types don't increment by_type counters."""
        audit_path = tmp_path / "audit.jsonl"
        audit = EvolutionAudit(
            audit_path=str(audit_path),
            policy_path=str(tmp_path / "nope.yaml"),
        )
        entry = _make_audit_entry("UNKNOWN_TYPE", "success")
        audit.log_evolution(entry)

        dash = SkillDashboard(evolution_audit=audit)
        result = dash.get_evolution_summary()
        assert result["total_evolutions"] == 1
        assert result["by_type"]["FIX"] == 0
        assert result["by_type"]["DERIVED"] == 0
        assert result["by_type"]["CAPTURED"] == 0

    def test_health_summary_no_queue_omits_frozen(self, tmp_path):
        """Without evolution_queue, frozen_skills stays empty."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")
        dash = SkillDashboard(version_store=store)
        result = dash.get_health_summary()
        assert result["frozen_skills"] == []

    def test_lineage_tree_origin_value_serialized(self, tmp_path):
        """Origin enum is serialized as its string value."""
        store = SkillVersionStore(db_path=tmp_path / "test.db")
        skill = _make_skill("origin-test", "origin__imp_aaa11111", origin=Origin.CAPTURED)
        store.register_skill(skill)

        dash = SkillDashboard(version_store=store)
        result = dash.get_lineage_tree("origin-test")
        assert result["nodes"][0]["origin"] == "captured"

    def test_full_report_generated_at_is_iso(self):
        """Full report generated_at is valid ISO 8601."""
        dash = SkillDashboard()
        report = dash.get_full_report()
        # Should parse without error
        dt = datetime.fromisoformat(report["generated_at"])
        assert dt.tzinfo is not None  # timezone-aware
