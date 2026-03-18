"""Phase 5 — Session Management & Context Compaction.

Tracks watcher task execution sessions, compacts completed work into
reusable context summaries, and provides session-aware context injection
for new task dispatches.

Design:
  - Session = a bounded execution window with an ID, timestamps, and
    a list of task stems executed within it.
  - Completed tasks produce a compact summary (extracted from their
    OUTPUT/ report + CONTRACT block).
  - When a new task dispatches, recent session context is injected into
    the worker prompt so the subprocess has awareness of prior work.
  - Sessions auto-expire after a configurable timeout (default 2 hours).
  - Session state is persisted to STATE/sessions/ as JSON.
  - Context summaries are bounded (max 3 KB per injection) to prevent
    prompt bloat.

Not an LLM call — all logic is deterministic.
"""

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
STATE_DIR = BASE / "STATE"
SESSION_DIR = STATE_DIR / "sessions"
OUTPUT_DIR = BASE / "OUTPUT"

# Tunables
SESSION_TIMEOUT_S = 2 * 3600  # 2 hours — matches conversation buffer
MAX_CONTEXT_INJECTION_BYTES = 3072  # 3 KB cap on injected context
MAX_RECENT_SUMMARIES = 5  # max prior task summaries to inject
MAX_SUMMARY_LENGTH = 400  # per-task summary char cap


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TaskRecord:
    """Record of a single task execution within a session."""

    stem: str
    status: str = "pending"  # pending|completed|failed
    started_at: float = 0.0
    completed_at: float = 0.0
    summary: str = ""  # compact summary from CONTRACT block
    files_changed: str = ""
    confidence: str = ""


@dataclass
class Session:
    """A bounded execution session for the watcher."""

    session_id: str
    created_at: float = 0.0
    last_activity: float = 0.0
    status: str = "active"  # active|expired|archived
    tasks: list[dict] = field(default_factory=list)

    def is_expired(self) -> bool:
        if not self.last_activity:
            return True
        return (time.time() - self.last_activity) > SESSION_TIMEOUT_S

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            session_id=data["session_id"],
            created_at=data.get("created_at", 0.0),
            last_activity=data.get("last_activity", 0.0),
            status=data.get("status", "active"),
            tasks=data.get("tasks", []),
        )


# ---------------------------------------------------------------------------
# CONTRACT block parser
# ---------------------------------------------------------------------------

_CONTRACT_RE = re.compile(
    r"##\s*CONTRACT\b(.*?)(?:\Z|\n##\s)",
    re.DOTALL | re.IGNORECASE,
)

_FIELD_RE = re.compile(r"^(summary|files_changed|verification|confidence):\s*(.+)$", re.MULTILINE)


def extract_contract(text: str) -> dict:
    """Extract CONTRACT fields from an output report.

    Returns dict with keys: summary, files_changed, verification, confidence.
    Missing fields are empty strings.
    """
    result = {"summary": "", "files_changed": "", "verification": "", "confidence": ""}
    match = _CONTRACT_RE.search(text)
    if not match:
        return result
    block = match.group(1)
    for m in _FIELD_RE.finditer(block):
        key = m.group(1).strip()
        val = m.group(2).strip()
        if key in result:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manage watcher task sessions with persistence and context injection."""

    def __init__(self, session_dir: Path | None = None, output_dir: Path | None = None):
        self._session_dir = session_dir or SESSION_DIR
        self._output_dir = output_dir or OUTPUT_DIR
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._current: Session | None = None

    def get_or_create_session(self) -> Session:
        """Get the current active session, or create a new one.

        If the current session is expired, archives it and creates fresh.
        """
        if self._current and not self._current.is_expired():
            return self._current

        # Try to load most recent active session from disk
        session = self._load_latest_active()
        if session and not session.is_expired():
            self._current = session
            return session

        # Archive expired session if any
        if session:
            self._archive(session)

        # Create new session
        session_id = f"ses_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        session = Session(
            session_id=session_id,
            created_at=time.time(),
            last_activity=time.time(),
        )
        self._current = session
        self._persist(session)
        return session

    def record_task_start(self, stem: str) -> Session:
        """Record that a task has started executing."""
        session = self.get_or_create_session()
        record = TaskRecord(stem=stem, status="pending", started_at=time.time())
        session.tasks.append(asdict(record))
        session.last_activity = time.time()
        self._persist(session)
        return session

    def record_task_completion(self, stem: str, success: bool) -> None:
        """Record task completion and extract summary from output report."""
        session = self.get_or_create_session()
        status = "completed" if success else "failed"

        # Find the task record
        for task in session.tasks:
            if task["stem"] == stem and task["status"] == "pending":
                task["status"] = status
                task["completed_at"] = time.time()

                # Extract summary from output report
                contract = self._extract_task_summary(stem)
                task["summary"] = contract.get("summary", "")[:MAX_SUMMARY_LENGTH]
                task["files_changed"] = contract.get("files_changed", "")
                task["confidence"] = contract.get("confidence", "")
                break

        session.last_activity = time.time()
        self._persist(session)

    def build_context_injection(self, current_stem: str) -> str:
        """Build a context injection string from recent session history.

        Returns a bounded string summarizing recent completed tasks for
        injection into the next worker's prompt. Returns empty string
        if no useful context exists.
        """
        session = self.get_or_create_session()

        # Gather completed tasks from current session (excluding current task)
        completed = [
            t
            for t in session.tasks
            if t["status"] in ("completed", "failed") and t["stem"] != current_stem and t.get("summary")
        ]

        if not completed:
            # Check most recent archived session
            completed = self._load_recent_archived_summaries()

        if not completed:
            return ""

        # Take most recent N
        recent = completed[-MAX_RECENT_SUMMARIES:]

        lines = ["SESSION CONTEXT — recent work in this execution window:"]
        for t in recent:
            status_icon = "done" if t["status"] == "completed" else "FAILED"
            lines.append(f"  [{status_icon}] {t['stem']}: {t.get('summary', 'no summary')}")
            if t.get("files_changed") and t["files_changed"] != "none":
                lines.append(f"         files: {t['files_changed']}")

        lines.append("")
        lines.append(
            "Use this context to avoid duplicating work or contradicting prior decisions. Do not re-do completed tasks."
        )

        result = "\n".join(lines)

        # Hard cap
        if len(result.encode("utf-8")) > MAX_CONTEXT_INJECTION_BYTES:
            result = result[: MAX_CONTEXT_INJECTION_BYTES - 20] + "\n  ...(truncated)"

        return result

    # --- Persistence ---

    def _persist(self, session: Session) -> None:
        """Write session state to disk."""
        try:
            path = self._session_dir / f"{session.session_id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(session.to_dict(), indent=2, default=str) + "\n")
            tmp.rename(path)
        except OSError:
            pass  # best-effort persistence

    def _load_latest_active(self) -> Session | None:
        """Load the most recent active session from disk."""
        if not self._session_dir.exists():
            return None
        candidates = []
        for path in self._session_dir.glob("ses_*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") == "active":
                    candidates.append((data.get("last_activity", 0), data))
            except (json.JSONDecodeError, OSError):
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return Session.from_dict(candidates[0][1])

    def _archive(self, session: Session) -> None:
        """Mark session as archived and persist."""
        session.status = "archived"
        self._persist(session)

    def _load_recent_archived_summaries(self) -> list[dict]:
        """Load task summaries from the most recent archived session."""
        if not self._session_dir.exists():
            return []
        candidates = []
        for path in self._session_dir.glob("ses_*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") == "archived":
                    candidates.append((data.get("last_activity", 0), data))
            except (json.JSONDecodeError, OSError):
                continue
        if not candidates:
            return []
        candidates.sort(key=lambda x: x[0], reverse=True)
        latest = candidates[0][1]
        return [t for t in latest.get("tasks", []) if t.get("status") in ("completed", "failed") and t.get("summary")][
            -MAX_RECENT_SUMMARIES:
        ]

    def _extract_task_summary(self, stem: str) -> dict:
        """Find the most recent output report for a task stem and extract CONTRACT."""
        if not self._output_dir.exists():
            return {}
        # Output files follow pattern: {stem}__YYYYMMDD-HHMMSS.md
        matches = sorted(self._output_dir.glob(f"{stem}__*.md"))
        if not matches:
            return {}
        try:
            text = matches[-1].read_text(encoding="utf-8")
            return extract_contract(text)
        except OSError:
            return {}

    # --- Cleanup ---

    def cleanup_old_sessions(self, max_age_days: int = 7) -> int:
        """Remove session files older than max_age_days. Returns count removed."""
        if not self._session_dir.exists():
            return 0
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0
        for path in self._session_dir.glob("ses_*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("last_activity", 0) < cutoff:
                    path.unlink()
                    removed += 1
            except (json.JSONDecodeError, OSError):
                continue
        return removed
