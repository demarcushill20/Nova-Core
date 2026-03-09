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
    __slots__ = ("role", "content", "timestamp")

    def __init__(self, role: str, content: str, timestamp: float) -> None:
        self.role = role
        self.content = content
        self.timestamp = timestamp


class ConversationBuffer:
    """Per-chat conversation history with bounded retention."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.last_activity: float = 0.0

    def add(self, role: str, content: str) -> None:
        now = time.time()
        self.messages.append(Message(role=role, content=content, timestamp=now))
        self.last_activity = now
        self._evict()

    def get_history(self) -> list[dict]:
        """Return messages as dicts suitable for LLM context."""
        self._evict()
        return [{"role": m.role, "content": m.content} for m in self.messages]

    def is_session_start(self) -> bool:
        """True if no recent activity (new session)."""
        if not self.messages:
            return True
        return (time.time() - self.last_activity) > MAX_AGE_SECONDS

    def clear(self) -> None:
        self.messages.clear()
        self.last_activity = 0.0

    def _evict(self) -> None:
        cutoff = time.time() - MAX_AGE_SECONDS
        self.messages = [m for m in self.messages if m.timestamp > cutoff]
        if len(self.messages) > MAX_MESSAGES:
            self.messages = self.messages[-MAX_MESSAGES:]


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

    def is_session_start(self, chat_id: str) -> bool:
        return self.get(chat_id).is_session_start()

    def _save_chat(self, chat_id: str) -> None:
        """Persist a single chat buffer to disk."""
        try:
            PERSIST_DIR.mkdir(parents=True, exist_ok=True)
            buf = self.get(chat_id)
            data = {
                "messages": [
                    {"role": m.role, "content": m.content, "timestamp": m.timestamp}
                    for m in buf.messages
                ],
                "last_activity": buf.last_activity,
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
                buf._evict()  # Clean stale messages
                if buf.messages:  # Only keep non-empty buffers
                    self._buffers[chat_id] = buf
                    loaded += 1
            except (json.JSONDecodeError, KeyError, OSError) as e:
                _log.warning("Failed to load chat %s: %s", chat_id, e)
        if loaded:
            _log.info("Restored %d conversation(s) from disk", loaded)
