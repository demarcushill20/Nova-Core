"""Working memory for active task context.

Persists the *what* and *why* alongside each delegated task,
surviving beyond the conversation buffer window (20 msg / 2hr).
This ensures CEO Nova can reference the original user intent
when tasks complete — even hours later.

Storage: STATE/working_memory.json (flat JSON, loaded on startup).
Archive: STATE/working_memory_archive.json (completed tasks, last 100).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_log = logging.getLogger("telegram_bot.working_memory")

STATE_DIR = Path("/home/nova/nova-core/STATE")
WM_FILE = STATE_DIR / "working_memory.json"
WM_ARCHIVE = STATE_DIR / "working_memory_archive.json"


@dataclass
class ActiveTask:
    task_stem: str
    chat_id: str
    original_message: str       # What the user actually said
    intent_summary: str         # 1-line summary of the goal
    created_at: float
    status: str                 # "pending" | "running" | "completed" | "failed"
    context_snapshot: list      # Last N conversation messages at delegation time
    task_file: str = ""         # Path to TASKS/ file (proof of delegation)
    message_id: int = 0         # Telegram message_id for reply threading


class WorkingMemoryStore:
    """Flat JSON store for active task context."""

    def __init__(self) -> None:
        self._tasks: dict[str, ActiveTask] = {}
        self._load()

    def add(self, task: ActiveTask) -> None:
        """Record a new delegated task."""
        self._tasks[task.task_stem] = task
        self._save()
        _log.info("WM_ADD stem=%s chat=%s summary=%r",
                  task.task_stem, task.chat_id, task.intent_summary[:80])

    def get(self, task_stem: str) -> ActiveTask | None:
        return self._tasks.get(task_stem)

    def update_status(self, task_stem: str, status: str) -> None:
        task = self._tasks.get(task_stem)
        if task:
            task.status = status
            self._save()

    def complete(self, task_stem: str,
                 output_file: str = "",
                 output_summary_line: str = "",
                 completion_response: str = "") -> ActiveTask | None:
        """Mark task completed, archive it, record in recent completions, return task."""
        task = self._tasks.pop(task_stem, None)
        if task:
            task.status = "completed"
            self._archive(task)
            self._save()
            _log.info("WM_COMPLETE stem=%s summary=%r", task_stem, task.intent_summary[:80])

            # Record in recent completions store for session reconstruction
            try:
                from telegram.recent_completions import record_completion
                record_completion(
                    task_stem=task_stem,
                    chat_id=task.chat_id,
                    original_message=task.original_message,
                    intent_summary=task.intent_summary,
                    output_file=output_file,
                    output_summary_line=output_summary_line,
                    completion_response=completion_response,
                )
            except Exception as e:
                _log.warning("Failed to record recent completion for %s: %s",
                             task_stem, e)
        return task

    def active_tasks(self) -> list[ActiveTask]:
        """All active tasks across all chats."""
        return list(self._tasks.values())

    def active_for_chat(self, chat_id: str) -> list[ActiveTask]:
        """Active tasks for a specific chat."""
        return [t for t in self._tasks.values() if t.chat_id == chat_id]

    def cleanup_stale(self, max_age_seconds: float = 86400) -> int:
        """Archive tasks older than max_age_seconds. Returns count archived."""
        cutoff = time.time() - max_age_seconds
        stale = [s for s, t in self._tasks.items() if t.created_at < cutoff]
        for stem in stale:
            task = self._tasks.pop(stem)
            task.status = "stale"
            self._archive(task)
            _log.info("WM_STALE stem=%s age_h=%.1f", stem,
                      (time.time() - task.created_at) / 3600)
        if stale:
            self._save()
        return len(stale)

    def format_for_context(self, chat_id: str) -> str:
        """Format active tasks as context block for conversation injection."""
        tasks = self.active_for_chat(chat_id)
        if not tasks:
            return ""
        lines = ["ACTIVE BACKGROUND TASKS (reference if the user asks about them):"]
        for t in tasks:
            age_min = int((time.time() - t.created_at) / 60)
            lines.append(f"- [{t.status}] {t.intent_summary} (started {age_min}m ago)")
            lines.append(f"  Original request: \"{t.original_message[:120]}\"")
        return "\n".join(lines)

    def format_completion_context(self, task: ActiveTask) -> str:
        """Format a completed task's original context for the completion prompt."""
        lines = [
            f"ORIGINAL USER REQUEST: \"{task.original_message}\"",
            f"GOAL: {task.intent_summary}",
        ]
        if task.context_snapshot:
            lines.append("CONVERSATION CONTEXT AT DELEGATION TIME:")
            for msg in task.context_snapshot[-5:]:  # Last 5 messages
                role = "User" if msg.get("role") == "user" else "Nova"
                lines.append(f"  {role}: {msg.get('content', '')[:150]}")
        return "\n".join(lines)

    # --- Persistence ---

    def _save(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = {stem: asdict(t) for stem, t in self._tasks.items()}
            WM_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            _log.warning("Failed to save working memory: %s", e)

    def _load(self) -> None:
        try:
            if not WM_FILE.exists():
                return
            data = json.loads(WM_FILE.read_text(encoding="utf-8"))
            for stem, d in data.items():
                self._tasks[stem] = ActiveTask(**d)
            if self._tasks:
                _log.info("Loaded %d active task(s) from working memory", len(self._tasks))
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            _log.warning("Failed to load working memory: %s", e)

    def _archive(self, task: ActiveTask) -> None:
        try:
            archive: list = []
            if WM_ARCHIVE.exists():
                try:
                    archive = json.loads(WM_ARCHIVE.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            archive.append(asdict(task))
            # Keep last 100 archived tasks
            if len(archive) > 100:
                archive = archive[-100:]
            WM_ARCHIVE.write_text(json.dumps(archive, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            _log.warning("Failed to archive task %s: %s", task.task_stem, e)
