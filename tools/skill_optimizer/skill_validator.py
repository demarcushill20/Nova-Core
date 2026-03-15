"""Pre-flight skill validation for the optimization pipeline.

Checks that a skill is eligible and ready for optimization before
entering the eval loop. This is the entry gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tools.skill_optimizer.skill_discovery import SkillRecord
from tools.skill_optimizer.validate_datasets import validate_all

BASE_DIR = Path(__file__).resolve().parent.parent.parent


@dataclass
class SkillReadiness:
    """Whether a skill is ready for optimization."""

    skill_name: str
    ready: bool
    reason: str
    dataset_errors: int = 0
    dataset_warnings: int = 0


def check_readiness(
    skill: SkillRecord,
    require_valid_datasets: bool = True,
) -> SkillReadiness:
    """Check if a skill is ready for optimization."""

    # Check frozen
    if not skill.optimization_enabled:
        return SkillReadiness(
            skill_name=skill.name,
            ready=False,
            reason=f"frozen: {skill.frozen_reason or 'no reason given'}",
        )

    # Check skill type
    if skill.skill_type != "prompt":
        return SkillReadiness(
            skill_name=skill.name,
            ready=False,
            reason=f"skill_type={skill.skill_type} (only prompt skills in rollout 1)",
        )

    # Check SKILL.md exists
    skill_md = Path(skill.path) / "SKILL.md"
    if not skill_md.exists():
        return SkillReadiness(
            skill_name=skill.name,
            ready=False,
            reason="missing SKILL.md",
        )

    # Check dataset validity
    if require_valid_datasets:
        skill_dict = {"name": skill.name, "path": skill.path, "neighbors": skill.neighbors}
        report = validate_all([skill_dict])

        if report.has_errors:
            return SkillReadiness(
                skill_name=skill.name,
                ready=False,
                reason=f"dataset validation failed: {report.error_count} errors",
                dataset_errors=report.error_count,
                dataset_warnings=report.warning_count,
            )

    return SkillReadiness(
        skill_name=skill.name,
        ready=True,
        reason="all checks passed",
    )
