"""Adapter: telegram.send_file

Send a file to Telegram via the Bot API. Uses existing bot configuration
from environment variables (TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_ID).
"""

import os
from pathlib import Path

import httpx


def _resolve_repo_root() -> Path:
    """Resolve the repo root dynamically (same approach as runner.py)."""
    return Path(__file__).resolve().parent.parent.parent


def telegram_send_file(
    path: str,
    caption: str = "",
    _sandbox: Path | None = None,
) -> dict:
    """Send a file to Telegram via the Bot API.

    Args:
        path: Relative or absolute path to the file (must be within sandbox).
        caption: Optional caption for the file message.
        _sandbox: Internal override for repo root (testing only).

    Returns:
        dict with keys: ok, file_sent, telegram_message_id, path
    """
    if not path or not isinstance(path, str):
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": "",
            "error": "path is required (non-empty str)",
        }

    root = (_sandbox if _sandbox is not None else _resolve_repo_root()).resolve()

    # Resolve path relative to sandbox
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = root / path
    file_path = file_path.resolve()

    # Sandbox enforcement
    try:
        file_path.relative_to(root)
    except ValueError:
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": f"Path escapes sandbox: {path!r} resolves outside {root}",
        }

    # Verify file exists
    if not file_path.is_file():
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": f"File not found: {path}",
        }

    # Get Telegram credentials from environment
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "").strip()

    if not bot_token:
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": "Missing TELEGRAM_BOT_TOKEN environment variable",
        }

    if not chat_id:
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": "Missing ALLOWED_CHAT_ID environment variable",
        }

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    try:
        with open(file_path, "rb") as f:
            files = {"document": (file_path.name, f)}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption[:1024]  # Telegram caption limit

            with httpx.Client(timeout=60) as client:
                resp = client.post(url, data=data, files=files)
                resp.raise_for_status()
                result = resp.json()

    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": f"Telegram API error: {exc.response.status_code} — {exc.response.text[:200]}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": f"Send failed: {exc}",
        }

    if not result.get("ok"):
        return {
            "ok": False,
            "file_sent": False,
            "telegram_message_id": "",
            "path": path,
            "error": f"Telegram API returned ok=false: {result.get('description', '')}",
        }

    message_id = str(result.get("result", {}).get("message_id", ""))

    return {
        "ok": True,
        "file_sent": True,
        "telegram_message_id": message_id,
        "path": path,
    }
