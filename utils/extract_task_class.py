"""Utility: safely extract task_class from a workflow JSON record."""

from __future__ import annotations

from typing import Any

# Canonical task classes recognised by the classifier / orchestrator.
KNOWN_TASK_CLASSES = frozenset(
    {
        "research",
        "code_impl",
        "code_review",
        "system",
        "simple",
        "unknown",
    }
)


def extract_task_class(record: Any) -> str | None:
    """Return the task_class string from *record*, or None for invalid shapes.

    Safely handles:
    - Non-dict inputs (list, str, int, None) → None
    - Missing ``task_class`` key → None
    - Non-string ``task_class`` value → None
    - Empty / whitespace-only strings → None

    Does **not** restrict to KNOWN_TASK_CLASSES — callers may check membership
    separately if needed.
    """
    if not isinstance(record, dict):
        return None

    val = record.get("task_class")

    if not isinstance(val, str):
        return None

    val = val.strip()
    return val if val else None
