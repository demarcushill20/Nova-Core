"""Task Specification Generator — converts Decisions into TASKS/*.md files."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from novatrade.autonomy.decision_engine import ActionMode, Decision

log = logging.getLogger("novatrade.autonomy.task_generator")


class TaskSpec(BaseModel):
    """Specification for a generated task file."""

    title: str
    body: str
    priority: str = "medium"  # high / medium / low
    category: str = ""  # research / plan / execute / repair / validate
    target_dimension: str | None = None
    goal_justification: str = ""  # "Why am I doing this?"
    estimated_effort: str = "medium"  # light / medium / heavy
    auto_execute: bool = True  # autonomy-generated tasks execute without human gating


class TaskSpecGenerator:
    """Converts Decision objects into concrete TASKS/*.md files."""

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self.tasks_dir = Path(base_path) / "TASKS"

    def from_decision(self, decision: Decision) -> TaskSpec:
        """Generate a TaskSpec from a Decision."""
        generators = {
            ActionMode.RESEARCH: self._research_spec,
            ActionMode.PLAN: self._plan_spec,
            ActionMode.EXECUTE: self._execute_spec,
            ActionMode.REPAIR: self._repair_spec,
            ActionMode.VALIDATE: self._validate_spec,
        }
        generator = generators.get(decision.mode)
        if generator is None:
            # MONITOR doesn't generate tasks
            return TaskSpec(
                title="Monitor system health",
                body="All systems nominal — no action required.",
                priority="low",
                category="monitor",
                goal_justification=decision.reason,
            )
        return generator(decision)

    # Recently-completed tasks within this window are treated as duplicates,
    # preventing the decision engine from regenerating the same task right
    # after it finishes.
    DONE_DEDUP_WINDOW_S: float = 24 * 3600  # 24 hours

    def has_pending_task(self, category: str, target_dimension: str | None) -> bool:
        """Check if a pending *or recently completed* task exists for this category+dimension.

        When target_dimension is None, matches on category slug alone
        (e.g. "validate_recent_improvements" for VALIDATE tasks).

        A ``.done`` file counts as a match if its mtime is within
        ``DONE_DEDUP_WINDOW_S`` (default 24 h), preventing the engine from
        regenerating a task that was only just completed.
        """
        if not self.tasks_dir.exists():
            return False
        if target_dimension:
            slug_fragment = self._slugify(f"{category} {target_dimension.replace('_', ' ')}")
        else:
            slug_fragment = self._slugify(category)

        now = time.time()
        for f in self.tasks_dir.iterdir():
            if slug_fragment not in f.name:
                continue
            # Active tasks — always count as duplicates
            if f.suffix in (".md", ".inprogress"):
                return True
            # Recently-completed tasks — count if within dedup window
            if f.name.endswith(".done"):
                try:
                    age = now - f.stat().st_mtime
                    if age < self.DONE_DEDUP_WINDOW_S:
                        return True
                except OSError:
                    continue
        return False

    def write_task_file(self, spec: TaskSpec, skip_dedup: bool = False) -> Path | None:
        """Write a TaskSpec to TASKS/<seq>_<slug>.md.

        Returns None if a duplicate pending task already exists (unless skip_dedup=True).
        """
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        # Dedup: don't create duplicate tasks for the same category(+dimension).
        # When target_dimension is None (e.g. VALIDATE), matches on category alone.
        if not skip_dedup and spec.category and self.has_pending_task(spec.category, spec.target_dimension):
            log.info(
                "Skipping duplicate task: %s/%s already pending",
                spec.category,
                spec.target_dimension,
            )
            return None

        seq = self._next_sequence()
        slug = self._slugify(spec.title)
        filename = f"{seq:04d}_{slug}.md"
        path = self.tasks_dir / filename

        # Escape quotes in justification for YAML safety
        safe_justification = spec.goal_justification.replace('"', '\\"')

        frontmatter = (
            f"---\n"
            f"priority: {spec.priority}\n"
            f"category: {spec.category}\n"
            f"target_dimension: {spec.target_dimension or 'none'}\n"
            f'goal_justification: "{safe_justification}"\n'
            f"estimated_effort: {spec.estimated_effort}\n"
            f"auto_execute: {str(spec.auto_execute).lower()}\n"
            f'generated_at: "{datetime.now(timezone.utc).isoformat()}"\n'
            f"source: autonomy-decision-engine\n"
            f"---\n\n"
        )

        content = frontmatter + f"# {spec.title}\n\n{spec.body}\n"

        # Atomic write via tempfile + os.replace
        fd, tmp_path = tempfile.mkstemp(dir=str(self.tasks_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        log.info("Generated task: %s", path.name)
        return path

    def _next_sequence(self) -> int:
        """Determine the next task sequence number."""
        if not self.tasks_dir.exists():
            return 1
        max_seq = 0
        for f in self.tasks_dir.iterdir():
            match = re.match(r"^(\d+)_", f.name)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        return max_seq + 1

    @staticmethod
    def _slugify(text: str, max_len: int = 60) -> str:
        """Convert title to a filesystem-safe slug."""
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
        return slug[:max_len]

    def _research_spec(self, decision: Decision) -> TaskSpec:
        dim = decision.target_dimension or "unknown"
        actions = "\n".join(f"- {a}" for a in decision.suggested_actions)
        return TaskSpec(
            title=f"Research {dim.replace('_', ' ')} gaps",
            body=(
                f"## Objective\n"
                f"Fill knowledge gaps in the **{dim}** dimension.\n\n"
                f"## Context\n"
                f"{decision.reason}\n\n"
                f"## Actions\n{actions}\n\n"
                f"## Expected Outcome\n"
                f"Clear understanding of current {dim} state with actionable findings.\n"
                f"Write a diagnostic report to OUTPUT/ with specific findings."
            ),
            priority="medium",
            category="research",
            target_dimension=dim,
            goal_justification=decision.reason,
            estimated_effort="medium",
        )

    def _plan_spec(self, decision: Decision) -> TaskSpec:
        dim = decision.target_dimension or "unknown"
        actions = "\n".join(f"- {a}" for a in decision.suggested_actions)
        return TaskSpec(
            title=f"Plan {dim.replace('_', ' ')} improvement",
            body=(
                f"## Objective\n"
                f"Create an improvement plan for the **{dim}** dimension.\n\n"
                f"## Context\n"
                f"{decision.reason}\n\n"
                f"## Actions\n{actions}\n\n"
                f"## Expected Outcome\n"
                f"Detailed step-by-step plan to raise {dim} score above target.\n"
                f"Write plan to OUTPUT/ and create follow-up EXECUTE task."
            ),
            priority="high",
            category="plan",
            target_dimension=dim,
            goal_justification=decision.reason,
            estimated_effort="medium",
        )

    def _execute_spec(self, decision: Decision) -> TaskSpec:
        dim = decision.target_dimension or "unknown"
        actions = "\n".join(f"- {a}" for a in decision.suggested_actions)
        return TaskSpec(
            title=f"Execute {dim.replace('_', ' ')} plan",
            body=(
                f"## Objective\n"
                f"Execute the existing improvement plan for **{dim}**.\n\n"
                f"## Context\n"
                f"{decision.reason}\n\n"
                f"## Actions\n{actions}\n\n"
                f"## Autonomy Note\n"
                f"This task was auto-generated by the decision engine. Execute immediately.\n"
                f"Do NOT wait for human approval — take corrective action now.\n\n"
                f"## Expected Outcome\n"
                f"Measurable improvement in {dim} score."
            ),
            priority="high",
            category="execute",
            target_dimension=dim,
            goal_justification=decision.reason,
            estimated_effort="heavy",
        )

    def _repair_spec(self, decision: Decision) -> TaskSpec:
        dim = decision.target_dimension or "unknown"
        actions = "\n".join(f"- {a}" for a in decision.suggested_actions)
        return TaskSpec(
            title=f"Repair {dim.replace('_', ' ')} regression",
            body=(
                f"## Objective\n"
                f"Fix the detected regression in **{dim}**.\n\n"
                f"## Context\n"
                f"{decision.reason}\n\n"
                f"## Actions\n{actions}\n\n"
                f"## Autonomy Note\n"
                f"This is a CRITICAL auto-generated repair task. Execute immediately.\n"
                f"Do NOT wait for human approval — diagnose and fix the issue now.\n"
                f"If you cannot fix it, escalate via Telegram alert.\n\n"
                f"## Expected Outcome\n"
                f"Restore {dim} score to previous level or higher."
            ),
            priority="high",
            category="repair",
            target_dimension=dim,
            goal_justification=decision.reason,
            estimated_effort="heavy",
        )

    def _validate_spec(self, decision: Decision) -> TaskSpec:
        actions = "\n".join(f"- {a}" for a in decision.suggested_actions)
        return TaskSpec(
            title="Validate recent improvements",
            body=(
                f"## Objective\n"
                f"Verify that recent improvements are stable.\n\n"
                f"## Context\n"
                f"{decision.reason}\n\n"
                f"## Actions\n{actions}\n\n"
                f"## Expected Outcome\n"
                f"Confirmation that all dimensions remain above target."
            ),
            priority="medium",
            category="validate",
            target_dimension=decision.target_dimension,
            goal_justification=decision.reason,
            estimated_effort="light",
        )
