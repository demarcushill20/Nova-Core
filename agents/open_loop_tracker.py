"""Phase 7 — Open-Loop Tracker.

Explicit tracking of unresolved work, pending decisions, blocked tasks,
and project continuity state across sessions.

An "open loop" is a structured record of work that has been identified
but not yet completed or resolved. Unlike implicit scattered artifacts,
open loops are first-class objects with lifecycle state.

This module does NOT:
- Auto-close loops without explicit evidence
- Reopen resolved loops casually
- Create loops from weak signals
- Bypass layer/store guardrails

Usage:
    from agents.open_loop_tracker import (
        OpenLoop, LoopAction, create_loop, update_loop, resolve_loop,
        find_duplicate, get_active_loops,
    )

    loop = create_loop(
        title="Fix auth timeout in telegram module",
        summary="Repeated 504 errors in production",
        source="watcher",
        project="nova-core",
    )
    if loop.action == "loop_created":
        # loop.loop is the OpenLoop object
        pass
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_LOOP_STATUSES = frozenset(
    {
        "proposed",  # Identified but not yet confirmed
        "open",  # Confirmed active unresolved work
        "blocked",  # Waiting on external input or dependency
        "deferred",  # Explicitly postponed
        "resolved",  # Work completed or question answered
        "closed_rejected",  # Determined to be invalid or not actionable
        "stale",  # No activity for extended period, auto-flagged
    }
)

VALID_LOOP_ACTIONS = frozenset(
    {
        "no_loop_action",  # Nothing to do
        "loop_created",  # New loop created
        "loop_updated",  # Existing loop updated
        "loop_blocked",  # Loop moved to blocked
        "loop_deferred",  # Loop deferred
        "loop_resolved",  # Loop resolved/completed
        "loop_rejected_insufficient_evidence",  # Not enough evidence
        "loop_rejected_duplicate",  # Duplicate detected
    }
)

# Allowed state transitions: from_status → {allowed_target_statuses}
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"open", "closed_rejected"}),
    "open": frozenset({"blocked", "deferred", "resolved", "closed_rejected", "stale"}),
    "blocked": frozenset({"open", "deferred", "resolved", "closed_rejected", "stale"}),
    "deferred": frozenset({"open", "blocked", "resolved", "closed_rejected", "stale"}),
    "resolved": frozenset(),  # Terminal — no transitions out
    "closed_rejected": frozenset(),  # Terminal
    "stale": frozenset({"open", "resolved", "closed_rejected"}),
}

# Minimum summary length to create a loop
_MIN_SUMMARY_LEN = 10

# Minimum title length
_MIN_TITLE_LEN = 5

# Default loop storage directory
_DEFAULT_LOOP_DIR = Path("STATE") / "open_loops"

# Stale threshold (days without update)
_STALE_THRESHOLD_DAYS = 14


# ---------------------------------------------------------------------------
# Open Loop data model
# ---------------------------------------------------------------------------


@dataclass
class OpenLoop:
    """A tracked open-loop record."""

    loop_id: str
    title: str
    summary: str
    source: str  # Who/what created this loop
    project: str = "nova-core"
    status: str = "proposed"  # One of VALID_LOOP_STATUSES
    opened_at: str = ""  # ISO timestamp
    updated_at: str = ""  # ISO timestamp
    due_hint: str = ""  # Informal time sensitivity
    blocker: str = ""  # What's blocking, if blocked
    owner: str = ""  # Actor/agent responsible
    related_task_ids: list[str] = field(default_factory=list)
    related_files: list[str] = field(default_factory=list)
    related_memories: list[str] = field(default_factory=list)
    confidence: str = "medium"
    closure_reason: str = ""  # Why it was resolved/rejected
    closure_evidence: str = ""  # What proved resolution
    tags: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # State transition log

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> OpenLoop:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def dedupe_key(self) -> str:
        """Stable key for deduplication."""
        raw = f"{self.project}|{self.title.lower().strip()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Loop action result
# ---------------------------------------------------------------------------


@dataclass
class LoopActionResult:
    """Result of an open-loop operation."""

    action: str  # One of VALID_LOOP_ACTIONS
    reason: str
    loop: OpenLoop | None = None  # The loop object (if created/updated)
    loop_id: str = ""
    previous_status: str = ""
    new_status: str = ""
    stored: bool = False
    store_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "action": self.action,
            "reason": self.reason,
            "loop_id": self.loop_id,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "stored": self.stored,
            "store_path": self.store_path,
        }
        if self.loop:
            d["loop"] = self.loop.to_dict()
        return d


# ---------------------------------------------------------------------------
# Loop persistence
# ---------------------------------------------------------------------------


class LoopStore:
    """File-based persistence for open loops in STATE/open_loops/."""

    def __init__(self, base: Path | None = None):
        self._base = base or _DEFAULT_LOOP_DIR

    def save(self, loop: OpenLoop) -> str:
        """Save/update a loop to disk. Returns file path."""
        self._base.mkdir(parents=True, exist_ok=True)
        filename = f"loop_{loop.loop_id}.json"
        path = self._base / filename
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(loop.to_dict(), indent=2, default=str))
            tmp.replace(path)
        except OSError:
            pass
        return str(path)

    def load(self, loop_id: str) -> OpenLoop | None:
        """Load a loop by ID."""
        path = self._base / f"loop_{loop_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return OpenLoop.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def load_all(self, *, status_filter: str | None = None) -> list[OpenLoop]:
        """Load all loops, optionally filtered by status."""
        if not self._base.exists():
            return []
        loops = []
        for f in sorted(self._base.glob("loop_*.json")):
            try:
                data = json.loads(f.read_text())
                loop = OpenLoop.from_dict(data)
                if status_filter is None or loop.status == status_filter:
                    loops.append(loop)
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        return loops

    def find_by_dedupe_key(self, key: str) -> OpenLoop | None:
        """Find an existing loop matching a dedupe key."""
        for loop in self.load_all():
            if loop.dedupe_key() == key:
                return loop
        return None

    def get_active_loops(self) -> list[OpenLoop]:
        """Get all non-terminal loops (proposed, open, blocked, deferred, stale)."""
        active_statuses = {"proposed", "open", "blocked", "deferred", "stale"}
        if not self._base.exists():
            return []
        loops = []
        for f in sorted(self._base.glob("loop_*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
                loop = OpenLoop.from_dict(data)
                if loop.status in active_statuses:
                    loops.append(loop)
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        return loops


# ---------------------------------------------------------------------------
# Loop lifecycle operations
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _generate_loop_id(title: str, source: str) -> str:
    ts = int(time.time())
    h = hashlib.sha256(f"{title}:{source}:{ts}".encode()).hexdigest()[:8]
    return f"ol-{ts}-{h}"


def create_loop(
    *,
    title: str,
    summary: str,
    source: str,
    project: str = "nova-core",
    confidence: str = "medium",
    blocker: str = "",
    due_hint: str = "",
    owner: str = "",
    related_task_ids: list[str] | None = None,
    related_files: list[str] | None = None,
    tags: list[str] | None = None,
    store: LoopStore | None = None,
) -> LoopActionResult:
    """Create a new open loop.

    Validates input quality, checks for duplicates, and persists.

    Args:
        title: Short description of the unresolved work.
        summary: Detailed description of what's unresolved.
        source: Who/what identified this loop.
        project: Related project.
        confidence: Confidence level.
        blocker: What's blocking progress, if anything.
        due_hint: Informal time sensitivity.
        owner: Actor responsible.
        related_task_ids: Related task stems.
        related_files: Related file paths.
        tags: Categorization tags.
        store: LoopStore for persistence (created if None).

    Returns:
        LoopActionResult with action and loop object.
    """
    # Validate input quality
    if len(title.strip()) < _MIN_TITLE_LEN:
        return LoopActionResult(
            action="loop_rejected_insufficient_evidence",
            reason=f"title_too_short: {len(title.strip())} < {_MIN_TITLE_LEN} chars",
        )

    if len(summary.strip()) < _MIN_SUMMARY_LEN:
        return LoopActionResult(
            action="loop_rejected_insufficient_evidence",
            reason=f"summary_too_short: {len(summary.strip())} < {_MIN_SUMMARY_LEN} chars",
        )

    store = store or LoopStore()

    # Dedupe check
    candidate = OpenLoop(
        loop_id="",  # temporary
        title=title.strip()[:100],
        summary=summary.strip()[:500],
        source=source,
        project=project,
    )
    existing = store.find_by_dedupe_key(candidate.dedupe_key())
    if existing and existing.status not in ("resolved", "closed_rejected"):
        return LoopActionResult(
            action="loop_rejected_duplicate",
            reason=f"duplicate: matches existing loop {existing.loop_id} ({existing.status})",
            loop=existing,
            loop_id=existing.loop_id,
        )

    # Create the loop
    loop_id = _generate_loop_id(title, source)
    now = _now_iso()
    initial_status = "blocked" if blocker else "open"

    loop = OpenLoop(
        loop_id=loop_id,
        title=title.strip()[:100],
        summary=summary.strip()[:500],
        source=source,
        project=project,
        status=initial_status,
        opened_at=now,
        updated_at=now,
        due_hint=due_hint,
        blocker=blocker,
        owner=owner,
        related_task_ids=related_task_ids or [],
        related_files=related_files or [],
        confidence=confidence,
        tags=tags or [],
        history=[
            {
                "timestamp": now,
                "from_status": "proposed",
                "to_status": initial_status,
                "reason": f"created by {source}",
            }
        ],
    )

    path = store.save(loop)

    return LoopActionResult(
        action="loop_created",
        reason=f"new loop from {source}: {title[:60]}",
        loop=loop,
        loop_id=loop_id,
        previous_status="proposed",
        new_status=initial_status,
        stored=True,
        store_path=path,
    )


def update_loop(
    loop: OpenLoop,
    *,
    new_status: str | None = None,
    summary_append: str = "",
    blocker: str | None = None,
    reason: str = "",
    store: LoopStore | None = None,
) -> LoopActionResult:
    """Update an existing open loop.

    Validates state transitions. Appends to history.

    Args:
        loop: The loop to update.
        new_status: Target status (must be an allowed transition).
        summary_append: Additional summary text to append.
        blocker: New blocker (or empty to clear).
        reason: Reason for the update.
        store: LoopStore for persistence.

    Returns:
        LoopActionResult with action and updated loop.
    """
    store = store or LoopStore()
    now = _now_iso()
    old_status = loop.status
    action = "loop_updated"

    # State transition
    if new_status and new_status != old_status:
        allowed = _ALLOWED_TRANSITIONS.get(old_status, frozenset())
        if new_status not in allowed:
            return LoopActionResult(
                action="no_loop_action",
                reason=f"invalid_transition: {old_status} → {new_status} not allowed",
                loop=loop,
                loop_id=loop.loop_id,
                previous_status=old_status,
                new_status=old_status,
            )

        loop.status = new_status
        loop.history.append(
            {
                "timestamp": now,
                "from_status": old_status,
                "to_status": new_status,
                "reason": reason or f"status changed to {new_status}",
            }
        )

        if new_status == "blocked":
            action = "loop_blocked"
        elif new_status == "deferred":
            action = "loop_deferred"
        elif new_status == "resolved":
            action = "loop_resolved"

    # Update fields
    if summary_append:
        loop.summary = (loop.summary + " | " + summary_append.strip())[:500]

    if blocker is not None:
        loop.blocker = blocker

    loop.updated_at = now

    path = store.save(loop)

    return LoopActionResult(
        action=action,
        reason=reason or f"loop updated ({old_status} → {loop.status})",
        loop=loop,
        loop_id=loop.loop_id,
        previous_status=old_status,
        new_status=loop.status,
        stored=True,
        store_path=path,
    )


def resolve_loop(
    loop: OpenLoop,
    *,
    closure_reason: str,
    closure_evidence: str = "",
    store: LoopStore | None = None,
) -> LoopActionResult:
    """Resolve an open loop with explicit evidence.

    Args:
        loop: The loop to resolve.
        closure_reason: Why this loop is resolved.
        closure_evidence: Evidence of resolution (e.g., task stem, commit).
        store: LoopStore for persistence.

    Returns:
        LoopActionResult.
    """
    if not closure_reason or len(closure_reason.strip()) < 5:
        return LoopActionResult(
            action="no_loop_action",
            reason="closure_reason_required: must provide a substantive reason (≥5 chars)",
            loop=loop,
            loop_id=loop.loop_id,
        )

    loop.closure_reason = closure_reason
    loop.closure_evidence = closure_evidence

    return update_loop(
        loop,
        new_status="resolved",
        reason=f"resolved: {closure_reason[:100]}",
        store=store,
    )


def reject_loop(
    loop: OpenLoop,
    *,
    reason: str,
    store: LoopStore | None = None,
) -> LoopActionResult:
    """Reject/close a loop as invalid or not actionable."""
    return update_loop(
        loop,
        new_status="closed_rejected",
        reason=f"rejected: {reason[:100]}",
        store=store,
    )


def mark_stale(loops: list[OpenLoop], store: LoopStore | None = None) -> list[LoopActionResult]:
    """Mark loops as stale if they haven't been updated recently.

    Only marks loops in active states (open, blocked, deferred).
    """
    store = store or LoopStore()
    results = []
    cutoff_s = _STALE_THRESHOLD_DAYS * 86400

    for loop in loops:
        if loop.status not in ("open", "blocked", "deferred"):
            continue
        try:
            updated_ts = time.mktime(time.strptime(loop.updated_at, "%Y-%m-%dT%H:%M:%SZ"))
            age_s = time.time() - updated_ts
            if age_s > cutoff_s:
                result = update_loop(
                    loop,
                    new_status="stale",
                    reason=f"no update for {int(age_s / 86400)} days",
                    store=store,
                )
                results.append(result)
        except (ValueError, OverflowError):
            continue

    return results


# ---------------------------------------------------------------------------
# Loop detection from events (conservative)
# ---------------------------------------------------------------------------


def detect_loop_from_event(
    event: dict[str, Any],
    store: LoopStore | None = None,
) -> LoopActionResult | None:
    """Detect if a runtime event implies an open loop.

    Conservative: only creates loops from clear unresolved-work signals.
    Returns None if no loop should be created.

    Supported event patterns:
    - task_failed → unresolved failure
    - plan_created with open steps → unfinished plan
    - session_end with open_threads → session continuity
    """
    event_type = event.get("event_type", "")
    title = event.get("title", "")
    summary = event.get("summary", "")
    source = event.get("source", "unknown")
    confidence = event.get("confidence", "medium")

    # Pattern 1: Task failure → unresolved work
    if event_type == "task_failed":
        task_stem = event.get("task_stem", event.get("stem", ""))
        return create_loop(
            title=f"Unresolved failure: {task_stem or title}"[:100],
            summary=f"Task failed: {summary}"[:500],
            source=source,
            confidence=confidence,
            related_task_ids=[task_stem] if task_stem else [],
            related_files=event.get("related_files", []),
            tags=["#loop/failure"],
            store=store,
        )

    # Pattern 2: Plan with pending next steps
    if event_type in ("plan_created", "plan_revised"):
        next_actions = event.get("next_actions", [])
        open_threads = event.get("open_threads", [])
        if next_actions or open_threads:
            items = next_actions or open_threads
            return create_loop(
                title=f"Plan follow-up: {title}"[:100],
                summary=f"Pending: {'; '.join(items[:5])}"[:500],
                source=source,
                confidence=confidence,
                tags=["#loop/plan_followup"],
                store=store,
            )

    # Pattern 3: Session end with unresolved threads
    if event_type == "session_end":
        open_threads = event.get("open_threads", [])
        if open_threads:
            return create_loop(
                title=f"Session continuity: {len(open_threads)} open threads"[:100],
                summary=f"Unresolved from session: {'; '.join(open_threads[:5])}"[:500],
                source=source,
                confidence="medium",
                tags=["#loop/session_continuity"],
                store=store,
            )

    # Pattern 4: Repeated failures (same stem)
    if event_type == "task_failed" and event.get("retry_count", 0) >= 2:
        return create_loop(
            title=f"Recurring failure: {event.get('stem', title)}"[:100],
            summary=f"Failed {event.get('retry_count', 0)}+ times: {summary}"[:500],
            source=source,
            confidence="high",
            tags=["#loop/recurring_failure"],
            store=store,
        )

    return None


# ---------------------------------------------------------------------------
# Loop recall helpers
# ---------------------------------------------------------------------------


def get_active_loops_for_recall(
    store: LoopStore | None = None,
    *,
    project: str | None = None,
    max_results: int = 10,
) -> list[dict]:
    """Get active loops formatted for recall results.

    Returns dicts compatible with router recall result format.
    """
    store = store or LoopStore()
    loops = store.get_active_loops()

    if project:
        loops = [lp for lp in loops if lp.project == project]

    # Sort by recency
    loops.sort(key=lambda lp: lp.updated_at, reverse=True)
    loops = loops[:max_results]

    results = []
    for loop in loops:
        results.append(
            {
                "loop_id": loop.loop_id,
                "title": loop.title,
                "summary": loop.summary,
                "status": loop.status,
                "source": loop.source,
                "project": loop.project,
                "confidence": loop.confidence,
                "opened_at": loop.opened_at,
                "updated_at": loop.updated_at,
                "blocker": loop.blocker,
                "due_hint": loop.due_hint,
                "tags": loop.tags,
                "related_task_ids": loop.related_task_ids,
                "_source_adapter": "open_loop_tracker",
                "event_type": "open_loop",
                "current_layer": "episodic",
            }
        )

    return results
