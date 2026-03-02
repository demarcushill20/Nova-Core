"""Telegram command parser for NovaCore.

Parses raw Telegram messages into canonical action dicts
per PROTOCOL/telegram_commands.md v1.1.

No file I/O. No side effects. Pure parsing only.
"""

_MAX_MSG_LEN = 4096
_MAX_TITLE_LEN = 200
_TAIL_DEFAULT = 50
_TAIL_MAX = 200
_VALID_MODES = ("compact", "normal", "verbose")

_KNOWN_COMMANDS = frozenset(
    ("run", "status", "last", "get", "tail", "cancel", "mode", "help")
)


# --- Helpers -----------------------------------------------------------------


def _base(action: str, chat_id: str, ts: float) -> dict:
    """Build the base canonical action dict."""
    return {"action": action, "source": "telegram", "chat_id": chat_id, "ts": ts}


def _ok(action: dict) -> dict:
    return {"ok": True, "action": action}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def normalize_task_id(raw: str) -> str:
    """Strip leading '#' from task IDs."""
    return raw.lstrip("#")


def parse_int(raw: str) -> int | None:
    """Parse a string as a positive integer, or return None."""
    try:
        v = int(raw)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def normalize_command(text: str) -> tuple[str, str]:
    """Extract the command word and the rest of the first line.

    Returns (command_lower, rest_of_message).
    The command word is the first token after '/'.
    rest_of_message is everything after the command word (including newlines).
    """
    # text is already stripped and starts with '/'
    without_slash = text[1:]
    # Split on first whitespace to get command vs rest
    parts = without_slash.split(None, 1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    return cmd, rest


# --- Command parsers ---------------------------------------------------------


def _parse_run(rest: str, chat_id: str, ts: float) -> dict:
    lines = rest.split("\n", 1)
    title = lines[0].strip() if lines else ""
    if not title:
        return _err("Error: /run requires a title. Usage: /run <title>")
    if len(title) > _MAX_TITLE_LEN:
        return _err(f"Error: title too long (max {_MAX_TITLE_LEN} chars)")
    body = lines[1] if len(lines) > 1 else ""
    action = _base("run_task", chat_id, ts)
    action["title"] = title
    action["body"] = body
    return _ok(action)


def _parse_status(chat_id: str, ts: float) -> dict:
    return _ok(_base("get_status", chat_id, ts))


def _parse_last(chat_id: str, ts: float) -> dict:
    return _ok(_base("get_last", chat_id, ts))


def _parse_get(rest: str, chat_id: str, ts: float) -> dict:
    parts = rest.split()
    if not parts:
        return _err("Error: /get requires a filename. Usage: /get <filename> [page]")
    filename = parts[0]
    page = 1
    if len(parts) >= 2:
        page = parse_int(parts[1])
        if page is None:
            return _err("Error: page must be a positive integer")
    action = _base("get_output", chat_id, ts)
    action["filename"] = filename
    action["page"] = page
    return _ok(action)


def _parse_tail(rest: str, chat_id: str, ts: float) -> dict:
    parts = rest.split()
    if not parts:
        return _err("Error: /tail requires a task_id. Usage: /tail <task_id> [lines]")
    task_id = normalize_task_id(parts[0])
    lines = _TAIL_DEFAULT
    if len(parts) >= 2:
        lines = parse_int(parts[1])
        if lines is None or lines > _TAIL_MAX:
            return _err(f"Error: lines must be a positive integer (max {_TAIL_MAX})")
    action = _base("tail_log", chat_id, ts)
    action["task_id"] = task_id
    action["lines"] = lines
    return _ok(action)


def _parse_cancel(rest: str, chat_id: str, ts: float) -> dict:
    parts = rest.split()
    if not parts:
        return _err(
            'Error: /cancel requires a task_id or "last". '
            "Usage: /cancel <task_id|last>"
        )
    raw_id = parts[0]
    task_id = raw_id if raw_id.lower() == "last" else normalize_task_id(raw_id)
    action = _base("cancel_task", chat_id, ts)
    action["task_id"] = task_id
    return _ok(action)


def _parse_mode(rest: str, chat_id: str, ts: float) -> dict:
    parts = rest.split()
    if not parts:
        # Query current mode
        return _ok(_base("get_mode", chat_id, ts))
    mode = parts[0].lower()
    if mode not in _VALID_MODES:
        return _err(
            f'Error: unknown mode "{parts[0]}". Choose: compact, normal, verbose'
        )
    action = _base("set_mode", chat_id, ts)
    action["mode"] = mode
    return _ok(action)


def _parse_help(chat_id: str, ts: float) -> dict:
    return _ok(_base("show_help", chat_id, ts))


# --- Main entry point --------------------------------------------------------


def parse_message(text: str, chat_id: str, ts: float) -> dict | None:
    """Parse a raw Telegram message into a canonical action dict.

    Returns:
        {"ok": True,  "action": <dict>}  — parsed successfully
        {"ok": False, "error": <str>}    — parse error with message
        None                             — non-command, silently ignore
    """
    text = text.strip()

    if not text.startswith("/"):
        return None

    if len(text) > _MAX_MSG_LEN:
        return _err(f"Error: message too long (max {_MAX_MSG_LEN} chars)")

    cmd, rest = normalize_command(text)

    if cmd not in _KNOWN_COMMANDS:
        return _err(f"Unknown command: /{cmd}. Send /help for available commands.")

    if cmd == "run":
        return _parse_run(rest, chat_id, ts)
    if cmd == "status":
        return _parse_status(chat_id, ts)
    if cmd == "last":
        return _parse_last(chat_id, ts)
    if cmd == "get":
        return _parse_get(rest, chat_id, ts)
    if cmd == "tail":
        return _parse_tail(rest, chat_id, ts)
    if cmd == "cancel":
        return _parse_cancel(rest, chat_id, ts)
    if cmd == "mode":
        return _parse_mode(rest, chat_id, ts)
    if cmd == "help":
        return _parse_help(chat_id, ts)

    return None  # unreachable but defensive
