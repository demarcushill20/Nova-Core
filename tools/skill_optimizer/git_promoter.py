"""Git promotion for accepted skill changes.

Creates feature branches with structured commits. Never touches main directly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


@dataclass
class PromotionResult:
    """Result of a git promotion attempt."""

    run_id: str
    branch_name: str
    skills_promoted: list[str]
    commit_sha: str | None = None
    success: bool = False
    error: str | None = None


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd or BASE_DIR,
    )


def create_promotion_branch(run_id: str) -> str:
    """Create a feature branch for promoted changes."""
    branch_name = f"skill-opt/{run_id}"

    # Ensure we're on a clean state
    status = _git(["status", "--porcelain"])
    if status.stdout.strip():
        raise RuntimeError("Working tree is not clean. Commit or stash changes first.")

    # Create branch from current HEAD
    result = _git(["checkout", "-b", branch_name])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create branch: {result.stderr}")

    return branch_name


def apply_description_change(skill_path: Path, new_description: str) -> None:
    """Update a skill's description in its SKILL.md frontmatter."""
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_path}")

    content = skill_md.read_text()

    # Find and replace the description in frontmatter
    if not content.startswith("---"):
        raise ValueError("SKILL.md has no frontmatter")

    end = content.find("\n---", 3)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not properly closed")

    fm_block = content[3:end]
    body = content[end:]

    # Replace description line(s) in frontmatter
    # Handle both single-line and multiline descriptions
    import re

    # Match 'description: ...' possibly followed by continuation lines
    # Single line: description: "some text" or description: some text
    # Multiline: description: > or description: | followed by indented lines
    new_fm = re.sub(
        r'(description:\s*)(?:>-?\s*\n(?:\s+.*\n)*|\|-?\s*\n(?:\s+.*\n)*|"[^"]*"|\'[^\']*\'|[^\n]*)',
        f"description: {json.dumps(new_description)}",
        fm_block,
        count=1,
    )

    skill_md.write_text(f"---{new_fm}{body}")


def commit_changes(
    run_id: str,
    skill_name: str,
    old_description: str,
    new_description: str,
    metrics_delta: dict,
) -> str | None:
    """Create a structured commit for a skill change.

    Returns the commit SHA or None if nothing to commit.
    """
    # Stage all changes in the skill directory
    _git(["add", "-A"])

    # Check if there are staged changes
    status = _git(["diff", "--cached", "--name-only"])
    if not status.stdout.strip():
        return None

    # Build structured commit message
    delta_str = ""
    if metrics_delta:
        parts = []
        for key, val in metrics_delta.items():
            if isinstance(val, float):
                parts.append(f"{key}: {val:+.4f}")
        delta_str = ", ".join(parts)

    message = (
        f"feat(skill-opt): optimize {skill_name} description\n"
        f"\n"
        f"Run: {run_id}\n"
        f"Skill: {skill_name}\n"
        f"Metrics: {delta_str}\n"
        f"\n"
        f"Old: {old_description[:100]}{'...' if len(old_description) > 100 else ''}\n"
        f"New: {new_description[:100]}{'...' if len(new_description) > 100 else ''}\n"
    )

    result = _git(["commit", "-m", message])
    if result.returncode != 0:
        return None

    # Get commit SHA
    sha_result = _git(["rev-parse", "HEAD"])
    return sha_result.stdout.strip() if sha_result.returncode == 0 else None


def promote_accepted_changes(
    run_id: str,
    accepted_skills: list[dict],
) -> PromotionResult:
    """Promote all accepted changes from a run to a feature branch.

    Each accepted_skill dict should have:
    - name: skill name
    - path: skill directory path
    - old_description: original description
    - new_description: accepted candidate description
    - metrics_delta: dict of metric changes

    Returns a PromotionResult.
    """
    if not accepted_skills:
        return PromotionResult(
            run_id=run_id,
            branch_name="",
            skills_promoted=[],
            success=True,
            error="nothing to promote",
        )

    try:
        # Create feature branch
        branch_name = create_promotion_branch(run_id)

        promoted: list[str] = []
        last_sha = None

        for skill_data in accepted_skills:
            name = skill_data["name"]
            skill_path = Path(skill_data["path"])

            # Apply change
            apply_description_change(skill_path, skill_data["new_description"])

            # Commit
            sha = commit_changes(
                run_id=run_id,
                skill_name=name,
                old_description=skill_data.get("old_description", ""),
                new_description=skill_data["new_description"],
                metrics_delta=skill_data.get("metrics_delta", {}),
            )
            if sha:
                promoted.append(name)
                last_sha = sha

        # Switch back to previous branch
        _git(["checkout", "-"])

        return PromotionResult(
            run_id=run_id,
            branch_name=branch_name,
            skills_promoted=promoted,
            commit_sha=last_sha,
            success=True,
        )

    except Exception as e:
        # Try to get back to previous branch
        _git(["checkout", "-"])
        return PromotionResult(
            run_id=run_id,
            branch_name="",
            skills_promoted=[],
            success=False,
            error=str(e),
        )
