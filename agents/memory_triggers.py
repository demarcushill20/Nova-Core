"""Phase 3 — Automatic Memory Trigger Engine.

Deterministic event-driven memory capture for NovaCore. Observes runtime
events, evaluates trigger eligibility, builds memory candidates, deduplicates,
and routes through the Unified Memory Router.

Usage:
    from agents.memory_triggers import trigger_engine

    # Fire a trigger from any event source
    result = trigger_engine.fire(
        trigger_class="task_lifecycle",
        event_type="task_completed",
        source="watcher",
        title="Implemented vault schema validation",
        summary="Added ADR type to canonical enum. All 73 tests pass.",
        caller="watcher.dispatch",
        ctx=trace_ctx,
        # Optional enrichment:
        task_stem="0200_build_auth",
        task_class="code_impl",
        confidence="high",
        related_files=["schemas/vault_note_schema.py"],
    )
    if result.stored:
        print(f"Memory created: {result.path_or_id}")
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from utils.structured_log import slog
from utils.trace_context import TraceContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trigger Classes — categories of events that can produce memory
# ---------------------------------------------------------------------------

VALID_TRIGGER_CLASSES = frozenset(
    {
        "task_lifecycle",  # task completion, failure
        "plan_lifecycle",  # plan creation, completion, failure
        "error_failure",  # runtime errors, crash reports
        "session_boundary",  # session start/end, checkpoint
        "operator_decision",  # explicit operator instructions
    }
)

# Which event_types are allowed for each trigger class
TRIGGER_CLASS_EVENT_TYPES: dict[str, frozenset[str]] = {
    "task_lifecycle": frozenset(
        {
            "task_completed",
            "task_failed",
        }
    ),
    "plan_lifecycle": frozenset(
        {
            "plan_created",
            "plan_revised",
        }
    ),
    "error_failure": frozenset(
        {
            "task_failed",
            "bug_fixed",
        }
    ),
    "session_boundary": frozenset(
        {
            "session_end",
            "heartbeat_cycle",
        }
    ),
    "operator_decision": frozenset(
        {
            "decision_made",
            "user_preference",
        }
    ),
}


# ---------------------------------------------------------------------------
# Trigger Policy — eligibility, suppression, cooldown
# ---------------------------------------------------------------------------


@dataclass
class TriggerPolicy:
    """Configuration for trigger behavior."""

    # Minimum seconds between identical triggers (content-hash based)
    dedupe_window_s: int = 300  # 5 minutes

    # Minimum seconds between triggers of the same class + source
    cooldown_window_s: int = 60  # 1 minute

    # Maximum triggers per class within rolling window
    rate_limit_window_s: int = 3600  # 1 hour
    rate_limit_max: int = 20  # max 20 per class per hour

    # Title minimum length to be considered meaningful
    min_title_length: int = 5

    # Summary minimum length
    min_summary_length: int = 10


DEFAULT_POLICY = TriggerPolicy()


# ---------------------------------------------------------------------------
# Trigger Result
# ---------------------------------------------------------------------------


@dataclass
class TriggerResult:
    """Outcome of a trigger fire attempt."""

    fired: bool = False
    stored: bool = False
    suppressed: bool = False
    suppression_reason: str = ""
    store_used: str = ""
    path_or_id: str = ""
    rejection_reason: str = ""
    trigger_class: str = ""
    event_type: str = ""
    assigned_layer: str = ""
    trace_id: str = ""
    memory_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Dedupe Tracker — in-memory recent-trigger suppression
# ---------------------------------------------------------------------------


class DedupeTracker:
    """Tracks recent triggers to prevent duplicate/noisy memory creation.

    Uses two mechanisms:
    1. Content hash dedupe: identical title+summary+event_type within window
    2. Class+source cooldown: same trigger_class+source within cooldown

    Both are in-memory only — resets on process restart, which is acceptable
    since the watcher is a long-running daemon.
    """

    def __init__(self, policy: TriggerPolicy | None = None):
        self._policy = policy or DEFAULT_POLICY
        # content_hash → last_fired_timestamp
        self._content_hashes: dict[str, float] = {}
        # (trigger_class, source) → last_fired_timestamp
        self._class_source_times: dict[tuple[str, str], float] = {}
        # trigger_class → list of timestamps in current rate window
        self._class_counts: dict[str, list[float]] = {}

    def check_and_record(
        self,
        trigger_class: str,
        source: str,
        event_type: str,
        title: str,
        summary: str,
    ) -> tuple[bool, str]:
        """Check if trigger should fire. Returns (allowed, reason).

        If allowed, also records the trigger for future dedup checks.
        """
        now = time.monotonic()

        # 1. Content hash dedupe
        content_hash = self._compute_hash(event_type, title, summary)
        last_seen = self._content_hashes.get(content_hash)
        if last_seen and (now - last_seen) < self._policy.dedupe_window_s:
            elapsed = int(now - last_seen)
            return False, (
                f"content_dedupe: identical event seen {elapsed}s ago (window={self._policy.dedupe_window_s}s)"
            )

        # 2. Class+source cooldown
        key = (trigger_class, source)
        last_class = self._class_source_times.get(key)
        if last_class and (now - last_class) < self._policy.cooldown_window_s:
            elapsed = int(now - last_class)
            return False, (
                f"cooldown: {trigger_class}+{source} fired {elapsed}s ago (cooldown={self._policy.cooldown_window_s}s)"
            )

        # 3. Rate limit per class
        timestamps = self._class_counts.get(trigger_class, [])
        # Prune expired entries
        cutoff = now - self._policy.rate_limit_window_s
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self._policy.rate_limit_max:
            return False, (
                f"rate_limit: {trigger_class} has {len(timestamps)} triggers "
                f"in last {self._policy.rate_limit_window_s}s "
                f"(max={self._policy.rate_limit_max})"
            )

        # All checks passed — record this trigger
        self._content_hashes[content_hash] = now
        self._class_source_times[key] = now
        timestamps.append(now)
        self._class_counts[trigger_class] = timestamps

        # Periodic cleanup of stale entries
        self._cleanup_stale(now)

        return True, "allowed"

    def reset(self) -> None:
        """Clear all tracking state. Useful for testing."""
        self._content_hashes.clear()
        self._class_source_times.clear()
        self._class_counts.clear()

    @staticmethod
    def _compute_hash(event_type: str, title: str, summary: str) -> str:
        """Compute a content fingerprint for dedupe."""
        content = f"{event_type}|{title.strip().lower()}|{summary.strip().lower()[:200]}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _cleanup_stale(self, now: float) -> None:
        """Remove entries older than the largest window. Runs periodically."""
        max_window = max(
            self._policy.dedupe_window_s,
            self._policy.rate_limit_window_s,
        )
        cutoff = now - max_window

        # Prune content hashes
        stale_hashes = [k for k, v in self._content_hashes.items() if v < cutoff]
        for k in stale_hashes:
            del self._content_hashes[k]

        # Prune class+source times
        stale_cs_keys = [k for k, v in self._class_source_times.items() if v < cutoff]
        for cs_key in stale_cs_keys:
            del self._class_source_times[cs_key]


# ---------------------------------------------------------------------------
# Trigger Engine — the main pipeline
# ---------------------------------------------------------------------------


class TriggerEngine:
    """Automatic memory trigger pipeline.

    Evaluates trigger eligibility, deduplicates, builds memory candidates,
    and routes them through the Unified Memory Router.

    Pipeline:
        fire() → validate → dedupe → build candidate → router.ingest → router.store → trace
    """

    def __init__(
        self,
        policy: TriggerPolicy | None = None,
        dedupe: DedupeTracker | None = None,
        router: Any | None = None,
    ):
        self._policy = policy or DEFAULT_POLICY
        self._dedupe = dedupe or DedupeTracker(self._policy)
        # Lazy import to avoid circular dependency
        self._router = router

    @property
    def router(self):
        if self._router is None:
            from agents.memory_router import router

            self._router = router
        return self._router

    def fire(
        self,
        *,
        trigger_class: str,
        event_type: str,
        source: str,
        title: str,
        summary: str,
        caller: str = "",
        ctx: TraceContext | None = None,
        # Optional enrichment fields
        task_stem: str = "",
        task_class: str = "",
        confidence: str = "medium",
        related_files: list[str] | None = None,
        related_task_ids: list[str] | None = None,
        tags: list[str] | None = None,
        target_store: str | None = None,
        content: str | None = None,
        provenance: str = "automatic",
        extra: dict[str, Any] | None = None,
    ) -> TriggerResult:
        """Fire a memory trigger. The main entry point.

        Pipeline:
        1. Validate trigger class and event type
        2. Check content quality (min lengths)
        3. Dedupe / cooldown / rate limit
        4. Build event dict
        5. Route through router.ingest_event() → router.store()
        6. Emit structured trace

        Returns TriggerResult with full outcome details.
        """
        start = time.monotonic()
        ctx = ctx or TraceContext.new("memory_trigger")
        trace_id = ctx.trace_id

        # --- Step 1: Validate trigger class ---
        if trigger_class not in VALID_TRIGGER_CLASSES:
            return self._reject(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                f"invalid_trigger_class: {trigger_class!r}",
            )

        # Validate event_type is appropriate for this trigger class
        allowed_events = TRIGGER_CLASS_EVENT_TYPES.get(trigger_class, frozenset())
        if event_type not in allowed_events:
            return self._reject(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                f"event_type_mismatch: {event_type!r} not in {trigger_class} allowed types {sorted(allowed_events)}",
            )

        # --- Step 2: Content quality gate ---
        if len(title.strip()) < self._policy.min_title_length:
            return self._reject(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                f"title_too_short: {len(title.strip())} < {self._policy.min_title_length}",
            )
        if len(summary.strip()) < self._policy.min_summary_length:
            return self._reject(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                f"summary_too_short: {len(summary.strip())} < {self._policy.min_summary_length}",
            )

        # --- Step 3: Dedupe / cooldown / rate limit ---
        allowed, dedupe_reason = self._dedupe.check_and_record(
            trigger_class,
            source,
            event_type,
            title,
            summary,
        )
        if not allowed:
            return self._suppress(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                dedupe_reason,
            )

        # --- Step 4: Build event dict for router ingestion ---
        event_dict: dict[str, Any] = {
            "source": source,
            "event_type": event_type,
            "title": title[:100],
            "summary": summary,
            "confidence": confidence,
            "provenance": provenance,
            "tags": tags or [],
            "related_files": related_files or [],
            "related_task_ids": related_task_ids or [],
        }
        if task_stem:
            event_dict["related_task_ids"] = event_dict.get("related_task_ids", []) + [task_stem]
        if target_store:
            event_dict["target_store"] = target_store
        if content:
            event_dict["content"] = content
        if extra:
            # Merge extra fields that don't conflict with required ones
            for k, v in extra.items():
                if k not in event_dict:
                    event_dict[k] = v

        # --- Step 5: Route through memory router ---
        try:
            obj = self.router.ingest_event(
                event_dict,
                caller=f"trigger:{caller}" if caller else f"trigger:{trigger_class}",
                ctx=ctx,
            )
        except ValueError as exc:
            return self._reject(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                f"ingest_failed: {exc}",
            )

        try:
            store_result = self.router.store(
                obj,
                caller=f"trigger:{caller}" if caller else f"trigger:{trigger_class}",
                ctx=ctx,
            )
        except Exception as exc:
            logger.warning(
                "Trigger store failed (non-fatal): trigger_class=%s event_type=%s error=%s",
                trigger_class,
                event_type,
                exc,
            )
            return self._reject(
                trigger_class,
                event_type,
                source,
                title,
                trace_id,
                ctx,
                caller,
                f"store_error: {exc}",
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        result = TriggerResult(
            fired=True,
            stored=store_result.stored,
            store_used=store_result.store_used,
            path_or_id=store_result.path_or_id,
            rejection_reason=store_result.rejection_reason or "",
            trigger_class=trigger_class,
            event_type=event_type,
            assigned_layer=obj.current_layer,
            trace_id=trace_id,
            memory_id=obj.memory_id,
        )

        # --- Step 6: Structured trace ---
        slog.event(
            "memory.trigger.fired",
            ctx,
            operation="trigger_fire",
            trigger_class=trigger_class,
            memory_event_type=event_type,
            source=source,
            caller=caller,
            title=title[:100],
            assigned_layer=obj.current_layer,
            store_used=store_result.store_used,
            stored=store_result.stored,
            memory_id=obj.memory_id,
            path_or_id=store_result.path_or_id,
            rejection_reason=store_result.rejection_reason,
            latency_ms=elapsed_ms,
        )

        return result

    def _reject(
        self,
        trigger_class: str,
        event_type: str,
        source: str,
        title: str,
        trace_id: str,
        ctx: TraceContext,
        caller: str,
        reason: str,
    ) -> TriggerResult:
        """Return a rejection result and log it."""
        slog.event(
            "memory.trigger.rejected",
            ctx,
            level="warn",
            operation="trigger_reject",
            trigger_class=trigger_class,
            memory_event_type=event_type,
            source=source,
            caller=caller,
            title=title[:100],
            rejection_reason=reason,
        )
        return TriggerResult(
            fired=False,
            trigger_class=trigger_class,
            event_type=event_type,
            rejection_reason=reason,
            trace_id=trace_id,
        )

    def _suppress(
        self,
        trigger_class: str,
        event_type: str,
        source: str,
        title: str,
        trace_id: str,
        ctx: TraceContext,
        caller: str,
        reason: str,
    ) -> TriggerResult:
        """Return a suppression result and log it."""
        slog.event(
            "memory.trigger.suppressed",
            ctx,
            operation="trigger_suppress",
            trigger_class=trigger_class,
            memory_event_type=event_type,
            source=source,
            caller=caller,
            title=title[:100],
            suppression_reason=reason,
        )
        return TriggerResult(
            fired=False,
            suppressed=True,
            suppression_reason=reason,
            trigger_class=trigger_class,
            event_type=event_type,
            trace_id=trace_id,
        )


# ---------------------------------------------------------------------------
# Singleton trigger engine
# ---------------------------------------------------------------------------

trigger_engine = TriggerEngine()
