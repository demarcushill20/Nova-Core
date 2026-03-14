"""Phase 6 — Memory Consolidation & Reflection.

Deterministic, evidence-based consolidation of accumulated memory into
higher-value artifacts. Compresses working/episodic windows into summaries,
detects repeated patterns, and generates promotion candidates.

This module does NOT:
- Auto-promote without explicit policy checks
- Create memories from weak evidence
- Bypass layer/store guardrails
- Perform freeform self-reflection

Usage:
    from agents.memory_consolidator import (
        consolidate_window, summarize_session, extract_pattern_candidates,
        ConsolidationResult, WindowSpec,
    )

    result = consolidate_window(WindowSpec(
        window_type="session",
        source_items=working_memory_entries,
    ))
    if result.action == "summary_created":
        # result.output_artifact is a CanonicalMemoryObject ready to store
        router.store(result.output_artifact, caller="consolidator")
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_CONSOLIDATION_ACTIONS = frozenset(
    {
        "no_action",  # Nothing to consolidate
        "summary_created",  # Summary artifact produced
        "checkpoint_created",  # Checkpoint artifact produced
        "promotion_candidate_created",  # Promotion candidate emitted
        "rejected_insufficient_evidence",  # Not enough evidence to consolidate
    }
)

VALID_WINDOW_TYPES = frozenset(
    {
        "session",  # Session task batch
        "working_memory",  # Recent working-memory entries
        "task_batch",  # Group of related task completions
        "failure_window",  # Recent failures/retries
        "heartbeat",  # Heartbeat operational window
        "plan_execution",  # Plan step outcomes
    }
)

# Minimum items needed for each window type to produce output
_MIN_WINDOW_SIZE: dict[str, int] = {
    "session": 1,
    "working_memory": 2,
    "task_batch": 2,
    "failure_window": 2,
    "heartbeat": 3,
    "plan_execution": 1,
}

# Minimum items for pattern extraction
_MIN_PATTERN_EVIDENCE = 3

# Maximum summary length
_MAX_SUMMARY_LEN = 500

# Maximum items to process in a single window
_MAX_WINDOW_ITEMS = 50


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class WindowSpec:
    """Specification for a consolidation window."""

    window_type: str  # One of VALID_WINDOW_TYPES
    source_items: list[dict]  # Raw input items from adapters
    time_start: float = 0.0  # Window start (epoch)
    time_end: float = 0.0  # Window end (epoch)
    session_id: str = ""  # Session ID if applicable
    project: str = "nova-core"
    caller: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsolidationResult:
    """Result of a consolidation operation."""

    action: str  # One of VALID_CONSOLIDATION_ACTIONS
    reason: str  # Human-readable justification
    input_count: int  # Number of source items processed
    dedupe_removed: int  # Items removed by deduplication
    output_artifact: dict | None = None  # Summary/checkpoint/candidate dict
    output_layer: str = ""  # Target layer for the artifact
    confidence: str = "medium"  # Confidence in the output
    promotion_eligible: bool = False  # Whether output is promotion-worthy
    suppression_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PatternCandidate:
    """A candidate pattern extracted from repeated evidence."""

    pattern_description: str
    evidence_count: int
    source_event_types: list[str]
    confidence: str
    promotion_eligible: bool
    blocker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Deduplication / anti-amplification
# ---------------------------------------------------------------------------


def _content_hash(item: dict) -> str:
    """Compute a stable content hash for dedup."""
    key_parts = [
        item.get("event_type", ""),
        item.get("title", ""),
        item.get("summary", "")[:100],
        item.get("source", ""),
    ]
    raw = "|".join(key_parts).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def dedupe_window_items(items: list[dict]) -> tuple[list[dict], int]:
    """Remove duplicate items from a consolidation window.

    Deduplicates by content hash (event_type + title + summary prefix + source).
    Returns (deduped_items, removed_count).
    """
    seen: dict[str, dict] = {}
    for item in items:
        h = _content_hash(item)
        if h not in seen:
            seen[h] = item
        # else: duplicate, skip (keep first occurrence)
    removed = len(items) - len(seen)
    return list(seen.values()), removed


def check_anti_amplification(
    items: list[dict],
    window_type: str,
) -> list[str]:
    """Check for anti-amplification violations.

    Returns list of suppression reasons. Empty = safe to proceed.
    """
    reasons: list[str] = []

    # Rule 1: Heartbeat noise — don't consolidate if >80% heartbeat
    if window_type != "heartbeat":
        hb_count = sum(1 for i in items if i.get("event_type") == "heartbeat_cycle")
        if len(items) > 0 and hb_count / len(items) > 0.8:
            reasons.append(f"heartbeat_noise: {hb_count}/{len(items)} items are heartbeat cycles")

    # Rule 2: All low confidence — don't create higher-layer artifact
    conf_counts = Counter(i.get("confidence", "medium") for i in items)
    if conf_counts.get("low", 0) == len(items) and len(items) > 0:
        reasons.append("all_low_confidence: every item has low confidence")

    # Rule 3: Uniform event type with no substance
    if len(items) >= 3:
        types = set(i.get("event_type", "") for i in items)
        if len(types) == 1:
            avg_summary_len = sum(len(i.get("summary", "")) for i in items) / len(items)
            if avg_summary_len < 20:
                reasons.append(
                    f"uniform_thin: all {len(items)} items are {types.pop()} with avg "
                    f"summary {avg_summary_len:.0f} chars"
                )

    return reasons


# ---------------------------------------------------------------------------
# Core consolidation
# ---------------------------------------------------------------------------


def consolidate_window(spec: WindowSpec) -> ConsolidationResult:
    """Consolidate a window of memory items into a higher-value artifact.

    Deterministic pipeline:
    1. Validate window type and minimum size
    2. Cap and deduplicate items
    3. Check anti-amplification rules
    4. Generate summary artifact based on window type
    5. Assess promotion eligibility

    Args:
        spec: WindowSpec describing the input window.

    Returns:
        ConsolidationResult with action, output, and reasoning.
    """
    if spec.window_type not in VALID_WINDOW_TYPES:
        return ConsolidationResult(
            action="no_action",
            reason=f"invalid_window_type: {spec.window_type}",
            input_count=len(spec.source_items),
            dedupe_removed=0,
        )

    items = spec.source_items[:_MAX_WINDOW_ITEMS]
    min_size = _MIN_WINDOW_SIZE.get(spec.window_type, 1)

    if len(items) < min_size:
        return ConsolidationResult(
            action="rejected_insufficient_evidence",
            reason=f"window_too_small: {len(items)} items < {min_size} minimum for {spec.window_type}",
            input_count=len(items),
            dedupe_removed=0,
        )

    # Dedup
    deduped, removed = dedupe_window_items(items)

    if len(deduped) < min_size:
        return ConsolidationResult(
            action="rejected_insufficient_evidence",
            reason=f"after_dedupe: {len(deduped)} unique items < {min_size} minimum",
            input_count=len(items),
            dedupe_removed=removed,
        )

    # Anti-amplification check
    suppression = check_anti_amplification(deduped, spec.window_type)
    if suppression:
        return ConsolidationResult(
            action="rejected_insufficient_evidence",
            reason=f"anti_amplification: {'; '.join(suppression)}",
            input_count=len(items),
            dedupe_removed=removed,
            suppression_reasons=suppression,
        )

    # Route to window-specific handler
    handler = _WINDOW_HANDLERS.get(spec.window_type, _handle_generic_window)
    return handler(deduped, removed, spec)


# ---------------------------------------------------------------------------
# Window-specific handlers
# ---------------------------------------------------------------------------


def _handle_session_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Consolidate a session task batch into a session summary."""
    completed = [i for i in items if i.get("status") == "completed" or i.get("event_type") == "task_completed"]
    failed = [i for i in items if i.get("status") == "failed" or i.get("event_type") == "task_failed"]

    # Build summary from task data
    task_summaries = []
    for item in items:
        stem = item.get("stem", item.get("title", "unknown"))
        summary = item.get("summary", "")[:100]
        status = item.get("status", item.get("event_type", "unknown"))
        if summary:
            task_summaries.append(f"[{status}] {stem}: {summary}")
        else:
            task_summaries.append(f"[{status}] {stem}")

    full_summary = (
        f"Session {spec.session_id or 'unknown'}: "
        f"{len(completed)} completed, {len(failed)} failed, "
        f"{len(items)} total tasks. " + "; ".join(task_summaries[:5])
    )[:_MAX_SUMMARY_LEN]

    # Confidence based on completion rate
    if len(items) > 0 and len(completed) / len(items) >= 0.8:
        confidence = "high"
    elif len(failed) > len(completed):
        confidence = "low"
    else:
        confidence = "medium"

    artifact = _build_summary_artifact(
        event_type="session_end",
        title=f"session_summary: {spec.session_id or 'batch'}"[:100],
        summary=full_summary,
        source="consolidator",
        layer="episodic",
        confidence=confidence,
        tags=[f"#session/{spec.session_id}"] if spec.session_id else [],
        item_count=len(items),
        project=spec.project,
    )

    return ConsolidationResult(
        action="summary_created",
        reason=f"session_summary: {len(items)} tasks consolidated",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
        output_artifact=artifact,
        output_layer="episodic",
        confidence=confidence,
    )


def _handle_working_memory_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Consolidate recent working-memory entries into a checkpoint."""
    event_types = Counter(i.get("event_type", "unknown") for i in items)

    # Build checkpoint summary
    type_breakdown = ", ".join(f"{et}={c}" for et, c in event_types.most_common(5))
    recent_titles = [i.get("title", "")[:60] for i in items[:5]]

    summary = (
        f"Working memory checkpoint: {len(items)} events "
        f"({type_breakdown}). Recent: {'; '.join(t for t in recent_titles if t)}"
    )[:_MAX_SUMMARY_LEN]

    artifact = _build_summary_artifact(
        event_type="heartbeat_cycle",
        title="working_memory_checkpoint"[:100],
        summary=summary,
        source="consolidator",
        layer="working",
        confidence="medium",
        tags=["#consolidation/checkpoint"],
        item_count=len(items),
        project=spec.project,
    )

    return ConsolidationResult(
        action="checkpoint_created",
        reason=f"working_checkpoint: {len(items)} events consolidated",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
        output_artifact=artifact,
        output_layer="working",
        confidence="medium",
    )


def _handle_task_batch_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Consolidate a batch of related task completions."""
    completed = [
        i
        for i in items
        if i.get("event_type") in ("task_completed", "code_changed", "bug_fixed") or i.get("status") == "completed"
    ]

    if not completed:
        return ConsolidationResult(
            action="rejected_insufficient_evidence",
            reason="no_completed_tasks: batch contains no completed items",
            input_count=len(items) + dedupe_removed,
            dedupe_removed=dedupe_removed,
        )

    summaries = [i.get("summary", "")[:80] for i in completed[:5]]
    summary = (f"Task batch: {len(completed)} tasks completed. " + "; ".join(s for s in summaries if s))[
        :_MAX_SUMMARY_LEN
    ]

    # High confidence if all completed, medium otherwise
    confidence = "high" if len(completed) == len(items) else "medium"

    # Promotion-eligible if 3+ high-confidence completions
    high_conf = sum(1 for i in completed if i.get("confidence") == "high")
    promotable = high_conf >= 3

    artifact = _build_summary_artifact(
        event_type="task_completed",
        title=f"task_batch_summary: {len(completed)} tasks"[:100],
        summary=summary,
        source="consolidator",
        layer="episodic",
        confidence=confidence,
        tags=["#consolidation/task_batch"],
        item_count=len(items),
        project=spec.project,
    )

    return ConsolidationResult(
        action="summary_created",
        reason=f"task_batch: {len(completed)} completed tasks consolidated",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
        output_artifact=artifact,
        output_layer="episodic",
        confidence=confidence,
        promotion_eligible=promotable,
    )


def _handle_failure_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Consolidate repeated failures into an incident summary."""
    failures = [i for i in items if i.get("event_type") in ("task_failed",) or i.get("status") == "failed"]

    if len(failures) < 2:
        return ConsolidationResult(
            action="rejected_insufficient_evidence",
            reason=f"too_few_failures: {len(failures)} < 2 minimum",
            input_count=len(items) + dedupe_removed,
            dedupe_removed=dedupe_removed,
        )

    # Extract failure pattern
    error_summaries = [f.get("summary", "")[:80] for f in failures[:5]]
    summary = (f"Failure pattern: {len(failures)} failures detected. " + "; ".join(s for s in error_summaries if s))[
        :_MAX_SUMMARY_LEN
    ]

    artifact = _build_summary_artifact(
        event_type="task_failed",
        title=f"failure_incident: {len(failures)} failures"[:100],
        summary=summary,
        source="consolidator",
        layer="episodic",
        confidence="medium",
        tags=["#consolidation/failure_incident"],
        item_count=len(items),
        project=spec.project,
    )

    return ConsolidationResult(
        action="summary_created",
        reason=f"failure_incident: {len(failures)} failures consolidated",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
        output_artifact=artifact,
        output_layer="episodic",
        confidence="medium",
    )


def _handle_heartbeat_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Consolidate heartbeat events into an operational summary."""
    healthy = sum(1 for i in items if "HEALTHY" in (i.get("title", "") + i.get("summary", "")))
    unhealthy = sum(1 for i in items if "UNHEALTHY" in (i.get("title", "") + i.get("summary", "")))

    status = "HEALTHY" if unhealthy == 0 else f"DEGRADED ({unhealthy} unhealthy)"

    summary = (f"Heartbeat window: {len(items)} cycles, {healthy} healthy, {unhealthy} unhealthy. Status: {status}.")[
        :_MAX_SUMMARY_LEN
    ]

    artifact = _build_summary_artifact(
        event_type="heartbeat_cycle",
        title=f"heartbeat_summary: {status}"[:100],
        summary=summary,
        source="consolidator",
        layer="working",
        confidence="high" if unhealthy == 0 else "medium",
        tags=["#consolidation/heartbeat"],
        item_count=len(items),
        project=spec.project,
    )

    return ConsolidationResult(
        action="checkpoint_created",
        reason=f"heartbeat_summary: {len(items)} cycles consolidated",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
        output_artifact=artifact,
        output_layer="working",
        confidence="high" if unhealthy == 0 else "medium",
    )


def _handle_plan_execution_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Consolidate plan execution outcomes into a plan summary."""
    summaries = [i.get("summary", "")[:80] for i in items[:5]]
    plan_id = ""
    for item in items:
        pid = item.get("plan_id", "")
        if pid:
            plan_id = pid
            break

    summary = (
        "Plan execution summary"
        + (f" ({plan_id})" if plan_id else "")
        + f": {len(items)} steps. "
        + "; ".join(s for s in summaries if s)
    )[:_MAX_SUMMARY_LEN]

    confidence = (
        "high"
        if all(
            i.get("event_type") in ("task_completed", "plan_created") or i.get("status") == "completed" for i in items
        )
        else "medium"
    )

    artifact = _build_summary_artifact(
        event_type="plan_created",
        title=f"plan_summary: {plan_id or 'unknown'}"[:100],
        summary=summary,
        source="consolidator",
        layer="episodic",
        confidence=confidence,
        tags=["#consolidation/plan"],
        item_count=len(items),
        project=spec.project,
    )

    return ConsolidationResult(
        action="summary_created",
        reason=f"plan_summary: {len(items)} steps consolidated",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
        output_artifact=artifact,
        output_layer="episodic",
        confidence=confidence,
        promotion_eligible=confidence == "high" and len(items) >= 3,
    )


def _handle_generic_window(
    items: list[dict],
    dedupe_removed: int,
    spec: WindowSpec,
) -> ConsolidationResult:
    """Fallback handler for unknown window types."""
    return ConsolidationResult(
        action="no_action",
        reason=f"no_handler: window type {spec.window_type} has no specific handler",
        input_count=len(items) + dedupe_removed,
        dedupe_removed=dedupe_removed,
    )


# Window type → handler mapping
_WINDOW_HANDLERS = {
    "session": _handle_session_window,
    "working_memory": _handle_working_memory_window,
    "task_batch": _handle_task_batch_window,
    "failure_window": _handle_failure_window,
    "heartbeat": _handle_heartbeat_window,
    "plan_execution": _handle_plan_execution_window,
}


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------


def extract_pattern_candidates(
    items: list[dict],
    *,
    min_evidence: int = _MIN_PATTERN_EVIDENCE,
) -> list[PatternCandidate]:
    """Extract promotion-worthy pattern candidates from repeated evidence.

    Looks for repeated successful outcomes with similar characteristics.
    Conservative: requires min_evidence occurrences of the same pattern.

    Args:
        items: Memory items to analyze (typically episodic artifacts).
        min_evidence: Minimum occurrences to consider a pattern.

    Returns:
        List of PatternCandidate objects. May be empty.
    """
    if len(items) < min_evidence:
        return []

    # Group by event_type + task_class (or similar grouping key)
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = f"{item.get('event_type', 'unknown')}:{item.get('task_class', 'unknown')}"
        groups.setdefault(key, []).append(item)

    candidates: list[PatternCandidate] = []
    for key, group in groups.items():
        if len(group) < min_evidence:
            continue

        # Check quality: at least some high/medium confidence
        high_conf = sum(1 for i in group if i.get("confidence") == "high")
        med_conf = sum(1 for i in group if i.get("confidence") == "medium")
        if high_conf + med_conf < min_evidence:
            continue

        # Extract common elements
        event_types = list(set(i.get("event_type", "") for i in group))
        summaries = [i.get("summary", "")[:80] for i in group[:3]]

        description = (f"Repeated {key}: {len(group)} occurrences. " + "; ".join(s for s in summaries if s))[
            :_MAX_SUMMARY_LEN
        ]

        # Promotion-eligible only if majority high confidence
        promotable = high_conf >= min_evidence
        blocker = None if promotable else f"insufficient high-confidence evidence ({high_conf}/{min_evidence})"

        candidates.append(
            PatternCandidate(
                pattern_description=description,
                evidence_count=len(group),
                source_event_types=event_types,
                confidence="high" if promotable else "medium",
                promotion_eligible=promotable,
                blocker=blocker,
            )
        )

    return candidates


# ---------------------------------------------------------------------------
# Session summarization
# ---------------------------------------------------------------------------


def summarize_session(
    session_data: dict,
    working_memory: list[dict] | None = None,
) -> ConsolidationResult:
    """Summarize a session into an episodic summary artifact.

    Combines session task records with optional working memory entries
    to produce a structured session summary.

    Args:
        session_data: Session dict with session_id, tasks, etc.
        working_memory: Optional working-memory entries from the session period.

    Returns:
        ConsolidationResult with summary artifact.
    """
    tasks = session_data.get("tasks", [])

    if not tasks:
        return ConsolidationResult(
            action="rejected_insufficient_evidence",
            reason="empty_session: no tasks recorded",
            input_count=0,
            dedupe_removed=0,
        )

    # Combine task records and working memory
    all_items = list(tasks)
    if working_memory:
        all_items.extend(working_memory[:20])

    spec = WindowSpec(
        window_type="session",
        source_items=all_items,
        session_id=session_data.get("session_id", ""),
        project=session_data.get("project", "nova-core"),
    )

    return consolidate_window(spec)


# ---------------------------------------------------------------------------
# Artifact builder helper
# ---------------------------------------------------------------------------


def _build_summary_artifact(
    *,
    event_type: str,
    title: str,
    summary: str,
    source: str,
    layer: str,
    confidence: str,
    tags: list[str],
    item_count: int,
    project: str,
) -> dict:
    """Build a summary artifact dict suitable for router.ingest_event()."""
    return {
        "source": source,
        "event_type": event_type,
        "provenance": "consolidation",
        "title": title[:100],
        "summary": summary[:_MAX_SUMMARY_LEN],
        "confidence": confidence,
        "current_layer": layer,
        "memory_layer_candidate": layer,
        "tags": tags,
        "related_project": project,
        "consolidation_metadata": {
            "consolidated_item_count": item_count,
            "consolidation_type": "summary",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
