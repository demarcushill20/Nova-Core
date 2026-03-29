"""Skill version dataclasses with lineage tracking.

Defines the core data model for versioned skills:
- SkillVersion: a single versioned snapshot of a skill
- SkillLineage: provenance (origin, generation, parent chain)
- ExecutionStats: cumulative usage counters
- Origin: how the skill was created (imported, fixed, derived, captured)

IMPORTANT: This is SkillVersion, NOT SkillRecord — the latter exists in
tools/skill_optimizer/skill_discovery.py and must not be conflicted with.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Origin(Enum):
    """How a skill version was created."""

    IMPORTED = "imported"  # Human-authored
    FIXED = "fixed"  # Auto-repaired
    DERIVED = "derived"  # Enhanced / evolved
    CAPTURED = "captured"  # Auto-learned from execution


@dataclass
class SkillLineage:
    """Provenance metadata for a skill version."""

    origin: Origin
    generation: int = 0
    parent_ids: list[str] = field(default_factory=list)
    content_snapshot: bytes | None = None
    content_diff: str | None = None
    change_summary: str | None = None


@dataclass
class ExecutionStats:
    """Cumulative usage counters for a skill version."""

    selections: int = 0
    executions: int = 0
    completions: int = 0
    failures: int = 0
    fallbacks: int = 0


@dataclass
class SkillVersion:
    """A single versioned snapshot of a skill.

    Combines identity, metadata, lineage, and execution stats into one
    serialisable record suitable for SQLite persistence.
    """

    skill_id: str
    name: str
    description: str
    path: str
    lineage: SkillLineage
    is_active: bool = True
    version: str = "1.0.0"
    stats: ExecutionStats = field(default_factory=ExecutionStats)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    created_by: str = "system"

    def to_dict(self) -> dict:
        """Serialise to a plain dict (JSON-safe)."""
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "is_active": self.is_active,
            "version": self.version,
            "lineage": {
                "origin": self.lineage.origin.value,
                "generation": self.lineage.generation,
                "parent_ids": list(self.lineage.parent_ids),
                "content_diff": self.lineage.content_diff,
                "change_summary": self.lineage.change_summary,
                # content_snapshot is bytes — omit from JSON serialisation
            },
            "stats": {
                "selections": self.stats.selections,
                "executions": self.stats.executions,
                "completions": self.stats.completions,
                "failures": self.stats.failures,
                "fallbacks": self.stats.fallbacks,
            },
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SkillVersion:
        """Deserialise from a plain dict."""
        lineage_d = d.get("lineage", {})
        stats_d = d.get("stats", {})
        return cls(
            skill_id=d["skill_id"],
            name=d["name"],
            description=d.get("description", ""),
            path=d.get("path", ""),
            is_active=d.get("is_active", True),
            version=d.get("version", "1.0.0"),
            lineage=SkillLineage(
                origin=Origin(lineage_d.get("origin", "imported")),
                generation=lineage_d.get("generation", 0),
                parent_ids=lineage_d.get("parent_ids", []),
                content_diff=lineage_d.get("content_diff"),
                change_summary=lineage_d.get("change_summary"),
            ),
            stats=ExecutionStats(
                selections=stats_d.get("selections", 0),
                executions=stats_d.get("executions", 0),
                completions=stats_d.get("completions", 0),
                failures=stats_d.get("failures", 0),
                fallbacks=stats_d.get("fallbacks", 0),
            ),
            created_at=d.get(
                "created_at", datetime.now(timezone.utc).isoformat()
            ),
            created_by=d.get("created_by", "system"),
        )


def generate_skill_id(name: str, origin: Origin, generation: int = 0) -> str:
    """Generate a deterministic skill ID.

    Format:
        IMPORTED:               {name}__imp_{hash8}
        FIXED/DERIVED/CAPTURED: {name}__v_{generation}_{hash8}

    Where hash8 is the first 8 hex chars of a SHA-256 digest.
    """
    if origin == Origin.IMPORTED:
        h = hashlib.sha256(name.encode()).hexdigest()[:8]
        return f"{name}__imp_{h}"
    else:
        h = hashlib.sha256((name + str(generation)).encode()).hexdigest()[:8]
        return f"{name}__v_{generation}_{h}"
