"""Pipeline result reporting — terminal-friendly and JSON formats.

Produces structured reports from PipelineResult for operator review,
logging, and downstream consumption.
"""

from __future__ import annotations

import json
from typing import Any

from novatrade.pipeline.orchestrator import PipelineResult, PipelineStage

# ---------------------------------------------------------------------------
# Status symbols for terminal display
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[str, str] = {
    "ok": "[OK]",
    "warning": "[WARN]",
    "error": "[ERR]",
    "skipped": "[SKIP]",
}


# ---------------------------------------------------------------------------
# Markdown / terminal report
# ---------------------------------------------------------------------------


def format_pipeline_report(result: PipelineResult) -> str:
    """Format PipelineResult as a terminal-friendly markdown report.

    Sections:
        1. Header (mode, doctrine, total experiments)
        2. Stage timeline (name, status, duration)
        3. Gate pass rate breakdown
        4. Top survivors table
        5. Data quality summary
        6. Error summary
    """
    lines: list[str] = []

    # --- Header ---
    lines.append("=" * 60)
    lines.append("  NovaTrade Pipeline Report")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"  Mode:         {result.mode.value.upper()}")
    if result.doctrine_name:
        lines.append(f"  Doctrine:     {result.doctrine_name}")
    lines.append(f"  Experiments:  {result.total_experiments}")
    lines.append(f"  Best Score:   {result.best_score:.4f}")
    if result.best_experiment_id:
        lines.append(f"  Best ID:      {result.best_experiment_id}")
    lines.append(f"  Gate Pass:    {result.gate_pass_rate * 100:.1f}%")
    lines.append("")

    # --- Stage timeline ---
    lines.append("-" * 60)
    lines.append("  Stage Timeline")
    lines.append("-" * 60)
    lines.append(f"  {'Stage':<24} {'Status':<10} {'Duration':>10}")
    lines.append(f"  {'-' * 24} {'-' * 10} {'-' * 10}")

    total_ms = 0
    for stage in result.stages:
        icon = _STATUS_ICON.get(stage.status, f"[{stage.status}]")
        duration_str = _format_duration(stage.duration_ms)
        lines.append(f"  {stage.name:<24} {icon:<10} {duration_str:>10}")
        total_ms += stage.duration_ms

    lines.append(f"  {'-' * 24} {'-' * 10} {'-' * 10}")
    lines.append(f"  {'TOTAL':<24} {'':10} {_format_duration(total_ms):>10}")
    lines.append("")

    # --- Gate pass rate breakdown ---
    lines.append("-" * 60)
    lines.append("  Gate Pass Rate")
    lines.append("-" * 60)
    if result.total_experiments > 0:
        passed = int(result.gate_pass_rate * result.total_experiments)
        failed = result.total_experiments - passed
        lines.append(f"  Passed:  {passed}/{result.total_experiments}")
        lines.append(f"  Failed:  {failed}/{result.total_experiments}")
        lines.append(f"  Rate:    {result.gate_pass_rate * 100:.1f}%")
    else:
        lines.append("  No experiments executed.")
    lines.append("")

    # --- Top survivors table ---
    if result.top_survivors:
        lines.append("-" * 60)
        lines.append("  Top Survivors")
        lines.append("-" * 60)

        # Header row
        lines.append(f"  {'#':<3} {'Experiment ID':<20} {'Score':>7} {'PF':>6} {'MaxDD':>7} {'Trades':>7}")
        lines.append(f"  {'-' * 3} {'-' * 20} {'-' * 7} {'-' * 6} {'-' * 7} {'-' * 7}")

        for i, s in enumerate(result.top_survivors[:5], start=1):
            exp_id = _truncate(str(s.get("experiment_id", "?")), 20)
            score = s.get("score", 0.0)
            pf = s.get("profit_factor")
            dd = s.get("max_dd")
            tc = s.get("trade_count")

            score_str = f"{score:.4f}" if score is not None else "N/A"
            pf_str = f"{pf:.2f}" if pf is not None else "N/A"
            dd_str = f"{dd:.2f}%" if dd is not None else "N/A"
            tc_str = str(tc) if tc is not None else "N/A"

            lines.append(f"  {i:<3} {exp_id:<20} {score_str:>7} {pf_str:>6} {dd_str:>7} {tc_str:>7}")
        lines.append("")

    # --- Data quality summary ---
    if result.data_quality_issues > 0:
        lines.append("-" * 60)
        lines.append("  Data Quality")
        lines.append("-" * 60)
        lines.append(f"  Issues found: {result.data_quality_issues}")
        # Check stage details for specifics
        for stage in result.stages:
            if stage.name == "validate_data" and stage.details:
                if "fixes_applied" in stage.details:
                    lines.append(f"  Fixes applied: {stage.details['fixes_applied']}")
                if "issue_list" in stage.details:
                    for issue in stage.details["issue_list"][:5]:
                        lines.append(f"    - {issue}")
        lines.append("")

    # --- Error summary ---
    if result.errors:
        lines.append("-" * 60)
        lines.append("  Errors")
        lines.append("-" * 60)
        for err in result.errors:
            lines.append(f"  - {err}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------


def format_pipeline_json(result: PipelineResult) -> str:
    """Format PipelineResult as a JSON string.

    All fields are serialised, including stage details and top survivors.
    Suitable for logging, API responses, and downstream ingestion.
    """
    data: dict[str, Any] = {
        "mode": result.mode.value,
        "doctrine_name": result.doctrine_name,
        "total_experiments": result.total_experiments,
        "gate_pass_rate": round(result.gate_pass_rate, 4),
        "best_score": round(result.best_score, 6),
        "best_experiment_id": result.best_experiment_id,
        "data_quality_issues": result.data_quality_issues,
        "top_survivors": _serialise_survivors(result.top_survivors),
        "stages": [_serialise_stage(s) for s in result.stages],
        "errors": result.errors,
    }
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_stage(stage: PipelineStage) -> dict[str, Any]:
    """Convert a PipelineStage to a JSON-safe dict."""
    return {
        "name": stage.name,
        "status": stage.status,
        "duration_ms": stage.duration_ms,
        "details": stage.details,
    }


def _serialise_survivors(survivors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure all survivor values are JSON-serialisable."""
    safe: list[dict[str, Any]] = []
    for s in survivors:
        entry: dict[str, Any] = {}
        for k, v in s.items():
            is_nan = isinstance(v, float) and (v != v)
            is_inf = isinstance(v, float) and v in (float("inf"), float("-inf"))
            if is_nan or is_inf:
                entry[k] = None
            else:
                entry[k] = v
        safe.append(entry)
    return safe


def _format_duration(ms: int) -> str:
    """Format milliseconds as human-readable duration string."""
    if ms < 1000:
        return f"{ms}ms"
    elif ms < 60_000:
        return f"{ms / 1000:.1f}s"
    else:
        minutes = ms // 60_000
        seconds = (ms % 60_000) / 1000
        return f"{minutes}m {seconds:.0f}s"


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if longer than max_len."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
