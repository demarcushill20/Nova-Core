"""Persistent skill usage history store for NovaCore planner.

Manages STATE/skill_usage_history.json with exact JSON shape per spec:
  {
    "skill_name": {
      "runs": int,
      "successes": int,
      "failures": int,
      "avg_duration_ms": int,
      "last_used_ts": "ISO-8601 string",
      "total_retries": int
    }
  }
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("NOVACORE_STATE", "/home/nova/nova-core/STATE"))
HISTORY_FILE = STATE_DIR / "skill_usage_history.json"


class SkillHistoryStore:
    """Read/write persistent skill usage statistics."""

    def __init__(self, path: Path | None = None):
        self.path = path or HISTORY_FILE
        self._data: dict = self.load()

    # -- persistence ----------------------------------------------------------

    def load(self) -> dict:
        """Load history data from disk. Returns empty dict on error."""
        if self.path.exists():
            try:
                text = self.path.read_text(encoding="utf-8")
                if text.strip():
                    data = json.loads(text)
                    if isinstance(data, dict):
                        return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self, data: dict) -> None:
        """Persist data dict to disk atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self._data = data

    # -- recording ------------------------------------------------------------

    def record_run(
        self,
        skill_name: str,
        success: bool,
        duration_ms: int,
        retries: int,
    ) -> None:
        """Record one skill execution with exact spec fields."""
        data = self._data

        if skill_name not in data:
            data[skill_name] = {
                "runs": 0,
                "successes": 0,
                "failures": 0,
                "avg_duration_ms": 0,
                "last_used_ts": None,
                "total_retries": 0,
            }

        entry = data[skill_name]
        old_runs = entry["runs"]
        entry["runs"] = old_runs + 1

        if success:
            entry["successes"] += 1
        else:
            entry["failures"] += 1

        # Running average for duration
        entry["avg_duration_ms"] = round(
            ((entry["avg_duration_ms"] * old_runs) + duration_ms) / entry["runs"]
        )

        entry["last_used_ts"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entry["total_retries"] += retries

        self.save(data)

    # -- queries --------------------------------------------------------------

    def get_stats(self, skill_name: str) -> dict:
        """Return the full stats dict for a skill, or empty dict if unknown."""
        return self._data.get(skill_name, {})

    def get_recency_score(self, skill_name: str) -> float:
        """Return a 0.0–1.0 recency score based on time since last use.

        Uses exponential decay with a 24-hour half-life.
        Returns 0.5 (neutral) for unknown skills.
        """
        entry = self._data.get(skill_name)
        if not entry or not entry.get("last_used_ts"):
            return 0.5

        try:
            ts_str = entry["last_used_ts"]
            last_used = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_elapsed = (now - last_used).total_seconds() / 3600
            return max(0.0, min(1.0, math.exp(-hours_elapsed / 24)))
        except (ValueError, TypeError):
            return 0.5

    def get_success_rate(self, skill_name: str) -> float:
        """Return success rate for a skill (0.0–1.0).

        Returns 0.5 (neutral) for unknown skills or zero runs.
        """
        entry = self._data.get(skill_name)
        if not entry or entry["runs"] == 0:
            return 0.5
        return entry["successes"] / entry["runs"]
