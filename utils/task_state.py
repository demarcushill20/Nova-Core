from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("/home/nova/nova-core/STATE/task_state")


@dataclass
class TaskState:
    task_id: str
    status: str  # pending|running|blocked|completed|failed
    current_step: int
    max_steps: int
    last_error: str = ""
    updated_at: str = ""


def _state_path(task_id: str) -> Path:
    return STATE_DIR / f"{task_id}.json"


def load_task_state(task_id: str) -> TaskState | None:
    path = _state_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        # Truncated/corrupt state (e.g., crash mid-write): treat as no state.
        return None
    # Forward-compatible: ignore unknown keys written by newer versions.
    valid = {f.name for f in fields(TaskState)}
    return TaskState(**{k: v for k, v in data.items() if k in valid})


def save_task_state(state: TaskState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.updated_at = datetime.now(timezone.utc).isoformat()
    target = _state_path(state.task_id)
    # Atomic write: serialize to a temp file in the same dir, then os.replace.
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, target)


def advance_task_state(task_id: str) -> TaskState:
    state = load_task_state(task_id)
    if state is None:
        raise FileNotFoundError(f"no task state for {task_id!r}")
    state.current_step = min(state.current_step + 1, state.max_steps)
    state.status = "completed" if state.current_step >= state.max_steps else "running"
    save_task_state(state)
    return state


def fail_task_state(task_id: str, error: str) -> TaskState:
    state = load_task_state(task_id)
    if state is None:
        raise FileNotFoundError(f"no task state for {task_id!r}")
    state.status = "failed"
    state.last_error = error
    save_task_state(state)
    return state
