"""Thread and message models with file-backed persistence.

Provides the foundational data model for threaded conversations in
Nova-Link. Each thread is stored as an individual JSON file in
STATE/threads/ with an index file for fast listing.

Thread summary uses an evolvable dict schema:
  Phase 2: {"type": "text", "content": "plain summary"}
  Future:  {"type": "structured", "content": "fallback",
            "goals": [...], "decisions": [...], ...}
Consumers should always read summary["content"] for the plain-text
fallback and check summary["type"] for richer formats.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("nova-link.threads")

STATE_DIR = Path("/home/nova/nova-core/STATE")
THREADS_DIR = STATE_DIR / "threads"
LEGACY_CONVERSATIONS_DIR = STATE_DIR / "conversations"
LEGACY_CHAT_ID = "530812511"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single message in a conversation thread."""

    id: str
    thread_id: str
    role: str  # "user" | "assistant" | "system"
    content: str
    created_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "thread_id": self.thread_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            id=d["id"],
            thread_id=d["thread_id"],
            role=d["role"],
            content=d["content"],
            created_at=d["created_at"],
            metadata=d.get("metadata", {}),
        )


@dataclass
class Thread:
    """A conversation thread containing messages.

    The ``summary`` field uses a dict schema that can evolve:
      - ``{"type": "text", "content": "..."}`` for plain-text summaries
      - ``{"type": "structured", "content": "fallback", ...}`` for rich summaries
    Readers should always check ``summary.get("content", "")`` as a safe fallback.
    """

    id: str
    title: str
    created_at: float
    updated_at: float
    status: str = "active"  # "active" | "archived"
    summary: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    message_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "summary": self.summary,
            "metadata": self.metadata,
            "message_count": self.message_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Thread:
        return cls(
            id=d["id"],
            title=d.get("title", "Untitled"),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            status=d.get("status", "active"),
            summary=d.get("summary"),
            metadata=d.get("metadata", {}),
            message_count=d.get("message_count", 0),
        )


# ---------------------------------------------------------------------------
# ThreadStore — file-backed persistence
# ---------------------------------------------------------------------------


class ThreadStore:
    """File-backed thread and message persistence.

    Storage layout::

        STATE/threads/_index.json           Thread index for fast listing
        STATE/threads/{thread_id}.json      Thread header + messages

    The index mirrors thread metadata for O(1) listing without reading
    every thread file.  Individual thread files are the source of truth.
    """

    def __init__(self, threads_dir: Path | str | None = None) -> None:
        self._dir = Path(threads_dir) if threads_dir else THREADS_DIR
        self._index_path = self._dir / "_index.json"
        self._dir.mkdir(parents=True, exist_ok=True)

    # ---- index I/O --------------------------------------------------------

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _log.warning("Failed to load thread index: %s", e)
        return {"default_thread_id": None, "migrated_from": None, "threads": {}}

    def _save_index(self, index: dict[str, Any]) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._index_path)

    def _upsert_index_entry(self, thread: Thread) -> None:
        """Update or insert a thread entry in the index."""
        index = self._load_index()
        index["threads"][thread.id] = thread.to_dict()
        self._save_index(index)

    # ---- thread file I/O --------------------------------------------------

    def _thread_path(self, thread_id: str) -> Path:
        return self._dir / f"{thread_id}.json"

    def _load_thread_file(self, thread_id: str) -> dict[str, Any] | None:
        path = self._thread_path(thread_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("Failed to load thread %s: %s", thread_id, e)
            return None

    def _save_thread_file(self, thread: Thread, messages: list[Message]) -> None:
        data = {
            "thread": thread.to_dict(),
            "messages": [m.to_dict() for m in messages],
        }
        path = self._thread_path(thread.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    # ---- thread CRUD ------------------------------------------------------

    def create_thread(
        self,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Thread:
        """Create a new empty thread."""
        now = time.time()
        thread = Thread(
            id=_new_id(),
            title=title or "New conversation",
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        self._save_thread_file(thread, [])
        self._upsert_index_entry(thread)
        _log.info("Created thread %s: %s", thread.id, thread.title)
        return thread

    def get_thread(self, thread_id: str) -> Thread | None:
        """Load a thread by ID (includes summary). Returns None if not found."""
        data = self._load_thread_file(thread_id)
        if data is None:
            return None
        return Thread.from_dict(data["thread"])

    def list_threads(
        self,
        *,
        status: str | None = "active",
        order_by: str = "updated_at",
        descending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Thread]:
        """List threads from the index with filtering, ordering, and pagination.

        Parameters:
            status: Filter by status (None = all).
            order_by: Sort key — any Thread field name.
            descending: Sort direction.
            limit: Max results per page.
            offset: Skip this many results (for pagination).
        """
        index = self._load_index()
        entries = list(index.get("threads", {}).values())
        if status is not None:
            entries = [e for e in entries if e.get("status") == status]
        entries.sort(key=lambda e: e.get(order_by, 0), reverse=descending)
        page = entries[offset : offset + limit]
        return [Thread.from_dict(e) for e in page]

    def update_thread(self, thread_id: str, **kwargs: Any) -> Thread | None:
        """Update mutable thread fields. Returns updated thread or None."""
        data = self._load_thread_file(thread_id)
        if data is None:
            return None
        thread = Thread.from_dict(data["thread"])
        messages = [Message.from_dict(m) for m in data.get("messages", [])]

        immutable = {"id", "created_at"}
        for key, value in kwargs.items():
            if key in immutable:
                continue
            if hasattr(thread, key):
                setattr(thread, key, value)
        thread.updated_at = time.time()

        self._save_thread_file(thread, messages)
        self._upsert_index_entry(thread)
        return thread

    def archive_thread(self, thread_id: str) -> bool:
        """Soft-archive a thread. Returns True if successful."""
        return self.update_thread(thread_id, status="archived") is not None

    # ---- message CRUD -----------------------------------------------------

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message | None:
        """Append a message to a thread. Returns the message or None if thread missing."""
        data = self._load_thread_file(thread_id)
        if data is None:
            _log.warning("Cannot add message: thread %s not found", thread_id)
            return None

        thread = Thread.from_dict(data["thread"])
        messages = [Message.from_dict(m) for m in data.get("messages", [])]

        now = time.time()
        msg = Message(
            id=_new_id(),
            thread_id=thread_id,
            role=role,
            content=content,
            created_at=now,
            metadata=metadata or {},
        )
        messages.append(msg)

        thread.message_count = len(messages)
        thread.updated_at = now

        # Auto-title from first user message if still default
        if thread.title == "New conversation" and role == "user":
            thread.title = content[:80].split("\n")[0]

        self._save_thread_file(thread, messages)
        self._upsert_index_entry(thread)
        return msg

    def get_messages(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        order: str = "asc",
    ) -> list[Message]:
        """Get messages for a thread with pagination.

        Parameters:
            limit: Max messages to return.
            offset: Skip this many messages from the start (asc) or end (desc).
            order: "asc" (oldest first) or "desc" (newest first).
        """
        data = self._load_thread_file(thread_id)
        if data is None:
            return []
        messages = [Message.from_dict(m) for m in data.get("messages", [])]
        if order == "desc":
            messages.reverse()
        return messages[offset : offset + limit]

    def get_message_count(self, thread_id: str) -> int:
        """Return the cached message count for a thread."""
        thread = self.get_thread(thread_id)
        return thread.message_count if thread else 0

    def compact_thread(
        self,
        thread_id: str,
        keep_recent: int,
        new_summary: dict[str, Any],
    ) -> Thread | None:
        """Remove older messages and replace the thread summary.

        Keeps the *last* ``keep_recent`` messages and discards the rest.
        The ``new_summary`` dict replaces any existing summary on the thread.

        Returns the updated Thread, or None if the thread doesn't exist.
        """
        data = self._load_thread_file(thread_id)
        if data is None:
            return None

        thread = Thread.from_dict(data["thread"])
        messages = [Message.from_dict(m) for m in data.get("messages", [])]

        removed = len(messages) - keep_recent
        if removed > 0:
            messages = messages[-keep_recent:]
            _log.info(
                "Compacted thread %s: removed %d messages, kept %d",
                thread_id,
                removed,
                len(messages),
            )

        thread.summary = new_summary
        thread.message_count = len(messages)
        thread.updated_at = time.time()

        self._save_thread_file(thread, messages)
        self._upsert_index_entry(thread)
        return thread

    # ---- default thread resolution ----------------------------------------

    def get_or_create_default_thread(self) -> Thread:
        """Return the stable default thread, creating or migrating as needed.

        Resolution order:
        1. Return existing default thread if valid.
        2. Migrate legacy conversation (STATE/conversations/530812511.json).
        3. Create a fresh default thread.

        The default thread ID is stored in the index and remains stable
        across calls — ``POST /api/chat`` without a ``thread_id`` always
        resolves to the same thread.
        """
        index = self._load_index()
        default_id = index.get("default_thread_id")

        # 1. Existing default?
        if default_id:
            thread = self.get_thread(default_id)
            if thread is not None:
                return thread
            _log.warning("Default thread %s missing from disk — will recreate", default_id)

        # 2. Try legacy migration
        migrated = self._migrate_legacy_conversation()
        if migrated:
            return migrated

        # 3. Fresh default
        thread = self.create_thread(
            title="Nova conversation",
            metadata={"default": True, "source": "nova-link"},
        )
        index = self._load_index()
        index["default_thread_id"] = thread.id
        self._save_index(index)
        _log.info("Created fresh default thread %s", thread.id)
        return thread

    def get_default_thread_id(self) -> str | None:
        """Return the current default thread ID (or None if unset)."""
        return self._load_index().get("default_thread_id")

    # ---- legacy migration -------------------------------------------------

    def _migrate_legacy_conversation(self) -> Thread | None:
        """One-time idempotent migration from the legacy ConversationBuffer.

        Reads STATE/conversations/530812511.json and creates a thread with
        the same messages and session summary.  Marks the migration in the
        index so repeat calls are no-ops.

        Does NOT delete or modify the legacy file (Telegram still uses it).
        """
        index = self._load_index()

        # Already migrated?
        if index.get("migrated_from") == LEGACY_CHAT_ID:
            default_id = index.get("default_thread_id")
            if default_id:
                thread = self.get_thread(default_id)
                if thread is not None:
                    return thread
            # Index says migrated but thread is gone — fall through to re-migrate
            _log.warning("Migration marker present but thread missing — re-migrating")

        legacy_path = LEGACY_CONVERSATIONS_DIR / f"{LEGACY_CHAT_ID}.json"
        if not legacy_path.exists():
            return None

        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("Failed to read legacy conversation: %s", e)
            return None

        legacy_messages = legacy.get("messages", [])
        session_summary = legacy.get("session_summary", "")

        if not legacy_messages:
            return None

        # Build thread
        first_ts = legacy_messages[0].get("timestamp", time.time())
        last_ts = legacy_messages[-1].get("timestamp", time.time())

        first_user = next((m for m in legacy_messages if m.get("role") == "user"), None)
        title = "Migrated conversation"
        if first_user:
            title = first_user["content"][:80].split("\n")[0]

        summary = None
        if session_summary:
            summary = {"type": "text", "content": session_summary}

        thread = Thread(
            id=_new_id(),
            title=title,
            created_at=first_ts,
            updated_at=last_ts,
            summary=summary,
            metadata={
                "default": True,
                "source": "migration",
                "migrated_from": LEGACY_CHAT_ID,
            },
        )

        messages: list[Message] = []
        for m in legacy_messages:
            messages.append(
                Message(
                    id=_new_id(),
                    thread_id=thread.id,
                    role=m["role"],
                    content=m["content"],
                    created_at=m.get("timestamp", time.time()),
                    metadata={"migrated": True},
                )
            )

        thread.message_count = len(messages)
        self._save_thread_file(thread, messages)

        # Update index
        index = self._load_index()
        index["default_thread_id"] = thread.id
        index["migrated_from"] = LEGACY_CHAT_ID
        index["threads"][thread.id] = thread.to_dict()
        self._save_index(index)

        _log.info(
            "Migrated legacy conversation %s → thread %s (%d messages)",
            LEGACY_CHAT_ID,
            thread.id,
            len(messages),
        )
        return thread

    def migrate_legacy_conversation(self) -> Thread | None:
        """Public API for legacy migration. Idempotent."""
        return self._migrate_legacy_conversation()
