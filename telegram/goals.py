"""Goal tracking for CEO Nova.

Persistent JSON-backed goal store for short-term and long-term priorities.
Goals are injected into conversation context so CEO Nova naturally
references them when relevant.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("telegram_bot.goals")

GOALS_FILE = Path("/home/nova/nova-core/STATE/goals.json")


def _load() -> dict:
    """Load goals from disk.

    Handles legacy format where the file is a bare JSON array instead of
    the expected {"goals": [...], "next_id": N} dict.
    """
    try:
        data = json.loads(GOALS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"goals": [], "next_id": 1}

    # Auto-heal bare-array format written by earlier buggy code
    if isinstance(data, list):
        _log.warning("goals.json contains bare array — auto-wrapping")
        next_id = max((g.get("id", 0) for g in data), default=0) + 1
        data = {"goals": data, "next_id": next_id}
        _save(data)  # persist the fix
    return data


def _save(data: dict) -> None:
    """Save goals to disk."""
    GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOALS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def add_goal(text: str, priority: str = "normal") -> dict:
    """Add a new goal. Returns the created goal dict."""
    data = _load()
    goal = {
        "id": data["next_id"],
        "text": text,
        "priority": priority,
        "created": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "completed": None,
    }
    data["goals"].append(goal)
    data["next_id"] += 1
    _save(data)
    _log.info("Goal added: id=%d text=%r", goal["id"], text[:50])
    return goal


def complete_goal(goal_id: int) -> dict | None:
    """Mark a goal as completed. Returns the goal or None if not found."""
    data = _load()
    for g in data["goals"]:
        if g["id"] == goal_id and g["status"] == "active":
            g["status"] = "completed"
            g["completed"] = datetime.now(timezone.utc).isoformat()
            _save(data)
            _log.info("Goal completed: id=%d", goal_id)
            return g
    return None


def remove_goal(goal_id: int) -> dict | None:
    """Remove a goal entirely. Returns the removed goal or None."""
    data = _load()
    for i, g in enumerate(data["goals"]):
        if g["id"] == goal_id:
            removed = data["goals"].pop(i)
            _save(data)
            _log.info("Goal removed: id=%d", goal_id)
            return removed
    return None


def list_goals(include_completed: bool = False) -> list[dict]:
    """List goals, optionally including completed ones."""
    data = _load()
    if include_completed:
        return data["goals"]
    return [g for g in data["goals"] if g["status"] == "active"]


def clear_completed() -> int:
    """Remove all completed goals. Returns count removed."""
    data = _load()
    before = len(data["goals"])
    data["goals"] = [g for g in data["goals"] if g["status"] != "completed"]
    after = len(data["goals"])
    _save(data)
    return before - after


def format_goals_for_context() -> str:
    """Format active goals as a brief context string for conversation injection."""
    try:
        active = list_goals(include_completed=False)
        if not active:
            return ""
        lines = ["ACTIVE GOALS (reference when relevant, don't list unless asked):"]
        for g in active:
            priority_tag = f" [{g['priority']}]" if g["priority"] != "normal" else ""
            lines.append(f"- {g['text']}{priority_tag}")
        return "\n".join(lines)
    except Exception as exc:
        _log.error("format_goals_for_context failed: %s", exc)
        return ""


def format_goals_for_display() -> str:
    """Format goals for the /goals command display."""
    try:
        active = list_goals(include_completed=False)
        completed = [g for g in _load()["goals"] if g["status"] == "completed"]

        if not active and not completed:
            return "No goals set. Use /goals add <goal> to set one."

        parts = []
        if active:
            parts.append("Active Goals:")
            for g in active:
                priority_tag = f" [{g['priority']}]" if g["priority"] != "normal" else ""
                parts.append(f"  #{g['id']} {g['text']}{priority_tag}")
        if completed:
            parts.append("\nCompleted:")
            for g in completed[-3:]:  # Show last 3 completed
                parts.append(f"  #{g['id']} {g['text']} (done)")

        parts.append("\nCommands: /goals add <text> | /goals done <id> | /goals remove <id> | /goals clear")
        return "\n".join(parts)
    except Exception as exc:
        _log.error("format_goals_for_display failed: %s", exc)
        return "Error loading goals. Use /goals add <text> to set a new goal."
