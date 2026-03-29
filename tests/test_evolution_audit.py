"""Tests for skills.evolution_audit — Phase 9 of Self-Evolving Skills.

Covers:
- AuditEntry creation, serialisation, deserialisation
- EvolutionAudit JSONL logging, retrieval, filtering
- Governance gate (generation cap, risk level)
- Auto-rollback (regression detection, edge cases)
- Promotion gate (quarantined CAPTURED skills)
- GovernanceEnforcer freeze/unfreeze, risk-level types, full validation
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skills.evolution_audit import (
    AUTO_ROLLBACK_WINDOW,
    REGRESSION_THRESHOLD,
    AuditEntry,
    EvolutionAudit,
    GovernanceEnforcer,
)
from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
    generate_skill_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(
    name: str = "test-skill",
    origin: Origin = Origin.IMPORTED,
    generation: int = 0,
    parent_ids: list[str] | None = None,
    is_active: bool = True,
    **kwargs,
) -> SkillVersion:
    """Create a SkillVersion with sensible defaults for testing."""
    skill_id = kwargs.pop(
        "skill_id", generate_skill_id(name, origin, generation)
    )
    return SkillVersion(
        skill_id=skill_id,
        name=name,
        description=kwargs.pop("description", f"Test skill: {name}"),
        path=kwargs.pop("path", f"SKILLS/{name}"),
        is_active=is_active,
        version=kwargs.pop("version", "1.0.0"),
        lineage=SkillLineage(
            origin=origin,
            generation=generation,
            parent_ids=parent_ids or [],
            change_summary=kwargs.pop("change_summary", None),
        ),
        stats=ExecutionStats(
            selections=kwargs.pop("selections", 0),
            executions=kwargs.pop("executions", 0),
            completions=kwargs.pop("completions", 0),
            failures=kwargs.pop("failures", 0),
            fallbacks=kwargs.pop("fallbacks", 0),
        ),
        created_by=kwargs.pop("created_by", "test"),
    )


def _make_entry(**overrides) -> AuditEntry:
    """Create an AuditEntry with sensible defaults."""
    defaults = {
        "timestamp": "2026-03-29T12:00:00Z",
        "skill_id": "skill_001",
        "skill_name": "test-skill",
        "parent_id": "skill_000",
        "evolution_type": "FIX",
        "trigger": "post_analysis",
        "direction": "repair",
        "result": "success",
        "before_metrics": {"completion_rate": 0.3},
        "after_metrics": {"completion_rate": 0.8},
    }
    defaults.update(overrides)
    return AuditEntry(**defaults)


def _mock_version_store(skills: dict[str, SkillVersion]) -> MagicMock:
    """Create a mock version store that returns skills by ID."""
    store = MagicMock()
    store.get_skill = lambda sid: skills.get(sid)
    return store


def _write_policy(tmp_path: Path) -> Path:
    """Write a minimal mutation_policy.yaml and return its path."""
    policy_path = tmp_path / "mutation_policy.yaml"
    policy_path.write_text(
        """\
version: "1.0.0"
evolution_policy:
  fix:
    max_generation: 5
    cooldown_seconds: 7200
  derived:
    max_generation: 3
    require_eval_gate: true
  captured:
    quarantine_successes: 5
    max_per_day: 2
forbidden_targets:
  - tool_permissions
  - safety_rules
"""
    )
    return policy_path


# ===========================================================================
# TestAuditEntry
# ===========================================================================


class TestAuditEntry:
    """AuditEntry dataclass creation, serialisation, deserialisation."""

    def test_creation_with_defaults(self):
        entry = AuditEntry(
            timestamp="2026-03-29T12:00:00Z",
            skill_id="s1",
            skill_name="my-skill",
            parent_id="s0",
            evolution_type="FIX",
            trigger="post_analysis",
            direction="repair",
            result="success",
        )
        assert entry.before_metrics == {}
        assert entry.after_metrics == {}
        assert entry.evolution_type == "FIX"

    def test_creation_with_metrics(self):
        entry = _make_entry(
            before_metrics={"rate": 0.5},
            after_metrics={"rate": 0.9},
        )
        assert entry.before_metrics == {"rate": 0.5}
        assert entry.after_metrics == {"rate": 0.9}

    def test_to_dict_roundtrip(self):
        entry = _make_entry()
        d = entry.to_dict()
        assert isinstance(d, dict)
        assert d["skill_id"] == "skill_001"
        assert d["evolution_type"] == "FIX"

    def test_from_dict_roundtrip(self):
        original = _make_entry()
        d = original.to_dict()
        restored = AuditEntry.from_dict(d)
        assert restored.skill_id == original.skill_id
        assert restored.skill_name == original.skill_name
        assert restored.before_metrics == original.before_metrics

    def test_from_dict_ignores_extra_keys(self):
        d = _make_entry().to_dict()
        d["unknown_field"] = "should be ignored"
        entry = AuditEntry.from_dict(d)
        assert entry.skill_id == "skill_001"
        assert not hasattr(entry, "unknown_field")

    def test_from_dict_preserves_all_fields(self):
        original = _make_entry(
            trigger="manual",
            direction="broaden",
            result="failed",
        )
        restored = AuditEntry.from_dict(original.to_dict())
        assert restored.trigger == "manual"
        assert restored.direction == "broaden"
        assert restored.result == "failed"


# ===========================================================================
# TestEvolutionAudit
# ===========================================================================


class TestEvolutionAudit:
    """EvolutionAudit JSONL logging, retrieval, governance, rollback, promotion."""

    @pytest.fixture
    def audit(self, tmp_path: Path) -> EvolutionAudit:
        return EvolutionAudit(
            audit_path=str(tmp_path / "audit.jsonl"),
            policy_path="nonexistent.yaml",  # empty policy
        )

    @pytest.fixture
    def audit_with_policy(self, tmp_path: Path) -> EvolutionAudit:
        policy = _write_policy(tmp_path)
        return EvolutionAudit(
            audit_path=str(tmp_path / "audit.jsonl"),
            policy_path=str(policy),
        )

    # -- JSONL logging / retrieval -----------------------------------------

    def test_log_evolution_creates_file(self, audit: EvolutionAudit, tmp_path):
        entry = _make_entry()
        audit.log_evolution(entry)
        log_file = tmp_path / "audit.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_log_evolution_appends(self, audit: EvolutionAudit, tmp_path):
        audit.log_evolution(_make_entry(skill_id="s1"))
        audit.log_evolution(_make_entry(skill_id="s2"))
        audit.log_evolution(_make_entry(skill_id="s3"))
        lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3

    def test_log_evolution_writes_valid_json(self, audit: EvolutionAudit, tmp_path):
        audit.log_evolution(_make_entry())
        line = (tmp_path / "audit.jsonl").read_text().strip()
        d = json.loads(line)
        assert d["skill_id"] == "skill_001"
        assert d["evolution_type"] == "FIX"

    def test_get_recent_entries_empty(self, audit: EvolutionAudit):
        entries = audit.get_recent_entries()
        assert entries == []

    def test_get_recent_entries_returns_all(self, audit: EvolutionAudit):
        for i in range(5):
            audit.log_evolution(_make_entry(skill_id=f"s{i}"))
        entries = audit.get_recent_entries()
        assert len(entries) == 5
        assert entries[0].skill_id == "s0"
        assert entries[4].skill_id == "s4"

    def test_get_recent_entries_respects_limit(self, audit: EvolutionAudit):
        for i in range(10):
            audit.log_evolution(_make_entry(skill_id=f"s{i}"))
        entries = audit.get_recent_entries(limit=3)
        assert len(entries) == 3
        # Should be the LAST 3 entries
        assert entries[0].skill_id == "s7"
        assert entries[2].skill_id == "s9"

    def test_get_entries_for_skill(self, audit: EvolutionAudit):
        audit.log_evolution(_make_entry(skill_name="alpha"))
        audit.log_evolution(_make_entry(skill_name="beta"))
        audit.log_evolution(_make_entry(skill_name="alpha"))
        entries = audit.get_entries_for_skill("alpha")
        assert len(entries) == 2
        assert all(e.skill_name == "alpha" for e in entries)

    def test_get_entries_for_skill_none_found(self, audit: EvolutionAudit):
        audit.log_evolution(_make_entry(skill_name="alpha"))
        entries = audit.get_entries_for_skill("nonexistent")
        assert entries == []

    # -- check_evolution_allowed -------------------------------------------

    def test_check_allowed_passes(self, audit: EvolutionAudit):
        allowed, reason = audit.check_evolution_allowed(
            "FIX", "my-skill", risk_level="low", generation=0
        )
        assert allowed is True
        assert reason == "allowed"

    def test_check_allowed_generation_cap_default(self, audit: EvolutionAudit):
        # Default max_generation is 5 when no policy loaded
        allowed, reason = audit.check_evolution_allowed(
            "FIX", "my-skill", generation=5
        )
        assert allowed is False
        assert "Generation 5 >= max 5" in reason

    def test_check_allowed_generation_cap_from_policy(
        self, audit_with_policy: EvolutionAudit
    ):
        # derived.max_generation=3 in the test policy
        allowed, reason = audit_with_policy.check_evolution_allowed(
            "DERIVED", "my-skill", risk_level="low", generation=3
        )
        assert allowed is False
        assert "Generation 3 >= max 3" in reason

    def test_check_allowed_risk_blocks_non_fix(self, audit: EvolutionAudit):
        allowed, reason = audit.check_evolution_allowed(
            "DERIVED", "skill-x", risk_level="high", generation=0
        )
        assert allowed is False
        assert "only allows FIX" in reason

    def test_check_allowed_risk_critical_blocks_captured(
        self, audit: EvolutionAudit
    ):
        allowed, reason = audit.check_evolution_allowed(
            "CAPTURED", "skill-y", risk_level="critical", generation=0
        )
        assert allowed is False
        assert "only allows FIX" in reason

    def test_check_allowed_fix_at_high_risk(self, audit: EvolutionAudit):
        allowed, reason = audit.check_evolution_allowed(
            "FIX", "skill-z", risk_level="high", generation=0
        )
        assert allowed is True

    # -- check_auto_rollback -----------------------------------------------

    def test_auto_rollback_triggers_on_regression(self, audit: EvolutionAudit):
        parent = _make_skill(
            name="skill-a",
            origin=Origin.IMPORTED,
            skill_id="parent_1",
            executions=20,
            completions=18,  # 90% rate
        )
        child = _make_skill(
            name="skill-a",
            origin=Origin.FIXED,
            generation=1,
            skill_id="child_1",
            executions=AUTO_ROLLBACK_WINDOW,
            completions=5,  # 50% rate — below 90% * 0.8 = 72%
        )
        store = _mock_version_store({"parent_1": parent, "child_1": child})
        assert audit.check_auto_rollback("child_1", "parent_1", store) is True

    def test_auto_rollback_skips_insufficient_executions(
        self, audit: EvolutionAudit
    ):
        parent = _make_skill(
            skill_id="p1", executions=20, completions=18
        )
        child = _make_skill(
            skill_id="c1",
            executions=AUTO_ROLLBACK_WINDOW - 1,  # not enough
            completions=1,
        )
        store = _mock_version_store({"p1": parent, "c1": child})
        assert audit.check_auto_rollback("c1", "p1", store) is False

    def test_auto_rollback_skips_parent_no_data(self, audit: EvolutionAudit):
        parent = _make_skill(
            skill_id="p2", executions=0, completions=0
        )
        child = _make_skill(
            skill_id="c2",
            executions=AUTO_ROLLBACK_WINDOW,
            completions=5,
        )
        store = _mock_version_store({"p2": parent, "c2": child})
        assert audit.check_auto_rollback("c2", "p2", store) is False

    def test_auto_rollback_passes_when_child_good(self, audit: EvolutionAudit):
        parent = _make_skill(
            skill_id="p3", executions=20, completions=16  # 80%
        )
        child = _make_skill(
            skill_id="c3",
            executions=AUTO_ROLLBACK_WINDOW,
            completions=9,  # 90% — better than parent
        )
        store = _mock_version_store({"p3": parent, "c3": child})
        assert audit.check_auto_rollback("c3", "p3", store) is False

    def test_auto_rollback_child_not_found(self, audit: EvolutionAudit):
        store = _mock_version_store({})
        assert audit.check_auto_rollback("missing", "also_missing", store) is False

    def test_auto_rollback_parent_not_found(self, audit: EvolutionAudit):
        child = _make_skill(
            skill_id="c_only",
            executions=AUTO_ROLLBACK_WINDOW,
            completions=5,
        )
        store = _mock_version_store({"c_only": child})
        assert audit.check_auto_rollback("c_only", "missing_parent", store) is False

    def test_auto_rollback_boundary_exact_threshold(
        self, audit: EvolutionAudit
    ):
        """Child rate exactly at threshold should NOT trigger rollback."""
        parent = _make_skill(
            skill_id="p4", executions=20, completions=20  # 100%
        )
        # Threshold = 100% * 0.8 = 80%; child at exactly 80% is fine
        child = _make_skill(
            skill_id="c4",
            executions=AUTO_ROLLBACK_WINDOW,
            completions=int(AUTO_ROLLBACK_WINDOW * REGRESSION_THRESHOLD),
        )
        store = _mock_version_store({"p4": parent, "c4": child})
        assert audit.check_auto_rollback("c4", "p4", store) is False

    # -- check_promotion ---------------------------------------------------

    def test_promotion_captured_not_ready(self, audit: EvolutionAudit):
        skill = _make_skill(
            skill_id="cap1",
            origin=Origin.CAPTURED,
            completions=2,
        )
        store = _mock_version_store({"cap1": skill})
        ready, reason = audit.check_promotion("cap1", store, required_successes=5)
        assert ready is False
        assert "2/5" in reason

    def test_promotion_captured_ready(self, audit: EvolutionAudit):
        skill = _make_skill(
            skill_id="cap2",
            origin=Origin.CAPTURED,
            completions=7,
        )
        store = _mock_version_store({"cap2": skill})
        ready, reason = audit.check_promotion("cap2", store, required_successes=5)
        assert ready is True
        assert "7/5" in reason

    def test_promotion_non_captured_always_passes(self, audit: EvolutionAudit):
        skill = _make_skill(
            skill_id="imp1",
            origin=Origin.IMPORTED,
            completions=0,
        )
        store = _mock_version_store({"imp1": skill})
        ready, reason = audit.check_promotion("imp1", store)
        assert ready is True
        assert "Non-captured" in reason

    def test_promotion_skill_not_found(self, audit: EvolutionAudit):
        store = _mock_version_store({})
        ready, reason = audit.check_promotion("missing", store)
        assert ready is False
        assert "not found" in reason

    def test_promotion_fixed_origin_passes(self, audit: EvolutionAudit):
        skill = _make_skill(
            skill_id="fix1",
            origin=Origin.FIXED,
            completions=0,
        )
        store = _mock_version_store({"fix1": skill})
        ready, reason = audit.check_promotion("fix1", store)
        assert ready is True

    def test_promotion_derived_origin_passes(self, audit: EvolutionAudit):
        skill = _make_skill(
            skill_id="der1",
            origin=Origin.DERIVED,
            completions=0,
        )
        store = _mock_version_store({"der1": skill})
        ready, reason = audit.check_promotion("der1", store)
        assert ready is True


# ===========================================================================
# TestGovernanceEnforcer
# ===========================================================================


class TestGovernanceEnforcer:
    """GovernanceEnforcer freeze, risk levels, full validation."""

    @pytest.fixture
    def enforcer(self) -> GovernanceEnforcer:
        return GovernanceEnforcer(policy_path="nonexistent.yaml")

    @pytest.fixture
    def enforcer_with_policy(self, tmp_path: Path) -> GovernanceEnforcer:
        policy = _write_policy(tmp_path)
        return GovernanceEnforcer(policy_path=str(policy))

    # -- freeze / unfreeze -------------------------------------------------

    def test_freeze_and_is_frozen(self, enforcer: GovernanceEnforcer):
        assert enforcer.is_frozen("my-skill") is False
        enforcer.freeze_skill("my-skill")
        assert enforcer.is_frozen("my-skill") is True

    def test_unfreeze(self, enforcer: GovernanceEnforcer):
        enforcer.freeze_skill("my-skill")
        enforcer.unfreeze_skill("my-skill")
        assert enforcer.is_frozen("my-skill") is False

    def test_unfreeze_nonexistent_noop(self, enforcer: GovernanceEnforcer):
        enforcer.unfreeze_skill("never-frozen")
        assert enforcer.is_frozen("never-frozen") is False

    def test_freeze_multiple_skills(self, enforcer: GovernanceEnforcer):
        enforcer.freeze_skill("a")
        enforcer.freeze_skill("b")
        assert enforcer.is_frozen("a") is True
        assert enforcer.is_frozen("b") is True
        assert enforcer.is_frozen("c") is False

    # -- get_allowed_evolution_types ---------------------------------------

    def test_allowed_types_critical(self):
        types = GovernanceEnforcer.get_allowed_evolution_types("critical")
        assert types == ["FIX"]

    def test_allowed_types_high(self):
        types = GovernanceEnforcer.get_allowed_evolution_types("high")
        assert types == ["FIX"]

    def test_allowed_types_medium(self):
        types = GovernanceEnforcer.get_allowed_evolution_types("medium")
        assert types == ["FIX", "DERIVED"]

    def test_allowed_types_low(self):
        types = GovernanceEnforcer.get_allowed_evolution_types("low")
        assert types == ["FIX", "DERIVED", "CAPTURED"]

    def test_allowed_types_unknown_defaults_to_all(self):
        types = GovernanceEnforcer.get_allowed_evolution_types("banana")
        assert types == ["FIX", "DERIVED", "CAPTURED"]

    # -- validate_evolution ------------------------------------------------

    def test_validate_passes(self, enforcer: GovernanceEnforcer):
        allowed, violations = enforcer.validate_evolution(
            "FIX", "my-skill", risk_level="low", generation=0
        )
        assert allowed is True
        assert violations == []

    def test_validate_frozen_skill(self, enforcer: GovernanceEnforcer):
        enforcer.freeze_skill("frozen-skill")
        allowed, violations = enforcer.validate_evolution(
            "FIX", "frozen-skill", risk_level="low", generation=0
        )
        assert allowed is False
        assert any("frozen" in v for v in violations)

    def test_validate_wrong_risk_level(self, enforcer: GovernanceEnforcer):
        allowed, violations = enforcer.validate_evolution(
            "DERIVED", "my-skill", risk_level="high", generation=0
        )
        assert allowed is False
        assert any("not allowed" in v for v in violations)

    def test_validate_generation_cap_default(self, enforcer: GovernanceEnforcer):
        # No policy loaded — default max_generation=5
        allowed, violations = enforcer.validate_evolution(
            "FIX", "my-skill", risk_level="low", generation=5
        )
        assert allowed is False
        assert any("Generation 5 >= max allowed 5" in v for v in violations)

    def test_validate_generation_cap_from_policy(
        self, enforcer_with_policy: GovernanceEnforcer
    ):
        # derived.max_generation=3 in the test policy
        allowed, violations = enforcer_with_policy.validate_evolution(
            "DERIVED", "my-skill", risk_level="low", generation=3
        )
        assert allowed is False
        assert any("Generation 3 >= max allowed 3" in v for v in violations)

    def test_validate_multiple_violations(
        self, enforcer: GovernanceEnforcer
    ):
        enforcer.freeze_skill("bad-skill")
        allowed, violations = enforcer.validate_evolution(
            "CAPTURED",
            "bad-skill",
            risk_level="high",
            generation=10,
        )
        assert allowed is False
        # Should have: frozen + risk level + generation cap = 3 violations
        assert len(violations) >= 3

    def test_validate_fix_at_low_risk_generation_0(
        self, enforcer_with_policy: GovernanceEnforcer
    ):
        allowed, violations = enforcer_with_policy.validate_evolution(
            "FIX", "good-skill", risk_level="low", generation=0
        )
        assert allowed is True
        assert violations == []

    def test_validate_captured_at_low_risk(
        self, enforcer_with_policy: GovernanceEnforcer
    ):
        allowed, violations = enforcer_with_policy.validate_evolution(
            "CAPTURED", "new-skill", risk_level="low", generation=0
        )
        assert allowed is True

    def test_validate_captured_at_medium_risk(
        self, enforcer: GovernanceEnforcer
    ):
        allowed, violations = enforcer.validate_evolution(
            "CAPTURED", "skill-x", risk_level="medium", generation=0
        )
        assert allowed is False
        assert any("not allowed" in v for v in violations)
