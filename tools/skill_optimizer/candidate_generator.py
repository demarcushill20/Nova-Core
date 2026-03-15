"""Candidate mutation generator for the skill optimization pipeline.

Generates policy-compliant candidate mutations by wrapping the vendored
improve_description.py. Applies constraints from mutation_policy.yaml
before and after generation.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
POLICY_PATH = BASE_DIR / "configs" / "mutation_policy.yaml"

# Import vendored mutation primitive
sys.path.insert(0, str(BASE_DIR / ".claude" / "skills" / "skill-creator"))
from scripts.improve_description import improve_description  # noqa: E402
from scripts.utils import parse_skill_md  # noqa: E402


@dataclass
class Candidate:
    """A candidate mutation for a skill description."""

    skill_name: str
    candidate_id: int
    original_description: str
    proposed_description: str
    mutation_type: str = "description"
    policy_violations: list[str] = field(default_factory=list)
    is_valid: bool = True
    generation_time: float = 0.0
    model: str | None = None


def load_mutation_policy() -> dict:
    """Load mutation policy configuration."""
    if POLICY_PATH.exists():
        return yaml.safe_load(POLICY_PATH.read_text())
    return {}


def check_policy(description: str, policy: dict, original_description: str = "") -> list[str]:
    """Check a candidate description against mutation policy.

    Returns list of violation messages (empty if compliant).
    """
    violations = []
    desc_policy = policy.get("description", {})

    # Length checks
    min_len = desc_policy.get("min_length", 30)
    max_len = desc_policy.get("max_length", 1024)
    if len(description) < min_len:
        violations.append(f"too_short: {len(description)} < {min_len}")
    if len(description) > max_len:
        violations.append(f"too_long: {len(description)} > {max_len}")

    # Disallowed patterns
    patterns = policy.get("disallowed_patterns", [])
    desc_lower = description.lower()
    for pattern in patterns:
        if pattern.lower() in desc_lower:
            violations.append(f"disallowed_pattern: '{pattern}'")

    # Anti-broadening: check if the candidate is much more generic
    if original_description:
        orig_words = set(re.findall(r"\w+", original_description.lower()))
        new_words = set(re.findall(r"\w+", description.lower()))
        # If we lost >50% of original specificity words, flag it
        if orig_words:
            retained = len(orig_words & new_words) / len(orig_words)
            if retained < 0.3:
                violations.append(f"specificity_loss: retained only {retained:.0%} of original keywords")

    return violations


def generate_candidates(
    skill_name: str,
    skill_path: Path,
    current_description: str,
    eval_results: dict,
    history: list[dict] | None = None,
    count: int = 3,
    model: str = "claude-sonnet-4-6",
    policy: dict | None = None,
    log_dir: Path | None = None,
) -> list[Candidate]:
    """Generate policy-compliant candidate mutations.

    Uses the vendored improve_description.py as the mutation primitive,
    then validates each candidate against mutation_policy.yaml.
    """
    if policy is None:
        policy = load_mutation_policy()

    _, _, skill_content = parse_skill_md(skill_path)
    candidates: list[Candidate] = []

    for i in range(count):
        t0 = time.time()
        try:
            # The vendored function generates one description per call
            proposed = improve_description(
                skill_name=skill_name,
                skill_content=skill_content,
                current_description=current_description,
                eval_results=eval_results,
                history=history or [],
                model=model,
                log_dir=log_dir,
                iteration=i + 1,
            )
            gen_time = time.time() - t0

            # Validate against policy
            violations = check_policy(proposed, policy, current_description)

            candidate = Candidate(
                skill_name=skill_name,
                candidate_id=i,
                original_description=current_description,
                proposed_description=proposed,
                mutation_type="description",
                policy_violations=violations,
                is_valid=len(violations) == 0,
                generation_time=round(gen_time, 2),
                model=model,
            )

            # Feed the generated description back as history for diversity
            if history is None:
                history = []
            history.append(
                {
                    "description": proposed,
                    "passed": eval_results.get("summary", {}).get("passed", 0),
                    "failed": eval_results.get("summary", {}).get("failed", 0),
                    "total": eval_results.get("summary", {}).get("total", 0),
                    "results": eval_results.get("results", []),
                    "note": f"candidate_{i}_{'valid' if candidate.is_valid else 'invalid'}",
                }
            )

        except Exception as e:
            gen_time = time.time() - t0
            candidate = Candidate(
                skill_name=skill_name,
                candidate_id=i,
                original_description=current_description,
                proposed_description="",
                mutation_type="description",
                policy_violations=[f"generation_error: {str(e)}"],
                is_valid=False,
                generation_time=round(gen_time, 2),
                model=model,
            )

        candidates.append(candidate)

    return candidates


def filter_valid_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Return only policy-compliant candidates."""
    return [c for c in candidates if c.is_valid]
