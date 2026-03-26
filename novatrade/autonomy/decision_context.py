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
    time_of_day: str = ""  # "morning", "afternoon", "evening", "night"
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
        return DecisionContext(
            progress_report=report,
            pending_tasks=self._load_pending_tasks(),
            active_goals=self._load_active_goals(),
            recent_decisions=self._load_recent_decisions(),
            time_of_day=self._classify_time(),
        )

    def _load_pending_tasks(self) -> list[TaskSummary]:
        """Load pending tasks from TASKS/ directory."""
        tasks: list[TaskSummary] = []
        if not self._tasks_dir.exists():
            return tasks
        for f in sorted(self._tasks_dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            # Skip completed/failed/cancelled
            stem = f.stem
            if any(stem.endswith(s) for s in (".done", ".failed", ".cancelled")):
                continue
            if f.name.endswith(".md.done") or f.name.endswith(".md.failed") or f.name.endswith(".md.cancelled"):
                continue
            title = stem.split("_", 1)[1] if "_" in stem else stem
            title = title.replace("_", " ")[:80]
            tasks.append(TaskSummary(stem=stem, title=title, status="pending"))
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

    def _load_recent_decisions(self, limit: int = 5) -> list[dict]:
        """Load recent decisions from STATE/decision_history.json."""
        if not self._decisions_path.exists():
            return []
        try:
            data = json.loads(self._decisions_path.read_text())
            items = data if isinstance(data, list) else []
            return items[-limit:]
        except (json.JSONDecodeError, OSError):
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
