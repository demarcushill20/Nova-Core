"""Task checkpointing for crash-resilient task resumption.

Phase 6B.14: Save task progress to disk so interrupted tasks can resume
after restart. Checkpoints are stored as JSON files in STATE/checkpoints/.

Stdlib only. Thread-safe via threading.Lock. Atomic writes via tmp+rename.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "STATE" / "checkpoints"

# Maximum retries before a task is considered permanently failed
MAX_TASK_RETRIES = 3

# Global lock for thread-safe file operations
_lock = threading.Lock()


@dataclass
class TaskCheckpoint:
    """Represents the saved state of a task in progress."""

    task_id: str
    task_file: str
    status: str  # "dispatched" | "in_progress" | "partial_output"
    started_at: str  # ISO timestamp
    last_updated: str  # ISO timestamp
    partial_output: str | None = None
    retry_count: int = 0


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _checkpoint_path(task_id: str) -> Path:
    """Return the filesystem path for a task's checkpoint file."""
    return CHECKPOINT_DIR / f"{task_id}.json"


def _save_checkpoint_unlocked(checkpoint: TaskCheckpoint) -> Path:
    """Write checkpoint — caller MUST hold _lock."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    target = _checkpoint_path(checkpoint.task_id)
    data = asdict(checkpoint)
    data["last_updated"] = _now_iso()

    fd, tmp_path = tempfile.mkstemp(dir=str(CHECKPOINT_DIR), suffix=".tmp", prefix=f"{checkpoint.task_id}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info("Checkpoint saved: %s (status=%s)", checkpoint.task_id, checkpoint.status)
    return target


def save_checkpoint(checkpoint: TaskCheckpoint) -> Path:
    """Write checkpoint to STATE/checkpoints/{task_id}.json.

    Uses atomic write (write to tmp, then rename) to prevent corruption.
    Thread-safe via _lock.
    """
    with _lock:
        return _save_checkpoint_unlocked(checkpoint)


def _load_checkpoint_unlocked(task_id: str) -> TaskCheckpoint | None:
    """Load checkpoint — caller MUST hold _lock."""
    path = _checkpoint_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskCheckpoint(
            task_id=data["task_id"],
            task_file=data["task_file"],
            status=data["status"],
            started_at=data["started_at"],
            last_updated=data["last_updated"],
            partial_output=data.get("partial_output"),
            retry_count=data.get("retry_count", 0),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Malformed checkpoint for %s: %s", task_id, exc)
        return None


def load_checkpoint(task_id: str) -> TaskCheckpoint | None:
    """Load checkpoint for a task, or None if not found or malformed."""
    with _lock:
        return _load_checkpoint_unlocked(task_id)


def clear_checkpoint(task_id: str) -> None:
    """Remove checkpoint file after successful completion."""
    with _lock:
        path = _checkpoint_path(task_id)
        path.unlink(missing_ok=True)
        logger.info("Checkpoint cleared: %s", task_id)


def list_incomplete_checkpoints() -> list[TaskCheckpoint]:
    """Find all checkpoints that haven't been cleared (incomplete tasks).

    Returns only checkpoints with retry_count < MAX_TASK_RETRIES.
    Skips malformed files gracefully.

    M6 fix: collects file paths under lock, parses outside to reduce lock
    hold time when the checkpoint directory contains many files.
    """
    if not CHECKPOINT_DIR.exists():
        return []

    # Collect paths under lock (fast), then parse without holding it
    with _lock:
        paths = sorted(CHECKPOINT_DIR.glob("*.json"))

    results: list[TaskCheckpoint] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cp = TaskCheckpoint(
                task_id=data["task_id"],
                task_file=data["task_file"],
                status=data["status"],
                started_at=data["started_at"],
                last_updated=data["last_updated"],
                partial_output=data.get("partial_output"),
                retry_count=data.get("retry_count", 0),
            )
            if cp.retry_count < MAX_TASK_RETRIES:
                results.append(cp)
            else:
                logger.warning(
                    "Checkpoint %s exceeded max retries (%d/%d) — skipping",
                    cp.task_id,
                    cp.retry_count,
                    MAX_TASK_RETRIES,
                )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Skipping malformed checkpoint %s: %s", path.name, exc)

    return results


def update_status(task_id: str, status: str, partial_output: str | None = None) -> Path | None:
    """Update the status of an existing checkpoint.

    Returns the checkpoint path, or None if no checkpoint exists.
    Thread-safe: holds _lock across the full load+modify+save to prevent
    race conditions (H1 fix — prior version used separate lock acquisitions).
    """
    with _lock:
        cp = _load_checkpoint_unlocked(task_id)
        if cp is None:
            return None
        cp.status = status
        if partial_output is not None:
            cp.partial_output = partial_output
        return _save_checkpoint_unlocked(cp)


def increment_retry(task_id: str) -> TaskCheckpoint | None:
    """Increment retry count for a task checkpoint.

    Returns the updated checkpoint, or None if not found.
    Thread-safe: holds _lock across the full load+modify+save (H1 fix).
    """
    with _lock:
        cp = _load_checkpoint_unlocked(task_id)
        if cp is None:
            return None
        cp.retry_count += 1
        _save_checkpoint_unlocked(cp)
        return cp
