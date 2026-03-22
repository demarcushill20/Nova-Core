"""Pipeline result reporting — terminal-friendly, JSON, and campaign formats.

Produces structured reports from PipelineResult for operator review,
logging, and downstream consumption.

Enhanced in P7.3 with:
- ``campaign_report()`` — comprehensive campaign analysis
- ``report_to_file()`` — save report to OUTPUT/ with timestamp
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Campaign report (P7.3)
# ---------------------------------------------------------------------------


def campaign_report(
    result: PipelineResult,
    *,
    stage_c_results: list[dict[str, Any]] | None = None,
    durability_grades: list[dict[str, Any]] | None = None,
    param_distributions: dict[str, dict[str, Any]] | None = None,
    mutation_lineage: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a comprehensive campaign report.

    Includes the standard pipeline report plus additional analysis sections:

    - Executive summary (total experiments, pass rate, best score)
    - Stage C gate results for top survivors
    - Durability grades for promoted strategies
    - Parameter distribution analysis (what ranges produce winners)
    - Mutation lineage for best configs (L1/L2/L3 mutations that led to best)

    Args:
        result: The PipelineResult from a campaign run.
        stage_c_results: Optional list of Stage C evaluation dicts.
        durability_grades: Optional list of durability grade dicts.
        param_distributions: Optional param name -> stats mapping.
        mutation_lineage: Optional mutation history for best experiments.

    Returns:
        A multi-section text report.
    """
    lines: list[str] = []

    # --- Executive Summary ---
    lines.append("=" * 70)
    lines.append("  NovaTrade Campaign Report")
    lines.append("=" * 70)
    lines.append("")
    lines.append("  EXECUTIVE SUMMARY")
    lines.append("  " + "-" * 40)
    lines.append(f"  Total Experiments:  {result.total_experiments}")
    if result.total_experiments > 0:
        passed = int(result.gate_pass_rate * result.total_experiments)
        lines.append(f"  Gate Pass Count:    {passed}")
    lines.append(f"  Gate Pass Rate:     {result.gate_pass_rate * 100:.1f}%")
    lines.append(f"  Best Score:         {result.best_score:.4f}")
    if result.best_experiment_id:
        lines.append(f"  Best Experiment:    {result.best_experiment_id}")
    if result.doctrine_name:
        lines.append(f"  Doctrine:           {result.doctrine_name}")
    lines.append(f"  Mode:               {result.mode.value.upper()}")

    # Stage timing summary
    total_ms = sum(s.duration_ms for s in result.stages)
    lines.append(f"  Total Duration:     {_format_duration(total_ms)}")
    error_count = sum(1 for s in result.stages if s.status == "error")
    warning_count = sum(1 for s in result.stages if s.status == "warning")
    if error_count:
        lines.append(f"  Stage Errors:       {error_count}")
    if warning_count:
        lines.append(f"  Stage Warnings:     {warning_count}")
    lines.append("")

    # --- Top Survivors ---
    if result.top_survivors:
        lines.append("  TOP SURVIVORS")
        lines.append("  " + "-" * 40)
        lines.append(f"  {'#':<3} {'Experiment ID':<22} {'Score':>8} {'PF':>6} {'MaxDD':>7} {'Trades':>7}")
        lines.append(f"  {'-' * 3} {'-' * 22} {'-' * 8} {'-' * 6} {'-' * 7} {'-' * 7}")
        for i, s in enumerate(result.top_survivors[:10], start=1):
            exp_id = _truncate(str(s.get("experiment_id", "?")), 22)
            score = s.get("score", 0.0)
            pf = s.get("profit_factor")
            dd = s.get("max_dd")
            tc = s.get("trade_count")
            score_str = f"{score:.4f}" if score is not None else "N/A"
            pf_str = f"{pf:.2f}" if pf is not None else "N/A"
            dd_str = f"{dd:.2f}%" if dd is not None else "N/A"
            tc_str = str(tc) if tc is not None else "N/A"
            lines.append(f"  {i:<3} {exp_id:<22} {score_str:>8} {pf_str:>6} {dd_str:>7} {tc_str:>7}")
        lines.append("")

    # --- Stage C Gate Results ---
    if stage_c_results:
        lines.append("  STAGE C GATE RESULTS")
        lines.append("  " + "-" * 40)
        for sc in stage_c_results:
            exp_id = sc.get("experiment_id", "?")
            passed = sc.get("passed", False)
            reason = sc.get("reason", "")
            status_str = "PASS" if passed else "FAIL"
            line = f"  {exp_id}: {status_str}"
            if reason:
                line += f" ({reason})"
            lines.append(line)
        lines.append("")

    # --- Durability Grades ---
    if durability_grades:
        lines.append("  DURABILITY GRADES")
        lines.append("  " + "-" * 40)
        lines.append(f"  {'Experiment ID':<22} {'Grade':>6} {'Score':>8} {'Detail'}")
        lines.append(f"  {'-' * 22} {'-' * 6} {'-' * 8} {'-' * 20}")
        for dg in durability_grades:
            exp_id = _truncate(str(dg.get("experiment_id", "?")), 22)
            grade = dg.get("grade", "?")
            score = dg.get("score", 0.0)
            detail = dg.get("detail", "")
            score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
            lines.append(f"  {exp_id:<22} {grade:>6} {score_str:>8} {detail}")
        lines.append("")

    # --- Parameter Distribution Analysis ---
    if param_distributions:
        lines.append("  PARAMETER DISTRIBUTION ANALYSIS")
        lines.append("  " + "-" * 40)
        lines.append("  (Ranges that produce winning configurations)")
        lines.append("")
        lines.append(f"  {'Parameter':<25} {'Min':>8} {'Max':>8} {'Mean':>8} {'Winners':>8}")
        lines.append(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")
        for param, stats in sorted(param_distributions.items()):
            pmin = stats.get("min", 0.0)
            pmax = stats.get("max", 0.0)
            pmean = stats.get("mean", 0.0)
            count = stats.get("winner_count", 0)
            lines.append(f"  {param:<25} {pmin:>8.3f} {pmax:>8.3f} {pmean:>8.3f} {count:>8}")
        lines.append("")

    # --- Mutation Lineage ---
    if mutation_lineage:
        lines.append("  MUTATION LINEAGE (Best Configs)")
        lines.append("  " + "-" * 40)
        for ml in mutation_lineage:
            exp_id = ml.get("experiment_id", "?")
            level = ml.get("mutation_level", "?")
            parent = ml.get("parent_id", "?")
            delta = ml.get("score_delta", 0.0)
            delta_str = f"{delta:+.4f}" if isinstance(delta, (int, float)) else "N/A"
            lines.append(f"  {exp_id}: L{level} from {parent} (delta={delta_str})")
        lines.append("")

    # --- Errors ---
    if result.errors:
        lines.append("  ERRORS")
        lines.append("  " + "-" * 40)
        for err in result.errors:
            lines.append(f"  - {err}")
        lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File output (P7.3)
# ---------------------------------------------------------------------------


def report_to_file(
    result: PipelineResult,
    output_dir: Path | str,
    *,
    report_format: str = "text",
    stage_c_results: list[dict[str, Any]] | None = None,
    durability_grades: list[dict[str, Any]] | None = None,
    param_distributions: dict[str, dict[str, Any]] | None = None,
    mutation_lineage: list[dict[str, Any]] | None = None,
) -> Path:
    """Save a pipeline report to a timestamped file in *output_dir*.

    Args:
        result: The PipelineResult to report.
        output_dir: Directory to write the report (e.g. OUTPUT/).
        report_format: ``"text"`` for terminal/markdown, ``"json"`` for JSON,
            ``"campaign"`` for the comprehensive campaign format.
        stage_c_results: For campaign format only.
        durability_grades: For campaign format only.
        param_distributions: For campaign format only.
        mutation_lineage: For campaign format only.

    Returns:
        Path to the written file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    mode = result.mode.value

    if report_format == "json":
        content = format_pipeline_json(result)
        filename = f"pipeline_report_{mode}_{ts}.json"
    elif report_format == "campaign":
        content = campaign_report(
            result,
            stage_c_results=stage_c_results,
            durability_grades=durability_grades,
            param_distributions=param_distributions,
            mutation_lineage=mutation_lineage,
        )
        filename = f"campaign_report_{ts}.txt"
    else:
        content = format_pipeline_report(result)
        filename = f"pipeline_report_{mode}_{ts}.txt"

    out_path = output_dir / filename

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=str(output_dir), suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1  # mark as closed
        os.replace(tmp_path, str(out_path))
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return out_path
