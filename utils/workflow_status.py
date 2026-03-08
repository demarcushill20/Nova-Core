"""Utility: safely read workflow status from STATE/workflows/ JSON files."""

import json
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "STATE"
WORKFLOWS_DIR = STATE_DIR / "workflows"

TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected", "halted"})


def is_terminal_status(status: str) -> bool:
    """Return True when *status* is a terminal workflow state."""
    return status in TERMINAL_STATUSES


def read_workflow_status(workflow_id: str, *, workflows_dir: Path | None = None) -> str | None:
    """Read a workflow JSON file and return its status field.

    Args:
        workflow_id: The workflow identifier (filename stem, without .json).
        workflows_dir: Override directory for testing. Defaults to STATE/workflows/.

    Returns:
        The status string if the file exists and contains a valid status field,
        or None if the file is missing, unreadable, or malformed.
    """
    wdir = workflows_dir or WORKFLOWS_DIR
    path = wdir / f"{workflow_id}.json"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None
