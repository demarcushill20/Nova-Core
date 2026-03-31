"""Traceability Graph — sub-goal->decision->task->outcome audit trail.

Maintains a complete chain of custody from high-level goals through
decisions, tasks, and measured outcomes.  Enables post-hoc analysis
of which decisions actually moved the needle and which were noise.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("novatrade.autonomy.traceability")


@dataclass
class TraceEntry:
    """A single link in the goal->decision->task->outcome chain."""

    sub_goal_id: str | None
    decision_id: str
    task_file: str
    outcome: str  # "effective" / "ineffective" / "unknown"
    delta: float
    pattern_id: str | None  # if a pattern was matched
    timestamp: str = ""
    dimension: str = ""  # target dimension for easier querying


class TraceabilityGraph:
    """Maintains sub-goal->decision->task->outcome audit trail.

    Persisted to STATE/traceability_graph.json with atomic writes.
    """

    _MAX_ENTRIES = 1000

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self._state_dir = Path(base_path) / "STATE"
        self._graph_path = self._state_dir / "traceability_graph.json"

    def record_trace(self, entry: TraceEntry) -> None:
        """Add a trace entry to the graph."""
        if not entry.timestamp:
            entry.timestamp = datetime.now(timezone.utc).isoformat()
        entries = self._load()
        entries.append(asdict(entry))
        entries = entries[-self._MAX_ENTRIES :]
        self._save(entries)

    def get_traces_for_subgoal(self, sub_goal_id: str) -> list[TraceEntry]:
        """Get all traces related to a sub-goal."""
        entries = self._load()
        return [self._to_entry(e) for e in entries if e.get("sub_goal_id") == sub_goal_id]

    def get_traces_for_dimension(self, dimension: str) -> list[TraceEntry]:
        """Get all traces for a specific dimension."""
        entries = self._load()
        return [self._to_entry(e) for e in entries if e.get("dimension") == dimension]

    def get_traces_for_decision(self, decision_id: str) -> list[TraceEntry]:
        """Get all traces linked to a specific decision."""
        entries = self._load()
        return [self._to_entry(e) for e in entries if e.get("decision_id") == decision_id]

    def summary(self) -> dict:
        """Summary stats: total traces, by outcome, by dimension."""
        entries = self._load()
        by_outcome: dict[str, int] = {}
        by_dimension: dict[str, int] = {}
        for e in entries:
            outcome = e.get("outcome", "unknown")
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            dim = e.get("dimension", "")
            if dim:
                by_dimension[dim] = by_dimension.get(dim, 0) + 1
        return {
            "total": len(entries),
            "by_outcome": by_outcome,
            "by_dimension": by_dimension,
        }

    @staticmethod
    def _to_entry(d: dict) -> TraceEntry:
        return TraceEntry(
            sub_goal_id=d.get("sub_goal_id"),
            decision_id=d.get("decision_id", ""),
            task_file=d.get("task_file", ""),
            outcome=d.get("outcome", "unknown"),
            delta=d.get("delta", 0.0),
            pattern_id=d.get("pattern_id"),
            timestamp=d.get("timestamp", ""),
            dimension=d.get("dimension", ""),
        )

    def _load(self) -> list[dict]:
        if not self._graph_path.exists():
            return []
        try:
            data = json.loads(self._graph_path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, entries: list[dict]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(entries, f, indent=2, default=str)
            os.replace(tmp, str(self._graph_path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
