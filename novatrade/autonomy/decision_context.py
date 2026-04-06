"""Decision context assembly for the adaptive decision engine."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.decision_context")


class TaskSummary(BaseModel):
    """Summary of a pending or recent task."""

    stem: str
    title: str
    status: str  # "pending", "in_progress", "done", "failed"
    priority: str = "medium"


class GoalSummary(BaseModel):
    """Summary of an active goal from STATE/goals.json."""

    id: int
    text: str
    status: str
    priority: str


class DecisionContext(BaseModel):
    """Everything the decision engine needs to make a choice."""

    progress_report: ProgressReport
    pending_tasks: list[TaskSummary] = Field(default_factory=list)
    active_goals: list[GoalSummary] = Field(default_factory=list)
    recent_decisions: list[dict] = Field(default_factory=list)  # last 5
    recent_outcomes: list[dict] = Field(default_factory=list)  # last 10 resolved outcomes
    effectiveness_summary: dict[str, float] = Field(default_factory=dict)  # mode → avg delta
    time_of_day: str = ""  # "morning", "afternoon", "evening", "night"
    degradation_tier: str = "FULL"  # from self_healing runtime
    open_circuit_breakers: list[str] = Field(default_factory=list)  # tripped breakers
    market_session: str = "closed"  # london/new_york/overlap/asian/closed
    active_investigations: list[str] = Field(default_factory=list)  # in-progress investigation tasks
    actionable_subgoals: list[str] = Field(default_factory=list)  # from goal tree
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextAssembler:
    """Builds a DecisionContext from the current system state."""

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self.base_path = Path(base_path)
        self._tasks_dir = self.base_path / "TASKS"
        self._goals_path = self.base_path / "STATE" / "goals.json"
        self._decisions_path = self.base_path / "STATE" / "decision_history.json"

    async def assemble(self, report: ProgressReport) -> DecisionContext:
        """Build a complete DecisionContext from current state."""
        outcomes = self._load_recent_outcomes()
        return DecisionContext(
            progress_report=report,
            pending_tasks=self._load_pending_tasks(),
            active_goals=self._load_active_goals(),
            recent_decisions=self._load_recent_decisions(),
            recent_outcomes=outcomes,
            effectiveness_summary=self._compute_effectiveness_summary(outcomes),
            time_of_day=self._classify_time(),
            degradation_tier=self._load_degradation_tier(),
            open_circuit_breakers=self._load_open_circuit_breakers(),
            market_session=self._classify_market_session(),
            active_investigations=self._load_active_investigations(),
            actionable_subgoals=self._load_actionable_subgoals(),
        )

    def _load_pending_tasks(self) -> list[TaskSummary]:
        """Load pending tasks from TASKS/ directory."""
        tasks: list[TaskSummary] = []
        if not self._tasks_dir.exists():
            return tasks
        for f in sorted(self._tasks_dir.iterdir()):
            if not f.is_file():
                continue
            # Match .md (pending) and .inprogress (active) files
            if f.name.endswith(".md"):
                stem = f.stem
                # Skip completed/failed/cancelled
                if any(stem.endswith(s) for s in (".done", ".failed", ".cancelled")):
                    continue
                title = stem.split("_", 1)[1] if "_" in stem else stem
                title = title.replace("_", " ")[:80]
                tasks.append(TaskSummary(stem=stem, title=title, status="pending"))
            elif f.suffix == ".inprogress":
                stem = f.stem  # e.g. "0001_repair_strategy.md"
                base_stem = stem.replace(".md", "") if stem.endswith(".md") else stem
                title = base_stem.split("_", 1)[1] if "_" in base_stem else base_stem
                title = title.replace("_", " ")[:80]
                tasks.append(TaskSummary(stem=base_stem, title=title, status="in_progress"))
        return tasks[-20:]  # last 20

    def _load_active_goals(self) -> list[GoalSummary]:
        """Load active goals from STATE/goals.json."""
        if not self._goals_path.exists():
            return []
        try:
            data = json.loads(self._goals_path.read_text())
            goals_list = data.get("goals", []) if isinstance(data, dict) else []
            return [
                GoalSummary(
                    id=g.get("id", 0),
                    text=g.get("text", ""),
                    status=g.get("status", "unknown"),
                    priority=g.get("priority", "normal"),
                )
                for g in goals_list
                if g.get("status") == "active"
            ]
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load goals: %s", exc)
            return []

    def _load_recent_decisions(self, limit: int = 20) -> list[dict]:
        """Load recent decisions from STATE/decision_history.json."""
        if not self._decisions_path.exists():
            return []
        try:
            data = json.loads(self._decisions_path.read_text())
            items = data if isinstance(data, list) else []
            return items[-limit:]
        except (json.JSONDecodeError, OSError):
            return []

    def _load_recent_outcomes(self, limit: int = 10) -> list[dict]:
        """Load recent resolved outcomes from STATE/decision_outcomes.json."""
        outcomes_path = self.base_path / "STATE" / "decision_outcomes.json"
        if not outcomes_path.exists():
            return []
        try:
            data = json.loads(outcomes_path.read_text())
            if not isinstance(data, list):
                return []
            resolved = [d for d in data if d.get("resolved_at") is not None]
            return resolved[-limit:]
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load outcomes: %s", exc)
            return []

    @staticmethod
    def _compute_effectiveness_summary(outcomes: list[dict]) -> dict[str, float]:
        """Compute average delta by mode from recent outcomes."""
        mode_deltas: dict[str, list[float]] = {}
        for o in outcomes:
            mode = o.get("mode", "")
            delta = o.get("delta")
            if mode and delta is not None:
                mode_deltas.setdefault(mode, []).append(delta)
        return {mode: sum(deltas) / len(deltas) for mode, deltas in mode_deltas.items() if deltas}

    def _load_degradation_tier(self) -> str:
        """Load current degradation tier from self-healing state."""
        path = self.base_path / "STATE" / "self_healing_state.json"
        if not path.exists():
            return "FULL"
        try:
            data = json.loads(path.read_text())
            return data.get("current_tier", "FULL")
        except (json.JSONDecodeError, OSError):
            return "FULL"

    def _load_open_circuit_breakers(self) -> list[str]:
        """Load names of open (tripped) circuit breakers."""
        path = self.base_path / "STATE" / "circuit_breaker_registry.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return []
            return [name for name, state in data.items() if isinstance(state, dict) and state.get("state") == "open"]
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _classify_market_session() -> str:
        """Classify current forex market session based on UTC hour and weekday."""
        now = datetime.now(timezone.utc)
        weekday, hour = now.weekday(), now.hour
        # Forex weekend: Friday 22:00 UTC → Sunday 22:00 UTC
        if weekday == 5:  # Saturday
            return "closed"
        if weekday == 6 and hour < 22:  # Sunday before open
            return "closed"
        if weekday == 4 and hour >= 22:  # Friday after close
            return "closed"
        # Weekday sessions
        if 0 <= hour < 7:
            return "asian"
        if 7 <= hour < 13:
            return "london"
        if 13 <= hour < 16:
            return "overlap"
        if 16 <= hour < 21:
            return "new_york"
        return "closed"

    def _load_active_investigations(self) -> list[str]:
        """Find in-progress investigation tasks in TASKS/."""
        investigations: list[str] = []
        if not self._tasks_dir.exists():
            return investigations
        for f in self._tasks_dir.iterdir():
            if f.suffix == ".inprogress" and "investigat" in f.name.lower():
                investigations.append(f.stem)
        return investigations[:10]

    def _load_actionable_subgoals(self) -> list[str]:
        """Load actionable sub-goals from the goal tree state."""
        path = self.base_path / "STATE" / "goal_trees" / "novatrade_100pct_operational.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            sub_goals = data.get("sub_goals", [])
            # Return titles of non-completed, non-blocked sub-goals
            completed_ids = {sg["sub_goal_id"] for sg in sub_goals if sg.get("status") in ("completed", "skipped")}
            actionable = []
            for sg in sub_goals:
                if sg.get("status") in ("completed", "skipped"):
                    continue
                deps = sg.get("dependencies", [])
                if all(d in completed_ids for d in deps):
                    actionable.append(sg.get("title", sg.get("sub_goal_id", "")))
            return actionable[:10]
        except (json.JSONDecodeError, OSError, KeyError):
            return []

    @staticmethod
    def _classify_time() -> str:
        """Classify current UTC hour into time-of-day bucket."""
        hour = datetime.now(timezone.utc).hour
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 22:
            return "evening"
        else:
            return "night"
