"""Telegram output formatting — report section stripper.

Strips structured report sections from worker output for clean
chat-mode Telegram replies.

No file I/O. No side effects. Pure formatting only.
classify_intent() lives in telegram/parse.py (same import-shim scope).
"""

import re

# --- Report stripping -------------------------------------------------------

# Section headers that mark the start of "structured junk".
# Everything from the FIRST match through EOF is removed.
_END_MARKERS = re.compile(
    r"^##\s*(?:CONTRACT|Files Referenced|Files Created(?:/Modified)?"
    r"|Security Notes|Verification)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Standalone CONTRACT block (no ##) — also an end marker.
_CONTRACT_BARE = re.compile(
    r"^CONTRACT\s*$", re.IGNORECASE | re.MULTILINE
)

# Metadata header lines produced by the worker.
_META_LINE = re.compile(
    r"^\*\*(?:Task(?: ID)?|Completed|Source|Status|Executed):\*\*\s.*$",
    re.MULTILINE,
)

# Title line: # Task 0010: ...
_TITLE_LINE = re.compile(r"^#\s+Task\s+\d{4}:.*$", re.MULTILINE)

# CONTRACT-style key-value lines (summary:, task_id:, status:, verification:).
_CONTRACT_FIELD = re.compile(
    r"^(?:summary|task_id|status|verification):\s.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Notifier telemetry footer.
_NOTIFIER_FOOTER = re.compile(r"\n---\nnotifier_pid=.*", re.DOTALL)

# Tool audit tables — very specific pattern: "| Memory | …" or "| Tool safety | …"
# as the first cell (standalone label, not part of a sentence).
_TOOL_TABLE_ROW = re.compile(
    r"^\|\s*(?:Memory|Tool safety|Markdown files)\s*\|.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Trailing horizontal rules.
_TRAILING_HR = re.compile(r"(\n---\s*)+\s*$")

# Leading horizontal rule after metadata removal.
_LEADING_HR = re.compile(r"^\s*---\s*\n")


def strip_report_sections(text: str) -> str:
    """Strip structured report sections, keeping only the answer.

    Safe to call on any text — returns it cleaned for chat display.
    """
    out = text

    # 1. Truncate from the first end-marker section through EOF.
    m = _END_MARKERS.search(out)
    if m:
        out = out[: m.start()]

    m = _CONTRACT_BARE.search(out)
    if m:
        out = out[: m.start()]

    # 2. Remove metadata lines.
    out = _META_LINE.sub("", out)

    # 3. Remove title line.
    out = _TITLE_LINE.sub("", out)

    # 4. Remove CONTRACT field lines (in case they appear before a section).
    out = _CONTRACT_FIELD.sub("", out)

    # 5. Remove notifier footer.
    out = _NOTIFIER_FOOTER.sub("", out)

    # 6. Remove tool audit table rows (very specific pattern).
    out = _TOOL_TABLE_ROW.sub("", out)

    # 7. Strip trailing horizontal rules.
    out = _TRAILING_HR.sub("", out)

    # 8. Strip leading horizontal rules.
    out = _LEADING_HR.sub("", out)

    # 9. Collapse excessive blank lines (3+ → 2).
    out = re.sub(r"\n{3,}", "\n\n", out)

    return out.strip()
