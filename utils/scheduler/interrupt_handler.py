"""Phase 8: Interrupt Handling & Preemption for the Intelligent Dynamic Block Scheduler.

Classifies incoming events (health alerts, operator messages, new tasks) into
interrupt levels, applies a preemption budget, pauses/resumes in-progress work,
and persists interrupt state for crash recovery.

Interrupt levels:
  CRITICAL     – stop immediately (production outage, kill switch)
  HIGH_INSERT  – finish checkpoint, then switch
  DEFER        – queue for next replan cycle
  LOG_ONLY     – record the event, do not schedule anything

The handler integrates with BlockState (from replanner.py) and produces
action dicts that the watcher dispatch loop can act on.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.replanner import BlockState

log = logging.getLogger("nova.scheduler.interrupt")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InterruptLevel(str, Enum):
    """Severity of an incoming interrupt."""

    CRITICAL = "critical"
    HIGH_INSERT = "high_insert"
    DEFER = "defer"
    LOG_ONLY = "log_only"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class InterruptEvent(BaseModel):
    """A single interrupt event from any source."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        default_factory=lambda: f"int_{int(time.time() * 1000)}",
    )
    source: str  # heartbeat / telegram / system / watcher
    event_type: str  # health_red / health_yellow / operator_message / ...
    level: InterruptLevel
    title: str
    detail: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    task_id: str | None = None  # associated task, if any


class InterruptPolicy(BaseModel):
    """Configurable rules for classifying and throttling interrupts."""

    model_config = ConfigDict(extra="forbid")

    max_preemptions_per_block: int = Field(
        default=2,
        ge=0,
        description="Maximum HIGH_INSERT preemptions before downgrade to DEFER",
    )
    auto_classify_rules: dict[str, str] = Field(
        default_factory=lambda: {
            "health_red": "critical",
            "health_yellow": "high_insert",
            "health_green": "log_only",
            "operator_critical": "critical",
            "operator_message": "high_insert",
            "operator_low": "defer",
            "operator_defer": "defer",
            "new_task_high": "high_insert",
            "new_task_medium": "defer",
            "new_task_low": "log_only",
            "budget_alert": "high_insert",
            "error_spike": "high_insert",
            "system_info": "log_only",
        },
    )


class InterruptState(BaseModel):
    """Mutable state tracking preemptions and paused work within a block."""

    model_config = ConfigDict(extra="forbid")

    preemption_count: int = 0
    interrupt_log: list[InterruptEvent] = Field(default_factory=list)
    paused_task_id: str | None = None
    paused_task_checkpoint: str | None = None  # checkpoint file path


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Operator prefix markers — messages starting with these override auto-classify
_OPERATOR_PREFIX_MAP: dict[str, InterruptLevel] = {
    "!critical": InterruptLevel.CRITICAL,
    "!low": InterruptLevel.DEFER,
    "!defer": InterruptLevel.DEFER,
}


def classify_interrupt(
    event_type: str,
    policy: InterruptPolicy | None = None,
    *,
    operator_text: str = "",
) -> InterruptLevel:
    """Classify an event type into an InterruptLevel.

    Priority:
      1. Operator prefix (!critical, !low, !defer) in *operator_text*
      2. Exact match in policy.auto_classify_rules
      3. Default: DEFER
    """
    # 1. Operator prefix override
    if operator_text:
        lower = operator_text.strip().lower()
        for prefix, level in _OPERATOR_PREFIX_MAP.items():
            if lower.startswith(prefix):
                return level

    # 2. Policy lookup
    pol = policy or InterruptPolicy()
    raw = pol.auto_classify_rules.get(event_type)
    if raw is not None:
        try:
            return InterruptLevel(raw)
        except ValueError:
            log.warning("Invalid level '%s' for event_type '%s', defaulting to DEFER", raw, event_type)
            return InterruptLevel.DEFER

    # 3. Fallback
    return InterruptLevel.DEFER


# ---------------------------------------------------------------------------
# Handling
# ---------------------------------------------------------------------------


def handle_interrupt(
    event: InterruptEvent,
    block_state: BlockState,
    interrupt_state: InterruptState,
    policy: InterruptPolicy | None = None,
) -> dict:
    """Process an interrupt event and decide on an action.

    Returns a dict with keys:
      action       – "logged" | "deferred" | "preempted"
      should_replan – bool
      detail       – human-readable explanation (optional)
      paused_task  – task ID that was paused (optional)
    """
    pol = policy or InterruptPolicy()

    # Always log the event
    interrupt_state.interrupt_log.append(event)

    # --- LOG_ONLY ----------------------------------------------------------
    if event.level == InterruptLevel.LOG_ONLY:
        return {"action": "logged", "should_replan": False}

    # --- DEFER -------------------------------------------------------------
    if event.level == InterruptLevel.DEFER:
        return {
            "action": "deferred",
            "should_replan": False,
            "detail": "queued for next replan",
        }

    # --- CRITICAL / HIGH_INSERT — preemption logic -------------------------
    if event.level == InterruptLevel.HIGH_INSERT and interrupt_state.preemption_count >= pol.max_preemptions_per_block:
        # Budget exhausted — downgrade
        return {
            "action": "deferred",
            "should_replan": False,
            "detail": "preemption budget exhausted",
        }

    # CRITICAL always honours, HIGH_INSERT within budget — preempt
    paused = block_state.in_progress_task_id
    if paused:
        interrupt_state.paused_task_id = paused
        interrupt_state.paused_task_checkpoint = f"STATE/checkpoints/{paused}_interrupt.json"

    interrupt_state.preemption_count += 1

    return {
        "action": "preempted",
        "paused_task": paused,
        "should_replan": True,
        "detail": f"Preempted for {event.level.value}: {event.title}",
    }


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------


def can_resume_paused(interrupt_state: InterruptState) -> bool:
    """Return True if a paused task exists that can be resumed."""
    return interrupt_state.paused_task_id is not None


def get_resume_task_id(interrupt_state: InterruptState) -> str | None:
    """Return the paused task ID (or None)."""
    return interrupt_state.paused_task_id


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path("STATE/scheduler")


def save_interrupt_state(
    state: InterruptState,
    path: Path | None = None,
) -> Path:
    """Persist interrupt state atomically to JSON.

    Default path: STATE/scheduler/interrupt_state.json
    """
    target = path or (_DEFAULT_STATE_DIR / "interrupt_state.json")
    target.parent.mkdir(parents=True, exist_ok=True)

    data = state.model_dump_json(indent=2)

    fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 — atomic write pattern
        mode="w",
        dir=str(target.parent),
        suffix=".tmp",
        delete=False,
    )
    try:
        fd.write(data)
        fd.flush()
        fd.close()
        Path(fd.name).replace(target)
    except Exception:
        try:
            Path(fd.name).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    log.debug("Saved interrupt state to %s", target)
    return target


def load_interrupt_state(path: Path | None = None) -> InterruptState:
    """Load interrupt state from JSON; return empty state if not found."""
    target = path or (_DEFAULT_STATE_DIR / "interrupt_state.json")

    if not target.exists():
        return InterruptState()

    try:
        raw = target.read_text()
        return InterruptState.model_validate_json(raw)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Failed to load interrupt state from %s: %s", target, exc)
        return InterruptState()
