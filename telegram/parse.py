"""Telegram command parser for NovaCore.

Parses raw Telegram messages into canonical action dicts
per PROTOCOL/telegram_commands.md v1.1.

No file I/O. No side effects. Pure parsing only.
"""

import re as _re

_MAX_MSG_LEN = 4096
_MAX_TITLE_LEN = 200
_TAIL_DEFAULT = 50
_TAIL_MAX = 200
_VALID_MODES = ("compact", "normal", "verbose")

_KNOWN_COMMANDS = frozenset(
    ("run", "status", "last", "get", "tail", "cancel", "mode", "help",
     "chat", "report", "goals", "briefing")
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
    first_line = lines[0].strip() if lines else ""
    if not first_line:
        return _err("Error: /run requires a title. Usage: /run <title>")
    body = lines[1] if len(lines) > 1 else ""
    # If the first line exceeds the title limit, truncate it for the
    # filename and push the full original text into the body so the
    # classifier and worker still see everything.
    if len(first_line) > _MAX_TITLE_LEN:
        title = first_line[:_MAX_TITLE_LEN]
        body = first_line + ("\n" + body if body else "")
    else:
        title = first_line
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


def _parse_chat(rest: str, chat_id: str, ts: float) -> dict:
    """Parse /chat <text> — force conversation path (no task queue)."""
    text = rest.strip()
    if not text:
        return _err("Error: /chat requires text. Usage: /chat <message>")
    action = _base("conversation", chat_id, ts)
    action["text"] = text
    return _ok(action)


def _parse_report(rest: str, chat_id: str, ts: float) -> dict:
    """Parse /report <text> — force full-report task."""
    result = _parse_run(rest, chat_id, ts)
    if result.get("ok"):
        result["action"]["intent"] = "task"
    return result


def _parse_goals(rest: str, chat_id: str, ts: float) -> dict:
    """Parse /goals [subcommand] [args].

    Subcommands:
      /goals           — list goals
      /goals add <text> — add a goal
      /goals done <id>  — mark goal done
      /goals remove <id> — remove a goal
      /goals clear      — remove completed goals
    """
    parts = rest.strip().split(None, 1)
    action = _base("goals", chat_id, ts)

    if not parts:
        action["subcommand"] = "list"
        return _ok(action)

    sub = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub == "add":
        if not arg:
            return _err("Error: /goals add requires text. Usage: /goals add <goal>")
        action["subcommand"] = "add"
        action["text"] = arg
    elif sub == "done":
        goal_id = parse_int(arg) if arg else None
        if goal_id is None:
            return _err("Error: /goals done requires a goal ID. Usage: /goals done <id>")
        action["subcommand"] = "done"
        action["goal_id"] = goal_id
    elif sub == "remove":
        goal_id = parse_int(arg) if arg else None
        if goal_id is None:
            return _err("Error: /goals remove requires a goal ID. Usage: /goals remove <id>")
        action["subcommand"] = "remove"
        action["goal_id"] = goal_id
    elif sub == "clear":
        action["subcommand"] = "clear"
    else:
        return _err(f'Error: unknown /goals subcommand "{sub}". '
                    "Use: add, done, remove, clear")

    return _ok(action)


def _parse_briefing(chat_id: str, ts: float) -> dict:
    """Parse /briefing — generate a system briefing."""
    return _ok(_base("briefing", chat_id, ts))


# --- Intent classification ---------------------------------------------------

# Task keywords: report-style terms that clearly need the task queue
_TASK_KEYWORDS = _re.compile(
    r"\b(report|contract|full output|debug|verbose|audit"
    r"|show sources|show files|detailed)\b",
    _re.IGNORECASE,
)

# Action verbs: implementation/engineering work that needs background processing
_ACTION_VERBS = _re.compile(
    r"\b(implement|refactor|build|deploy|create|fix|update|write|rewrite"
    r"|test|review|analyze|investigate|research|scan|optimize|migrate"
    r"|set up|install|configure|add|remove|delete|move|rename"
    r"|generate|scaffold|upgrade|patch|redesign|rearchitect)\b",
    _re.IGNORECASE,
)

# Chat signals: conversational patterns that override action verbs
_CHAT_SIGNALS = _re.compile(
    r"^(hey|hi|hello|sup|yo|what'?s up|how are|how'?s it|good morning"
    r"|good evening|good night|thanks|thank you|ok|okay|sure|cool"
    r"|nice|great|awesome|perfect|got it|sounds good|let'?s go"
    r"|what do you think|how do we|should we|can you explain"
    r"|tell me about|what is|what are|who is|when did|why did"
    r"|remind me|what was|do you remember"
    # Follow-up / affirmative / deferral responses (Phase 10)
    r"|yes|yeah|yep|yup|no|nah|nope|not really"
    r"|do it|do that|go for it|let'?s do it|let'?s do that"
    r"|absolutely|definitely|for sure|please do|make it so"
    r"|not yet|maybe later|hold off|never mind|forget it"
    r"|lgtm|ship it|works for me|I'?m good|all good"
    r"|right|exactly|agreed|fair enough|makes sense"
    r"|why not|go ahead)\b",
    _re.IGNORECASE,
)

# Deliverable detector: action verb + object implies a concrete output.
# Overrides chat signals — "should we build a billing system?" → task.
# Determiner is optional so bare nouns also match: "research databases" → task.
# Pronouns (it, them, me, us) are excluded — "fix it" stays in chat.
_DELIVERABLE_RE = _re.compile(
    r"\b(build|create|implement|write|deploy|set up|install|fix|refactor"
    r"|generate|scaffold|migrate|redesign|configure|patch|add|move"
    r"|delete|remove|rewrite|upgrade|optimize|research|investigate"
    r"|analyze|scan|test|review)\s+"
    r"(?:(?:a|an|the|my|our|new|this|that|some)\s+)*"
    r"(?!it\b|them\b|me\b|us\b|him\b|her\b)"
    r"[a-zA-Z]\w{2,}",
    _re.IGNORECASE,
)

# Request verb pattern: explicit request prefix + action verb → task.
# "Can you build...?", "Please deploy...", "I need you to implement..." → task.
# These override chat signals because they're direct requests for work.
_REQUEST_VERB = _re.compile(
    r"(?:can you|could you|please|I need (?:you )?to|would you|I want (?:you )?to"
    r"|go ahead and|I'd like (?:you )?to)\s+"
    r"(?:implement|refactor|build|deploy|create|fix|update|write|rewrite"
    r"|test|review|analyze|investigate|research|scan|optimize|migrate"
    r"|set up|install|configure|add|remove|delete|move|rename"
    r"|generate|scaffold|upgrade|patch|redesign|rearchitect)\b",
    _re.IGNORECASE,
)


# Artifact request pattern: requests for file outputs that need the task queue.
# "send me a PDF", "convert to document", "export as CSV" → task.
# These don't contain traditional action verbs but clearly need background work.
_ARTIFACT_REQUEST = _re.compile(
    r"(?:send|email|give|share|export|convert|make|turn|save|download)\s+"
    r".*\b(?:pdf|document|doc|file|attachment|spreadsheet|csv|zip|archive)\b"
    r"|\b(?:as a pdf|to a pdf|to pdf|as pdf|into a pdf"
    r"|as a doc|to a doc|as a document|to a document"
    r"|as a csv|to a csv|as csv|to csv"
    r"|as a file|to a file|as a zip)\b",
    _re.IGNORECASE,
)


def classify_intent(message: str) -> str:
    """Classify a raw user message as 'chat' or 'task'.

    Priority:
      1. /chat  prefix  → chat
      2. /report prefix → task
      3. /run prefix     → task
      4. Other / commands → task
      5. Report-style task keywords → task
      6. Artifact requests (send PDF, convert to doc) → task
      7. Explicit request verbs (can you build...) → task
      8. Deliverable detector (action verb + object) → task
      9. Action verbs WITHOUT chat signals → task
     10. Default plain text → chat
    """
    text = message.strip()
    if not text:
        return "chat"

    low = text.lower()

    if low.startswith("/chat"):
        return "chat"
    if low.startswith("/report"):
        return "task"
    if text.startswith("/"):
        return "task"

    # Report-style keywords always route to task
    if _TASK_KEYWORDS.search(text):
        return "task"

    # Artifact requests: "send me a PDF", "convert to document" → task
    # These don't use traditional action verbs but clearly need the task queue.
    if _ARTIFACT_REQUEST.search(text):
        return "task"

    has_action = _ACTION_VERBS.search(text)
    has_chat_signal = _CHAT_SIGNALS.match(text)

    # Explicit request verbs: "can you build...", "please deploy...",
    # "I need you to implement..." → task regardless of chat signals.
    # These are direct requests for work, not conversational queries.
    if _REQUEST_VERB.search(text):
        return "task"

    # Deliverable detector: if the message implies a concrete output artifact,
    # it's a task even if it starts with a chat signal.
    # "Should we build a billing system?" → task (has deliverable)
    # "Sounds good, research databases" → task (bare noun deliverable)
    # "Should we refactor?" → chat (no deliverable object)
    if has_action and has_chat_signal and _DELIVERABLE_RE.search(text):
        return "task"

    # Action verbs route to task UNLESS the message also starts with
    # a conversational signal (e.g., "should we refactor?" → chat)
    if has_action and not has_chat_signal:
        return "task"

    return "chat"


# --- Main entry point --------------------------------------------------------


def parse_message(text: str, chat_id: str, ts: float) -> dict | None:
    """Parse a raw Telegram message into a canonical action dict.

    Plain text (no leading ``/``) is treated as ``/run <text>`` so users
    can queue tasks conversationally.

    Returns:
        {"ok": True,  "action": <dict>}  — parsed successfully
        {"ok": False, "error": <str>}    — parse error with message
    """
    text = text.strip()

    if len(text) > _MAX_MSG_LEN:
        return _err(f"Error: message too long (max {_MAX_MSG_LEN} chars)")

    # Plain text (no leading /) → classify intent first
    if not text.startswith("/"):
        intent = classify_intent(text)
        if intent == "chat":
            # Fast conversation path — no task file needed
            action = _base("conversation", chat_id, ts)
            action["text"] = text
            return _ok(action)
        # Task intent → create task file via run_task
        result = _parse_run(text, chat_id, ts)
        if result.get("ok"):
            result["action"]["intent"] = "task"
        return result

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
    if cmd == "chat":
        return _parse_chat(rest, chat_id, ts)
    if cmd == "report":
        return _parse_report(rest, chat_id, ts)
    if cmd == "goals":
        return _parse_goals(rest, chat_id, ts)
    if cmd == "briefing":
        return _parse_briefing(chat_id, ts)

    return None  # unreachable but defensive
