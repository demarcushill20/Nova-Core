"""Utility: validate that a workflow record contains required keys."""

from __future__ import annotations

from typing import Any

# Required keys and their expected types for a valid workflow record.
REQUIRED_FIELDS: dict[str, type] = {
    "task_id": str,
    "workflow_id": str,
    "status": str,
    "task_class": str,
}

VALID_STATUSES = frozenset(
    {
        "created",
        "planning",
        "executing",
        "completed",
        "failed",
        "halted",
        "rejected",
    }
)


def validate_workflow_record(
    record: Any,
) -> tuple[bool, list[str]]:
    """Check that *record* is a dict with the required workflow keys.

    Returns:
        (True, [])          – record is valid
        (False, [reasons])  – record is invalid; reasons lists each problem
    """
    errors: list[str] = []

    if not isinstance(record, dict):
        return False, [f"expected dict, got {type(record).__name__}"]

    for key, expected_type in REQUIRED_FIELDS.items():
        val = record.get(key)
        if val is None:
            errors.append(f"missing required key: {key}")
        elif not isinstance(val, expected_type):
            errors.append(f"{key}: expected {expected_type.__name__}, got {type(val).__name__}")
        elif isinstance(val, str) and not val.strip():
            errors.append(f"{key}: must not be empty or blank")

    # Validate status value if present and correctly typed.
    status = record.get("status")
    if isinstance(status, str) and status.strip() and status not in VALID_STATUSES:
        errors.append(f"status: unknown value {status!r}")

    return (len(errors) == 0, errors)
