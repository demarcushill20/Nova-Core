"""Recent completions store for session reconstruction.

Tracks recently completed tasks so CEO Nova can reference them
during session restarts — even after the working memory archive
has moved them out of active context.

Storage: STATE/recent_completions.json
Retention: 4 hours (configurable)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_log = logging.getLogger("telegram_bot.recent_completions")

STATE_DIR = Path("/home/nova/nova-core/STATE")
RC_FILE = STATE_DIR / "recent_completions.json"

# Default retention: 4 hours
DEFAULT_RETENTION_SECONDS = 4 * 3600
# Max entries to keep
MAX_ENTRIES = 20


def _load() -> list[dict]:
    """Load recent completions from disk."""
    try:
        if RC_FILE.exists():
            return json.loads(RC_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("Failed to load recent completions: %s", e)
    return []


def _save(entries: list[dict]) -> None:
    """Save recent completions to disk."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RC_FILE.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        _log.warning("Failed to save recent completions: %s", e)


def record_completion(
    task_stem: str,
    chat_id: str,
    original_message: str,
    intent_summary: str,
    output_file: str,
    output_summary_line: str,
    completion_response: str,
) -> None:
    """Record a completed task for future session reconstruction."""
    entries = _load()
    entry = {
        "task_stem": task_stem,
        "chat_id": chat_id,
        "original_message": original_message[:300],
        "intent_summary": intent_summary[:200],
        "completed_at": time.time(),
        "output_file": output_file,
        "output_summary_line": output_summary_line[:300],
        "completion_response": completion_response[:300],
    }
    entries.append(entry)
    # Cap at MAX_ENTRIES
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    _save(entries)
    _log.info("RC_RECORD stem=%s chat=%s", task_stem, chat_id)


def get_recent(
    max_age_seconds: float = DEFAULT_RETENTION_SECONDS,
    limit: int = 5,
    chat_id: str | None = None,
) -> list[dict]:
    """Get recently completed tasks within the retention window."""
    entries = _load()
    cutoff = time.time() - max_age_seconds
    results = [
        e for e in entries
        if e.get("completed_at", 0) > cutoff
        and (chat_id is None or e.get("chat_id") == chat_id)
    ]
    # Most recent first
    results.sort(key=lambda e: e.get("completed_at", 0), reverse=True)
    return results[:limit]


def cleanup(max_age_seconds: float = DEFAULT_RETENTION_SECONDS) -> int:
    """Remove entries older than max_age_seconds. Returns count removed."""
    entries = _load()
    cutoff = time.time() - max_age_seconds
    fresh = [e for e in entries if e.get("completed_at", 0) > cutoff]
    removed = len(entries) - len(fresh)
    if removed > 0:
        _save(fresh)
        _log.info("RC_CLEANUP removed %d stale entries", removed)
    return removed


def format_for_context(
    max_age_seconds: float = DEFAULT_RETENTION_SECONDS,
    limit: int = 5,
    chat_id: str | None = None,
) -> str:
    """Format recent completions as a context block for session restart injection."""
    recent = get_recent(max_age_seconds=max_age_seconds, limit=limit, chat_id=chat_id)
    if not recent:
        return ""
    lines = ["RECENTLY COMPLETED TASKS (last 4 hours):"]
    for e in recent:
        age_min = int((time.time() - e.get("completed_at", 0)) / 60)
        if age_min < 60:
            age_str = f"{age_min}m ago"
        else:
            age_str = f"{age_min // 60}h {age_min % 60}m ago"
        lines.append(f'- "{e.get("intent_summary", "?")}" — completed {age_str}')
        if e.get("output_file"):
            fname = Path(e["output_file"]).name
            lines.append(f"  Output: {fname}")
        if e.get("output_summary_line"):
            lines.append(f"  Summary: {e['output_summary_line']}")
    return "\n".join(lines)
