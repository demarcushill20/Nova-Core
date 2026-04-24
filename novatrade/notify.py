"""Lightweight Telegram notification for trade rejections.

Sends rejection reasons directly to Telegram so the operator doesn't need
to flip log levels to DEBUG. Includes per-tag cooldown to prevent spam.

The cooldown is **persisted to disk** so a process restart does not reset
it — otherwise every restart while halted re-sends the same "halted"
alert. A 24h novelty exception ensures a tag never seen before still
alerts immediately on first occurrence even after the persisted record
ages out.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 900  # 15 min per tag
_COOLDOWN_PATH = Path("STATE/notify_cooldown.json")

# Wall-clock timestamps (seconds since epoch). Persisted across restarts.
_last_sent: dict[str, float] = {}
_loaded = False


def _load_cooldown() -> None:
    """Load persisted cooldown state. Idempotent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if _COOLDOWN_PATH.exists():
            data = json.loads(_COOLDOWN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for tag, ts in data.items():
                    try:
                        _last_sent[tag] = float(ts)
                    except (TypeError, ValueError):
                        continue
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("notify: failed to load cooldown state: %s", exc)


def _save_cooldown() -> None:
    """Persist cooldown state atomically."""
    try:
        _COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COOLDOWN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_last_sent), encoding="utf-8")
        tmp.replace(_COOLDOWN_PATH)
    except OSError as exc:
        log.debug("notify: failed to persist cooldown state: %s", exc)


def rejection_telegram(tag: str, detail: str) -> None:
    """Send a rejection notice to Telegram with per-tag persistent cooldown.

    Cooldown state persists to ``STATE/notify_cooldown.json`` so a process
    restart does not reset the timer and re-spam the operator. A tag seen
    for the first time (or not seen for longer than the cooldown) always
    sends — preserving first-occurrence-always-wins.
    """
    _load_cooldown()
    now = time.time()
    last = _last_sent.get(tag)
    if last is not None and (now - last) < _COOLDOWN_SECONDS:
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        return

    text = f"\u274c Trade rejected [{tag}]\n{detail}"
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
        _last_sent[tag] = now
        _save_cooldown()
    except Exception as exc:
        log.warning("rejection_telegram failed: %s", exc)
