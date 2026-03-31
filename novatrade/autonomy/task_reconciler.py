"""Task Reconciler — closes the task->outcome->learning loop.

Run at the start of each autonomy cycle to:
1. Find recently completed tasks (.done files)
2. Parse their decision_id and sub_goal_id from frontmatter
3. Check current dimension score vs. success_metric
4. Update the sub-goal's evidence and status
5. Feed the result back into OutcomeAnalyzer
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.task_reconciler")

# How far back to look for completed tasks (seconds)
_LOOKBACK_SECONDS = 24 * 3600  # 24 hours


@dataclass
class TaskReconciliation:
    """Result of reconciling a single completed task."""

    task_file: str
    decision_id: str
    sub_goal_id: str | None
    success_metric: str
    metric_met: bool
    expired: bool
    notes: str


class TaskReconciler:
    """Scan completed tasks and reconcile against success criteria."""

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self._base_path = Path(base_path)
        self._tasks_dir = self._base_path / "TASKS"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconcile(self, report: ProgressReport) -> list[TaskReconciliation]:
        """Scan .done files from the last 24h and check success metrics.

        Returns a list of TaskReconciliation results for tasks that have
        a ``decision_id`` in their frontmatter.
        """
        if not self._tasks_dir.exists():
            return []

        now = time.time()
        results: list[TaskReconciliation] = []

        for path in self._tasks_dir.iterdir():
            if not path.name.endswith(".done"):
                continue

            # Skip old files
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > _LOOKBACK_SECONDS:
                continue

            # Parse frontmatter
            try:
                fm = self._parse_frontmatter(path)
            except Exception as exc:
                log.debug("Failed to parse frontmatter for %s: %s", path.name, exc)
                continue

            decision_id = fm.get("decision_id", "")
            if not decision_id:
                continue  # nothing to reconcile without a decision link

            sub_goal_id = fm.get("sub_goal_id") or None
            success_metric = fm.get("success_metric", "")
            generated_at = fm.get("generated_at", "")
            expiry_minutes = self._parse_int(fm.get("expiry_minutes", ""), default=120)

            # Check expiry
            expired = self._check_expiry(generated_at, expiry_minutes, mtime)

            # Check success metric
            metric_met = False
            notes = ""
            if success_metric:
                metric_met = self._check_success_metric(success_metric, report)
                if expired:
                    notes = "Task completed after expiry window"
                elif metric_met:
                    notes = "Success metric met"
                else:
                    notes = "Success metric NOT met"
            else:
                notes = "No success_metric defined — skipping metric check"

            rec = TaskReconciliation(
                task_file=path.name,
                decision_id=decision_id,
                sub_goal_id=sub_goal_id,
                success_metric=success_metric,
                metric_met=metric_met,
                expired=expired,
                notes=notes,
            )
            results.append(rec)

            # Update sub-goal evidence if we have a sub_goal_id
            if sub_goal_id:
                self._update_subgoal(sub_goal_id, rec)

        return results

    # ------------------------------------------------------------------
    # Frontmatter parsing (manual regex, no yaml dependency)
    # ------------------------------------------------------------------

    _FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    _KV_PATTERN = re.compile(r'^(\w+):\s*"?(.*?)"?\s*$')

    def _parse_frontmatter(self, path: Path) -> dict[str, str]:
        """Parse YAML frontmatter from a task file.

        Returns a dict of key-value pairs from the ``---`` delimited block.
        Strips surrounding double-quotes from values.
        """
        text = path.read_text(errors="replace")
        match = self._FM_PATTERN.search(text)
        if not match:
            return {}

        fm: dict[str, str] = {}
        for line in match.group(1).splitlines():
            kv = self._KV_PATTERN.match(line)
            if kv:
                key = kv.group(1)
                value = kv.group(2).strip().strip('"')
                fm[key] = value
        return fm

    # ------------------------------------------------------------------
    # Metric checking
    # ------------------------------------------------------------------

    # Supports formats like:
    #   "system_health >= 70"
    #   "execution_pipeline score >= 80 within 2 hours"
    #   "strategy_validity >= 60"
    _METRIC_PATTERN = re.compile(r"(\w+)\s+(?:score\s+)?([><=!]+)\s+(\d+(?:\.\d+)?)")

    def _check_success_metric(self, metric: str, report: ProgressReport) -> bool:
        """Check if a success metric is met against the current report.

        Parses formats like ``dimension_name >= N`` or
        ``dimension_name score >= N within M hours``.
        Returns False on parse failure (graceful).
        """
        match = self._METRIC_PATTERN.search(metric)
        if not match:
            log.debug("Could not parse success_metric: %s", metric)
            return False

        dimension = match.group(1)
        operator = match.group(2)
        threshold = float(match.group(3))

        # Look up dimension score
        dim_score_obj = report.dimensions.get(dimension)
        if dim_score_obj is None:
            log.debug("Dimension '%s' not found in report", dimension)
            return False

        actual = dim_score_obj.score
        return self._compare(actual, operator, threshold)

    @staticmethod
    def _compare(actual: float, operator: str, threshold: float) -> bool:
        """Compare actual value against threshold using the given operator."""
        ops = {
            ">=": actual >= threshold,
            ">": actual > threshold,
            "<=": actual <= threshold,
            "<": actual < threshold,
            "==": actual == threshold,
            "!=": actual != threshold,
        }
        return ops.get(operator, False)

    # ------------------------------------------------------------------
    # Expiry checking
    # ------------------------------------------------------------------

    def _check_expiry(self, generated_at: str, expiry_minutes: int, completed_at: float) -> bool:
        """Check if task completed after its expiry window.

        Returns True if the task is expired (completed too late).
        Returns False if within window or if timestamps can't be parsed.
        """
        if not generated_at or expiry_minutes <= 0:
            return False

        try:
            gen_dt = datetime.fromisoformat(generated_at)
            # Ensure timezone-aware
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            gen_epoch = gen_dt.timestamp()
        except (ValueError, OSError):
            return False

        deadline = gen_epoch + (expiry_minutes * 60)
        return completed_at > deadline

    # ------------------------------------------------------------------
    # Sub-goal update
    # ------------------------------------------------------------------

    def _update_subgoal(self, sub_goal_id: str, rec: TaskReconciliation) -> None:
        """Update a sub-goal's evidence based on reconciliation result.

        Loads the goal tree, finds the sub-goal, and appends evidence.
        Non-fatal: logs and continues on any failure.
        """
        try:
            from novatrade.autonomy.decomposition import GoalDecomposer

            decomposer = GoalDecomposer(base_path=str(self._base_path))

            # We need to find which goal tree contains this sub-goal.
            # The standard tree is "novatrade_profitable_autonomous"
            tree = decomposer.load("novatrade_100pct_operational")
            if tree is None:
                log.debug("No goal tree found for sub-goal update")
                return

            sg = tree.get_subgoal(sub_goal_id)
            if sg is None:
                log.debug("Sub-goal %s not found in tree", sub_goal_id)
                return

            # Add task to tasks_generated if not already present
            if rec.task_file not in sg.tasks_generated:
                sg.tasks_generated.append(rec.task_file)

            # Add evidence
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if rec.metric_met:
                evidence = f"[{ts}] Task {rec.task_file} succeeded — {rec.notes}"
            elif rec.expired:
                evidence = f"[{ts}] Task {rec.task_file} expired — {rec.notes}"
            else:
                evidence = f"[{ts}] Task {rec.task_file} completed but metric not met — {rec.notes}"
            sg.evidence.append(evidence)
            # Keep only last 10 evidence entries
            sg.evidence = sg.evidence[-10:]
            sg.last_checked = datetime.now(timezone.utc)

            decomposer.persist(tree)
            log.info("Updated sub-goal %s with evidence from %s", sub_goal_id, rec.task_file)

        except Exception as exc:
            log.debug("Failed to update sub-goal %s: %s", sub_goal_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_int(value: str, default: int = 0) -> int:
        """Parse an int from a string, returning default on failure."""
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
