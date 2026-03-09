"""In-memory conversation buffer for CEO Nova Telegram conversations.

Stores the last N messages per chat_id with automatic eviction
by count and age. Thread-safe for asyncio use.
"""
from __future__ import annotations

import time


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


class ConversationManager:
    """Manages conversation buffers for multiple chat IDs."""

    def __init__(self) -> None:
        self._buffers: dict[str, ConversationBuffer] = {}

    def get(self, chat_id: str) -> ConversationBuffer:
        if chat_id not in self._buffers:
            self._buffers[chat_id] = ConversationBuffer()
        return self._buffers[chat_id]

    def add_user_message(self, chat_id: str, content: str) -> None:
        self.get(chat_id).add("user", content)

    def add_assistant_message(self, chat_id: str, content: str) -> None:
        self.get(chat_id).add("assistant", content)

    def get_history(self, chat_id: str) -> list[dict]:
        return self.get(chat_id).get_history()

    def is_session_start(self, chat_id: str) -> bool:
        return self.get(chat_id).is_session_start()
