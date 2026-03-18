"""Conversation recap persistence for session reconstruction.

Maintains a lightweight summary of each chat's conversation state
so CEO Nova can reconstruct context when the conversation buffer
expires (2hr idle → new session).

Storage: STATE/conversation_recap/{chat_id}.json
Max age: 8 hours (beyond that, recap is too stale to be useful)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_log = logging.getLogger("telegram_bot.recap")

STATE_DIR = Path("/home/nova/nova-core/STATE")
RECAP_DIR = STATE_DIR / "conversation_recap"

# Recap older than this is too stale to use
MAX_RECAP_AGE_SECONDS = 8 * 3600


def _recap_path(chat_id: str) -> Path:
    return RECAP_DIR / f"{chat_id}.json"


def load_recap(chat_id: str) -> dict | None:
    """Load the conversation recap for a chat. Returns None if missing or stale."""
    path = _recap_path(chat_id)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        # Check staleness
        updated_at = data.get("updated_at", 0)
        if time.time() - updated_at > MAX_RECAP_AGE_SECONDS:
            _log.info("RECAP_STALE chat=%s age_h=%.1f", chat_id, (time.time() - updated_at) / 3600)
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("Failed to load recap for %s: %s", chat_id, e)
        return None


def update_recap(chat_id: str, user_msg: str, assistant_msg: str) -> None:
    """Update the conversation recap after an assistant response.

    This is a Python-side extraction — no LLM call needed.
    Captures literal last messages and maintains a recent thread.
    """
    RECAP_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing or create new
    recap = load_recap(chat_id) or {}

    recap["updated_at"] = time.time()
    recap["last_user_message"] = user_msg[:300]
    recap["last_assistant_response"] = assistant_msg[:300]

    # Maintain a thread of recent user messages (last 3)
    recent_thread = recap.get("recent_thread", [])
    recent_thread = recent_thread[-2:] + [user_msg[:150]]
    recap["recent_thread"] = recent_thread

    # Save
    try:
        path = _recap_path(chat_id)
        path.write_text(json.dumps(recap, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        _log.warning("Failed to save recap for %s: %s", chat_id, e)


def format_for_context(chat_id: str) -> str:
    """Format the recap as a context block for session restart injection."""
    recap = load_recap(chat_id)
    if not recap:
        return ""

    parts = ["LAST CONVERSATION RECAP:"]

    if recap.get("last_user_message"):
        parts.append(f'Last user message: "{recap["last_user_message"]}"')
    if recap.get("last_assistant_response"):
        parts.append(f'Last assistant response: "{recap["last_assistant_response"]}"')
    if recap.get("recent_thread"):
        thread = recap["recent_thread"]
        if len(thread) > 1:
            parts.append("Recent conversation thread:")
            for msg in thread:
                parts.append(f'  - "{msg}"')

    return "\n".join(parts)


def clear_recap(chat_id: str) -> None:
    """Clear the recap for a chat (e.g., after new session is well-established)."""
    path = _recap_path(chat_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
