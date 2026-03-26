"""Goal Decomposition Engine — manages sub-goal DAGs and progress tracking."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections import defaultdict, deque
from pathlib import Path

from novatrade.autonomy.decomposition.models import (
    GoalDecomposition,
    SubGoal,
    SubGoalStatus,
)
from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.decomposition.engine")


class GoalDecomposer:
    """Manages goal decomposition trees: progress updates, actionable sub-goals, topological sort."""

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self.base_path = Path(base_path)
        self._state_dir = self.base_path / "STATE" / "goal_trees"

    def update_progress(
        self,
        decomposition: GoalDecomposition,
        report: ProgressReport,
    ) -> GoalDecomposition:
        """Update sub-goal statuses based on current dimension scores.

        WARNING: Mutates the decomposition object in-place and returns it.
        If you need the original state preserved, pass a deep copy.

        Maps dimension scores to sub-goal progress:
        - Score >= 70 (GREEN) → progress 1.0 (completed)
        - Score 40-70 (YELLOW) → progress proportional
        - Score < 40 (RED) → progress 0.0
        """
        dim_scores: dict[str, float] = {}
        for name, dim in report.dimensions.items():
            dim_scores[name] = dim.score

        for sg in decomposition.sub_goals:
            dim_score = dim_scores.get(sg.dimension)
            if dim_score is None:
                continue

            # Map dimension score to sub-goal progress
            if dim_score >= 70:
                new_progress = 1.0
            elif dim_score >= 40:
                new_progress = (dim_score - 40) / 30  # 0.0-1.0 within YELLOW
            else:
                new_progress = 0.0

            # Only update if sub-goal isn't manually completed or skipped
            if sg.status not in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED):
                sg.progress = round(new_progress, 2)
                if new_progress >= 1.0:
                    sg.status = SubGoalStatus.COMPLETED
                elif new_progress > 0:
                    sg.status = SubGoalStatus.IN_PROGRESS
                    sg.blockers = []  # Clear stale blockers
                # Check if blocked by unmet dependencies
                elif self._has_unmet_deps(sg, decomposition):
                    sg.status = SubGoalStatus.BLOCKED
                    sg.blockers = self._get_unmet_dep_names(sg, decomposition)

        return decomposition

    @staticmethod
    def _completed_ids(decomposition: GoalDecomposition) -> set[str]:
        """Return the set of sub-goal IDs that are completed or skipped."""
        return {
            sg.sub_goal_id
            for sg in decomposition.sub_goals
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED)
        }

    def get_actionable_subgoals(self, decomposition: GoalDecomposition) -> list[SubGoal]:
        """Return sub-goals that are actionable: NOT_STARTED or IN_PROGRESS with all deps met."""
        actionable: list[SubGoal] = []
        completed_ids = self._completed_ids(decomposition)

        for sg in decomposition.sub_goals:
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED):
                continue
            # Check all dependencies are met
            deps_met = all(dep in completed_ids for dep in sg.dependencies)
            if deps_met:
                actionable.append(sg)

        # Sort by priority (1=highest)
        actionable.sort(key=lambda s: s.priority)
        return actionable

    def get_blocked_subgoals(
        self,
        decomposition: GoalDecomposition,
    ) -> list[tuple[SubGoal, list[str]]]:
        """Return blocked sub-goals with their unmet dependency IDs."""
        completed_ids = self._completed_ids(decomposition)
        blocked: list[tuple[SubGoal, list[str]]] = []
        for sg in decomposition.sub_goals:
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED):
                continue
            unmet = [dep for dep in sg.dependencies if dep not in completed_ids]
            if unmet:
                blocked.append((sg, unmet))
        return blocked

    def topological_sort(self, decomposition: GoalDecomposition) -> list[SubGoal]:
        """Return sub-goals in dependency-respecting execution order (Kahn's algorithm)."""
        id_to_sg = {sg.sub_goal_id: sg for sg in decomposition.sub_goals}
        in_degree: dict[str, int] = defaultdict(int)
        adjacency: dict[str, list[str]] = defaultdict(list)

        for sg in decomposition.sub_goals:
            if sg.sub_goal_id not in in_degree:
                in_degree[sg.sub_goal_id] = 0

        for src, dst in decomposition.dag_edges:
            adjacency[src].append(dst)
            in_degree[dst] += 1

        # BFS
        queue: deque[str] = deque()
        for sid, deg in in_degree.items():
            if deg == 0:
                queue.append(sid)

        result: list[SubGoal] = []
        while queue:
            node = queue.popleft()
            if node in id_to_sg:
                result.append(id_to_sg[node])
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return result

    def has_cycle(self, decomposition: GoalDecomposition) -> bool:
        """Check if the DAG contains a cycle."""
        sorted_goals = self.topological_sort(decomposition)
        return len(sorted_goals) != len(decomposition.sub_goals)

    @staticmethod
    def _validate_goal_id(goal_id: str) -> str:
        """Ensure goal_id is filesystem-safe (no path traversal)."""
        if not re.match(r"^[a-zA-Z0-9_-]+$", goal_id):
            raise ValueError(f"Invalid goal_id: {goal_id!r} — must be alphanumeric with underscores/hyphens")
        return goal_id

    def persist(self, decomposition: GoalDecomposition) -> None:
        """Save decomposition to STATE/goal_trees/<goal_id>.json."""
        self._validate_goal_id(decomposition.goal_id)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / f"{decomposition.goal_id}.json"

        fd, tmp_path = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(decomposition.model_dump_json(indent=2))
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load(self, goal_id: str) -> GoalDecomposition | None:
        """Load a decomposition from STATE/goal_trees/<goal_id>.json."""
        self._validate_goal_id(goal_id)
        path = self._state_dir / f"{goal_id}.json"
        if not path.exists():
            return None
        try:
            return GoalDecomposition.model_validate_json(path.read_text())
        except Exception as exc:
            log.warning("Failed to load decomposition %s: %s", goal_id, exc)
            return None

    def _has_unmet_deps(self, sg: SubGoal, decomposition: GoalDecomposition) -> bool:
        """Check if a sub-goal has unmet dependencies."""
        completed_ids = self._completed_ids(decomposition)
        return any(dep not in completed_ids for dep in sg.dependencies)

    def _get_unmet_dep_names(self, sg: SubGoal, decomposition: GoalDecomposition) -> list[str]:
        """Get names of unmet dependencies."""
        completed_ids = self._completed_ids(decomposition)
        return [dep for dep in sg.dependencies if dep not in completed_ids]
