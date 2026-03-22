"""Phase 8 — Memory Governance, Retention, and Hygiene.

Deterministic governance pipeline for Nova-Core's file-backed memory stores.
Evaluates retention rules, marks stale artifacts, prunes terminal objects,
archives old operational state, and protects high-value memory — all with
dry-run support, structured audit logging, and conservative defaults.

This module does NOT:
- Touch Fusion Memory (prompt-delegated, not callable from Python)
- Modify Obsidian vault files (MCP-managed, read-only to governance)
- Delete protected memory without explicit override
- Silently remove any file without logging
- Apply opaque heuristics — every decision traces to a named rule

Usage:
    from agents.memory_governance import (
        GovernanceEngine, GovernanceAction, GovernanceResult,
        RetentionRule, ProtectionLevel,
    )

    engine = GovernanceEngine()
    results = engine.run_sweep(dry_run=True)
    for r in results:
        print(r.action, r.target_path, r.rule_name)
"""

from __future__ import annotations

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
_STATE_DIR = _BASE / "STATE"
_MEMORY_DIR = _BASE / "MEMORY"
_LOGS_DIR = _BASE / "LOGS"

# Retention thresholds (seconds)
_DAY = 86400
WORKING_MEMORY_MAX_AGE = 7 * _DAY
SESSION_STALE_AGE = 30 * _DAY
SESSION_PRUNE_AGE = 60 * _DAY
OPEN_LOOP_STALE_AGE = 14 * _DAY
OPEN_LOOP_TERMINAL_PRUNE_AGE = 30 * _DAY
OPEN_LOOP_STALE_PRUNE_AGE = 60 * _DAY
OPERATIONAL_STATE_ARCHIVE_AGE = 30 * _DAY
NOTIFICATION_PRUNE_AGE = 14 * _DAY
LOG_ROTATION_PRUNE_AGE = 30 * _DAY
EPISODIC_ARCHIVE_AGE = 90 * _DAY

# ---------------------------------------------------------------------------
# Enums / Types
# ---------------------------------------------------------------------------

VALID_GOVERNANCE_ACTIONS = frozenset(
    {
        "no_action",
        "marked_stale",
        "compacted",
        "archived",
        "pruned",
        "protected_skip",
        "rejected_unsafe",
        "rejected_insufficient_evidence",
    }
)


class ProtectionLevel:
    """Protection classification for governance decisions."""

    PROTECTED = "protected"  # Never auto-modified
    HIGH = "high"  # Operator-managed, governance skips
    MEDIUM = "medium"  # Long-term, compaction possible with evidence
    LOW = "low"  # Standard retention rules apply
    NONE = "none"  # Fully governed by retention rules


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RetentionRule:
    """A single retention rule that can fire during governance sweep."""

    name: str
    description: str
    target_store: str  # e.g., "open_loops", "sessions", "working_memory"
    max_age_seconds: int
    action: str  # from VALID_GOVERNANCE_ACTIONS
    protection_check: bool = True  # Whether to check protection before acting
    min_age_seconds: int = 0  # Minimum age before rule can fire


@dataclass
class GovernanceResult:
    """Result of a single governance decision on one target."""

    action: str
    target_store: str
    target_path: str
    rule_name: str
    dry_run: bool
    protection_level: str = ProtectionLevel.NONE
    rejection_reason: str = ""
    detail: str = ""
    artifact_age_days: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SweepSummary:
    """Aggregate summary of a governance sweep."""

    sweep_id: str
    dry_run: bool
    started_at: str
    completed_at: str = ""
    items_examined: int = 0
    items_acted_on: int = 0
    items_protected: int = 0
    items_skipped: int = 0
    results: list[GovernanceResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["results"] = [r.to_dict() for r in self.results]
        return d


# ---------------------------------------------------------------------------
# Protection classification
# ---------------------------------------------------------------------------


def classify_protection(path: Path, metadata: dict | None = None) -> str:
    """Classify the protection level of a file-backed artifact.

    Protection is determined by:
    1. Explicit `protected: true` in artifact metadata
    2. Store/path location (procedural stores are always protected)
    3. Content characteristics (active open loops, high importance)

    Args:
        path: Path to the artifact file.
        metadata: Optional parsed JSON metadata from the artifact.

    Returns:
        ProtectionLevel string.
    """
    path_str = str(path)

    # Explicit protection flag in metadata
    if metadata and metadata.get("protected"):
        return ProtectionLevel.PROTECTED

    # Procedural layer stores — always protected
    if "agent_patterns" in path_str:
        return ProtectionLevel.PROTECTED

    # Config and budgets — always protected
    if "/config/" in path_str or "/budgets/" in path_str:
        return ProtectionLevel.PROTECTED

    # Open loops — active ones are protected
    if "open_loops" in path_str and metadata:
        status = metadata.get("status", "")
        if status in ("proposed", "open", "blocked", "deferred"):
            return ProtectionLevel.PROTECTED
        if status == "stale":
            return ProtectionLevel.LOW
        # Terminal: resolved, closed_rejected
        return ProtectionLevel.NONE

    # Episodic artifacts — medium protection
    if "workflow_learnings" in path_str:
        importance = 0.0
        if metadata:
            importance = metadata.get("importance", metadata.get("scores", {}).get("importance", 0.0))
            if isinstance(importance, str):
                try:
                    importance = float(importance)
                except ValueError:
                    importance = 0.0
        if importance >= 0.5:
            return ProtectionLevel.MEDIUM
        return ProtectionLevel.LOW

    # Operator-authored
    if metadata and metadata.get("source") == "operator":
        return ProtectionLevel.HIGH

    # Default: governed by retention rules
    return ProtectionLevel.NONE


def is_protected(level: str) -> bool:
    """Check if a protection level prevents governance actions."""
    return level in (ProtectionLevel.PROTECTED, ProtectionLevel.HIGH)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _file_age_seconds(path: Path) -> float:
    """Get file age in seconds from mtime."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


def _file_age_days(path: Path) -> float:
    """Get file age in days from mtime."""
    return _file_age_seconds(path) / _DAY


def _safe_read_json(path: Path) -> dict | None:
    """Read JSON from a file, returning None on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _archive_file(path: Path, archive_dir: Path) -> str:
    """Move a file to an archive directory, preserving provenance in name.

    Returns the archive path string, or empty string on failure.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    # Encode original path into archive filename to preserve provenance
    try:
        safe_name = str(path.relative_to(_BASE)).replace("/", "__")
    except ValueError:
        # Path is outside _BASE (e.g., in test tmp dirs) — use filename only
        safe_name = path.name
    dest = archive_dir / safe_name
    # Handle collisions
    if dest.exists():
        dest = archive_dir / f"{int(time.time())}_{safe_name}"
    try:
        shutil.move(str(path), str(dest))
        return str(dest)
    except OSError as exc:
        logger.warning("Failed to archive %s: %s", path, exc)
        return ""


def _prune_file(path: Path) -> bool:
    """Delete a file. Returns True on success."""
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("Failed to prune %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Governance Engine
# ---------------------------------------------------------------------------


class GovernanceEngine:
    """Deterministic governance engine for Nova-Core memory stores.

    Evaluates retention rules against file-backed stores, produces
    explicit action decisions, supports dry-run mode, and emits
    structured audit results.

    Args:
        state_dir: Override STATE/ directory (for testing).
        memory_dir: Override MEMORY/ directory (for testing).
        logs_dir: Override LOGS/ directory (for testing).
        now: Override current timestamp (for deterministic testing).
    """

    def __init__(
        self,
        state_dir: Path | None = None,
        memory_dir: Path | None = None,
        logs_dir: Path | None = None,
        now: float | None = None,
    ):
        self._state_dir = state_dir or _STATE_DIR
        self._memory_dir = memory_dir or _MEMORY_DIR
        self._logs_dir = logs_dir or _LOGS_DIR
        self._now = now  # If set, used instead of time.time()

    def _time(self) -> float:
        return self._now if self._now is not None else time.time()

    def _age_seconds(self, path: Path) -> float:
        try:
            return self._time() - path.stat().st_mtime
        except OSError:
            return 0.0

    def _age_days(self, path: Path) -> float:
        return self._age_seconds(path) / _DAY

    # -------------------------------------------------------------------
    # Full sweep
    # -------------------------------------------------------------------

    def run_sweep(self, *, dry_run: bool = True) -> SweepSummary:
        """Run a full governance sweep across all managed stores.

        Args:
            dry_run: If True (default), evaluate rules but make no changes.

        Returns:
            SweepSummary with all results and aggregate counts.
        """
        from utils.structured_log import TraceContext, slog

        sweep_id = f"gov-{int(self._time())}"
        ctx = TraceContext.new("memory_governance")
        summary = SweepSummary(
            sweep_id=sweep_id,
            dry_run=dry_run,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time())),
        )

        # Run each workflow, collect results
        workflows = [
            self._sweep_open_loops,
            self._sweep_sessions,
            self._sweep_working_memory,
            self._sweep_notifications,
            self._sweep_rotated_logs,
            self._sweep_operational_state,
            self._sweep_workflow_learnings,
        ]

        for workflow in workflows:
            try:
                results = workflow(dry_run=dry_run)
                for r in results:
                    summary.results.append(r)
                    summary.items_examined += 1
                    if r.action in ("pruned", "archived", "marked_stale", "compacted"):
                        summary.items_acted_on += 1
                    elif r.action == "protected_skip":
                        summary.items_protected += 1
                    elif r.action == "no_action":
                        summary.items_skipped += 1
            except Exception as exc:
                summary.errors.append(f"{workflow.__name__}: {exc}")
                logger.exception("Governance workflow %s failed", workflow.__name__)

        summary.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time()))

        # Emit aggregate trace
        slog.event(
            "memory.governance.sweep",
            ctx,
            operation="governance_sweep",
            sweep_id=sweep_id,
            dry_run=dry_run,
            items_examined=summary.items_examined,
            items_acted_on=summary.items_acted_on,
            items_protected=summary.items_protected,
            items_skipped=summary.items_skipped,
            error_count=len(summary.errors),
        )

        return summary

    # -------------------------------------------------------------------
    # Workflow: Open loops
    # -------------------------------------------------------------------

    def _sweep_open_loops(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for STATE/open_loops/ — stale marking + terminal pruning."""
        results: list[GovernanceResult] = []
        loop_dir = self._state_dir / "open_loops"
        if not loop_dir.exists():
            return results

        for f in sorted(loop_dir.glob("loop_*.json")):
            metadata = _safe_read_json(f)
            if metadata is None:
                continue

            status = metadata.get("status", "")
            protection = classify_protection(f, metadata)
            age = self._age_seconds(f)

            # Active loops: check for staleness
            if status in ("proposed", "open", "blocked", "deferred"):
                updated_at = metadata.get("updated_at", "")
                # Use updated_at if available, else fall back to file mtime
                loop_age = age
                if updated_at:
                    try:
                        import datetime

                        dt = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        loop_age = self._time() - dt.timestamp()
                    except (ValueError, AttributeError):
                        pass

                if loop_age >= OPEN_LOOP_STALE_AGE:
                    result = GovernanceResult(
                        action="marked_stale",
                        target_store="open_loops",
                        target_path=str(f),
                        rule_name="open_loop_stale_14d",
                        dry_run=dry_run,
                        protection_level=protection,
                        artifact_age_days=loop_age / _DAY,
                        detail=f"Loop {metadata.get('loop_id', '?')} status={status} → stale",
                    )
                    if not dry_run:
                        self._mark_loop_stale(f, metadata)
                    results.append(result)
                else:
                    results.append(
                        GovernanceResult(
                            action="protected_skip",
                            target_store="open_loops",
                            target_path=str(f),
                            rule_name="open_loop_active_protected",
                            dry_run=dry_run,
                            protection_level=protection,
                            artifact_age_days=loop_age / _DAY,
                        )
                    )
                continue

            # Stale loops: prune after 60 days
            if status == "stale" and age >= OPEN_LOOP_STALE_PRUNE_AGE:
                result = GovernanceResult(
                    action="archived",
                    target_store="open_loops",
                    target_path=str(f),
                    rule_name="open_loop_stale_archive_60d",
                    dry_run=dry_run,
                    protection_level=protection,
                    artifact_age_days=age / _DAY,
                    detail="Stale loop archived after 60d",
                )
                if not dry_run:
                    archive_dir = self._state_dir / "_archive" / "open_loops"
                    _archive_file(f, archive_dir)
                results.append(result)
                continue

            # Terminal loops: prune after 30 days
            if status in ("resolved", "closed_rejected") and age >= OPEN_LOOP_TERMINAL_PRUNE_AGE:
                result = GovernanceResult(
                    action="archived",
                    target_store="open_loops",
                    target_path=str(f),
                    rule_name="open_loop_terminal_archive_30d",
                    dry_run=dry_run,
                    protection_level=protection,
                    artifact_age_days=age / _DAY,
                    detail=f"Terminal loop ({status}) archived after 30d",
                )
                if not dry_run:
                    archive_dir = self._state_dir / "_archive" / "open_loops"
                    _archive_file(f, archive_dir)
                results.append(result)
                continue

            # Otherwise: no action
            results.append(
                GovernanceResult(
                    action="no_action",
                    target_store="open_loops",
                    target_path=str(f),
                    rule_name="open_loop_within_retention",
                    dry_run=dry_run,
                    protection_level=protection,
                    artifact_age_days=age / _DAY,
                )
            )

        self._emit_workflow_trace("open_loops", results, dry_run)
        return results

    def _mark_loop_stale(self, path: Path, metadata: dict) -> None:
        """Mark an open loop as stale via the open_loop_tracker."""
        try:
            from agents.open_loop_tracker import LoopStore, OpenLoop, mark_stale

            store = LoopStore(base=path.parent)
            loop = OpenLoop.from_dict(metadata)
            mark_stale([loop], store=store)
        except Exception as exc:
            logger.warning("Failed to mark loop stale via tracker: %s", exc)
            # Fallback: direct file update
            try:
                metadata["status"] = "stale"
                metadata["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time()))
                history = metadata.get("history", [])
                history.append(
                    {
                        "from": metadata.get("status", "unknown"),
                        "to": "stale",
                        "timestamp": metadata["updated_at"],
                        "reason": "governance_sweep_stale_14d",
                    }
                )
                metadata["history"] = history
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(metadata, indent=2, default=str))
                tmp.rename(path)
            except OSError:
                logger.warning("Fallback stale marking also failed for %s", path)

    # -------------------------------------------------------------------
    # Workflow: Sessions
    # -------------------------------------------------------------------

    def _sweep_sessions(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for STATE/sessions/ — archive old sessions."""
        results: list[GovernanceResult] = []
        session_dir = self._state_dir / "sessions"
        if not session_dir.exists():
            return results

        for f in sorted(session_dir.glob("ses_*.json")):
            age = self._age_seconds(f)
            protection = classify_protection(f)

            if age >= SESSION_PRUNE_AGE:
                result = GovernanceResult(
                    action="archived",
                    target_store="sessions",
                    target_path=str(f),
                    rule_name="session_archive_60d",
                    dry_run=dry_run,
                    protection_level=protection,
                    artifact_age_days=age / _DAY,
                )
                if not dry_run:
                    archive_dir = self._state_dir / "_archive" / "sessions"
                    _archive_file(f, archive_dir)
                results.append(result)
            elif age >= SESSION_STALE_AGE:
                results.append(
                    GovernanceResult(
                        action="marked_stale",
                        target_store="sessions",
                        target_path=str(f),
                        rule_name="session_stale_30d",
                        dry_run=dry_run,
                        protection_level=protection,
                        artifact_age_days=age / _DAY,
                    )
                )
            else:
                results.append(
                    GovernanceResult(
                        action="no_action",
                        target_store="sessions",
                        target_path=str(f),
                        rule_name="session_within_retention",
                        dry_run=dry_run,
                        protection_level=protection,
                        artifact_age_days=age / _DAY,
                    )
                )

        self._emit_workflow_trace("sessions", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Workflow: Working memory (supplemental to existing auto-cleanup)
    # -------------------------------------------------------------------

    def _sweep_working_memory(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for STATE/working_memory/ — verify cleanup is effective."""
        results: list[GovernanceResult] = []
        wm_dir = self._state_dir / "working_memory"
        if not wm_dir.exists():
            return results

        for f in sorted(wm_dir.glob("wm_*.json")):
            age = self._age_seconds(f)
            if age >= WORKING_MEMORY_MAX_AGE:
                # WorkingMemoryAdapter should have cleaned this up already.
                # If it's still here, prune it.
                result = GovernanceResult(
                    action="pruned",
                    target_store="working_memory",
                    target_path=str(f),
                    rule_name="working_memory_prune_7d",
                    dry_run=dry_run,
                    artifact_age_days=age / _DAY,
                    detail="Stale working memory file missed by adapter cleanup",
                )
                if not dry_run:
                    _prune_file(f)
                results.append(result)
            else:
                results.append(
                    GovernanceResult(
                        action="no_action",
                        target_store="working_memory",
                        target_path=str(f),
                        rule_name="working_memory_within_retention",
                        dry_run=dry_run,
                        artifact_age_days=age / _DAY,
                    )
                )

        self._emit_workflow_trace("working_memory", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Workflow: Notifications
    # -------------------------------------------------------------------

    def _sweep_notifications(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for STATE/notified/ — prune old notifications."""
        results: list[GovernanceResult] = []
        notif_dir = self._state_dir / "notified"
        if not notif_dir.exists():
            return results

        for f in sorted(notif_dir.iterdir()):
            if not f.is_file():
                continue
            age = self._age_seconds(f)
            if age >= NOTIFICATION_PRUNE_AGE:
                result = GovernanceResult(
                    action="pruned",
                    target_store="notifications",
                    target_path=str(f),
                    rule_name="notification_prune_14d",
                    dry_run=dry_run,
                    artifact_age_days=age / _DAY,
                )
                if not dry_run:
                    _prune_file(f)
                results.append(result)

        self._emit_workflow_trace("notifications", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Workflow: Rotated structured logs
    # -------------------------------------------------------------------

    def _sweep_rotated_logs(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for LOGS/structured.*.jsonl — prune old rotations."""
        results: list[GovernanceResult] = []
        if not self._logs_dir.exists():
            return results

        for f in sorted(self._logs_dir.glob("structured.*.jsonl")):
            age = self._age_seconds(f)
            if age >= LOG_ROTATION_PRUNE_AGE:
                result = GovernanceResult(
                    action="pruned",
                    target_store="rotated_logs",
                    target_path=str(f),
                    rule_name="rotated_log_prune_30d",
                    dry_run=dry_run,
                    artifact_age_days=age / _DAY,
                )
                if not dry_run:
                    _prune_file(f)
                results.append(result)

        self._emit_workflow_trace("rotated_logs", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Workflow: Operational state archives
    # -------------------------------------------------------------------

    def _sweep_operational_state(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for STATE/ operational directories — archive old files."""
        results: list[GovernanceResult] = []
        archivable_dirs = ["workflows", "plans", "improvement_runs", "intents"]

        for dirname in archivable_dirs:
            target_dir = self._state_dir / dirname
            if not target_dir.exists():
                continue

            for f in sorted(target_dir.glob("*.json")):
                age = self._age_seconds(f)
                protection = classify_protection(f)

                if is_protected(protection):
                    results.append(
                        GovernanceResult(
                            action="protected_skip",
                            target_store=dirname,
                            target_path=str(f),
                            rule_name=f"operational_{dirname}_protected",
                            dry_run=dry_run,
                            protection_level=protection,
                            artifact_age_days=age / _DAY,
                        )
                    )
                    continue

                if age >= OPERATIONAL_STATE_ARCHIVE_AGE:
                    result = GovernanceResult(
                        action="archived",
                        target_store=dirname,
                        target_path=str(f),
                        rule_name=f"operational_{dirname}_archive_30d",
                        dry_run=dry_run,
                        protection_level=protection,
                        artifact_age_days=age / _DAY,
                    )
                    if not dry_run:
                        archive_dir = self._state_dir / "_archive" / dirname
                        _archive_file(f, archive_dir)
                    results.append(result)

        self._emit_workflow_trace("operational_state", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Workflow: Workflow learnings (episodic artifacts)
    # -------------------------------------------------------------------

    def _sweep_workflow_learnings(self, *, dry_run: bool) -> list[GovernanceResult]:
        """Governance for MEMORY/workflow_learnings/ — thin artifact detection."""
        results: list[GovernanceResult] = []
        wl_dir = self._memory_dir / "workflow_learnings"
        if not wl_dir.exists():
            return results

        for f in sorted(wl_dir.glob("mem_*.json")):
            metadata = _safe_read_json(f)
            protection = classify_protection(f, metadata)
            age = self._age_seconds(f)

            if is_protected(protection) or protection == ProtectionLevel.MEDIUM:
                results.append(
                    GovernanceResult(
                        action="protected_skip",
                        target_store="workflow_learnings",
                        target_path=str(f),
                        rule_name="episodic_protected",
                        dry_run=dry_run,
                        protection_level=protection,
                        artifact_age_days=age / _DAY,
                    )
                )
                continue

            # Detect thin artifacts: very small files with minimal content
            if metadata is not None:
                summary = metadata.get("task_summary", "") or metadata.get("summary", "")
                is_thin = len(summary) < 20
                is_old = age >= EPISODIC_ARCHIVE_AGE

                if is_thin and is_old:
                    result = GovernanceResult(
                        action="archived",
                        target_store="workflow_learnings",
                        target_path=str(f),
                        rule_name="episodic_thin_archive_90d",
                        dry_run=dry_run,
                        protection_level=protection,
                        artifact_age_days=age / _DAY,
                        detail=f"Thin artifact (summary {len(summary)} chars) + >90d old",
                    )
                    if not dry_run:
                        archive_dir = self._memory_dir / "_archive" / "workflow_learnings"
                        _archive_file(f, archive_dir)
                    results.append(result)
                    continue

            results.append(
                GovernanceResult(
                    action="no_action",
                    target_store="workflow_learnings",
                    target_path=str(f),
                    rule_name="episodic_within_retention",
                    dry_run=dry_run,
                    protection_level=protection,
                    artifact_age_days=age / _DAY,
                )
            )

        self._emit_workflow_trace("workflow_learnings", results, dry_run)
        return results

    # -------------------------------------------------------------------
    # Individual governance actions (callable outside sweep)
    # -------------------------------------------------------------------

    def govern_open_loops(self, *, dry_run: bool = True) -> list[GovernanceResult]:
        """Run governance on open loops only."""
        return self._sweep_open_loops(dry_run=dry_run)

    def govern_sessions(self, *, dry_run: bool = True) -> list[GovernanceResult]:
        """Run governance on sessions only."""
        return self._sweep_sessions(dry_run=dry_run)

    def govern_working_memory(self, *, dry_run: bool = True) -> list[GovernanceResult]:
        """Run governance on working memory only."""
        return self._sweep_working_memory(dry_run=dry_run)

    # -------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------

    def _emit_workflow_trace(
        self,
        store_name: str,
        results: list[GovernanceResult],
        dry_run: bool,
    ) -> None:
        """Emit structured log for a governance workflow."""
        from utils.structured_log import TraceContext, slog

        ctx = TraceContext.new("memory_governance")
        acted = sum(1 for r in results if r.action in ("pruned", "archived", "marked_stale", "compacted"))
        protected = sum(1 for r in results if r.action == "protected_skip")

        slog.event(
            "memory.governance.workflow",
            ctx,
            operation="governance_workflow",
            target_store=store_name,
            dry_run=dry_run,
            items_examined=len(results),
            items_acted_on=acted,
            items_protected=protected,
        )


# ---------------------------------------------------------------------------
# Router integration helper
# ---------------------------------------------------------------------------


def run_governance_sweep(
    *,
    dry_run: bool = True,
    state_dir: Path | None = None,
    memory_dir: Path | None = None,
    logs_dir: Path | None = None,
) -> dict[str, Any]:
    """Convenience function for running governance from the router or scripts.

    Args:
        dry_run: If True, no changes are made.
        state_dir: Override STATE/ path (testing).
        memory_dir: Override MEMORY/ path (testing).
        logs_dir: Override LOGS/ path (testing).

    Returns:
        Dict summary of sweep results.
    """
    engine = GovernanceEngine(
        state_dir=state_dir,
        memory_dir=memory_dir,
        logs_dir=logs_dir,
    )
    summary = engine.run_sweep(dry_run=dry_run)
    return summary.to_dict()
