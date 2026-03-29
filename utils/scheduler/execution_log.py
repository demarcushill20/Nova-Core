"""Execution logging for the Intelligent Dynamic Block Scheduler.

Phase 5: Records task execution outcomes as JSONL entries for calibration
and after-action learning.  Each ExecutionRecord captures planned vs actual
duration, outcome quality, interruptions, and downstream effects.
"""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.work_unit import TaskClass, WorkMode, WorkUnit

log = logging.getLogger("nova.scheduler.execution_log")

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_DEFAULT_LOG_PATH = Path("STATE/scheduler/execution_log.jsonl")


# ---------------------------------------------------------------------------
# ExecutionRecord
# ---------------------------------------------------------------------------


class ExecutionRecord(BaseModel):
    """A single task execution record for calibration learning."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_class: TaskClass
    work_mode: WorkMode
    planned_duration_min: float = Field(ge=0.0)
    actual_duration_min: float = Field(ge=0.0)
    variance_ratio: float = Field(ge=0.0)  # actual / planned
    outcome: str  # success / partial / fail
    outcome_quality: float = Field(default=0.0, ge=0.0, le=100.0)
    interruptions: int = 0
    retry_count: int = 0
    tools_used: list[str] = Field(default_factory=list)
    unlocked_downstream: list[str] = Field(default_factory=list)
    timestamp: str = ""  # ISO format
    block_id: str = ""


# ---------------------------------------------------------------------------
# Helper: create from WorkUnit
# ---------------------------------------------------------------------------


def create_execution_record(
    work_unit: WorkUnit,
    actual_duration_min: float,
    outcome: str,
    block_id: str = "",
    outcome_quality: float = 0.0,
) -> ExecutionRecord:
    """Build an ExecutionRecord from a WorkUnit and actual results.

    Automatically computes variance_ratio = actual / planned.
    """
    planned = work_unit.estimated_expected_min
    variance_ratio = actual_duration_min / planned if planned > 0 else 1.0

    return ExecutionRecord(
        task_id=work_unit.id,
        task_class=work_unit.task_class,
        work_mode=work_unit.work_mode,
        planned_duration_min=planned,
        actual_duration_min=actual_duration_min,
        variance_ratio=variance_ratio,
        outcome=outcome,
        outcome_quality=outcome_quality,
        timestamp=datetime.now(timezone.utc).isoformat(),
        block_id=block_id,
    )


# ---------------------------------------------------------------------------
# Log I/O
# ---------------------------------------------------------------------------


def log_execution(
    record: ExecutionRecord,
    log_path: Path | None = None,
) -> Path:
    """Append an execution record as a JSON line to the log file.

    Creates the file (and parent directories) if they don't exist.
    Returns the path to the log file.
    """
    path = log_path or _DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    line = record.model_dump_json() + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()

    return path


def read_execution_log(
    log_path: Path | None = None,
    limit: int = 0,
) -> list[ExecutionRecord]:
    """Read execution records from a JSONL log file.

    Returns records in file order (most recent last).
    If limit > 0, returns only the last N records.
    Corrupt lines are skipped with a warning.
    """
    path = log_path or _DEFAULT_LOG_PATH

    if not path.exists():
        return []

    records: list[ExecutionRecord] = []
    with open(path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(ExecutionRecord.model_validate_json(line))
            except (json.JSONDecodeError, ValueError) as exc:
                warnings.warn(
                    f"Skipping corrupt line {line_num} in {path}: {exc}",
                    stacklevel=2,
                )

    if limit > 0:
        records = records[-limit:]

    return records
