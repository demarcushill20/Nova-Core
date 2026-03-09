"""Track delegated tasks for proactive completion notifications.

When CEO Nova delegates work to the task queue, this tracker records
the (task_stem, chat_id) pair so the bot can proactively notify the
user with a natural summary when the task completes.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

_log = logging.getLogger("telegram_bot.delegation")

OUTPUT = Path("/home/nova/nova-core/OUTPUT")
NOTIFIED_DIR = Path("/home/nova/nova-core/STATE/notified")


class DelegationTracker:
    """In-memory tracker for conversation-delegated tasks."""

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}  # task_stem → chat_id

    def track(self, task_stem: str, chat_id: str) -> None:
        """Record a delegated task for proactive completion checking."""
        self._pending[task_stem] = chat_id
        _log.info("Tracking delegation: stem=%s chat=%s", task_stem, chat_id)

    def get_chat_id(self, task_stem: str) -> str | None:
        return self._pending.get(task_stem)

    def complete(self, task_stem: str) -> str | None:
        """Remove and return the chat_id for a completed task."""
        return self._pending.pop(task_stem, None)

    def pending_stems(self) -> list[str]:
        return list(self._pending.keys())

    def has_pending(self) -> bool:
        return bool(self._pending)


def find_completed_output(task_stem: str) -> Path | None:
    """Find the OUTPUT file for a completed task.

    Output filenames follow the pattern: {stem}__YYYYMMDD-HHMMSS.md
    Returns the most recent match, or None if not found.
    """
    if not OUTPUT.exists():
        return None
    candidates = [
        p for p in OUTPUT.iterdir()
        if p.is_file() and p.name.startswith(task_stem) and p.suffix == ".md"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def claim_notification(output_name: str) -> bool:
    """Atomically claim the notification for an output file.

    Uses the same O_CREAT|O_EXCL mechanism as the notifier — exactly
    one process wins, preventing duplicate notifications.
    """
    NOTIFIED_DIR.mkdir(parents=True, exist_ok=True)
    marker = NOTIFIED_DIR / f"{output_name}.notified"
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, b"claimed by CEO Nova\n")
        os.close(fd)
        return True
    except FileExistsError:
        return False


def extract_output_summary(output_path: Path, max_chars: int = 2000) -> str:
    """Read the output file and extract key content for summarization."""
    text = output_path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]"
    return text


def get_recent_completions(max_age_seconds: int = 3600, limit: int = 3) -> list[dict]:
    """Get recently completed OUTPUT files for context injection.

    Returns a list of {"stem": ..., "summary_line": ...} for outputs
    completed within max_age_seconds, most recent first.
    """
    import time
    if not OUTPUT.exists():
        return []
    cutoff = time.time() - max_age_seconds
    results = []
    for p in sorted(OUTPUT.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file() or p.suffix != ".md":
            continue
        if p.stat().st_mtime < cutoff:
            break
        # Extract stem from filename pattern: {stem}__YYYYMMDD-HHMMSS.md
        name = p.stem
        parts = name.rsplit("__", 1)
        stem = parts[0] if len(parts) == 2 else name
        # Read first meaningful line as summary
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            summary_line = ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("**"):
                    summary_line = stripped[:200]
                    break
            results.append({"stem": stem, "summary_line": summary_line, "path": str(p)})
        except OSError:
            continue
        if len(results) >= limit:
            break
    return results


COMPLETION_SUMMARY_PROMPT = """\
A background task has completed. Summarize the result for the user naturally.

Rules:
- Keep it to 2-4 sentences
- Lead with what was accomplished, not metadata
- Mention key outcomes or changes
- If something failed or had issues, say so honestly
- Do NOT include task IDs, file paths, CONTRACT blocks, or internal metadata
- Do NOT use bullet points for a simple summary
- Be warm and natural — this is a proactive update, not a report

TASK OUTPUT:
"""
