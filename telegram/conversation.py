"""In-memory conversation buffer for CEO Nova Telegram conversations.

Stores the last N messages per chat_id with automatic eviction
by count and age. Supports disk persistence for restart survival.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_log = logging.getLogger("telegram_bot.conversation")


MAX_MESSAGES = 20
MAX_AGE_SECONDS = 2 * 3600  # 2 hours


class Message:
    __slots__ = ("content", "role", "timestamp")

    def __init__(self, role: str, content: str, timestamp: float) -> None:
        self.role = role
        self.content = content
        self.timestamp = timestamp


class ConversationBuffer:
    """Per-chat conversation history with bounded retention."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.last_activity: float = 0.0
        self.session_summary: str = ""  # compact summary of evicted messages

    def add(self, role: str, content: str) -> None:
        now = time.time()
        self.messages.append(Message(role=role, content=content, timestamp=now))
        self.last_activity = now
        self._evict()

    def get_history(self) -> list[dict]:
        """Return messages as dicts suitable for LLM context.

        If a session summary exists from compacted older messages,
        it is prepended as a system message.
        """
        self._evict()
        history = []
        if self.session_summary:
            history.append(
                {
                    "role": "user",
                    "content": f"[Prior conversation summary: {self.session_summary}]",
                }
            )
        history.extend({"role": m.role, "content": m.content} for m in self.messages)
        return history

    def is_session_start(self) -> bool:
        """True if no recent activity (new session)."""
        if not self.messages:
            return True
        return (time.time() - self.last_activity) > MAX_AGE_SECONDS

    def clear(self) -> None:
        self.messages.clear()
        self.last_activity = 0.0
        self.session_summary = ""

    def _evict(self) -> None:
        cutoff = time.time() - MAX_AGE_SECONDS
        expired = [m for m in self.messages if m.timestamp <= cutoff]
        self.messages = [m for m in self.messages if m.timestamp > cutoff]

        # Compact expired messages into session summary
        if expired:
            self._compact_into_summary(expired)

        if len(self.messages) > MAX_MESSAGES:
            overflow = self.messages[:-MAX_MESSAGES]
            self._compact_into_summary(overflow)
            self.messages = self.messages[-MAX_MESSAGES:]

    def _compact_into_summary(self, evicted: list[Message]) -> None:
        """Deterministic compaction of evicted messages into a brief summary.

        No LLM call — extracts key topics from user messages.
        """
        user_msgs = [m.content for m in evicted if m.role == "user"]
        if not user_msgs:
            return

        # Extract first line of each user message as topic indicator
        topics = []
        for msg in user_msgs:
            first_line = msg.strip().split("\n")[0][:100]
            if first_line:
                topics.append(first_line)

        if not topics:
            return

        # Build compact summary (cap at 500 chars)
        summary = f"User discussed {len(topics)} topic(s): " + "; ".join(topics)
        if len(summary) > 500:
            summary = summary[:497] + "..."

        if self.session_summary:
            combined = self.session_summary + " | " + summary
            if len(combined) > 800:
                # Keep most recent summary, trim old
                self.session_summary = summary
            else:
                self.session_summary = combined
        else:
            self.session_summary = summary


PERSIST_DIR = Path("/home/nova/nova-core/STATE/conversations")


class ConversationManager:
    """Manages conversation buffers for multiple chat IDs.

    Supports optional disk persistence so conversations survive bot restarts.
    """

    def __init__(self, persist: bool = True) -> None:
        self._buffers: dict[str, ConversationBuffer] = {}
        self._persist = persist
        if persist:
            self._load_from_disk()

    def get(self, chat_id: str) -> ConversationBuffer:
        if chat_id not in self._buffers:
            self._buffers[chat_id] = ConversationBuffer()
        return self._buffers[chat_id]

    def add_user_message(self, chat_id: str, content: str) -> None:
        self.get(chat_id).add("user", content)
        if self._persist:
            self._save_chat(chat_id)

    def add_assistant_message(self, chat_id: str, content: str) -> None:
        self.get(chat_id).add("assistant", content)
        if self._persist:
            self._save_chat(chat_id)

    def get_history(self, chat_id: str) -> list[dict]:
        return self.get(chat_id).get_history()

    def reload_chat(self, chat_id: str) -> None:
        """Re-read a chat from disk to pick up changes from other processes."""
        if not self._persist:
            return
        path = PERSIST_DIR / f"{chat_id}.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            buf = ConversationBuffer()
            for m in data.get("messages", []):
                buf.messages.append(
                    Message(
                        role=m["role"],
                        content=m["content"],
                        timestamp=m["timestamp"],
                    )
                )
            buf.last_activity = data.get("last_activity", 0.0)
            buf.session_summary = data.get("session_summary", "")
            buf._evict()
            self._buffers[chat_id] = buf
        except (json.JSONDecodeError, KeyError, OSError) as e:
            _log.warning("Failed to reload chat %s: %s", chat_id, e)

    def is_session_start(self, chat_id: str) -> bool:
        return self.get(chat_id).is_session_start()

    def _save_chat(self, chat_id: str) -> None:
        """Persist a single chat buffer to disk."""
        try:
            PERSIST_DIR.mkdir(parents=True, exist_ok=True)
            buf = self.get(chat_id)
            data = {
                "messages": [{"role": m.role, "content": m.content, "timestamp": m.timestamp} for m in buf.messages],
                "last_activity": buf.last_activity,
                "session_summary": buf.session_summary,
            }
            path = PERSIST_DIR / f"{chat_id}.json"
            path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        except OSError as e:
            _log.warning("Failed to persist chat %s: %s", chat_id, e)

    def _load_from_disk(self) -> None:
        """Restore conversation buffers from disk on startup."""
        if not PERSIST_DIR.exists():
            return
        loaded = 0
        for path in PERSIST_DIR.glob("*.json"):
            chat_id = path.stem
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                buf = ConversationBuffer()
                for m in data.get("messages", []):
                    buf.messages.append(
                        Message(
                            role=m["role"],
                            content=m["content"],
                            timestamp=m["timestamp"],
                        )
                    )
                buf.last_activity = data.get("last_activity", 0.0)
                buf.session_summary = data.get("session_summary", "")
                buf._evict()  # Clean stale messages
                if buf.messages:  # Only keep non-empty buffers
                    self._buffers[chat_id] = buf
                    loaded += 1
            except (json.JSONDecodeError, KeyError, OSError) as e:
                _log.warning("Failed to load chat %s: %s", chat_id, e)
        if loaded:
            _log.info("Restored %d conversation(s) from disk", loaded)
