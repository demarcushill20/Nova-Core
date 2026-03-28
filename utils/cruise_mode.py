"""CRUISE mode — reduce heartbeat frequency during stable GREEN periods.

When the system has been GREEN with MONITOR decisions for N consecutive
heartbeat cycles, CRUISE mode activates:
  - Skips LLM agent, research, and planning cycles (saves cost + CPU)
  - Still runs all health checks and autonomy scoring
  - Exits immediately on any non-MONITOR decision or alert level change

State is persisted to STATE/cruise_state.json for cross-heartbeat tracking.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("/home/nova/nova-core/STATE")
CRUISE_STATE_FILE = STATE_DIR / "cruise_state.json"

# Tuning: how many consecutive GREEN+MONITOR cycles before entering CRUISE
CRUISE_ENTRY_THRESHOLD = 3

# Maximum cycles in CRUISE before forcing a full cycle (prevents staleness)
CRUISE_MAX_CONSECUTIVE = 12  # ~6 hours at 30-min intervals


@dataclass
class CruiseState:
    """Persistent CRUISE mode state."""

    active: bool = False
    consecutive_stable: int = 0
    cruise_cycles: int = 0  # how many cycles spent in CRUISE
    last_score: float = 0.0
    last_alert_level: str = ""
    last_decision_mode: str = ""
    entered_at: str = ""  # ISO timestamp when CRUISE was entered
    last_updated: str = ""


def load_cruise_state() -> CruiseState:
    """Load CRUISE state from disk, or return defaults."""
    try:
        if CRUISE_STATE_FILE.exists():
            data = json.loads(CRUISE_STATE_FILE.read_text())
            return CruiseState(**{k: v for k, v in data.items() if k in CruiseState.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError, OSError):
        pass
    return CruiseState()


def save_cruise_state(state: CruiseState) -> None:
    """Atomically persist CRUISE state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.last_updated = datetime.now(timezone.utc).isoformat()
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f, indent=2)
        os.replace(tmp, str(CRUISE_STATE_FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_cruise_state(
    score: float,
    alert_level: str,
    decision_mode: str,
) -> CruiseState:
    """Update CRUISE state based on current autonomy cycle results.

    Returns the updated state (already persisted).
    """
    state = load_cruise_state()
    now = datetime.now(timezone.utc).isoformat()

    is_stable = alert_level == "GREEN" and decision_mode == "monitor"
    score_drift = abs(score - state.last_score) if state.last_score > 0 else 0.0

    if is_stable and score_drift < 5.0:
        # Stable cycle — increment counter
        state.consecutive_stable += 1

        if state.active:
            state.cruise_cycles += 1
            # Force exit after max consecutive CRUISE cycles
            if state.cruise_cycles >= CRUISE_MAX_CONSECUTIVE:
                print(f"[cruise] Exiting CRUISE — max consecutive cycles ({CRUISE_MAX_CONSECUTIVE}) reached")
                state.active = False
                state.cruise_cycles = 0
        elif state.consecutive_stable >= CRUISE_ENTRY_THRESHOLD:
            # Enter CRUISE mode
            state.active = True
            state.cruise_cycles = 0
            state.entered_at = now
            print(
                f"[cruise] Entering CRUISE mode after {state.consecutive_stable} "
                f"stable cycles (score={score}, alert={alert_level})"
            )
    else:
        # Unstable — exit CRUISE immediately
        if state.active:
            print(
                f"[cruise] Exiting CRUISE — "
                f"alert={alert_level}, decision={decision_mode}, "
                f"score_drift={score_drift:.1f}"
            )
        state.active = False
        state.consecutive_stable = 0
        state.cruise_cycles = 0

    state.last_score = score
    state.last_alert_level = alert_level
    state.last_decision_mode = decision_mode

    save_cruise_state(state)
    return state


def is_cruise_active() -> bool:
    """Quick check if CRUISE mode is currently active."""
    state = load_cruise_state()
    return state.active
