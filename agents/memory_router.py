"""Phase 1+2+4+5+6+7 — Unified Memory Router with Open-Loop Tracking.

Single internal gateway for all memory operations. Routes reads, writes,
and promotions through a central abstraction with structured tracing.

Phase 5 additions:
- Intent-aware recall classification
- Routing policy by intent (temporal, decision, procedural, etc.)
- Multi-factor ranking (recency, confidence, source authority, promotion level)
- Blended retrieval with deduplication
- Retrieval explanation metadata on every result
- Backward compatibility: legacy intents auto-mapped to Phase 5 equivalents

Usage:
    from agents.memory_router import router

    # Recall with Phase 5 intent
    result = router.recall("how do we validate vault schemas?",
                           intent="procedural_recall",
                           task_class="code_impl", keywords=["schema", "vault"])

    # Legacy intent still works (auto-mapped)
    result = router.recall("vault schema validation", intent="pattern_retrieval",
                           task_class="code_impl", keywords=["schema", "vault"])

    # Store a memory with explicit layer
    obj = router.ingest_event({
        "source": "watcher",
        "event_type": "task_completed",
        "title": "Fixed vault schema",
        "summary": "Added ADR type to canonical enum",
        ...
    })
    # obj.current_layer is auto-assigned from event_type
    result = router.store(obj)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from utils.structured_log import slog
from utils.trace_context import TraceContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical Memory Object (Phase 0 spec → Phase 1 implementation)
# ---------------------------------------------------------------------------

# Valid enum values
VALID_SOURCES = frozenset(
    {
        "watcher",
        "heartbeat",
        "telegram",
        "orchestrator",
        "promoter",
        "operator",
        "daily_summary",
    }
)
VALID_EVENT_TYPES = frozenset(
    {
        "task_completed",
        "task_failed",
        "research_completed",
        "plan_created",
        "plan_revised",
        "code_changed",
        "pattern_detected",
        "decision_made",
        "bug_fixed",
        "user_preference",
        "heartbeat_cycle",
        "session_end",
        "conversation_insight",
        "workflow_learning_promoted",
        "agent_pattern_promoted",
    }
)
VALID_PROVENANCES = frozenset(
    {
        "automatic",
        "prompt_delegated",
        "operator_requested",
        "promotion",
        "consolidation",
    }
)
VALID_LAYERS = frozenset({"working", "episodic", "semantic", "procedural"})
VALID_TARGET_STORES = frozenset(
    {
        "fusion_memory",
        "obsidian_vault",
        "memory_file",
        "state_working",
        "discard",
    }
)
VALID_CONFIDENCES = frozenset({"high", "medium", "low"})
VALID_PROMOTION_STATUSES = frozenset(
    {
        "candidate",
        "persisted",
        "promoted",
        "rejected",
        "superseded",
        "archived",
    }
)
VALID_PROMOTION_ELIGIBILITY = frozenset(
    {
        "eligible",
        "ineligible",
        "needs_review",
        "already_promoted",
    }
)


# ---------------------------------------------------------------------------
# Phase 2: Layer Policy Definitions
# ---------------------------------------------------------------------------

# Event type → default layer assignment
EVENT_TYPE_TO_LAYER: dict[str, str] = {
    # working: transient session/cycle events
    "session_end": "working",
    "heartbeat_cycle": "working",
    # episodic: records of specific completed events
    "task_completed": "episodic",
    "task_failed": "episodic",
    "research_completed": "episodic",
    "plan_created": "episodic",
    "plan_revised": "episodic",
    "code_changed": "episodic",
    "decision_made": "episodic",
    "bug_fixed": "episodic",
    "conversation_insight": "episodic",
    "pattern_detected": "episodic",
    # semantic: verified/consolidated knowledge
    "workflow_learning_promoted": "semantic",
    "user_preference": "semantic",
    # procedural: proven reusable methods
    "agent_pattern_promoted": "procedural",
}

# Which stores are allowed for each layer
LAYER_ALLOWED_STORES: dict[str, frozenset[str]] = {
    "working": frozenset({"state_working", "fusion_memory", "discard"}),
    "episodic": frozenset({"memory_file", "fusion_memory", "obsidian_vault", "discard"}),
    "semantic": frozenset({"obsidian_vault", "fusion_memory", "discard"}),
    "procedural": frozenset({"obsidian_vault", "memory_file", "discard"}),
}

# Allowed promotion transitions: from_layer → set of allowed to_layers
ALLOWED_PROMOTIONS: dict[str, frozenset[str]] = {
    "working": frozenset({"episodic"}),
    "episodic": frozenset({"semantic"}),
    "semantic": frozenset({"procedural"}),
    "procedural": frozenset(),  # terminal layer
}


@dataclass
class CanonicalMemoryObject:
    """Normalized internal representation for all memory candidates."""

    # Identity
    memory_id: str
    timestamp: str

    # Source & Provenance
    source: str
    event_type: str
    provenance: str
    source_file: str | None = None
    source_function: str | None = None

    # Content
    title: str = ""
    summary: str = ""
    content: str | None = None
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    # Classification
    memory_layer_candidate: str = "episodic"  # Phase 1 field (retained for compat)
    target_store: str | None = None

    # Phase 2: Layer metadata
    current_layer: str = "episodic"  # Explicit layer assignment
    candidate_next_layer: str | None = None  # Where this could be promoted to
    promotion_eligibility: str = "ineligible"  # eligible, ineligible, needs_review, already_promoted
    layer_reason: str = ""  # Why this layer was assigned
    storage_intent: str = ""  # Caller's stated purpose for storing
    promotion_blocker: str | None = None  # What prevents promotion, if anything

    # Scoring (null until Phase 4)
    importance_score: float | None = None
    novelty_score: float | None = None
    durability_score: float | None = None
    confidence: str = "medium"

    # Relationships
    related_task_ids: list[str] = field(default_factory=list)
    related_files: list[str] = field(default_factory=list)
    related_project: str = "nova-core"
    supersedes: str | None = None

    # Lifecycle
    promotion_status: str = "candidate"
    open_loop_status: str | None = None
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_canonical_object(obj: CanonicalMemoryObject) -> tuple[bool, list[str]]:
    """Validate a CanonicalMemoryObject. Returns (valid, errors). Fail-closed."""
    errors: list[str] = []

    if not obj.memory_id:
        errors.append("missing required field: memory_id")
    if not obj.timestamp:
        errors.append("missing required field: timestamp")
    if not obj.source:
        errors.append("missing required field: source")
    if not obj.event_type:
        errors.append("missing required field: event_type")
    if not obj.provenance:
        errors.append("missing required field: provenance")
    if not obj.title:
        errors.append("missing required field: title")
    if not obj.summary:
        errors.append("missing required field: summary")
    if not obj.confidence:
        errors.append("missing required field: confidence")

    if errors:
        return False, errors

    if obj.source not in VALID_SOURCES:
        errors.append(f"invalid source: {obj.source!r}")
    if obj.confidence not in VALID_CONFIDENCES:
        errors.append(f"invalid confidence: {obj.confidence!r}")
    if obj.memory_layer_candidate not in VALID_LAYERS:
        errors.append(f"invalid memory_layer_candidate: {obj.memory_layer_candidate!r}")
    if obj.target_store is not None and obj.target_store not in VALID_TARGET_STORES:
        errors.append(f"invalid target_store: {obj.target_store!r}")
    if obj.promotion_status not in VALID_PROMOTION_STATUSES:
        errors.append(f"invalid promotion_status: {obj.promotion_status!r}")
    if len(obj.title) > 100:
        errors.append(f"title too long: {len(obj.title)} (max 100)")

    # Phase 2: Layer field validation
    if obj.current_layer not in VALID_LAYERS:
        errors.append(f"invalid current_layer: {obj.current_layer!r}")
    if obj.candidate_next_layer is not None and obj.candidate_next_layer not in VALID_LAYERS:
        errors.append(f"invalid candidate_next_layer: {obj.candidate_next_layer!r}")
    if obj.promotion_eligibility not in VALID_PROMOTION_ELIGIBILITY:
        errors.append(f"invalid promotion_eligibility: {obj.promotion_eligibility!r}")

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Phase 2: Layer Policy Functions
# ---------------------------------------------------------------------------


def check_store_layer_compatibility(current_layer: str, target_store: str) -> tuple[bool, str]:
    """Check if a store is allowed for a given layer. Returns (compatible, reason)."""
    if target_store == "discard":
        return True, "discard always allowed"

    allowed = LAYER_ALLOWED_STORES.get(current_layer)
    if allowed is None:
        return False, f"unknown layer: {current_layer!r}"

    if target_store in allowed:
        return True, f"{target_store} is allowed for {current_layer} layer"

    return False, (f"store {target_store!r} is not allowed for {current_layer!r} layer (allowed: {sorted(allowed)})")


def check_promotion_boundary(from_layer: str, to_layer: str) -> tuple[bool, str]:
    """Check if a promotion transition is allowed. Returns (allowed, reason)."""
    if from_layer not in VALID_LAYERS:
        return False, f"unknown source layer: {from_layer!r}"
    if to_layer not in VALID_LAYERS:
        return False, f"unknown target layer: {to_layer!r}"
    if from_layer == to_layer:
        return False, f"promotion to same layer ({from_layer}) is a no-op"

    allowed = ALLOWED_PROMOTIONS.get(from_layer, frozenset())
    if to_layer in allowed:
        return True, f"promotion {from_layer} → {to_layer} is allowed"

    return False, (
        f"promotion {from_layer} → {to_layer} is not allowed "
        f"(from {from_layer}, allowed targets: {sorted(allowed) or 'none (terminal)'})"
    )


def infer_layer_from_event_type(event_type: str) -> str:
    """Infer the default memory layer from an event type.

    Returns the inferred layer, defaulting to 'episodic' for unknown types.
    """
    return EVENT_TYPE_TO_LAYER.get(event_type, "episodic")


def compute_candidate_next_layer(current_layer: str) -> str | None:
    """Compute the next promotion target for a given layer, or None if terminal."""
    allowed = ALLOWED_PROMOTIONS.get(current_layer, frozenset())
    if not allowed:
        return None
    # Return the single allowed target (each layer has at most one)
    return next(iter(sorted(allowed)))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RecallResult:
    """Result of a memory recall operation."""

    results: list[dict] = field(default_factory=list)
    sources_queried: list[str] = field(default_factory=list)
    total_found: int = 0
    query: str = ""
    intent: str = ""
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StoreResult:
    """Result of a memory store operation."""

    stored: bool = False
    store_used: str = ""
    path_or_id: str = ""
    validation_errors: list[str] = field(default_factory=list)
    rejection_reason: str | None = None
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromoteResult:
    """Result of a memory promotion operation."""

    promoted: bool = False
    source_store: str = ""
    target_store: str = ""
    reason: str = ""
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class MemoryAdapter(Protocol):
    """Interface that all memory backend adapters must satisfy."""

    @property
    def name(self) -> str: ...

    def recall(self, query: str, **kwargs: Any) -> list[dict]: ...

    def store(self, obj: CanonicalMemoryObject) -> StoreResult: ...

    def is_available(self) -> bool: ...


# ---------------------------------------------------------------------------
# MemoryFileAdapter — wraps agents/memory_engine.py
# ---------------------------------------------------------------------------


class MemoryFileAdapter:
    """Adapter for file-based MEMORY/ artifacts (workflow_learnings, agent_patterns)."""

    name = "memory_file"

    def __init__(self, base: Path | None = None):
        from agents.memory_engine import MEMORY_DIR

        self._base = base or MEMORY_DIR

    def recall(self, query: str, **kwargs: Any) -> list[dict]:
        """Retrieve related patterns from MEMORY/ using keyword matching."""
        from agents.memory_engine import retrieve_related_patterns

        task_class = kwargs.get("task_class", "unknown")
        keywords = kwargs.get("keywords", [])
        max_results = kwargs.get("max_results", 5)

        if not keywords and query:
            # Extract keywords from query string
            keywords = [w for w in query.lower().split() if len(w) > 2]

        return retrieve_related_patterns(
            task_class=task_class,
            keywords=keywords,
            max_results=max_results,
            base=self._base,
        )

    def store(self, obj: CanonicalMemoryObject) -> StoreResult:
        """Store a memory object as a MEMORY/ artifact."""
        from agents.memory_engine import MemoryArtifact, write_memory_artifact

        # Map CanonicalMemoryObject → MemoryArtifact
        # Artifact ID must match mem_<workflow_id>_<timestamp> format
        workflow_id = obj.related_task_ids[0] if obj.related_task_ids else "router"
        ts_suffix = obj.memory_id.split("-")[-1] if "-" in obj.memory_id else str(int(time.time()))
        artifact_id = f"mem_{workflow_id}_{ts_suffix}"

        artifact = MemoryArtifact(
            artifact_id=artifact_id,
            workflow_id=workflow_id,
            task_summary=obj.summary[:500],
            task_class=self._map_task_class(obj),
            roles_involved=obj.entities[:10] if obj.entities else ["router"],
            key_decisions=[obj.summary],
            successful_patterns=[obj.title],
            failure_patterns=[],
            verification_outcome="not_verified",
            reusable_guidance=obj.summary[:500],
            confidence=obj.confidence,
            created_at=obj.timestamp,
        )

        try:
            path = write_memory_artifact(artifact, target="workflow_learnings", base=self._base)
            return StoreResult(
                stored=True,
                store_used=self.name,
                path_or_id=str(path),
            )
        except ValueError as exc:
            return StoreResult(
                stored=False,
                store_used=self.name,
                rejection_reason=str(exc),
            )

    def is_available(self) -> bool:
        return self._base.exists()

    @staticmethod
    def _map_task_class(obj: CanonicalMemoryObject) -> str:
        """Map event_type to a valid task_class for MemoryArtifact."""
        from agents.memory_engine import VALID_TASK_CLASSES

        # If tags contain a task class, use it
        for tag in obj.tags:
            clean = tag.lstrip("#").replace("class/", "")
            if clean in VALID_TASK_CLASSES:
                return clean

        # Map common event types
        mapping = {
            "research_completed": "research",
            "code_changed": "code_impl",
            "bug_fixed": "code_impl",
            "task_completed": "unknown",
            "task_failed": "unknown",
        }
        return mapping.get(obj.event_type, "unknown")


# ---------------------------------------------------------------------------
# VaultAdapter — wraps tools/mcp_vault_server.py
# ---------------------------------------------------------------------------


class VaultAdapter:
    """Adapter for Obsidian Vault (read via vault_search/vault_read)."""

    name = "obsidian_vault"

    def recall(self, query: str, **kwargs: Any) -> list[dict]:
        """Search the Obsidian vault for relevant notes."""
        try:
            from tools.mcp_vault_server import vault_search
        except ImportError:
            logger.debug("vault MCP server not available")
            return []

        folder = kwargs.get("folder", "")
        max_results = kwargs.get("max_results", 5)

        try:
            result = vault_search(query=query, folder=folder)
        except Exception as exc:
            logger.debug("vault_search failed: %s", exc)
            return []

        if "error" in result:
            logger.debug("vault_search error: %s", result["error"])
            return []

        hits = result.get("results", [])
        # Normalize to standard format
        normalized: list[dict] = []
        for hit in hits[:max_results]:
            normalized.append(
                {
                    "path": hit.get("path", ""),
                    "title": hit.get("path", "").rsplit("/", 1)[-1].replace(".md", ""),
                    "snippet": hit.get("snippet", "")[:300],
                    "score": hit.get("score", 0),
                    "source": self.name,
                }
            )
        return normalized

    def store(self, obj: CanonicalMemoryObject) -> StoreResult:
        """Store to vault via vault_write. Only for appropriate note types."""
        try:
            from tools.mcp_vault_server import vault_write
        except ImportError:
            return StoreResult(
                stored=False,
                store_used=self.name,
                rejection_reason="vault MCP server not available",
            )

        # Map event_type to vault note type and folder
        type_map = {
            "research_completed": ("research-summary", "40-research"),
            "workflow_learning_promoted": ("workflow-learning", "30-workflow-learnings"),
            "agent_pattern_promoted": ("agent-pattern", "20-agent-patterns"),
        }

        mapping = type_map.get(obj.event_type)
        if not mapping:
            return StoreResult(
                stored=False,
                store_used=self.name,
                rejection_reason=f"no vault mapping for event_type={obj.event_type!r}",
            )

        note_type, folder = mapping
        # Build minimal valid frontmatter
        from schemas.vault_note_schema import VALID_NOTE_TYPES

        required_tag = VALID_NOTE_TYPES.get(note_type, "")

        frontmatter: dict[str, Any] = {
            "type": note_type,
            "title": obj.title[:100],
            "confidence": obj.confidence,
            "source": "nova-core-memory",
            "tags": [required_tag] + [t for t in obj.tags if t != required_tag][:9],
        }

        # Build path
        slug = obj.memory_id.replace("/", "-").replace(" ", "-")[:60]
        path = f"{folder}/{slug}.md"
        body = obj.content or obj.summary

        try:
            result = vault_write(path=path, frontmatter=frontmatter, body=body)
        except Exception as exc:
            return StoreResult(
                stored=False,
                store_used=self.name,
                rejection_reason=str(exc),
            )

        if "error" in result:
            return StoreResult(
                stored=False,
                store_used=self.name,
                rejection_reason=result["error"],
                validation_errors=result.get("schema_errors", []),
            )

        return StoreResult(
            stored=True,
            store_used=self.name,
            path_or_id=result.get("path", path),
        )

    def is_available(self) -> bool:
        try:
            from tools.mcp_vault_server import vault_list

            result = vault_list("")
            return "error" not in result
        except Exception:
            return False


# ---------------------------------------------------------------------------
# FusionMemoryAdapter — skeleton for prompt-delegated MCP backend
# ---------------------------------------------------------------------------


class FusionMemoryAdapter:
    """Adapter for Fusion Memory MCP (Pinecone + Neo4j + Redis).

    Phase 1 skeleton. Fusion Memory writes are currently prompt-delegated
    (embedded in Claude subprocess prompts, not called from Python).
    The adapter cannot make real MCP calls from Python yet.
    """

    name = "fusion_memory"

    def recall(self, query: str, **kwargs: Any) -> list[dict]:
        """Fusion Memory recall is not yet callable from Python."""
        logger.debug("FusionMemoryAdapter.recall: not implemented in Phase 1")
        return []

    def store(self, obj: CanonicalMemoryObject) -> StoreResult:
        """Fusion Memory store is not yet callable from Python."""
        return StoreResult(
            stored=False,
            store_used=self.name,
            rejection_reason="fusion_memory writes are prompt-delegated; not callable from Python in Phase 1",
        )

    def is_available(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# WorkingMemoryAdapter — STATE/working_memory/ for transient events
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path("STATE")
_WORKING_MEMORY_DIR_NAME = "working_memory"
_WORKING_MEMORY_MAX_AGE_S = 7 * 24 * 3600  # 7-day retention
_MAX_WINDOW_ITEMS = 50  # Phase 6: max items to read for consolidation windows


class WorkingMemoryAdapter:
    """Adapter for STATE/working_memory/ — transient working-layer persistence.

    Stores working-layer events (heartbeat cycles, session boundaries) as
    compact JSON files with automatic 7-day retention. Not for durable
    knowledge — this is scratch state.
    """

    name = "state_working"

    def __init__(self, base: Path | None = None):
        self._base = (base or _DEFAULT_STATE_DIR) / _WORKING_MEMORY_DIR_NAME

    def recall(self, query: str, **kwargs: Any) -> list[dict]:
        """Read recent working-memory entries. Simple chronological scan."""
        max_results = kwargs.get("max_results", 10)
        if not self._base.exists():
            return []

        entries: list[dict] = []
        for f in sorted(self._base.glob("wm_*.json"), reverse=True):
            if len(entries) >= max_results:
                break
            try:
                data = json.loads(f.read_text())
                data["_source_file"] = f.name
                data["_source_adapter"] = self.name
                entries.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return entries

    def store(self, obj: CanonicalMemoryObject) -> StoreResult:
        """Persist a working-layer event to STATE/working_memory/."""
        self._base.mkdir(parents=True, exist_ok=True)

        # Build compact record — only essential fields for working layer
        ts_epoch = int(time.time())
        filename = f"wm_{ts_epoch}_{obj.memory_id.replace('/', '_')[-20:]}.json"
        record = {
            "memory_id": obj.memory_id,
            "timestamp": obj.timestamp,
            "source": obj.source,
            "event_type": obj.event_type,
            "title": obj.title,
            "summary": obj.summary[:500],
            "current_layer": obj.current_layer,
            "confidence": obj.confidence,
            "tags": obj.tags[:5],
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        path = self._base / filename
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(record, indent=2))
            tmp.replace(path)
        except OSError as exc:
            return StoreResult(
                stored=False,
                store_used=self.name,
                rejection_reason=f"write failed: {exc}",
            )

        # Best-effort cleanup of stale entries
        self._cleanup_stale()

        return StoreResult(
            stored=True,
            store_used=self.name,
            path_or_id=str(path),
        )

    def is_available(self) -> bool:
        # STATE/ directory should always exist in production
        return self._base.parent.exists()

    def _cleanup_stale(self) -> None:
        """Remove working-memory files older than retention window."""
        if not self._base.exists():
            return
        cutoff = time.time() - _WORKING_MEMORY_MAX_AGE_S
        for f in self._base.glob("wm_*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue


# ---------------------------------------------------------------------------
# Memory Router
# ---------------------------------------------------------------------------


class MemoryRouter:
    """Unified Memory Router — central gateway for all memory operations.

    Phase 1+2+4+5+6+7+8+9: routing, evaluation, intent-aware recall,
    consolidation, open-loop tracking, memory governance, and compaction.

    All memory reads and writes should go through this router. It selects
    the appropriate backend adapter, validates inputs, and emits structured
    trace logs.
    """

    def __init__(
        self,
        adapters: list[MemoryAdapter] | None = None,
        memory_file_base: Path | None = None,
        loop_store_base: Path | None = None,
    ):
        self._loop_store_base = loop_store_base
        self._adapters: dict[str, MemoryAdapter]
        if adapters is not None:
            self._adapters = {a.name: a for a in adapters}
        else:
            self._adapters = {
                "memory_file": MemoryFileAdapter(base=memory_file_base),
                "obsidian_vault": VaultAdapter(),
                "fusion_memory": FusionMemoryAdapter(),
                "state_working": WorkingMemoryAdapter(),
            }

    def get_adapter(self, name: str) -> MemoryAdapter | None:
        return self._adapters.get(name)

    # -------------------------------------------------------------------
    # recall — Phase 1: Fully functional
    # -------------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        intent: str,
        scope: str = "all",
        task_class: str = "",
        keywords: list[str] | None = None,
        max_results: int = 5,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> RecallResult:
        """Retrieve relevant memories from one or more backends.

        Phase 5: Intent-aware recall with classification, routing policy,
        multi-factor ranking, blended retrieval, and explanation metadata.

        Backward compatible: legacy intents (pattern_retrieval, vault_context,
        etc.) are auto-mapped to Phase 5 intents.

        Args:
            query: Search query string.
            intent: Why the recall is happening. Accepts both Phase 5 intents
                    (temporal_recall, decision_recall, etc.) and legacy intents
                    (pattern_retrieval, vault_context, etc.).
            scope: Which adapters to query: "all", "memory_files",
                   "vault", "fusion", "working"
            task_class: Task classification for relevance scoring.
            keywords: Optional keyword list for retrieval.
            max_results: Maximum results to return.
            caller: Caller identifier for tracing.
            ctx: Optional trace context for correlation.

        Returns:
            RecallResult with bounded, ranked results and explanation metadata.
            Fail-open.
        """
        from agents.recall_intent import (
            classify_intent,
            dedupe_results,
            get_routing_plan,
            rank_results,
        )

        start = time.monotonic()
        ctx = ctx or TraceContext.new("memory_router")
        trace_id = ctx.trace_id

        # Phase 5: Classify intent
        classification = classify_intent(query, caller_intent=intent)
        plan = get_routing_plan(
            classification.intent,
            scope_override=scope if scope != "all" else None,
            max_results=max_results,
        )

        # Query adapters per routing plan
        sources_queried: list[str] = []
        all_results: list[dict] = []

        # Primary adapters
        for name in plan.primary_adapters:
            adapter = self._adapters.get(name)
            if not adapter:
                continue
            sources_queried.append(name)
            try:
                hits = adapter.recall(
                    query,
                    task_class=task_class,
                    keywords=keywords or [],
                    max_results=plan.max_results_per_adapter,
                )
                for hit in hits:
                    hit["_source_adapter"] = name
                all_results.extend(hits)
            except Exception as exc:
                logger.warning("Recall from %s failed (non-fatal): %s", name, exc)

        # Secondary adapters (blended retrieval)
        if plan.secondary_adapters and plan.blend_mode != "none":
            need_secondary = plan.blend_mode == "merge" or (plan.blend_mode == "fallback" and len(all_results) == 0)
            if need_secondary:
                for name in plan.secondary_adapters:
                    adapter = self._adapters.get(name)
                    if not adapter:
                        continue
                    sources_queried.append(name)
                    try:
                        hits = adapter.recall(
                            query,
                            task_class=task_class,
                            keywords=keywords or [],
                            max_results=plan.max_results_per_adapter,
                        )
                        for hit in hits:
                            hit["_source_adapter"] = name
                        all_results.extend(hits)
                    except Exception as exc:
                        logger.warning("Recall from %s failed (non-fatal): %s", name, exc)

        # Phase 7: Inject tracked open loops for open_loop_recall intent
        if classification.intent == "open_loop_recall":
            try:
                from agents.open_loop_tracker import get_active_loops_for_recall

                loop_results = get_active_loops_for_recall(max_results=max_results)
                all_results.extend(loop_results)
                if loop_results:
                    sources_queried.append("open_loop_tracker")
            except Exception as exc:
                logger.warning("Open loop recall failed (non-fatal): %s", exc)

        # Phase 5: Deduplicate blended results
        if len(sources_queried) > 1:
            all_results = dedupe_results(all_results)

        # Phase 5: Multi-factor ranking with explanation metadata
        all_results = rank_results(all_results, classification, plan)

        # Cap results after ranking
        all_results = all_results[:max_results]

        elapsed_ms = int((time.monotonic() - start) * 1000)

        result = RecallResult(
            results=all_results,
            sources_queried=sources_queried,
            total_found=len(all_results),
            query=query,
            intent=classification.intent,
            trace_id=trace_id,
        )

        # Structured trace — Phase 5: include classification and routing
        slog.event(
            "memory.router.recall",
            ctx,
            operation="recall",
            caller=caller,
            intent=classification.intent,
            intent_confidence=classification.confidence,
            intent_signals=classification.signals,
            intent_fallback=classification.fallback_used,
            intent_legacy_mapped=classification.legacy_mapped,
            scope=scope,
            query=query[:200],
            blend_mode=plan.blend_mode,
            ranking_emphasis=plan.ranking_emphasis,
            adapters_queried=sources_queried,
            results_count=len(all_results),
            latency_ms=elapsed_ms,
        )

        return result

    def _select_recall_adapters(self, intent: str, scope: str) -> list[str]:
        """Legacy adapter selection — kept for backward compatibility.

        Phase 5 recall() uses get_routing_plan() instead. This method is
        retained for any external callers that use it directly.
        """
        if scope == "memory_files":
            return ["memory_file"]
        if scope == "vault":
            return ["obsidian_vault"]
        if scope == "fusion":
            return ["fusion_memory"]
        if scope == "working":
            return ["state_working"]

        # Intent-based routing for scope="all"
        if intent == "pattern_retrieval":
            return ["memory_file"]
        if intent == "vault_context":
            return ["obsidian_vault"]
        if intent == "session_replay":
            return ["fusion_memory", "state_working"]

        return ["memory_file", "obsidian_vault"]

    # -------------------------------------------------------------------
    # store — Phase 1: Fully functional
    # -------------------------------------------------------------------

    def store(
        self,
        obj: CanonicalMemoryObject,
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> StoreResult:
        """Persist a validated memory object to the appropriate backend.

        Validates the object, selects the target adapter, and stores.
        Fail-closed: invalid objects are rejected.

        Args:
            obj: The CanonicalMemoryObject to store.
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            StoreResult indicating success/failure.
        """
        start = time.monotonic()
        ctx = ctx or TraceContext.new("memory_router")
        trace_id = ctx.trace_id

        # Validate
        valid, errors = validate_canonical_object(obj)
        if not valid:
            result = StoreResult(
                stored=False,
                store_used="",
                validation_errors=errors,
                rejection_reason="validation_failed",
                trace_id=trace_id,
            )
            slog.event(
                "memory.router.store",
                ctx,
                level="warn",
                operation="store",
                caller=caller,
                title=obj.title[:100] if obj.title else "",
                stored=False,
                validation_outcome="rejected",
                rejection_reason="validation_failed",
                validation_errors=errors,
            )
            return result

        # Phase 4: Candidate evaluation
        from agents.memory_evaluator import evaluate_candidate

        decision = evaluate_candidate(obj)

        # Apply scores to object
        obj.importance_score = decision.importance_score
        obj.novelty_score = decision.novelty_score
        obj.durability_score = decision.durability_score

        if decision.action == "reject":
            result = StoreResult(
                stored=False,
                store_used="",
                rejection_reason=f"evaluation_rejected: {decision.reason}",
                trace_id=trace_id,
            )
            slog.event(
                "memory.router.store",
                ctx,
                level="warn",
                operation="store",
                caller=caller,
                title=obj.title[:100],
                current_layer=obj.current_layer,
                stored=False,
                eval_action=decision.action,
                eval_reason=decision.reason,
                importance_score=decision.importance_score,
                novelty_score=decision.novelty_score,
                durability_score=decision.durability_score,
                validation_outcome="rejected",
                rejection_reason=f"evaluation_rejected: {decision.reason}",
            )
            return result

        if decision.action == "working_only" and decision.adjusted_layer:
            obj.current_layer = decision.adjusted_layer
            obj.memory_layer_candidate = decision.adjusted_layer
            obj.layer_reason = f"eval_downgrade: {decision.reason}"

        # Apply promotion metadata from evaluation
        obj.promotion_eligibility = decision.promotion_eligibility
        if decision.promotion_blocker:
            obj.promotion_blocker = decision.promotion_blocker

        # Select target adapter
        target = obj.target_store or self._infer_target_store(obj)

        # Phase 2: Store/layer compatibility check
        if target != "discard":
            compatible, compat_reason = check_store_layer_compatibility(obj.current_layer, target)
            if not compatible:
                result = StoreResult(
                    stored=False,
                    store_used=target,
                    rejection_reason=f"layer_store_mismatch: {compat_reason}",
                    trace_id=trace_id,
                )
                slog.event(
                    "memory.router.store",
                    ctx,
                    level="warn",
                    operation="store",
                    caller=caller,
                    title=obj.title[:100],
                    current_layer=obj.current_layer,
                    adapter_selected=target,
                    stored=False,
                    validation_outcome="rejected",
                    rejection_reason=f"layer_store_mismatch: {compat_reason}",
                )
                return result

        adapter = self._adapters.get(target)

        if not adapter:
            result = StoreResult(
                stored=False,
                store_used=target,
                rejection_reason=f"no adapter for target_store={target!r}",
                trace_id=trace_id,
            )
            slog.event(
                "memory.router.store",
                ctx,
                level="warn",
                operation="store",
                caller=caller,
                title=obj.title[:100],
                current_layer=obj.current_layer,
                adapter_selected=target,
                stored=False,
                validation_outcome="pass",
                rejection_reason=f"no adapter for {target}",
            )
            return result

        if target == "discard":
            result = StoreResult(
                stored=False,
                store_used="discard",
                rejection_reason="target_store=discard",
                trace_id=trace_id,
            )
            slog.event(
                "memory.router.store",
                ctx,
                operation="store",
                caller=caller,
                title=obj.title[:100],
                current_layer=obj.current_layer,
                adapter_selected="discard",
                stored=False,
                validation_outcome="pass",
                rejection_reason="discarded",
            )
            return result

        # Persist via adapter
        store_result = adapter.store(obj)
        store_result.trace_id = trace_id

        elapsed_ms = int((time.monotonic() - start) * 1000)

        slog.event(
            "memory.router.store",
            ctx,
            operation="store",
            caller=caller,
            title=obj.title[:100],
            memory_event_type=obj.event_type,
            current_layer=obj.current_layer,
            adapter_selected=target,
            stored=store_result.stored,
            path_or_id=store_result.path_or_id,
            validation_outcome="pass",
            rejection_reason=store_result.rejection_reason,
            eval_action=decision.action,
            eval_reason=decision.reason,
            importance_score=decision.importance_score,
            promotion_eligibility=obj.promotion_eligibility,
            latency_ms=elapsed_ms,
        )

        return store_result

    def _infer_target_store(self, obj: CanonicalMemoryObject) -> str:
        """Infer target store from event_type and layer when target_store is null."""
        # Working-layer events go to state_working
        if obj.current_layer == "working":
            return "state_working"

        # Events that map to vault
        vault_events = {
            "research_completed",
            "workflow_learning_promoted",
            "agent_pattern_promoted",
        }
        if obj.event_type in vault_events:
            return "obsidian_vault"

        # Default to memory_file for most event types
        return "memory_file"

    # -------------------------------------------------------------------
    # ingest_event — Phase 1: Fully functional
    # -------------------------------------------------------------------

    def ingest_event(
        self,
        event: dict[str, Any],
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> CanonicalMemoryObject:
        """Normalize a raw event dict into a CanonicalMemoryObject.

        Required event fields: source, event_type, title, summary.
        All other fields have defaults.

        Args:
            event: Raw event dictionary from any source.
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            CanonicalMemoryObject ready for store() or further processing.

        Raises:
            ValueError: If required fields are missing.
        """
        ctx = ctx or TraceContext.new("memory_router")

        required = ("source", "event_type", "title", "summary")
        missing = [f for f in required if not event.get(f)]
        if missing:
            raise ValueError(f"ingest_event missing required fields: {missing}")

        ts = event.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Generate memory_id from source abbreviation + timestamp
        source_abbrev = event["source"][:3]
        ts_epoch = int(time.time())
        memory_id = event.get("memory_id") or f"cm-{source_abbrev}-{ts_epoch}"

        # Phase 2: Auto-assign layer metadata from event_type
        event_type = event["event_type"]
        provenance = event.get("provenance", "automatic")

        # Layer assignment priority:
        # 1. Explicit current_layer from caller (if valid)
        # 2. Operator override (provenance=operator_requested → trust caller)
        # 3. Inferred from event_type via EVENT_TYPE_TO_LAYER mapping
        explicit_layer = event.get("current_layer")
        if explicit_layer and explicit_layer in VALID_LAYERS:
            assigned_layer = explicit_layer
            layer_reason = f"explicit: caller specified current_layer={explicit_layer!r}"
        elif provenance == "operator_requested" and explicit_layer:
            assigned_layer = explicit_layer if explicit_layer in VALID_LAYERS else "episodic"
            layer_reason = f"operator_override: provenance=operator_requested, layer={assigned_layer!r}"
        else:
            assigned_layer = infer_layer_from_event_type(event_type)
            layer_reason = f"inferred: event_type={event_type!r} → {assigned_layer}"

        candidate_next = compute_candidate_next_layer(assigned_layer)

        # Promotion eligibility based on layer
        if assigned_layer == "procedural":
            promo_eligibility = "already_promoted"
        elif assigned_layer in ("working", "episodic"):
            promo_eligibility = "eligible"
        else:
            # semantic: needs review before procedural promotion
            promo_eligibility = "needs_review"

        # Allow caller override for promotion_eligibility
        if event.get("promotion_eligibility") in VALID_PROMOTION_ELIGIBILITY:
            promo_eligibility = event["promotion_eligibility"]

        obj = CanonicalMemoryObject(
            memory_id=memory_id,
            timestamp=ts,
            source=event["source"],
            event_type=event_type,
            provenance=provenance,
            source_file=event.get("source_file"),
            source_function=event.get("source_function"),
            title=event["title"][:100],
            summary=event["summary"],
            content=event.get("content"),
            entities=event.get("entities", []),
            tags=event.get("tags", []),
            memory_layer_candidate=assigned_layer,  # Sync with current_layer
            target_store=event.get("target_store"),
            # Phase 2: Layer metadata
            current_layer=assigned_layer,
            candidate_next_layer=candidate_next,
            promotion_eligibility=promo_eligibility,
            layer_reason=layer_reason,
            storage_intent=event.get("storage_intent", ""),
            promotion_blocker=event.get("promotion_blocker"),
            # Scoring
            confidence=event.get("confidence", "medium"),
            related_task_ids=event.get("related_task_ids", []),
            related_files=event.get("related_files", []),
            related_project=event.get("related_project", "nova-core"),
            supersedes=event.get("supersedes"),
            promotion_status=event.get("promotion_status", "candidate"),
            open_loop_status=event.get("open_loop_status"),
        )

        slog.event(
            "memory.router.ingest",
            ctx,
            operation="ingest",
            caller=caller,
            memory_id=obj.memory_id,
            source=obj.source,
            memory_event_type=obj.event_type,
            title=obj.title[:100],
            current_layer=obj.current_layer,
            candidate_next_layer=obj.candidate_next_layer or "(terminal)",
            promotion_eligibility=obj.promotion_eligibility,
            layer_reason=obj.layer_reason,
            target_store=obj.target_store or "(inferred)",
        )

        return obj

    # -------------------------------------------------------------------
    # promote — Phase 1: Skeleton
    # -------------------------------------------------------------------

    def promote(
        self,
        memory_id: str,
        from_layer: str,
        target_layer: str,
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> PromoteResult:
        """Promote a memory from a lower layer to a higher one.

        Phase 2: Validates promotion boundaries. Does NOT yet perform the
        actual cross-store migration (that requires Phase 3+ store reads).
        Returns boundary-check result with structured tracing.

        Args:
            memory_id: Identifier of the memory to promote.
            from_layer: Current layer of the memory.
            target_layer: Desired target layer.
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            PromoteResult with boundary validation outcome.
        """
        ctx = ctx or TraceContext.new("memory_router")
        trace_id = ctx.trace_id

        # Phase 2: Enforce promotion boundaries
        allowed, reason = check_promotion_boundary(from_layer, target_layer)

        if not allowed:
            slog.event(
                "memory.router.promote",
                ctx,
                level="warn",
                operation="promote",
                caller=caller,
                memory_id=memory_id,
                from_layer=from_layer,
                target_layer=target_layer,
                status="rejected",
                rejection_reason=reason,
            )
            return PromoteResult(
                promoted=False,
                source_store=from_layer,
                target_store=target_layer,
                reason=f"promotion_boundary_violation: {reason}",
                trace_id=trace_id,
            )

        # Boundary check passed — actual migration is Phase 3+
        slog.event(
            "memory.router.promote",
            ctx,
            operation="promote",
            caller=caller,
            memory_id=memory_id,
            from_layer=from_layer,
            target_layer=target_layer,
            status="boundary_check_passed",
            reason=reason,
        )
        return PromoteResult(
            promoted=False,  # Not yet migrated, just validated
            source_store=from_layer,
            target_store=target_layer,
            reason=f"boundary_check_passed: {reason}. Actual migration deferred to Phase 3+.",
            trace_id=trace_id,
        )

    # -------------------------------------------------------------------
    # checkpoint — Phase 6: Implemented
    # -------------------------------------------------------------------

    def checkpoint(
        self,
        summary: str,
        open_threads: list[str] | None = None,
        next_actions: list[str] | None = None,
        project: str = "nova-core",
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Create a session checkpoint artifact.

        Phase 6: Creates a working-layer checkpoint from the provided summary,
        open threads, and next actions. Stores via the normal store() path
        with provenance="consolidation".

        Returns dict with status, action, and trace_id.
        """
        ctx = ctx or TraceContext.new("memory_router")

        if not summary or not summary.strip():
            slog.event(
                "memory.router.checkpoint",
                ctx,
                level="warn",
                operation="checkpoint",
                caller=caller,
                status="rejected",
                reason="empty_summary",
            )
            return {
                "status": "rejected",
                "reason": "empty_summary",
                "trace_id": ctx.trace_id,
            }

        # Build checkpoint content
        checkpoint_summary = summary[:500]
        if open_threads:
            checkpoint_summary += " | Open: " + "; ".join(open_threads[:5])
        if next_actions:
            checkpoint_summary += " | Next: " + "; ".join(next_actions[:5])
        checkpoint_summary = checkpoint_summary[:500]

        obj = self.ingest_event(
            {
                "source": "consolidator" if caller != "operator" else "operator",
                "event_type": "session_end",
                "provenance": "consolidation",
                "title": f"checkpoint: {summary[:60]}"[:100],
                "summary": checkpoint_summary,
                "confidence": "high",
                "tags": ["#consolidation/checkpoint"],
                "related_project": project,
            }
        )

        result = self.store(obj, caller=caller or "router.checkpoint", ctx=ctx)

        slog.event(
            "memory.router.checkpoint",
            ctx,
            operation="checkpoint",
            caller=caller,
            status="created" if result.stored else "store_failed",
            stored=result.stored,
            store_used=result.store_used,
        )

        return {
            "status": "created" if result.stored else "store_failed",
            "action": "checkpoint_created",
            "stored": result.stored,
            "store_used": result.store_used,
            "trace_id": ctx.trace_id,
        }

    # -------------------------------------------------------------------
    # consolidate — Phase 6: Implemented
    # -------------------------------------------------------------------

    def consolidate(
        self,
        window: str = "24h",
        *,
        window_type: str = "working_memory",
        caller: str = "",
        ctx: TraceContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Consolidate memories within a time window.

        Phase 6: Reads recent items from appropriate adapters, runs
        consolidation pipeline, and optionally stores the result.

        Args:
            window: Time window string (e.g., "24h", "7d"). Currently
                    used for documentation; items are sourced from adapters.
            window_type: Type of consolidation window. One of:
                         session, working_memory, task_batch, failure_window,
                         heartbeat, plan_execution.
            caller: Caller identifier.
            ctx: Optional trace context.

        Returns:
            Dict with consolidation result.
        """
        from agents.memory_consolidator import (
            WindowSpec,
            consolidate_window,
        )

        ctx = ctx or TraceContext.new("memory_router")

        # Read source items from appropriate adapters
        source_items: list[dict] = []
        adapters_used: list[str] = []

        # Determine which adapter this window type needs
        _ADAPTER_FOR_WINDOW = {
            "working_memory": "state_working",
            "heartbeat": "state_working",
            "session": "memory_file",
            "task_batch": "memory_file",
            "failure_window": "memory_file",
            "plan_execution": "memory_file",
        }
        required_adapter = _ADAPTER_FOR_WINDOW.get(window_type)

        if required_adapter and required_adapter not in self._adapters:
            slog.event(
                "memory.router.consolidate",
                ctx,
                level="warn",
                operation="consolidate",
                caller=caller,
                window=window,
                window_type=window_type,
                input_count=0,
                dedupe_removed=0,
                action="skipped_no_adapter",
                reason=f"adapter {required_adapter!r} not registered",
                adapters_used=[],
            )
            return {
                "status": "skipped_no_adapter",
                "reason": f"adapter {required_adapter!r} not registered",
                "input_count": 0,
                "dedupe_removed": 0,
                "output_layer": "",
                "confidence": "",
                "promotion_eligible": False,
                "stored": False,
                "store_used": "",
                "trace_id": ctx.trace_id,
            }

        if window_type in ("working_memory", "heartbeat"):
            adapter = self._adapters.get("state_working")
            if adapter:
                source_items = adapter.recall("", max_results=_MAX_WINDOW_ITEMS)
                adapters_used.append("state_working")
        elif window_type in ("session", "task_batch", "failure_window", "plan_execution"):
            adapter = self._adapters.get("memory_file")
            if adapter:
                source_items = adapter.recall(
                    "",
                    max_results=_MAX_WINDOW_ITEMS,
                    task_class=kwargs.get("task_class", ""),
                    keywords=kwargs.get("keywords", []),
                )
                adapters_used.append("memory_file")

        spec = WindowSpec(
            window_type=window_type,
            source_items=source_items,
            session_id=kwargs.get("session_id", ""),
            project=kwargs.get("project", "nova-core"),
            caller=caller,
        )

        result = consolidate_window(spec)

        # Store the output artifact if consolidation produced one
        stored = False
        store_used = ""
        if result.output_artifact and result.action in ("summary_created", "checkpoint_created"):
            obj = self.ingest_event(result.output_artifact)
            store_result = self.store(obj, caller=caller or "router.consolidate", ctx=ctx)
            stored = store_result.stored
            store_used = store_result.store_used

        slog.event(
            "memory.router.consolidate",
            ctx,
            operation="consolidate",
            caller=caller,
            window=window,
            window_type=window_type,
            input_count=result.input_count,
            dedupe_removed=result.dedupe_removed,
            action=result.action,
            reason=result.reason,
            output_layer=result.output_layer,
            confidence=result.confidence,
            promotion_eligible=result.promotion_eligible,
            stored=stored,
            store_used=store_used,
            adapters_used=adapters_used,
        )

        return {
            "status": result.action,
            "reason": result.reason,
            "input_count": result.input_count,
            "dedupe_removed": result.dedupe_removed,
            "output_layer": result.output_layer,
            "confidence": result.confidence,
            "promotion_eligible": result.promotion_eligible,
            "stored": stored,
            "store_used": store_used,
            "trace_id": ctx.trace_id,
        }

    # -------------------------------------------------------------------
    # summarize_session — Phase 6: Implemented
    # -------------------------------------------------------------------

    def summarize_session(
        self,
        session_id: str,
        *,
        session_data: dict | None = None,
        caller: str = "",
        ctx: TraceContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Summarize a session into an episodic summary artifact.

        Phase 6: Takes session data (from SessionManager or direct dict)
        and produces a consolidated session summary stored as episodic memory.

        Args:
            session_id: Session identifier.
            session_data: Optional session dict with tasks. If not provided,
                          reads from working memory.
            caller: Caller identifier.
            ctx: Optional trace context.

        Returns:
            Dict with summarization result.
        """
        from agents.memory_consolidator import summarize_session as _summarize

        ctx = ctx or TraceContext.new("memory_router")

        # Build session data if not provided
        if not session_data:
            session_data = {"session_id": session_id, "tasks": []}
            # Read from working memory as fallback
            adapter = self._adapters.get("state_working")
            if adapter:
                wm_items = adapter.recall("", max_results=20)
                session_data["tasks"] = wm_items

        result = _summarize(session_data)

        # Store if summary was created
        stored = False
        store_used = ""
        if result.output_artifact and result.action == "summary_created":
            obj = self.ingest_event(result.output_artifact)
            store_result = self.store(obj, caller=caller or "router.summarize_session", ctx=ctx)
            stored = store_result.stored
            store_used = store_result.store_used

        slog.event(
            "memory.router.summarize_session",
            ctx,
            operation="summarize_session",
            caller=caller,
            session_id=session_id,
            action=result.action,
            reason=result.reason,
            input_count=result.input_count,
            stored=stored,
        )

        return {
            "status": result.action,
            "reason": result.reason,
            "input_count": result.input_count,
            "stored": stored,
            "store_used": store_used,
            "trace_id": ctx.trace_id,
        }

    # -------------------------------------------------------------------
    # extract_patterns — Phase 6: Implemented
    # -------------------------------------------------------------------

    def extract_patterns(
        self,
        window: str = "7d",
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Extract pattern candidates from recent episodic memories.

        Phase 6: Scans memory_file artifacts for repeated patterns.
        Returns candidates but does NOT auto-promote them.

        Args:
            window: Time window string (documentation only).
            caller: Caller identifier.
            ctx: Optional trace context.

        Returns:
            Dict with pattern candidates found.
        """
        from agents.memory_consolidator import extract_pattern_candidates

        ctx = ctx or TraceContext.new("memory_router")

        # Read recent artifacts
        adapter = self._adapters.get("memory_file")
        items: list[dict] = []
        if adapter:
            items = adapter.recall(
                "",
                max_results=_MAX_WINDOW_ITEMS,
                task_class=kwargs.get("task_class", ""),
                keywords=kwargs.get("keywords", []),
            )

        candidates = extract_pattern_candidates(items)

        slog.event(
            "memory.router.extract_patterns",
            ctx,
            operation="extract_patterns",
            caller=caller,
            window=window,
            items_scanned=len(items),
            candidates_found=len(candidates),
        )

        return {
            "status": "patterns_found" if candidates else "no_patterns",
            "items_scanned": len(items),
            "candidates": [c.to_dict() for c in candidates],
            "candidates_count": len(candidates),
            "trace_id": ctx.trace_id,
        }

    # -------------------------------------------------------------------
    # reflect — Phase 6: Implemented (scoped consolidation)
    # -------------------------------------------------------------------

    def reflect(
        self,
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run a scoped reflection cycle.

        Phase 6: "Reflection" = consolidate recent working memory + extract
        pattern candidates. Not freeform introspection.

        Returns combined results from consolidation and pattern extraction.
        """
        ctx = ctx or TraceContext.new("memory_router")

        consolidation = self.consolidate(
            window="24h",
            window_type="working_memory",
            caller=caller or "router.reflect",
            ctx=ctx,
        )
        patterns = self.extract_patterns(
            window="7d",
            caller=caller or "router.reflect",
            ctx=ctx,
            **kwargs,
        )

        slog.event(
            "memory.router.reflect",
            ctx,
            operation="reflect",
            caller=caller,
            consolidation_status=consolidation.get("status"),
            patterns_found=patterns.get("candidates_count", 0),
        )

        return {
            "status": "completed",
            "consolidation": consolidation,
            "patterns": patterns,
            "trace_id": ctx.trace_id,
        }

    # -------------------------------------------------------------------
    # track_open_loop — Phase 7: Implemented
    # -------------------------------------------------------------------

    def track_open_loop(
        self,
        description: str,
        project: str = "nova-core",
        *,
        title: str = "",
        source: str = "",
        confidence: str = "medium",
        blocker: str = "",
        due_hint: str = "",
        owner: str = "",
        related_task_ids: list[str] | None = None,
        related_files: list[str] | None = None,
        tags: list[str] | None = None,
        caller: str = "",
        ctx: TraceContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Track an open loop — unresolved work, pending decision, or continuity item.

        Phase 7: Creates or deduplicates open-loop records via the
        LoopStore (STATE/open_loops/). Returns structured result.

        Args:
            description: Description of the unresolved work (used as summary).
            project: Related project.
            title: Short title (defaults to truncated description).
            source: Who/what identified this loop.
            confidence: Confidence level.
            blocker: What's blocking, if anything.
            due_hint: Informal time sensitivity.
            owner: Actor responsible.
            related_task_ids: Related task stems.
            related_files: Related file paths.
            tags: Categorization tags.
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            Dict with loop action result.
        """
        from agents.open_loop_tracker import LoopStore, create_loop

        ctx = ctx or TraceContext.new("memory_router")
        store = LoopStore(base=self._loop_store_base) if self._loop_store_base else None

        result = create_loop(
            title=(title or description[:100]).strip(),
            summary=description.strip(),
            source=source or caller or "router",
            project=project,
            confidence=confidence,
            blocker=blocker,
            due_hint=due_hint,
            owner=owner,
            related_task_ids=related_task_ids,
            related_files=related_files,
            tags=tags,
            store=store,
        )

        slog.event(
            "memory.router.track_open_loop",
            ctx,
            operation="track_open_loop",
            caller=caller,
            loop_action=result.action,
            loop_id=result.loop_id,
            title=(title or description[:60])[:100],
            new_status=result.new_status,
            previous_status=result.previous_status,
            reason=result.reason,
            stored=result.stored,
            project=project,
            confidence=confidence,
        )

        return result.to_dict()

    def detect_open_loops(
        self,
        event: dict[str, Any],
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Detect if an event implies an open loop and track it.

        Phase 7: Conservative detection from task failures, plan
        follow-ups, and session continuity events.

        Args:
            event: Raw event dict (same format as ingest_event input).
            caller: Caller identifier.
            ctx: Optional trace context.

        Returns:
            Dict with detection result, or empty if no loop detected.
        """
        from agents.open_loop_tracker import LoopStore, detect_loop_from_event

        ctx = ctx or TraceContext.new("memory_router")
        store = LoopStore(base=self._loop_store_base) if self._loop_store_base else None

        result = detect_loop_from_event(event, store=store)

        if result is None:
            return {"status": "no_loop_detected", "trace_id": ctx.trace_id}

        slog.event(
            "memory.router.detect_open_loop",
            ctx,
            operation="detect_open_loop",
            caller=caller,
            loop_action=result.action,
            loop_id=result.loop_id,
            source_event_type=event.get("event_type", ""),
            reason=result.reason,
            stored=result.stored,
        )

        return result.to_dict()

    def resolve_open_loop(
        self,
        loop_id: str,
        *,
        closure_reason: str,
        closure_evidence: str = "",
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Resolve an open loop with explicit evidence.

        Args:
            loop_id: ID of the loop to resolve.
            closure_reason: Why this loop is resolved.
            closure_evidence: Evidence of resolution.
            caller: Caller identifier.
            ctx: Optional trace context.

        Returns:
            Dict with resolution result.
        """
        from agents.open_loop_tracker import LoopStore, resolve_loop

        ctx = ctx or TraceContext.new("memory_router")
        store = LoopStore(base=self._loop_store_base)

        loop = store.load(loop_id)
        if not loop:
            return {
                "action": "no_loop_action",
                "reason": f"loop_not_found: {loop_id}",
                "trace_id": ctx.trace_id,
            }

        result = resolve_loop(
            loop,
            closure_reason=closure_reason,
            closure_evidence=closure_evidence,
            store=store,
        )

        slog.event(
            "memory.router.resolve_open_loop",
            ctx,
            operation="resolve_open_loop",
            caller=caller,
            loop_id=loop_id,
            loop_action=result.action,
            previous_status=result.previous_status,
            new_status=result.new_status,
            reason=result.reason,
        )

        return result.to_dict()

    def get_open_loops(
        self,
        *,
        project: str | None = None,
        max_results: int = 10,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Get all active open loops.

        Args:
            project: Filter by project.
            max_results: Maximum loops to return.
            caller: Caller identifier.
            ctx: Optional trace context.

        Returns:
            Dict with active loops list.
        """
        from agents.open_loop_tracker import LoopStore, get_active_loops_for_recall

        ctx = ctx or TraceContext.new("memory_router")
        store = LoopStore(base=self._loop_store_base) if self._loop_store_base else None
        loops = get_active_loops_for_recall(store=store, project=project, max_results=max_results)

        slog.event(
            "memory.router.get_open_loops",
            ctx,
            operation="get_open_loops",
            caller=caller,
            project=project or "all",
            loops_found=len(loops),
        )

        return {
            "status": "ok",
            "loops": loops,
            "count": len(loops),
            "trace_id": ctx.trace_id,
        }

    # -------------------------------------------------------------------
    # generate_diary — Phase 6: Implemented
    # -------------------------------------------------------------------

    def generate_diary(
        self,
        session_id: str,
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate a diary entry from a session.

        Phase 6: Alias for summarize_session() — produces an episodic
        session summary that can be used as a diary entry.
        """
        return self.summarize_session(
            session_id,
            caller=caller or "router.generate_diary",
            ctx=ctx,
            **kwargs,
        )

    # -------------------------------------------------------------------
    # run_governance — Phase 8: Memory governance sweep
    # -------------------------------------------------------------------

    def run_governance(
        self,
        *,
        dry_run: bool = True,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Run a memory governance sweep across all managed stores.

        Phase 8: Evaluates retention rules, marks stale artifacts, archives
        old operational state, prunes expired items, and protects high-value
        memory — all with dry-run support and structured audit logging.

        Args:
            dry_run: If True (default), evaluate rules but make no changes.
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            Dict with sweep summary including items examined, acted on,
            protected, and detailed per-item results.
        """
        from agents.memory_governance import GovernanceEngine

        ctx = ctx or TraceContext.new("memory_router")

        engine = GovernanceEngine(
            state_dir=self._loop_store_base.parent if self._loop_store_base else None,
        )
        summary = engine.run_sweep(dry_run=dry_run)

        slog.event(
            "memory.router.governance",
            ctx,
            operation="governance_sweep",
            caller=caller,
            dry_run=dry_run,
            items_examined=summary.items_examined,
            items_acted_on=summary.items_acted_on,
            items_protected=summary.items_protected,
            error_count=len(summary.errors),
        )

        return summary.to_dict()

    # -------------------------------------------------------------------
    # run_compaction — Phase 9: Dedup, compaction, supersession
    # -------------------------------------------------------------------

    def run_compaction(
        self,
        *,
        dry_run: bool = True,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Run a memory compaction sweep for dedup/supersession.

        Phase 9: Detects duplicates and near-duplicates, compacts thin
        artifact groups, marks superseded artifacts, preserves provenance.

        Args:
            dry_run: If True (default), evaluate but make no changes.
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            Dict with compaction summary.
        """
        from agents.memory_compactor import CompactionEngine

        ctx = ctx or TraceContext.new("memory_router")

        engine = CompactionEngine(
            memory_dir=self._adapters.get("memory_file", None)
            and getattr(self._adapters["memory_file"], "_base", None),
            state_dir=self._loop_store_base.parent if self._loop_store_base else None,
        )
        summary = engine.run_compaction(dry_run=dry_run)

        slog.event(
            "memory.router.compaction",
            ctx,
            operation="compaction_sweep",
            caller=caller,
            dry_run=dry_run,
            items_examined=summary.items_examined,
            duplicates_found=summary.duplicates_found,
            items_compacted=summary.items_compacted,
            items_superseded=summary.items_superseded,
            items_protected=summary.items_protected,
            error_count=len(summary.errors),
        )

        return summary.to_dict()

    # -------------------------------------------------------------------
    # collect_metrics — Phase 10: Memory health metrics
    # -------------------------------------------------------------------

    def collect_metrics(
        self,
        *,
        caller: str = "",
        ctx: TraceContext | None = None,
    ) -> dict[str, Any]:
        """Collect a memory health metrics snapshot.

        Phase 10: Gathers layer counts, open loop status, event counters
        from structured logs, and file-based store counts.

        Args:
            caller: Caller identifier for tracing.
            ctx: Optional trace context.

        Returns:
            Dict with complete metrics snapshot.
        """
        from agents.memory_metrics import collect_metrics as _collect

        ctx = ctx or TraceContext.new("memory_router")

        metrics = _collect(
            state_dir=self._loop_store_base.parent if self._loop_store_base else None,
        )

        slog.event(
            "memory.router.metrics",
            ctx,
            operation="collect_metrics",
            caller=caller,
            total_artifacts=metrics.layer_counts.total(),
            active_loops=metrics.open_loop_counts.active(),
            store_calls=metrics.slog_counters.store_calls,
            recall_calls=metrics.slog_counters.recall_calls,
        )

        return metrics.to_dict()


# ---------------------------------------------------------------------------
# Singleton router instance
# ---------------------------------------------------------------------------

router = MemoryRouter()
