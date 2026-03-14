"""Phase 10 — Memory Metrics Collection & Health Reporting.

Deterministic metrics collection from file-backed stores, structured logs,
and router return types. Produces snapshot reports for memory health
observability.

This module does NOT:
- Query Fusion Memory (prompt-delegated, not callable from Python)
- Query Obsidian vault (MCP-managed)
- Invent fake precision for unmeasurable metrics
- Modify any memory artifacts
- Require external infrastructure

Usage:
    from agents.memory_metrics import (
        MemoryMetrics, collect_metrics, generate_health_report,
    )

    metrics = collect_metrics()
    report = generate_health_report(metrics)
"""

from __future__ import annotations

import json
import logging
import os
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
_STRUCTURED_LOG = _LOGS_DIR / "structured.jsonl"

_DAY = 86400


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LayerCounts:
    """Artifact counts by memory layer."""

    working: int = 0
    episodic: int = 0
    semantic: int = 0  # Not countable from file system
    procedural: int = 0

    def total(self) -> int:
        return self.working + self.episodic + self.semantic + self.procedural

    def to_dict(self) -> dict[str, int]:
        d = asdict(self)
        d["total"] = self.total()
        return d


@dataclass
class OpenLoopCounts:
    """Open loop status distribution."""

    proposed: int = 0
    open: int = 0
    blocked: int = 0
    deferred: int = 0
    resolved: int = 0
    closed_rejected: int = 0
    stale: int = 0

    def active(self) -> int:
        return self.proposed + self.open + self.blocked + self.deferred

    def terminal(self) -> int:
        return self.resolved + self.closed_rejected

    def total(self) -> int:
        return self.active() + self.terminal() + self.stale

    def to_dict(self) -> dict[str, int]:
        d = asdict(self)
        d["active"] = self.active()
        d["terminal"] = self.terminal()
        d["total"] = self.total()
        return d


@dataclass
class SlogCounters:
    """Counters derived from structured log events."""

    store_calls: int = 0
    store_successes: int = 0
    store_rejections: int = 0
    recall_calls: int = 0
    recall_with_results: int = 0
    recall_empty: int = 0
    promotion_eligible: int = 0
    consolidation_calls: int = 0
    consolidation_successes: int = 0
    governance_sweeps: int = 0
    compaction_sweeps: int = 0
    open_loops_created: int = 0
    open_loops_resolved: int = 0
    schema_validation_failures: int = 0

    def rejection_rate(self) -> float:
        if self.store_calls == 0:
            return 0.0
        return self.store_rejections / self.store_calls

    def recall_success_rate(self) -> float:
        if self.recall_calls == 0:
            return 0.0
        return self.recall_with_results / self.recall_calls

    def promotion_rate(self) -> float:
        if self.store_calls == 0:
            return 0.0
        return self.promotion_eligible / self.store_calls

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rejection_rate"] = round(self.rejection_rate(), 4)
        d["recall_success_rate"] = round(self.recall_success_rate(), 4)
        d["promotion_rate"] = round(self.promotion_rate(), 4)
        return d


@dataclass
class MemoryMetrics:
    """Complete memory health metrics snapshot."""

    collected_at: str = ""
    layer_counts: LayerCounts = field(default_factory=LayerCounts)
    open_loop_counts: OpenLoopCounts = field(default_factory=OpenLoopCounts)
    slog_counters: SlogCounters = field(default_factory=SlogCounters)
    file_counts: dict[str, int] = field(default_factory=dict)
    session_count: int = 0
    notification_count: int = 0
    structured_log_size_bytes: int = 0
    blind_spots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected_at": self.collected_at,
            "layer_counts": self.layer_counts.to_dict(),
            "open_loop_counts": self.open_loop_counts.to_dict(),
            "slog_counters": self.slog_counters.to_dict(),
            "file_counts": self.file_counts,
            "session_count": self.session_count,
            "notification_count": self.notification_count,
            "structured_log_size_bytes": self.structured_log_size_bytes,
            "blind_spots": self.blind_spots,
        }


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def _count_json_files(directory: Path, pattern: str = "*.json") -> int:
    """Count JSON files in a directory."""
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


def _count_files(directory: Path) -> int:
    """Count all files in a directory."""
    if not directory.exists():
        return 0
    return sum(1 for f in directory.iterdir() if f.is_file())


def collect_layer_counts(
    state_dir: Path | None = None,
    memory_dir: Path | None = None,
) -> LayerCounts:
    """Count artifacts by memory layer from file system."""
    state = state_dir or _STATE_DIR
    memory = memory_dir or _MEMORY_DIR

    return LayerCounts(
        working=_count_json_files(state / "working_memory", "wm_*.json"),
        episodic=_count_json_files(memory / "workflow_learnings", "mem_*.json"),
        semantic=0,  # Cannot count — Fusion Memory / Obsidian are external
        procedural=_count_json_files(memory / "agent_patterns", "mem_*.json"),
    )


def collect_open_loop_counts(
    state_dir: Path | None = None,
) -> OpenLoopCounts:
    """Count open loops by status."""
    state = state_dir or _STATE_DIR
    loop_dir = state / "open_loops"
    counts = OpenLoopCounts()

    if not loop_dir.exists():
        return counts

    for f in loop_dir.glob("loop_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            status = data.get("status", "")
            if hasattr(counts, status):
                setattr(counts, status, getattr(counts, status) + 1)
        except (OSError, json.JSONDecodeError):
            pass

    return counts


def collect_slog_counters(
    log_path: Path | None = None,
    max_lines: int = 50000,
) -> SlogCounters:
    """Parse structured log to extract event counters.

    Args:
        log_path: Path to structured.jsonl (default: LOGS/structured.jsonl).
        max_lines: Safety cap on lines to parse.

    Returns:
        SlogCounters with event-based metrics.
    """
    path = log_path or _STRUCTURED_LOG
    counters = SlogCounters()

    if not path.exists():
        return counters

    try:
        lines_read = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if lines_read >= max_lines:
                    break
                lines_read += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event = entry.get("event", "")
                data = entry.get("data", {})

                if event == "memory.router.store":
                    counters.store_calls += 1
                    if data.get("stored") is True:
                        counters.store_successes += 1
                    elif data.get("stored") is False:
                        counters.store_rejections += 1
                    if data.get("validation_errors"):
                        counters.schema_validation_failures += 1
                    if data.get("promotion_eligibility") == "eligible":
                        counters.promotion_eligible += 1

                elif event == "memory.router.recall":
                    counters.recall_calls += 1
                    # Router emits "results_count"; support legacy "total_found" too
                    found = data.get("results_count") or data.get("total_found") or 0
                    if found > 0:
                        counters.recall_with_results += 1
                    else:
                        counters.recall_empty += 1

                elif event == "memory.router.track_open_loop" or event == "memory.router.detect_open_loop":
                    if data.get("loop_action") == "loop_created":
                        counters.open_loops_created += 1

                elif event == "memory.router.resolve_open_loop":
                    if "resolved" in str(data.get("loop_action", "")):
                        counters.open_loops_resolved += 1

                elif event == "memory.router.consolidate":
                    counters.consolidation_calls += 1
                    if data.get("action") in ("summary_created", "checkpoint_created"):
                        counters.consolidation_successes += 1

                elif event == "memory.governance.sweep":
                    counters.governance_sweeps += 1

                elif event == "memory.compaction.sweep":
                    counters.compaction_sweeps += 1

    except OSError as exc:
        logger.warning("Failed to read structured log: %s", exc)

    return counters


def collect_metrics(
    state_dir: Path | None = None,
    memory_dir: Path | None = None,
    logs_dir: Path | None = None,
) -> MemoryMetrics:
    """Collect a complete metrics snapshot.

    Args:
        state_dir: Override STATE/ directory.
        memory_dir: Override MEMORY/ directory.
        logs_dir: Override LOGS/ directory.

    Returns:
        MemoryMetrics snapshot.
    """
    state = state_dir or _STATE_DIR
    memory = memory_dir or _MEMORY_DIR
    logs = logs_dir or _LOGS_DIR
    log_path = logs / "structured.jsonl"

    metrics = MemoryMetrics(
        collected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        layer_counts=collect_layer_counts(state_dir=state, memory_dir=memory),
        open_loop_counts=collect_open_loop_counts(state_dir=state),
        slog_counters=collect_slog_counters(log_path=log_path),
        file_counts={
            "workflow_learnings": _count_json_files(memory / "workflow_learnings", "mem_*.json"),
            "agent_patterns": _count_json_files(memory / "agent_patterns", "mem_*.json"),
            "open_loops": _count_json_files(state / "open_loops", "loop_*.json"),
            "working_memory": _count_json_files(state / "working_memory", "wm_*.json"),
            "sessions": _count_json_files(state / "sessions", "ses_*.json"),
        },
        session_count=_count_json_files(state / "sessions", "ses_*.json"),
        notification_count=_count_files(state / "notified") if (state / "notified").exists() else 0,
        structured_log_size_bytes=log_path.stat().st_size if log_path.exists() else 0,
        blind_spots=[
            "Fusion Memory items not countable (prompt-delegated)",
            "Obsidian vault items not countable (MCP-managed)",
            "Recall latency not instrumented",
            "Semantic layer count unavailable from file system",
        ],
    )

    return metrics


# ---------------------------------------------------------------------------
# Health report generation
# ---------------------------------------------------------------------------


def generate_health_report(metrics: MemoryMetrics | None = None) -> str:
    """Generate a markdown health report from metrics.

    Args:
        metrics: Pre-collected metrics, or None to collect fresh.

    Returns:
        Markdown string suitable for writing to a file.
    """
    if metrics is None:
        metrics = collect_metrics()

    lc = metrics.layer_counts
    olc = metrics.open_loop_counts
    sc = metrics.slog_counters

    lines = [
        "# Memory Health Dashboard",
        "",
        f"Generated: {metrics.collected_at}",
        "",
        "---",
        "",
        "## Layer Distribution",
        "",
        "| Layer | Count |",
        "|-------|-------|",
        f"| Working | {lc.working} |",
        f"| Episodic | {lc.episodic} |",
        f"| Semantic | {lc.semantic} (not measurable from file system) |",
        f"| Procedural | {lc.procedural} |",
        f"| **Total (file-backed)** | **{lc.total()}** |",
        "",
        "## Store File Counts",
        "",
        "| Store | Count |",
        "|-------|-------|",
    ]

    for store, count in sorted(metrics.file_counts.items()):
        lines.append(f"| {store} | {count} |")

    lines += [
        "",
        "## Open Loops",
        "",
        "| Status | Count |",
        "|--------|-------|",
        f"| Proposed | {olc.proposed} |",
        f"| Open | {olc.open} |",
        f"| Blocked | {olc.blocked} |",
        f"| Deferred | {olc.deferred} |",
        f"| Stale | {olc.stale} |",
        f"| Resolved | {olc.resolved} |",
        f"| Closed/Rejected | {olc.closed_rejected} |",
        f"| **Active** | **{olc.active()}** |",
        f"| **Terminal** | **{olc.terminal()}** |",
        "",
        "## Event Counters (from structured log)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Store calls | {sc.store_calls} |",
        f"| Store successes | {sc.store_successes} |",
        f"| Store rejections | {sc.store_rejections} |",
        f"| **Rejection rate** | **{sc.rejection_rate():.1%}** |",
        f"| Recall calls | {sc.recall_calls} |",
        f"| Recall with results | {sc.recall_with_results} |",
        f"| **Recall success rate** | **{sc.recall_success_rate():.1%}** |",
        f"| Promotion eligible | {sc.promotion_eligible} |",
        f"| **Promotion rate** | **{sc.promotion_rate():.1%}** |",
        f"| Consolidation calls | {sc.consolidation_calls} |",
        f"| Consolidation successes | {sc.consolidation_successes} |",
        f"| Governance sweeps | {sc.governance_sweeps} |",
        f"| Compaction sweeps | {sc.compaction_sweeps} |",
        f"| Open loops created | {sc.open_loops_created} |",
        f"| Open loops resolved | {sc.open_loops_resolved} |",
        f"| Schema validation failures | {sc.schema_validation_failures} |",
        "",
        "## Operational Health",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Structured log size | {metrics.structured_log_size_bytes:,} bytes |",
        f"| Session files | {metrics.session_count} |",
        f"| Notification files | {metrics.notification_count} |",
        "",
        "## Known Blind Spots",
        "",
    ]
    for bs in metrics.blind_spots:
        lines.append(f"- {bs}")

    return "\n".join(lines) + "\n"
