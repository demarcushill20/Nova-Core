"""Evolution audit trail, governance enforcement, and rollback management.

Phase 9 of Self-Evolving Skills — provides:
- Structured audit logging for all evolution events (JSONL)
- Extended governance enforcement per origin type and risk level
- Automatic rollback when evolved skills regress below parent performance
- Promotion gates for quarantined (CAPTURED) skills

Uses:
- skills.skill_record for SkillVersion, Origin
- skills.version_store for SkillVersionStore (auto-rollback queries)
- configs/mutation_policy.yaml for evolution constraints

IMPORTANT: stdlib + PyYAML only — no other external dependencies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rollback constants
# ---------------------------------------------------------------------------
AUTO_ROLLBACK_WINDOW = 10  # executions before comparing to parent
REGRESSION_THRESHOLD = 0.8  # new must be >= 80% of parent's completion rate


# ---------------------------------------------------------------------------
# AuditEntry
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    """A single evolution audit record."""

    timestamp: str
    skill_id: str
    skill_name: str
    parent_id: str
    evolution_type: str  # FIX, DERIVED, CAPTURED
    trigger: str  # post_analysis, metric_monitor, manual
    direction: str  # broaden, narrow, repair, etc.
    result: str  # success, failed, rolled_back
    before_metrics: dict = field(default_factory=dict)
    after_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise to a plain dict (JSON-safe)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> AuditEntry:
        """Deserialise from a dict, ignoring unknown keys."""
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# EvolutionAudit — audit trail + rollback / promotion checks
# ---------------------------------------------------------------------------


class EvolutionAudit:
    """Audit trail and governance enforcement for skill evolution.

    Writes structured JSONL to ``audit_path`` and reads evolution policy
    from ``policy_path`` (mutation_policy.yaml).
    """

    def __init__(
        self,
        audit_path: str = "STATE/evolution_audit.jsonl",
        policy_path: str = "configs/mutation_policy.yaml",
    ) -> None:
        self._audit_path = Path(audit_path)
        self._policy = self._load_policy(policy_path)

    # -- policy loading ----------------------------------------------------

    def _load_policy(self, path: str) -> dict:
        """Load the ``evolution_policy`` section from mutation_policy.yaml."""
        try:
            import yaml

            p = Path(path)
            if not p.exists():
                return {}
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            return data.get("evolution_policy", {})
        except Exception as exc:
            logger.warning("Failed to load evolution policy: %s", exc)
            return {}

    # -- audit CRUD --------------------------------------------------------

    def log_evolution(self, entry: AuditEntry) -> None:
        """Append an audit entry to the JSONL log."""
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except Exception as exc:
            logger.error("Failed to write audit entry: %s", exc)

    def get_recent_entries(self, limit: int = 50) -> list[AuditEntry]:
        """Read the most recent ``limit`` audit entries."""
        entries: list[AuditEntry] = []
        try:
            if not self._audit_path.exists():
                return []
            lines = self._audit_path.read_text().strip().split("\n")
            for line in lines[-limit:]:
                if line.strip():
                    entries.append(AuditEntry.from_dict(json.loads(line)))
        except Exception as exc:
            logger.warning("Failed to read audit entries: %s", exc)
        return entries

    def get_entries_for_skill(self, skill_name: str) -> list[AuditEntry]:
        """Get all audit entries for a specific skill (last 500)."""
        return [e for e in self.get_recent_entries(limit=500) if e.skill_name == skill_name]

    # -- governance gate ---------------------------------------------------

    def check_evolution_allowed(
        self,
        evolution_type: str,
        skill_name: str,
        risk_level: str = "medium",
        generation: int = 0,
    ) -> tuple[bool, str]:
        """Check if an evolution is allowed by governance policy.

        Returns ``(allowed, reason)``.
        """
        # Generation cap per evolution type
        type_config = self._policy.get(evolution_type.lower(), {})
        max_gen = type_config.get("max_generation", 5)
        if generation >= max_gen:
            return False, f"Generation {generation} >= max {max_gen}"

        # Risk-level constraints: high/critical only permit FIX
        if risk_level in ("critical", "high") and evolution_type != "FIX":
            return (
                False,
                f"Risk level '{risk_level}' only allows FIX, not {evolution_type}",
            )

        return True, "allowed"

    # -- auto-rollback check -----------------------------------------------

    def check_auto_rollback(
        self,
        skill_id: str,
        parent_id: str,
        version_store,
    ) -> bool:
        """Check if an evolved skill should be automatically rolled back.

        Compares the child's ``completion_rate`` against its parent's after
        ``AUTO_ROLLBACK_WINDOW`` executions.

        Returns ``True`` if rollback should happen.
        """
        child = version_store.get_skill(skill_id)
        parent = version_store.get_skill(parent_id)

        if not child or not parent:
            return False

        # Need enough executions to judge
        if child.stats.executions < AUTO_ROLLBACK_WINDOW:
            return False

        # Parent has no data — skip comparison
        if parent.stats.executions == 0:
            return False

        child_rate = child.stats.completions / max(child.stats.executions, 1)
        parent_rate = parent.stats.completions / max(parent.stats.executions, 1)

        # Rollback if child significantly worse than parent
        if parent_rate > 0 and child_rate < parent_rate * REGRESSION_THRESHOLD:
            logger.warning(
                "Auto-rollback triggered for %s: child_rate=%.2f < parent_rate=%.2f * %.2f",
                child.name,
                child_rate,
                parent_rate,
                REGRESSION_THRESHOLD,
            )
            return True

        return False

    # -- promotion gate (quarantined CAPTURED skills) ----------------------

    def check_promotion(
        self,
        skill_id: str,
        version_store,
        required_successes: int = 5,
    ) -> tuple[bool, str]:
        """Check if a quarantined skill is ready for promotion.

        Quarantined skills (CAPTURED origin) need ``required_successes``
        successful completions before full activation.

        Returns ``(ready, reason)``.
        """
        skill = version_store.get_skill(skill_id)
        if not skill:
            return False, "Skill not found"

        if skill.lineage.origin.value != "captured":
            return True, "Non-captured skills don't need promotion"

        if skill.stats.completions >= required_successes:
            return (
                True,
                f"Reached {skill.stats.completions}/{required_successes} successful completions",
            )

        return (
            False,
            f"Only {skill.stats.completions}/{required_successes} successful completions",
        )


# ---------------------------------------------------------------------------
# GovernanceEnforcer — centralised gate for all evolution types
# ---------------------------------------------------------------------------


class GovernanceEnforcer:
    """Centralised governance enforcement for all evolution types.

    Maintains a runtime frozen-skill set and validates evolution requests
    against risk level, generation cap, and policy constraints.
    """

    def __init__(
        self,
        policy_path: str = "configs/mutation_policy.yaml",
    ) -> None:
        self._policy = self._load_policy(policy_path)
        self._frozen_skills: set[str] = set()

    def _load_policy(self, path: str) -> dict:
        """Load full mutation policy."""
        try:
            import yaml

            p = Path(path)
            if not p.exists():
                return {}
            with open(p) as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to load governance policy: %s", exc)
            return {}

    # -- freeze / unfreeze -------------------------------------------------

    def freeze_skill(self, skill_name: str) -> None:
        """Freeze a skill — prevents all evolution."""
        self._frozen_skills.add(skill_name)
        logger.info("Froze skill: %s", skill_name)

    def unfreeze_skill(self, skill_name: str) -> None:
        """Unfreeze a skill — re-enables evolution."""
        self._frozen_skills.discard(skill_name)

    def is_frozen(self, skill_name: str) -> bool:
        """Check if a skill is currently frozen."""
        return skill_name in self._frozen_skills

    # -- risk-level helpers ------------------------------------------------

    @staticmethod
    def get_allowed_evolution_types(risk_level: str) -> list[str]:
        """Return evolution types permitted at a given risk level.

        - critical / high: FIX only
        - medium: FIX, DERIVED
        - low (and anything else): FIX, DERIVED, CAPTURED
        """
        if risk_level in ("critical", "high"):
            return ["FIX"]
        elif risk_level == "medium":
            return ["FIX", "DERIVED"]
        else:
            return ["FIX", "DERIVED", "CAPTURED"]

    # -- full validation ---------------------------------------------------

    def validate_evolution(
        self,
        evolution_type: str,
        skill_name: str,
        risk_level: str = "low",
        generation: int = 0,
    ) -> tuple[bool, list[str]]:
        """Full governance validation before evolution.

        Returns ``(allowed, list_of_violation_messages)``.
        An empty violations list means the evolution is permitted.
        """
        violations: list[str] = []

        # Check frozen
        if self.is_frozen(skill_name):
            violations.append(f"Skill '{skill_name}' is frozen — no evolution allowed")

        # Check risk level
        allowed_types = self.get_allowed_evolution_types(risk_level)
        if evolution_type not in allowed_types:
            violations.append(f"Evolution type '{evolution_type}' not allowed for risk_level '{risk_level}'")

        # Check generation cap from policy
        evolution_config = self._policy.get("evolution_policy", {})
        type_config = evolution_config.get(evolution_type.lower(), {})
        max_gen = type_config.get("max_generation", 5)
        if generation >= max_gen:
            violations.append(f"Generation {generation} >= max allowed {max_gen}")

        return len(violations) == 0, violations
