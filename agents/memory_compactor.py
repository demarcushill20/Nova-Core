"""Phase 9 — Memory Compaction, Deduplication & Supersession.

Deterministic detection of duplicate and near-duplicate memories,
safe compaction of repeated low-value artifacts, and explicit
supersession with provenance preservation.

This module does NOT:
- Touch Fusion Memory (prompt-delegated)
- Modify Obsidian vault files (MCP-managed)
- Silently delete any artifact without logging
- Merge unrelated memories based on vague heuristics
- Compact protected/procedural/semantic memory
- Auto-merge active open loops

Usage:
    from agents.memory_compactor import (
        CompactionEngine, DedupMatch, CompactionResult,
        MatchClass, detect_duplicates, supersede_artifact,
    )

    engine = CompactionEngine()
    results = engine.run_compaction(dry_run=True)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
_MEMORY_DIR = _BASE / "MEMORY"
_STATE_DIR = _BASE / "STATE"

# Thresholds
_THIN_SUMMARY_LEN = 20
_MAX_COMPACTION_GROUP = 20  # Max artifacts to compact in one batch


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MatchClass:
    """Classification of how two artifacts relate."""

    NO_MATCH = "no_match"
    EXACT_DUPLICATE = "exact_duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    MERGE_CANDIDATE = "merge_candidate"
    SUPERSESSION_CANDIDATE = "supersession_candidate"
    REJECTED_AMBIGUOUS = "rejected_ambiguous"
    PROTECTED_SKIP = "protected_skip"


VALID_COMPACTION_ACTIONS = frozenset(
    {
        "no_action",
        "deduplicated",
        "compacted",
        "superseded",
        "protected_skip",
        "rejected_ambiguous",
        "rejected_unsafe",
    }
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DedupMatch:
    """Result of comparing two artifacts for deduplication."""

    match_class: str
    artifact_a_id: str
    artifact_b_id: str
    artifact_a_path: str
    artifact_b_path: str
    confidence: str  # low, medium, high
    matching_fields: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompactionResult:
    """Result of a single compaction/dedup/supersession action."""

    action: str  # from VALID_COMPACTION_ACTIONS
    target_store: str
    surviving_path: str
    affected_paths: list[str]
    rule_name: str
    dry_run: bool
    match_class: str = MatchClass.NO_MATCH
    provenance_links: list[str] = field(default_factory=list)
    detail: str = ""
    rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompactionSweepSummary:
    """Aggregate summary of a compaction sweep."""

    sweep_id: str
    dry_run: bool
    started_at: str
    completed_at: str = ""
    items_examined: int = 0
    duplicates_found: int = 0
    items_compacted: int = 0
    items_superseded: int = 0
    items_protected: int = 0
    items_rejected: int = 0
    results: list[CompactionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["results"] = [r.to_dict() for r in self.results]
        return d


# ---------------------------------------------------------------------------
# Content hashing (reuses consolidation approach)
# ---------------------------------------------------------------------------


def content_hash(item: dict) -> str:
    """Compute deterministic content hash for an artifact.

    Uses same fields as consolidation dedup: event_type + title + summary[:100] + source.
    """
    parts = [
        str(item.get("event_type", item.get("task_class", ""))),
        str(item.get("title", item.get("task_summary", ""))),
        str(item.get("summary", item.get("task_summary", "")))[:100],
        str(item.get("source", "")),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def title_key(item: dict) -> str:
    """Compute normalized title key for near-duplicate detection."""
    title = item.get("title", item.get("task_summary", ""))
    project = item.get("related_project", item.get("project", "nova-core"))
    raw = f"{project}|{str(title).lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def workflow_key(item: dict) -> str | None:
    """Extract workflow_id if present."""
    wf = item.get("workflow_id")
    if wf:
        return str(wf).strip()
    # Try to extract from artifact_id: mem_<workflow_id>_<timestamp>
    aid = item.get("artifact_id", "")
    if aid.startswith("mem_"):
        parts = aid.split("_")
        if len(parts) >= 3:
            # workflow_id is everything between first "mem_" and last "_<timestamp>"
            return "_".join(parts[1:-1])
    return None


# ---------------------------------------------------------------------------
# Protection check (reuse governance logic)
# ---------------------------------------------------------------------------


def _is_merge_protected(path: Path, metadata: dict | None) -> tuple[bool, str]:
    """Check if an artifact is protected from compaction/merge.

    Returns (is_protected, reason).
    """
    if metadata is None:
        return True, "unreadable_metadata"

    # Explicit protection
    if metadata.get("protected"):
        return True, "explicit_protected_flag"

    # Procedural layer
    path_str = str(path)
    if "agent_patterns" in path_str:
        return True, "procedural_layer"

    # Active open loops
    if "open_loops" in path_str:
        status = metadata.get("status", "")
        if status in ("proposed", "open", "blocked", "deferred"):
            return True, "active_open_loop"

    # Operator-authored
    if metadata.get("source") == "operator":
        return True, "operator_authored"

    # High importance episodic
    importance = metadata.get("importance", metadata.get("importance_score", 0.0))
    if isinstance(importance, str):
        try:
            importance = float(importance)
        except ValueError:
            importance = 0.0
    # For merge protection, we only block at high importance + different content
    # Medium importance is allowed for dedup (exact matches) but not compaction

    return False, ""


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _safe_read_json(path: Path) -> dict | None:
    """Read JSON file, return None on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _archive_file(path: Path, archive_base: Path) -> str:
    """Move file to archive directory. Returns archive path or empty string."""
    archive_base.mkdir(parents=True, exist_ok=True)
    dest = archive_base / path.name
    if dest.exists():
        dest = archive_base / f"{int(time.time())}_{path.name}"
    try:
        shutil.move(str(path), str(dest))
        return str(dest)
    except OSError as exc:
        logger.warning("Failed to archive %s: %s", path, exc)
        return ""


def _write_json_atomic(path: Path, data: dict) -> bool:
    """Atomic JSON write via tmp+rename."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(path)
        return True
    except OSError as exc:
        logger.warning("Failed to write %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def detect_duplicates(
    artifacts: list[tuple[Path, dict]],
) -> list[DedupMatch]:
    """Detect exact and near-duplicate pairs among a list of artifacts.

    Args:
        artifacts: List of (file_path, parsed_metadata) tuples.

    Returns:
        List of DedupMatch results, one per detected pair.
    """
    matches: list[DedupMatch] = []

    # Index by content hash for exact duplicates
    by_hash: dict[str, list[tuple[Path, dict]]] = {}
    # Index by title key for near-duplicates
    by_title: dict[str, list[tuple[Path, dict]]] = {}
    # Index by workflow_id for supersession candidates
    by_workflow: dict[str, list[tuple[Path, dict]]] = {}

    for path, meta in artifacts:
        h = content_hash(meta)
        by_hash.setdefault(h, []).append((path, meta))

        tk = title_key(meta)
        by_title.setdefault(tk, []).append((path, meta))

        wk = workflow_key(meta)
        if wk:
            by_workflow.setdefault(wk, []).append((path, meta))

    seen_pairs: set[tuple[str, str]] = set()

    def _pair_key(p1: Path, p2: Path) -> tuple[str, str]:
        a, b = str(p1), str(p2)
        return (min(a, b), max(a, b))

    # 1. Exact duplicates (same content hash)
    for h, group in by_hash.items():
        if len(group) < 2:
            continue
        # Sort by mtime descending (newest first)
        group.sort(key=lambda x: x[0].stat().st_mtime if x[0].exists() else 0, reverse=True)
        newest = group[0]
        for older in group[1:]:
            pk = _pair_key(newest[0], older[0])
            if pk in seen_pairs:
                continue
            seen_pairs.add(pk)
            matches.append(
                DedupMatch(
                    match_class=MatchClass.EXACT_DUPLICATE,
                    artifact_a_id=_artifact_id(newest[1]),
                    artifact_b_id=_artifact_id(older[1]),
                    artifact_a_path=str(newest[0]),
                    artifact_b_path=str(older[0]),
                    confidence="high",
                    matching_fields=["content_hash"],
                    reason=f"Identical content hash: {h}",
                )
            )

    # 2. Workflow-based supersession candidates
    for wk, group in by_workflow.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: x[0].stat().st_mtime if x[0].exists() else 0, reverse=True)
        newest = group[0]
        for older in group[1:]:
            pk = _pair_key(newest[0], older[0])
            if pk in seen_pairs:
                continue
            seen_pairs.add(pk)
            # Check if they have same content hash (exact dup already caught)
            if content_hash(newest[1]) == content_hash(older[1]):
                continue
            matches.append(
                DedupMatch(
                    match_class=MatchClass.SUPERSESSION_CANDIDATE,
                    artifact_a_id=_artifact_id(newest[1]),
                    artifact_b_id=_artifact_id(older[1]),
                    artifact_a_path=str(newest[0]),
                    artifact_b_path=str(older[0]),
                    confidence="medium",
                    matching_fields=["workflow_id"],
                    reason=f"Same workflow_id: {wk}, newer may supersede older",
                )
            )

    # 3. Near-duplicates by title (not already caught)
    for _tk, group in by_title.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: x[0].stat().st_mtime if x[0].exists() else 0, reverse=True)
        newest = group[0]
        for older in group[1:]:
            pk = _pair_key(newest[0], older[0])
            if pk in seen_pairs:
                continue
            seen_pairs.add(pk)

            # Check event_type match — different types are ambiguous
            et_a = newest[1].get("event_type", newest[1].get("task_class", ""))
            et_b = older[1].get("event_type", older[1].get("task_class", ""))
            if et_a != et_b:
                matches.append(
                    DedupMatch(
                        match_class=MatchClass.REJECTED_AMBIGUOUS,
                        artifact_a_id=_artifact_id(newest[1]),
                        artifact_b_id=_artifact_id(older[1]),
                        artifact_a_path=str(newest[0]),
                        artifact_b_path=str(older[0]),
                        confidence="low",
                        matching_fields=["title_key"],
                        reason=f"Same title key but different event_types: {et_a} vs {et_b}",
                    )
                )
            else:
                matches.append(
                    DedupMatch(
                        match_class=MatchClass.NEAR_DUPLICATE,
                        artifact_a_id=_artifact_id(newest[1]),
                        artifact_b_id=_artifact_id(older[1]),
                        artifact_a_path=str(newest[0]),
                        artifact_b_path=str(older[0]),
                        confidence="medium",
                        matching_fields=["title_key", "event_type"],
                        reason=f"Same title key and event_type: {et_a}",
                    )
                )

    return matches


def _artifact_id(meta: dict) -> str:
    """Extract artifact ID from metadata dict."""
    return str(meta.get("artifact_id", "") or meta.get("memory_id", "") or meta.get("loop_id", "") or "unknown")


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


def supersede_artifact(
    surviving_path: Path,
    superseded_path: Path,
    *,
    dry_run: bool = True,
) -> CompactionResult:
    """Mark one artifact as superseded by another.

    The surviving artifact gets `supersedes` set to the superseded artifact's ID.
    The superseded artifact gets `promotion_status: "superseded"`.

    Neither artifact is deleted — both are retained with updated metadata.

    Args:
        surviving_path: Path to the newer/better artifact.
        superseded_path: Path to the older artifact being superseded.
        dry_run: If True, compute result but make no changes.

    Returns:
        CompactionResult with action details.
    """
    surviving_meta = _safe_read_json(surviving_path)
    superseded_meta = _safe_read_json(superseded_path)

    if surviving_meta is None or superseded_meta is None:
        return CompactionResult(
            action="rejected_unsafe",
            target_store="unknown",
            surviving_path=str(surviving_path),
            affected_paths=[str(superseded_path)],
            rule_name="supersede_artifact",
            dry_run=dry_run,
            rejection_reason="unreadable_metadata",
        )

    # Protection check
    protected, reason = _is_merge_protected(superseded_path, superseded_meta)
    if protected:
        return CompactionResult(
            action="protected_skip",
            target_store=_infer_store(superseded_path),
            surviving_path=str(surviving_path),
            affected_paths=[str(superseded_path)],
            rule_name="supersede_artifact",
            dry_run=dry_run,
            rejection_reason=reason,
        )

    surviving_id = _artifact_id(surviving_meta)
    superseded_id = _artifact_id(superseded_meta)

    # Prevent self-supersession
    if surviving_id == superseded_id and surviving_id != "unknown":
        return CompactionResult(
            action="rejected_unsafe",
            target_store=_infer_store(surviving_path),
            surviving_path=str(surviving_path),
            affected_paths=[str(superseded_path)],
            rule_name="supersede_artifact",
            dry_run=dry_run,
            rejection_reason="self_supersession",
        )

    # Prevent circular supersession
    if surviving_meta.get("supersedes") == superseded_id:
        # Already superseded
        return CompactionResult(
            action="no_action",
            target_store=_infer_store(surviving_path),
            surviving_path=str(surviving_path),
            affected_paths=[str(superseded_path)],
            rule_name="supersede_artifact",
            dry_run=dry_run,
            detail="already_superseded",
        )

    if not dry_run:
        # Update surviving artifact
        surviving_meta["supersedes"] = superseded_id
        _write_json_atomic(surviving_path, surviving_meta)

        # Update superseded artifact
        superseded_meta["promotion_status"] = "superseded"
        superseded_meta["superseded_by"] = surviving_id
        _write_json_atomic(superseded_path, superseded_meta)

    return CompactionResult(
        action="superseded",
        target_store=_infer_store(surviving_path),
        surviving_path=str(surviving_path),
        affected_paths=[str(superseded_path)],
        rule_name="supersede_artifact",
        dry_run=dry_run,
        match_class=MatchClass.SUPERSESSION_CANDIDATE,
        provenance_links=[superseded_id],
        detail=f"{surviving_id} supersedes {superseded_id}",
    )


# ---------------------------------------------------------------------------
# Compaction (thin artifact groups)
# ---------------------------------------------------------------------------


def compact_group(
    artifacts: list[tuple[Path, dict]],
    *,
    archive_dir: Path,
    dry_run: bool = True,
) -> CompactionResult:
    """Compact a group of related thin artifacts into a single surviving artifact.

    Keeps the artifact with the richest content (longest summary, highest
    confidence). Archives the rest with provenance links.

    Args:
        artifacts: List of (path, metadata) tuples in the same group.
        archive_dir: Where to archive compacted source artifacts.
        dry_run: If True, compute result but make no changes.

    Returns:
        CompactionResult.
    """
    if len(artifacts) < 2:
        return CompactionResult(
            action="no_action",
            target_store="unknown",
            surviving_path=str(artifacts[0][0]) if artifacts else "",
            affected_paths=[],
            rule_name="compact_group",
            dry_run=dry_run,
            detail="single_item_no_compaction",
        )

    # Check protection on all
    for path, meta in artifacts:
        protected, reason = _is_merge_protected(path, meta)
        if protected:
            return CompactionResult(
                action="protected_skip",
                target_store=_infer_store(path),
                surviving_path="",
                affected_paths=[str(p) for p, _ in artifacts],
                rule_name="compact_group",
                dry_run=dry_run,
                rejection_reason=f"group_contains_protected: {reason}",
            )

    # Pick survivor: longest summary, then highest confidence, then newest
    conf_rank = {"high": 3, "medium": 2, "low": 1}

    def _score(item: tuple[Path, dict]) -> tuple[int, int, float]:
        path, meta = item
        summary = str(meta.get("task_summary", "") or meta.get("summary", ""))
        conf = meta.get("confidence", "low")
        mtime = path.stat().st_mtime if path.exists() else 0
        return (len(summary), conf_rank.get(conf, 0), mtime)

    ranked = sorted(artifacts, key=_score, reverse=True)
    survivor_path, survivor_meta = ranked[0]
    to_archive = ranked[1:]

    # Collect source IDs
    source_ids = [_artifact_id(m) for _, m in to_archive]
    survivor_id = _artifact_id(survivor_meta)

    if not dry_run:
        # Update survivor with compaction provenance
        existing_compacted = survivor_meta.get("compacted_from", [])
        if not isinstance(existing_compacted, list):
            existing_compacted = []
        survivor_meta["compacted_from"] = existing_compacted + source_ids
        survivor_meta["provenance"] = "compaction"
        _write_json_atomic(survivor_path, survivor_meta)

        # Archive source artifacts
        for path, meta in to_archive:
            meta["promotion_status"] = "superseded"
            meta["superseded_by"] = survivor_id
            _write_json_atomic(path, meta)
            _archive_file(path, archive_dir)

    return CompactionResult(
        action="compacted",
        target_store=_infer_store(survivor_path),
        surviving_path=str(survivor_path),
        affected_paths=[str(p) for p, _ in to_archive],
        rule_name="compact_group",
        dry_run=dry_run,
        match_class=MatchClass.NEAR_DUPLICATE,
        provenance_links=source_ids,
        detail=f"Compacted {len(to_archive)} artifacts into {survivor_id}",
    )


# ---------------------------------------------------------------------------
# Exact dedup workflow
# ---------------------------------------------------------------------------


def deduplicate_exact(
    match: DedupMatch,
    *,
    archive_dir: Path,
    dry_run: bool = True,
) -> CompactionResult:
    """Handle an exact duplicate by archiving the older copy.

    Args:
        match: DedupMatch with match_class == EXACT_DUPLICATE.
        archive_dir: Where to archive the duplicate.
        dry_run: If True, compute result but make no changes.

    Returns:
        CompactionResult.
    """
    if match.match_class != MatchClass.EXACT_DUPLICATE:
        return CompactionResult(
            action="rejected_unsafe",
            target_store="unknown",
            surviving_path=match.artifact_a_path,
            affected_paths=[match.artifact_b_path],
            rule_name="deduplicate_exact",
            dry_run=dry_run,
            rejection_reason=f"not_exact_duplicate: {match.match_class}",
        )

    older_path = Path(match.artifact_b_path)
    older_meta = _safe_read_json(older_path)

    # Protection check
    protected, reason = _is_merge_protected(older_path, older_meta)
    if protected:
        return CompactionResult(
            action="protected_skip",
            target_store=_infer_store(older_path),
            surviving_path=match.artifact_a_path,
            affected_paths=[match.artifact_b_path],
            rule_name="deduplicate_exact",
            dry_run=dry_run,
            rejection_reason=reason,
        )

    if not dry_run:
        if older_meta:
            older_meta["promotion_status"] = "superseded"
            older_meta["superseded_by"] = match.artifact_a_id
            _write_json_atomic(older_path, older_meta)
        _archive_file(older_path, archive_dir)

    return CompactionResult(
        action="deduplicated",
        target_store=_infer_store(older_path),
        surviving_path=match.artifact_a_path,
        affected_paths=[match.artifact_b_path],
        rule_name="deduplicate_exact",
        dry_run=dry_run,
        match_class=MatchClass.EXACT_DUPLICATE,
        provenance_links=[match.artifact_b_id],
        detail=f"Exact duplicate archived: {match.artifact_b_id}",
    )


# ---------------------------------------------------------------------------
# Store inference helper
# ---------------------------------------------------------------------------


def _infer_store(path: Path) -> str:
    """Infer store name from file path."""
    p = str(path)
    if "workflow_learnings" in p:
        return "workflow_learnings"
    if "agent_patterns" in p:
        return "agent_patterns"
    if "open_loops" in p:
        return "open_loops"
    if "working_memory" in p:
        return "working_memory"
    if "sessions" in p:
        return "sessions"
    return "unknown"


# ---------------------------------------------------------------------------
# Compaction Engine
# ---------------------------------------------------------------------------


class CompactionEngine:
    """Deterministic compaction engine for Nova-Core memory stores.

    Scans file-backed stores, detects duplicates and near-duplicates,
    performs safe compaction and supersession with provenance preservation.

    Args:
        memory_dir: Override MEMORY/ directory (for testing).
        state_dir: Override STATE/ directory (for testing).
        now: Override current timestamp (for deterministic testing).
    """

    def __init__(
        self,
        memory_dir: Path | None = None,
        state_dir: Path | None = None,
        now: float | None = None,
    ):
        self._memory_dir = memory_dir or _MEMORY_DIR
        self._state_dir = state_dir or _STATE_DIR
        self._now = now

    def _time(self) -> float:
        return self._now if self._now is not None else time.time()

    # -------------------------------------------------------------------
    # Full sweep
    # -------------------------------------------------------------------

    def run_compaction(self, *, dry_run: bool = True) -> CompactionSweepSummary:
        """Run compaction sweep across all eligible stores.

        Args:
            dry_run: If True (default), compute actions but make no changes.

        Returns:
            CompactionSweepSummary with all results.
        """
        from utils.structured_log import TraceContext, slog

        sweep_id = f"compact-{int(self._time())}"
        ctx = TraceContext.new("memory_compactor")
        summary = CompactionSweepSummary(
            sweep_id=sweep_id,
            dry_run=dry_run,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time())),
        )

        workflows = [
            self._compact_workflow_learnings,
            self._compact_working_memory,
        ]

        for workflow in workflows:
            try:
                results = workflow(dry_run=dry_run)
                for r in results:
                    summary.results.append(r)
                    summary.items_examined += 1
                    if r.action == "deduplicated":
                        summary.duplicates_found += 1
                    elif r.action == "compacted":
                        summary.items_compacted += 1
                    elif r.action == "superseded":
                        summary.items_superseded += 1
                    elif r.action == "protected_skip":
                        summary.items_protected += 1
                    elif r.action in ("rejected_ambiguous", "rejected_unsafe"):
                        summary.items_rejected += 1
            except Exception as exc:
                summary.errors.append(f"{workflow.__name__}: {exc}")
                logger.exception("Compaction workflow %s failed", workflow.__name__)

        summary.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time()))

        slog.event(
            "memory.compaction.sweep",
            ctx,
            operation="compaction_sweep",
            sweep_id=sweep_id,
            dry_run=dry_run,
            items_examined=summary.items_examined,
            duplicates_found=summary.duplicates_found,
            items_compacted=summary.items_compacted,
            items_superseded=summary.items_superseded,
            items_protected=summary.items_protected,
            items_rejected=summary.items_rejected,
            error_count=len(summary.errors),
        )

        return summary

    # -------------------------------------------------------------------
    # Workflow: Workflow learnings dedup + supersession
    # -------------------------------------------------------------------

    def _compact_workflow_learnings(self, *, dry_run: bool) -> list[CompactionResult]:
        """Dedup and supersede workflow learning artifacts."""
        results: list[CompactionResult] = []
        wl_dir = self._memory_dir / "workflow_learnings"
        if not wl_dir.exists():
            return results

        # Load all artifacts
        artifacts: list[tuple[Path, dict]] = []
        for f in sorted(wl_dir.glob("mem_*.json")):
            meta = _safe_read_json(f)
            if meta is not None:
                artifacts.append((f, meta))

        if len(artifacts) < 2:
            return results

        # Detect duplicates
        matches = detect_duplicates(artifacts)
        archive_dir = self._memory_dir / "_archive" / "workflow_learnings"

        for match in matches:
            if match.match_class == MatchClass.EXACT_DUPLICATE:
                result = deduplicate_exact(match, archive_dir=archive_dir, dry_run=dry_run)
                results.append(result)

            elif match.match_class == MatchClass.SUPERSESSION_CANDIDATE:
                surviving_path = Path(match.artifact_a_path)
                superseded_path = Path(match.artifact_b_path)
                result = supersede_artifact(surviving_path, superseded_path, dry_run=dry_run)
                results.append(result)

            elif match.match_class == MatchClass.NEAR_DUPLICATE:
                # Near-duplicates: compact if both are thin
                a_meta = _safe_read_json(Path(match.artifact_a_path))
                b_meta = _safe_read_json(Path(match.artifact_b_path))
                if a_meta and b_meta:
                    a_summary = str(a_meta.get("task_summary", "") or a_meta.get("summary", ""))
                    b_summary = str(b_meta.get("task_summary", "") or b_meta.get("summary", ""))
                    if len(a_summary) < _THIN_SUMMARY_LEN and len(b_summary) < _THIN_SUMMARY_LEN:
                        result = compact_group(
                            [(Path(match.artifact_a_path), a_meta), (Path(match.artifact_b_path), b_meta)],
                            archive_dir=archive_dir,
                            dry_run=dry_run,
                        )
                        results.append(result)
                    else:
                        # Not thin enough to compact — supersede instead
                        result = supersede_artifact(
                            Path(match.artifact_a_path),
                            Path(match.artifact_b_path),
                            dry_run=dry_run,
                        )
                        results.append(result)

            elif match.match_class == MatchClass.REJECTED_AMBIGUOUS:
                results.append(
                    CompactionResult(
                        action="rejected_ambiguous",
                        target_store="workflow_learnings",
                        surviving_path=match.artifact_a_path,
                        affected_paths=[match.artifact_b_path],
                        rule_name="near_duplicate_different_types",
                        dry_run=dry_run,
                        match_class=MatchClass.REJECTED_AMBIGUOUS,
                        rejection_reason=match.reason,
                    )
                )

        self._emit_workflow_trace("workflow_learnings", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Workflow: Working memory dedup
    # -------------------------------------------------------------------

    def _compact_working_memory(self, *, dry_run: bool) -> list[CompactionResult]:
        """Deduplicate exact copies in working memory."""
        results: list[CompactionResult] = []
        wm_dir = self._state_dir / "working_memory"
        if not wm_dir.exists():
            return results

        artifacts: list[tuple[Path, dict]] = []
        for f in sorted(wm_dir.glob("wm_*.json")):
            meta = _safe_read_json(f)
            if meta is not None:
                artifacts.append((f, meta))

        if len(artifacts) < 2:
            return results

        matches = detect_duplicates(artifacts)
        archive_dir = self._state_dir / "_archive" / "working_memory"

        for match in matches:
            if match.match_class == MatchClass.EXACT_DUPLICATE:
                result = deduplicate_exact(match, archive_dir=archive_dir, dry_run=dry_run)
                results.append(result)
            # Working memory: only exact dedup, no merge/supersession

        self._emit_workflow_trace("working_memory", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Individual access
    # -------------------------------------------------------------------

    def compact_workflow_learnings(self, *, dry_run: bool = True) -> list[CompactionResult]:
        """Run compaction on workflow learnings only."""
        return self._compact_workflow_learnings(dry_run=dry_run)

    def compact_working_memory(self, *, dry_run: bool = True) -> list[CompactionResult]:
        """Run compaction on working memory only."""
        return self._compact_working_memory(dry_run=dry_run)

    # -------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------

    def _emit_workflow_trace(
        self,
        store_name: str,
        results: list[CompactionResult],
        dry_run: bool,
    ) -> None:
        """Emit structured log for a compaction workflow."""
        from utils.structured_log import TraceContext, slog

        ctx = TraceContext.new("memory_compactor")
        deduped = sum(1 for r in results if r.action == "deduplicated")
        compacted = sum(1 for r in results if r.action == "compacted")
        superseded = sum(1 for r in results if r.action == "superseded")
        protected = sum(1 for r in results if r.action == "protected_skip")

        slog.event(
            "memory.compaction.workflow",
            ctx,
            operation="compaction_workflow",
            target_store=store_name,
            dry_run=dry_run,
            items_examined=len(results),
            duplicates_deduped=deduped,
            items_compacted=compacted,
            items_superseded=superseded,
            items_protected=protected,
        )


# ---------------------------------------------------------------------------
# Router integration convenience
# ---------------------------------------------------------------------------


def run_compaction_sweep(
    *,
    dry_run: bool = True,
    memory_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Convenience function for running compaction from the router or scripts.

    Args:
        dry_run: If True (default), no changes are made.
        memory_dir: Override MEMORY/ path (testing).
        state_dir: Override STATE/ path (testing).

    Returns:
        Dict summary of compaction results.
    """
    engine = CompactionEngine(memory_dir=memory_dir, state_dir=state_dir)
    summary = engine.run_compaction(dry_run=dry_run)
    return summary.to_dict()
