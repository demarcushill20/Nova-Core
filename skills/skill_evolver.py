"""Skill evolution engine — AUTO-FIX, AUTO-IMPROVE, AUTO-LEARN.

Phases 3-5 of Self-Evolving Skills — the core evolution engine that
repairs failing skills, derives enhanced variants from success patterns,
and captures brand-new skills from novel execution patterns.

Supports three evolution types:
- evolve_fix(): Repair a failing skill (AUTO-FIX, Phase 3)
- evolve_derived(): Enhance a skill from success patterns (AUTO-IMPROVE, Phase 4)
- evolve_captured(): Create new skill from novel patterns (AUTO-LEARN, Phase 5)

Uses:
- skills.skill_record for SkillVersion, SkillLineage, Origin
- skills.version_store for persistence and snapshots
- skills.skill_patch for patch application and governance
- configs/mutation_policy.yaml for evolution constraints
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from skills.skill_patch import (
    GovernanceError,
    PatchError,
    apply_patch,
    check_governance,
)
from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
    generate_skill_id,
)
from skills.version_store import SkillVersionStore, snapshot_skill_dir

logger = logging.getLogger(__name__)

# Evolution limits
MAX_FIX_ATTEMPTS = 3  # retries per evolution attempt
MAX_LLM_ITERATIONS = 5  # LLM conversation turns
MAX_GENERATION = 5  # max fix generations before freezing
COOLDOWN_SECONDS = 7200  # 2 hours between evolutions of same skill
MAX_CAPTURES_PER_DAY = 2  # rate limit for AUTO-LEARN captures
MIN_TASK_EXAMPLES = 2  # minimum task examples to prove a pattern is real
DUPLICATE_NAME_THRESHOLD = 0.8  # SequenceMatcher ratio for duplicate detection


class EvolutionError(Exception):
    """Raised when evolution fails after all retries."""

    pass


class SkillEvolver:
    """Core evolution engine for self-repairing skills.

    Supports three evolution types:
    - evolve_fix(): Repair a failing skill (AUTO-FIX)
    - evolve_derived(): Enhance a skill from success patterns (Phase 4)
    - evolve_captured(): Create new skill from novel patterns (Phase 5)
    """

    def __init__(
        self,
        version_store: SkillVersionStore,
        mutation_policy_path: str = "configs/mutation_policy.yaml",
        skills_base_dir: str = ".claude/skills",
    ):
        self.version_store = version_store
        self.skills_base_dir = Path(skills_base_dir)
        self._evolution_history: dict[str, str] = {}  # skill_id -> last_task_id
        self._last_evolution_time: dict[str, float] = {}  # skill_id -> timestamp

        # AUTO-LEARN rate limiting
        self._captures_today: int = 0
        self._captures_today_date: str = ""  # ISO date string for reset

        # Load mutation policy
        self.forbidden_targets = self._load_forbidden_targets(mutation_policy_path)

    def _load_forbidden_targets(self, policy_path: str) -> list[str]:
        """Load forbidden mutation targets from mutation_policy.yaml."""
        try:
            import yaml

            path = Path(policy_path)
            if not path.exists():
                logger.warning("Mutation policy not found at %s", policy_path)
                return []
            with open(path) as f:
                policy = yaml.safe_load(f)

            if not policy:
                return []

            # Check for evolution-specific policy first, fall back to general
            evolution_policy = policy.get("evolution_policy", {})
            if evolution_policy:
                return evolution_policy.get("forbidden_targets", [])

            return policy.get("forbidden_targets", [])
        except Exception as e:
            logger.warning("Failed to load mutation policy: %s", e)
            return []

    def evolve_fix(
        self,
        skill_id: str,
        direction: str,
        task_id: str = "",
        failure_context: str = "",
    ) -> SkillVersion | None:
        """AUTO-FIX: Repair a failing skill.

        Args:
            skill_id: The skill to fix
            direction: What to change (from EvolutionSuggestion)
            task_id: The task that triggered this fix
            failure_context: Recent failure information

        Returns:
            New SkillVersion if fix succeeded, None if it failed.
        """
        # Get current skill version
        parent = self.version_store.get_skill(skill_id)
        if not parent:
            logger.error("Skill %s not found in version store", skill_id)
            return None

        # Check generation cap
        if parent.lineage.generation >= MAX_GENERATION:
            logger.warning(
                "Skill %s at generation %d — max reached, skipping fix",
                parent.name,
                parent.lineage.generation,
            )
            return None

        # Check cooldown (by skill_id AND by skill name to prevent bypass)
        last_time = max(
            self._last_evolution_time.get(skill_id, 0),
            self._last_evolution_time.get(f"name:{parent.name}", 0),
        )
        if time.time() - last_time < COOLDOWN_SECONDS:
            logger.info("Skill %s in cooldown, skipping fix", parent.name)
            return None

        # Anti-loop: same task can't trigger fix twice
        if self._evolution_history.get(skill_id) == task_id and task_id:
            logger.info(
                "Skill %s already fixed for task %s, skipping",
                parent.name,
                task_id,
            )
            return None

        # Load skill directory content
        skill_dir = Path(parent.path) if parent.path else self.skills_base_dir / parent.name
        if not skill_dir.exists():
            logger.error("Skill directory not found: %s", skill_dir)
            return None

        current_content = snapshot_skill_dir(str(skill_dir))
        if not current_content:
            logger.error("No content found in skill directory: %s", skill_dir)
            return None

        # Save pre-evolution snapshot
        self.version_store.save_snapshot(skill_id, current_content)

        # Get recent failure analyses for context
        recent_analyses = self.version_store.get_analyses_for_skill(skill_id, limit=5)
        failure_history = self._format_failure_history(recent_analyses)

        # Build fix prompt
        prompt = self._build_fix_prompt(
            skill_name=parent.name,
            skill_content=current_content,
            direction=direction,
            failure_context=failure_context,
            failure_history=failure_history,
            forbidden_targets=self.forbidden_targets,
        )

        # Apply-retry cycle
        patch_text = ""
        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            logger.info(
                "Fix attempt %d/%d for %s",
                attempt,
                MAX_FIX_ATTEMPTS,
                parent.name,
            )

            try:
                # Call LLM to generate patch
                patch_text = self._call_llm(prompt)
                if not patch_text:
                    logger.warning("LLM returned empty response on attempt %d", attempt)
                    continue

                # Apply patch to SKILL.md
                skill_md_path = str(skill_dir / "SKILL.md")
                new_content = apply_patch(
                    file_path=skill_md_path,
                    patch_text=patch_text,
                    governance_check=lambda old, new: check_governance(old, new, self.forbidden_targets),
                )

                # Validate the result
                validation_errors = self._validate_fix(new_content, parent.name)
                if validation_errors:
                    logger.warning(
                        "Validation failed on attempt %d: %s",
                        attempt,
                        validation_errors,
                    )
                    prompt = self._build_retry_prompt(prompt, patch_text, validation_errors)
                    continue

                # Save original content for rollback
                original_file_content = Path(skill_md_path).read_text(encoding="utf-8")

                # Write the fixed content
                Path(skill_md_path).write_text(new_content, encoding="utf-8")

                try:
                    # Generate diff
                    old_content = current_content.get("SKILL.md", "")
                    content_diff = "\n".join(
                        difflib.unified_diff(
                            old_content.split("\n"),
                            new_content.split("\n"),
                            fromfile=f"{parent.name}/SKILL.md (v{parent.lineage.generation})",
                            tofile=f"{parent.name}/SKILL.md (v{parent.lineage.generation + 1})",
                            lineterm="",
                        )
                    )

                    # Create new version
                    new_gen = parent.lineage.generation + 1
                    new_skill_id = generate_skill_id(parent.name, Origin.FIXED, new_gen)

                    new_version = SkillVersion(
                        skill_id=new_skill_id,
                        name=parent.name,
                        description=parent.description,
                        path=str(skill_dir),
                        is_active=True,
                        version=f"1.{new_gen}.0",
                        lineage=SkillLineage(
                            origin=Origin.FIXED,
                            generation=new_gen,
                            parent_ids=[parent.skill_id],
                            content_diff=content_diff,
                            change_summary=f"AUTO-FIX: {direction[:200]}",
                        ),
                        stats=ExecutionStats(),
                        created_at=datetime.now(timezone.utc).isoformat(),
                        created_by="auto_fix",
                    )

                    # Atomic evolution: register new + deactivate parent
                    self.version_store.evolve_skill(parent.skill_id, new_version, deactivate_parent=True)

                    # Save post-evolution snapshot
                    new_snapshot = snapshot_skill_dir(str(skill_dir))
                    self.version_store.save_snapshot(new_skill_id, new_snapshot)

                    # Update tracking
                    self._evolution_history[skill_id] = task_id
                    self._last_evolution_time[skill_id] = time.time()
                    self._last_evolution_time[new_skill_id] = time.time()
                    self._last_evolution_time[f"name:{parent.name}"] = time.time()

                    logger.info(
                        "Successfully fixed %s: %s (gen %d)",
                        parent.name,
                        new_skill_id,
                        new_gen,
                    )
                    return new_version

                except Exception as e:
                    # Rollback: restore original file content on any failure
                    Path(skill_md_path).write_text(original_file_content, encoding="utf-8")
                    logger.error("Evolution failed, restored original SKILL.md: %s", e)
                    raise

            except GovernanceError as e:
                logger.warning("Governance violation on attempt %d: %s", attempt, e)
                prompt = self._build_retry_prompt(prompt, patch_text, [str(e)])
                continue
            except PatchError as e:
                logger.warning("Patch application failed on attempt %d: %s", attempt, e)
                prompt = self._build_retry_prompt(prompt, patch_text, [str(e)])
                continue
            except Exception as e:
                logger.error("Unexpected error on attempt %d: %s", attempt, e)
                continue

        logger.error(
            "All %d fix attempts failed for %s",
            MAX_FIX_ATTEMPTS,
            parent.name,
        )
        return None

    # -----------------------------------------------------------------
    # Phase 4: AUTO-IMPROVE — evolve_derived
    # -----------------------------------------------------------------

    def evolve_derived(
        self,
        parent_id: str,
        direction: str,
        new_name: str | None = None,
        task_id: str = "",
        success_context: str = "",
    ) -> SkillVersion | None:
        """AUTO-IMPROVE: Derive an enhanced skill variant from a successful parent.

        Unlike evolve_fix():
        - Parent stays ACTIVE (derived branches, does not replace)
        - New skill gets a specialized name if provided
        - Requires eval gate comparison (returns the new version for caller to evaluate)

        Args:
            parent_id: The skill to derive from
            direction: What to improve/specialize
            new_name: Optional new name for specialization (defaults to parent name)
            task_id: The task that triggered this derivation
            success_context: What succeeded that prompted this enhancement

        Returns:
            New SkillVersion if derivation succeeded, None if failed.
        """
        # Get current skill version
        parent = self.version_store.get_skill(parent_id)
        if not parent:
            logger.error("Skill %s not found in version store", parent_id)
            return None

        # Resolve the effective name (custom or inherited)
        effective_name = new_name if new_name else parent.name

        # Check generation cap
        if parent.lineage.generation >= MAX_GENERATION:
            logger.warning(
                "Skill %s at generation %d — max reached, skipping derivation",
                parent.name,
                parent.lineage.generation,
            )
            return None

        # Check cooldown (by skill_id AND by skill name to prevent bypass)
        last_time = max(
            self._last_evolution_time.get(parent_id, 0),
            self._last_evolution_time.get(f"name:{parent.name}", 0),
        )
        if time.time() - last_time < COOLDOWN_SECONDS:
            logger.info("Skill %s in cooldown, skipping derivation", parent.name)
            return None

        # Anti-loop: same task can't trigger derivation twice
        if self._evolution_history.get(parent_id) == task_id and task_id:
            logger.info(
                "Skill %s already derived for task %s, skipping",
                parent.name,
                task_id,
            )
            return None

        # Load skill directory content
        skill_dir = Path(parent.path) if parent.path else self.skills_base_dir / parent.name
        if not skill_dir.exists():
            logger.error("Skill directory not found: %s", skill_dir)
            return None

        current_content = snapshot_skill_dir(str(skill_dir))
        if not current_content:
            logger.error("No content found in skill directory: %s", skill_dir)
            return None

        # Save pre-evolution snapshot
        self.version_store.save_snapshot(parent_id, current_content)

        # Get recent analyses for context
        recent_analyses = self.version_store.get_analyses_for_skill(parent_id, limit=5)
        success_history = self._format_success_history(recent_analyses)

        # Build derived prompt
        prompt = self._build_derived_prompt(
            skill_name=parent.name,
            effective_name=effective_name,
            skill_content=current_content,
            direction=direction,
            success_context=success_context,
            success_history=success_history,
        )

        # Apply-retry cycle
        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            logger.info(
                "Derivation attempt %d/%d for %s",
                attempt,
                MAX_FIX_ATTEMPTS,
                parent.name,
            )

            try:
                # Call LLM to generate enhanced content
                new_content = self._call_llm(prompt)
                if not new_content:
                    logger.warning("LLM returned empty response on attempt %d", attempt)
                    continue

                # Validate the result (allow name change if new_name given)
                validation_errors = self._validate_derived(new_content, effective_name)
                if validation_errors:
                    logger.warning(
                        "Validation failed on attempt %d: %s",
                        attempt,
                        validation_errors,
                    )
                    prompt = self._build_retry_prompt(prompt, new_content, validation_errors)
                    continue

                # Write the derived content to the skill directory
                skill_md_path = str(skill_dir / "SKILL.md")
                original_file_content = Path(skill_md_path).read_text(encoding="utf-8")
                Path(skill_md_path).write_text(new_content, encoding="utf-8")

                try:
                    # Generate diff
                    old_content = current_content.get("SKILL.md", "")
                    content_diff = "\n".join(
                        difflib.unified_diff(
                            old_content.split("\n"),
                            new_content.split("\n"),
                            fromfile=f"{parent.name}/SKILL.md (v{parent.lineage.generation})",
                            tofile=f"{effective_name}/SKILL.md (v{parent.lineage.generation + 1})",
                            lineterm="",
                        )
                    )

                    # Create new version
                    new_gen = parent.lineage.generation + 1
                    new_skill_id = generate_skill_id(effective_name, Origin.DERIVED, new_gen)

                    new_version = SkillVersion(
                        skill_id=new_skill_id,
                        name=effective_name,
                        description=parent.description,
                        path=str(skill_dir),
                        is_active=True,
                        version=f"1.{new_gen}.0",
                        lineage=SkillLineage(
                            origin=Origin.DERIVED,
                            generation=new_gen,
                            parent_ids=[parent.skill_id],
                            content_diff=content_diff,
                            change_summary=f"AUTO-IMPROVE: {direction[:200]}",
                        ),
                        stats=ExecutionStats(),
                        created_at=datetime.now(timezone.utc).isoformat(),
                        created_by="auto_improve",
                    )

                    # Atomic evolution: register new, parent STAYS ACTIVE
                    self.version_store.evolve_skill(parent.skill_id, new_version, deactivate_parent=False)

                    # Save post-evolution snapshot
                    new_snapshot = snapshot_skill_dir(str(skill_dir))
                    self.version_store.save_snapshot(new_skill_id, new_snapshot)

                    # Update tracking
                    self._evolution_history[parent_id] = task_id
                    self._last_evolution_time[parent_id] = time.time()
                    self._last_evolution_time[new_skill_id] = time.time()
                    self._last_evolution_time[f"name:{parent.name}"] = time.time()
                    if effective_name != parent.name:
                        self._last_evolution_time[f"name:{effective_name}"] = time.time()

                    logger.info(
                        "Successfully derived %s from %s: %s (gen %d)",
                        effective_name,
                        parent.name,
                        new_skill_id,
                        new_gen,
                    )
                    return new_version

                except Exception as e:
                    # Rollback: restore original file content on any failure
                    Path(skill_md_path).write_text(original_file_content, encoding="utf-8")
                    logger.error("Derivation failed, restored original SKILL.md: %s", e)
                    raise

            except Exception as e:
                logger.error(
                    "Unexpected error on derivation attempt %d: %s",
                    attempt,
                    e,
                )
                continue

        logger.error(
            "All %d derivation attempts failed for %s",
            MAX_FIX_ATTEMPTS,
            parent.name,
        )
        return None

    def detect_improvement_candidates(self, min_completions: int = 10, min_quality: float = 0.7) -> list[dict]:
        """Find skills that consistently succeed and could benefit from specialization.

        Scans all active skills via get_skill_health() and filters for those
        with high completion rates and consistent quality. Then checks
        get_analyses_for_skill() for DERIVED-type evolution suggestions.

        Args:
            min_completions: Minimum number of completions to consider
            min_quality: Minimum average quality score to consider

        Returns:
            List of dicts with: skill_id, skill_name, suggestion
        """
        candidates: list[dict] = []

        try:
            health_list = self.version_store.get_skill_health(min_selections=min_completions)
        except Exception as e:
            logger.error("Failed to get skill health: %s", e)
            return candidates

        for health in health_list:
            # Only consider healthy skills with sufficient quality
            if not health.is_healthy:
                continue
            if health.completions < min_completions:
                continue
            if health.avg_quality_score < min_quality:
                continue

            # Check for DERIVED-type suggestions in analyses
            analyses = self.version_store.get_analyses_for_skill(health.skill_id, limit=20)
            derived_suggestions: list[str] = []
            for analysis in analyses:
                suggestions = analysis.get("evolution_suggestions", [])
                for s in suggestions:
                    if isinstance(s, dict) and s.get("type") == "DERIVED":
                        derived_suggestions.append(s.get("direction", ""))

            # Build candidate entry
            if derived_suggestions:
                suggestion = derived_suggestions[0]
            else:
                suggestion = (
                    f"High-performing skill ({health.completions} completions, "
                    f"quality={health.avg_quality_score:.2f}) — "
                    "consider specialization"
                )

            candidates.append(
                {
                    "skill_id": health.skill_id,
                    "skill_name": health.skill_name,
                    "suggestion": suggestion,
                }
            )

        return candidates

    def _build_derived_prompt(
        self,
        skill_name: str,
        effective_name: str,
        skill_content: dict[str, str],
        direction: str,
        success_context: str,
        success_history: str,
    ) -> str:
        """Build the prompt for AUTO-IMPROVE derivation."""
        skill_md = skill_content.get("SKILL.md", "(not found)")
        metadata = skill_content.get("metadata.json", "(not found)")

        name_instruction = (
            f'The derived skill name MUST be "{effective_name}"'
            if effective_name != skill_name
            else f'The skill name MUST remain "{skill_name}"'
        )

        return f"""You are a skill evolution engine. Enhance the successful skill below.

## Source Skill: {skill_name}

### Current SKILL.md
```
{skill_md}
```

### Current metadata.json
```
{metadata}
```

### Enhancement Direction
{direction}

### Success Pattern
{success_context or "(no specific context)"}

### Recent Success History
{success_history or "(no history)"}

## Instructions
- This is an enhancement, not a fix — the parent skill works well
- Derive a specialized/improved variant based on the direction above
- {name_instruction}
- Output MUST be a valid SKILL.md file
- Preserve the frontmatter format (--- delimiters with name:, description:)
- Keep the core capability intact while adding the enhancement

## Output Format
Output the complete enhanced SKILL.md content. Do NOT wrap in code fences."""

    def _validate_derived(self, new_content: str, expected_name: str) -> list[str]:
        """Validate a derived SKILL.md.

        Same as _validate_fix but uses the effective name (which may differ
        from the parent name when new_name is provided).

        Returns list of validation errors. Empty means valid.
        """
        return self._validate_fix(new_content, expected_name)

    def _format_success_history(self, analyses: list[dict]) -> str:
        """Format recent analyses into a readable success history."""
        if not analyses:
            return ""

        lines = []
        for a in analyses[:5]:
            status = "completed" if a.get("completed") else "FAILED"
            quality = a.get("quality_score", "?")
            lines.append(f"- [{status}] quality={quality}")

        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------

    def _call_llm(self, prompt: str) -> str:
        """Call LLM to generate a patch. Uses stdin for large prompts."""
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # Prevent recursion

        try:
            result = subprocess.run(
                [
                    "claude",
                    "--print",
                    "-p",
                    "-",
                    "--output-format",
                    "text",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode != 0:
                logger.warning("LLM call failed: %s", result.stderr[:500])
                return ""
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.warning("LLM call timed out")
            return ""
        except FileNotFoundError:
            logger.warning("claude binary not found")
            return ""

    def _build_fix_prompt(
        self,
        skill_name: str,
        skill_content: dict[str, str],
        direction: str,
        failure_context: str,
        failure_history: str,
        forbidden_targets: list[str],
    ) -> str:
        """Build the prompt for AUTO-FIX."""
        skill_md = skill_content.get("SKILL.md", "(not found)")
        metadata = skill_content.get("metadata.json", "(not found)")

        forbidden_str = ", ".join(forbidden_targets) if forbidden_targets else "(none)"

        return f"""You are a skill evolution engine. Fix the failing skill below.

## Skill: {skill_name}

### Current SKILL.md
```
{skill_md}
```

### Current metadata.json
```
{metadata}
```

### What needs to be fixed
{direction}

### Recent failure context
{failure_context or "(no specific context)"}

### Failure history
{failure_history or "(no history)"}

## Constraints
- Do NOT modify these sections: {forbidden_str}
- The skill name MUST remain "{skill_name}"
- Output MUST be a valid SKILL.md file
- Preserve the frontmatter format (--- delimiters with name:, description:)

## Output Format
Output the complete fixed SKILL.md content. Do NOT wrap in code fences.
If making targeted changes, use SEARCH/REPLACE format:
<<<<<<< SEARCH
old content to find
=======
new replacement content
>>>>>>> REPLACE

For full rewrites, output the complete file content directly."""

    def _build_retry_prompt(
        self,
        original_prompt: str,
        failed_patch: str,
        errors: list[str],
    ) -> str:
        """Build a retry prompt with error feedback."""
        error_str = "\n".join(f"- {e}" for e in errors)
        return f"""{original_prompt}

## Previous attempt failed
Your previous patch:
```
{failed_patch[:2000]}
```

Errors:
{error_str}

Fix these errors and try again."""

    def _validate_fix(self, new_content: str, expected_name: str) -> list[str]:
        """Validate a fixed SKILL.md.

        Returns list of validation errors. Empty means valid.
        """
        errors = []

        if not new_content.strip():
            errors.append("Fixed content is empty")
            return errors

        # Check frontmatter exists
        if not new_content.startswith("---"):
            # Allow content without frontmatter (body-only fix)
            pass
        else:
            # Parse frontmatter
            parts = new_content.split("---", 2)
            if len(parts) < 3:
                errors.append("Malformed frontmatter (missing closing ---)")
            else:
                frontmatter = parts[1].strip()

                # Check name is preserved
                name_found = False
                for line in frontmatter.split("\n"):
                    if line.strip().startswith("name:"):
                        name_value = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if name_value != expected_name:
                            errors.append(f"Skill name changed from '{expected_name}' to '{name_value}'")
                        name_found = True
                        break

                if not name_found:
                    errors.append("name: field missing from frontmatter")

        # Check minimum content length
        if len(new_content.strip()) < 50:
            errors.append("Fixed content is suspiciously short (< 50 chars)")

        return errors

    def _format_failure_history(self, analyses: list[dict]) -> str:
        """Format recent analyses into a readable failure history."""
        if not analyses:
            return ""

        lines = []
        for a in analyses[:5]:
            completed = "completed" if a.get("completed") else "FAILED"
            reason = a.get("failure_reason", "")
            quality = a.get("quality_score", "?")
            lines.append(f"- [{completed}] quality={quality}: {reason}")

        return "\n".join(lines)

    def check_cooldown(self, skill_id: str, skill_name: str | None = None) -> bool:
        """Check if a skill is in cooldown period.

        Checks both skill_id and skill name to prevent cooldown bypass
        via new version IDs.

        Returns True if the skill CAN be evolved (not in cooldown).
        """
        last_time = self._last_evolution_time.get(skill_id, 0)
        if skill_name:
            last_time = max(
                last_time,
                self._last_evolution_time.get(f"name:{skill_name}", 0),
            )
        return time.time() - last_time >= COOLDOWN_SECONDS

    # -----------------------------------------------------------------
    # AUTO-LEARN: Skill Capture from Execution (Phase 5)
    # -----------------------------------------------------------------

    def evolve_captured(
        self,
        pattern_description: str,
        task_examples: list[str],
        suggested_name: str = "",
        task_id: str = "",
    ) -> SkillVersion | None:
        """AUTO-LEARN: Create a brand-new skill from a successful execution pattern.

        Unlike evolve_fix()/evolve_derived():
        - No parent skill (origin=CAPTURED, generation=0)
        - Creates the entire skill from scratch
        - Starts in quarantine (needs 5 successes before full activation)

        Args:
            pattern_description: What pattern was observed
            task_examples: List of task descriptions that exhibited this pattern
            suggested_name: Optional name suggestion
            task_id: The triggering task

        Returns:
            New SkillVersion if capture succeeded, None if failed.
        """
        # Validate minimum task examples (anti-noise)
        if len(task_examples) < MIN_TASK_EXAMPLES:
            logger.warning(
                "AUTO-LEARN: need %d+ task examples, got %d — skipping capture",
                MIN_TASK_EXAMPLES,
                len(task_examples),
            )
            return None

        # Rate limit: max captures per day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._captures_today_date != today:
            # New day — reset counter
            self._captures_today = 0
            self._captures_today_date = today

        if self._captures_today >= MAX_CAPTURES_PER_DAY:
            logger.warning(
                "AUTO-LEARN: daily capture limit reached (%d/%d) — skipping",
                self._captures_today,
                MAX_CAPTURES_PER_DAY,
            )
            return None

        # Determine skill name
        skill_name = self._sanitize_skill_name(suggested_name or pattern_description[:60])

        # Duplicate detection: check existing skills by name similarity
        duplicate = self._check_duplicate_skill(skill_name)
        if duplicate:
            logger.info(
                "AUTO-LEARN: similar skill '%s' already exists — skipping capture of '%s'",
                duplicate,
                skill_name,
            )
            return None

        # Build capture prompt and call LLM
        prompt = self._build_capture_prompt(
            pattern_description=pattern_description,
            task_examples=task_examples,
            suggested_name=skill_name,
        )

        llm_response = self._call_llm(prompt)
        if not llm_response:
            logger.warning("AUTO-LEARN: LLM returned empty response for capture")
            return None

        # Validate LLM output has minimum viable content
        if len(llm_response.strip()) < 50:
            logger.warning(
                "AUTO-LEARN: LLM response too short (%d chars) — skipping",
                len(llm_response.strip()),
            )
            return None

        # Extract name from generated SKILL.md if present
        generated_name = self._extract_name_from_skill_md(llm_response)
        if generated_name:
            skill_name = self._sanitize_skill_name(generated_name)

        # Create skill directory
        skill_dir = self.skills_base_dir / skill_name
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(
                "AUTO-LEARN: failed to create skill directory %s: %s",
                skill_dir,
                e,
            )
            return None

        # Write SKILL.md
        skill_md_path = skill_dir / "SKILL.md"
        skill_md_path.write_text(llm_response, encoding="utf-8")

        # Write metadata.json with quarantine flag
        metadata = {
            "name": skill_name,
            "origin": "captured",
            "optimization_enabled": False,
            "quarantine": True,
            "quarantine_successes_required": 5,
            "quarantine_successes": 0,
            "captured_from": {
                "pattern_description": pattern_description[:500],
                "task_examples": task_examples[:10],
                "task_id": task_id,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_path = skill_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        # Generate description from pattern
        description = f"AUTO-LEARN: {pattern_description[:200]}"

        # Create SkillVersion record
        skill_id = generate_skill_id(skill_name, Origin.CAPTURED, 0)
        new_version = SkillVersion(
            skill_id=skill_id,
            name=skill_name,
            description=description,
            path=str(skill_dir),
            is_active=True,
            version="1.0.0",
            lineage=SkillLineage(
                origin=Origin.CAPTURED,
                generation=0,
                parent_ids=[],
                change_summary=f"AUTO-LEARN: {pattern_description[:200]}",
            ),
            stats=ExecutionStats(),
            created_at=datetime.now(timezone.utc).isoformat(),
            created_by="auto_capture",
        )

        # Register in version store
        self.version_store.register_skill(new_version)

        # Save initial snapshot
        snapshot_content = snapshot_skill_dir(str(skill_dir))
        self.version_store.save_snapshot(skill_id, snapshot_content)

        # Update rate limit counter
        self._captures_today += 1

        # Track evolution
        if task_id:
            self._evolution_history[skill_id] = task_id

        logger.info(
            "AUTO-LEARN: captured new skill '%s' (%s) from %d task examples",
            skill_name,
            skill_id,
            len(task_examples),
        )
        return new_version

    def detect_novel_patterns(self, min_occurrences: int = 2) -> list[dict]:
        """Find tasks that succeeded without any skill applied — potential capture candidates.

        Queries execution_analyses for tasks where all judgments have applied=False
        but the task still succeeded (completed=True).

        Args:
            min_occurrences: Minimum number of similar unapplied-but-successful
                tasks to qualify as a pattern.

        Returns:
            List of dicts with: task_ids, description_pattern
        """

        try:
            conn = self.version_store._get_conn()
        except Exception:
            logger.warning("detect_novel_patterns: could not connect to DB")
            return []

        try:
            # Find tasks where no skill was applied but work completed
            rows = conn.execute(
                """SELECT task_id, skill_name
                   FROM execution_analyses
                   WHERE applied = 0 AND completed = 1
                   ORDER BY analyzed_at DESC
                   LIMIT 200"""
            ).fetchall()

            if not rows:
                return []

            # Group by task_id — a task is "novel" if ALL its judgments
            # have applied=False
            task_skills: dict[str, list[str]] = {}
            for row in rows:
                task_id = row["task_id"]
                task_skills.setdefault(task_id, []).append(row["skill_name"])

            # Check which tasks had any applied=True judgments
            applied_tasks: set[str] = set()
            if task_skills:
                placeholders = ",".join("?" for _ in task_skills)
                applied_rows = conn.execute(
                    f"""SELECT DISTINCT task_id
                       FROM execution_analyses
                       WHERE applied = 1 AND task_id IN ({placeholders})""",  # noqa: S608
                    list(task_skills.keys()),
                ).fetchall()
                applied_tasks = {r["task_id"] for r in applied_rows}

            # Novel tasks = completed without any skill applied
            novel_task_ids = [tid for tid in task_skills if tid not in applied_tasks]

            if len(novel_task_ids) < min_occurrences:
                return []

            # Group novel tasks into pattern clusters (by skill names attempted)
            # Tasks that tried the same set of skills form a pattern
            patterns: dict[str, list[str]] = {}
            for tid in novel_task_ids:
                key = "|".join(sorted(set(task_skills[tid])))
                patterns.setdefault(key, []).append(tid)

            results = []
            for pattern_key, tids in patterns.items():
                if len(tids) >= min_occurrences:
                    skills_attempted = pattern_key.split("|") if pattern_key else []
                    results.append(
                        {
                            "task_ids": tids,
                            "description_pattern": (
                                f"Tasks succeeded without applying skills: "
                                f"{', '.join(skills_attempted) if skills_attempted else 'none attempted'}"
                            ),
                        }
                    )

            return results
        except Exception as e:
            logger.warning("detect_novel_patterns: query failed: %s", e)
            return []
        finally:
            conn.close()

    def _build_capture_prompt(
        self,
        pattern_description: str,
        task_examples: list[str],
        suggested_name: str,
    ) -> str:
        """Build the prompt for AUTO-LEARN skill capture."""
        examples_str = "\n".join(f"  {i + 1}. {ex[:300]}" for i, ex in enumerate(task_examples[:10]))

        return f"""You are a skill creation engine. Create a brand-new SKILL.md file from scratch \
based on a successful execution pattern.

## Observed Pattern
{pattern_description}

## Task Examples That Exhibited This Pattern
{examples_str}

## Suggested Skill Name
{suggested_name}

## Requirements
Generate a complete SKILL.md file with:
1. YAML frontmatter (between --- delimiters) containing:
   - name: the skill name (use the suggested name or improve it)
   - description: a clear one-line description
   - activation:
     keywords: [list, of, activation, keywords]
   - tool_doctrine: (relevant tool workflows)
   - output_contract:
     required: [list of required outputs]
2. A body section with:
   - A heading with the skill name
   - Clear step-by-step instructions
   - Expected inputs and outputs
   - Error handling guidance

## Output Format
Output the complete SKILL.md content directly. Do NOT wrap in code fences.
The output must start with --- (YAML frontmatter delimiter)."""

    def _sanitize_skill_name(self, name: str) -> str:
        """Sanitize a string into a valid skill directory name.

        Converts to lowercase, replaces non-alphanumeric with hyphens,
        collapses consecutive hyphens, and strips leading/trailing hyphens.
        """
        # Lowercase and replace non-alphanumeric (except hyphens) with hyphens
        sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
        # Collapse consecutive hyphens
        sanitized = re.sub(r"-+", "-", sanitized)
        # Strip leading/trailing hyphens
        sanitized = sanitized.strip("-")
        # Truncate to reasonable length
        if len(sanitized) > 60:
            sanitized = sanitized[:60].rstrip("-")
        # Fallback if empty
        if not sanitized:
            sanitized = f"captured-skill-{int(time.time())}"
        return sanitized

    def _check_duplicate_skill(self, skill_name: str) -> str | None:
        """Check if a skill with a similar name already exists.

        Uses SequenceMatcher ratio for fuzzy name comparison.

        Returns:
            The name of the duplicate skill if found, None otherwise.
        """
        existing_skills = self.version_store.list_skills(active_only=True)
        for skill in existing_skills:
            ratio = difflib.SequenceMatcher(None, skill_name.lower(), skill.name.lower()).ratio()
            if ratio >= DUPLICATE_NAME_THRESHOLD:
                return skill.name
        return None

    def _extract_name_from_skill_md(self, content: str) -> str | None:
        """Extract the name field from SKILL.md frontmatter.

        Returns the name string if found, None otherwise.
        """
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter = parts[1]
        for line in frontmatter.split("\n"):
            stripped = line.strip()
            if stripped.startswith("name:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
        return None

    def get_evolution_history(self) -> dict[str, str]:
        """Return the evolution history (skill_id -> last_task_id)."""
        return dict(self._evolution_history)
