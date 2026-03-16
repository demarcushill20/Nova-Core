"""Validation report generator for NovaTrade MVP.

Reads evidence records and produces a structured ValidationReport that
answers key operator questions about system behavior.

Provider-neutral — only uses NovaTrade-native models.
"""

from __future__ import annotations

from novatrade.models import (
    EvidenceRecord,
    EvidenceType,
    ValidationReport,
)

# Maximum number of recent failures to include in the report.
_MAX_RECENT_FAILURES = 10


def generate_report(records: list[EvidenceRecord]) -> ValidationReport:
    """Build a ValidationReport from a list of evidence records.

    Pure function — no I/O.  Empty input produces a zero-valued report.
    """
    if not records:
        return ValidationReport()

    report = ValidationReport(total_records=len(records))

    timestamps = [r.timestamp for r in records if r.timestamp > 0]
    if timestamps:
        report.time_range_start = min(timestamps)
        report.time_range_end = max(timestamps)

    failures: list[dict] = []

    for rec in records:
        if rec.event_type == EvidenceType.EXECUTION:
            _process_execution(rec, report, failures)
        elif rec.event_type == EvidenceType.HEALTH_SNAPSHOT:
            _process_health(rec, report, failures)
        elif rec.event_type == EvidenceType.RECONCILIATION:
            _process_reconciliation(rec, report, failures)
        elif rec.event_type == EvidenceType.RISK_DECISION:
            # Standalone risk decisions (not part of an execution)
            pass
        elif rec.event_type == EvidenceType.ADAPTER_ERROR:
            report.adapter_error_count += 1
            failures.append(
                {
                    "type": "ADAPTER_ERROR",
                    "error": rec.error,
                    "timestamp": rec.timestamp,
                }
            )

    # Keep only the most recent failures.
    failures.sort(key=lambda f: f.get("timestamp", 0), reverse=True)
    report.recent_failures = failures[:_MAX_RECENT_FAILURES]

    return report


def format_report(report: ValidationReport) -> str:
    """Render a ValidationReport as a human-readable text summary."""
    lines = [
        "NovaTrade MVP Validation Report",
        "=" * 40,
        "",
    ]

    if report.total_records == 0:
        lines.append("No evidence records found.")
        return "\n".join(lines)

    lines.append(f"Total records:        {report.total_records}")
    lines.append("")

    # Time range
    if report.time_range_start > 0:
        import datetime as _dt

        start = _dt.datetime.fromtimestamp(report.time_range_start).strftime("%Y-%m-%d %H:%M:%S")
        end = _dt.datetime.fromtimestamp(report.time_range_end).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"Time range:           {start} — {end}")
        lines.append("")

    # Execution summary
    lines.append("Execution Summary")
    lines.append("-" * 40)
    lines.append(f"  Attempts:           {report.execution_count}")
    lines.append(f"  Filled:             {report.filled_count}")
    lines.append(f"  Denied (risk gate): {report.denied_count}")
    lines.append(f"  Adapter errors:     {report.adapter_error_count}")
    lines.append(f"  Exceptions:         {report.exception_count}")
    lines.append("")

    # Health summary
    lines.append("Health Snapshots")
    lines.append("-" * 40)
    lines.append(f"  Total:              {report.health_snapshots}")
    lines.append(f"  OK:                 {report.health_ok_count}")
    lines.append("")

    # Reconciliation summary
    lines.append("Reconciliation")
    lines.append("-" * 40)
    lines.append(f"  Runs:               {report.reconciliation_count}")
    lines.append(f"  OK:                 {report.reconciliation_ok_count}")
    lines.append(f"  Mismatches:         {report.mismatch_count}")
    lines.append("")

    # Stability verdict
    lines.append("Stability Verdict")
    lines.append("-" * 40)
    if report.stable:
        lines.append("  STABLE — no errors, no mismatches")
    else:
        reasons = []
        if report.total_records == 0:
            reasons.append("no evidence collected")
        if report.adapter_error_count > 0:
            reasons.append(f"{report.adapter_error_count} adapter error(s)")
        if report.exception_count > 0:
            reasons.append(f"{report.exception_count} exception(s)")
        if report.mismatch_count > 0:
            reasons.append(f"{report.mismatch_count} reconciliation mismatch(es)")
        lines.append(f"  UNSTABLE — {'; '.join(reasons) if reasons else 'insufficient data'}")
    lines.append("")

    # Recent failures
    if report.recent_failures:
        lines.append(f"Recent Failures (last {len(report.recent_failures)})")
        lines.append("-" * 40)
        for f in report.recent_failures:
            lines.append(f"  [{f.get('type', '?')}] {f.get('error', f.get('detail', 'unknown'))}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal processors
# ---------------------------------------------------------------------------


def _process_execution(
    rec: EvidenceRecord,
    report: ValidationReport,
    failures: list[dict],
) -> None:
    report.execution_count += 1
    outcome = rec.data.get("outcome", "")

    if outcome == "FILLED":
        report.filled_count += 1
    elif outcome == "DENIED":
        report.denied_count += 1
    elif outcome == "ADAPTER_ERROR":
        report.adapter_error_count += 1
        failures.append(
            {
                "type": "EXECUTION_ADAPTER_ERROR",
                "error": rec.error or rec.data.get("order_status", "unknown"),
                "timestamp": rec.timestamp,
            }
        )
    elif outcome == "EXCEPTION":
        report.exception_count += 1
        failures.append(
            {
                "type": "EXECUTION_EXCEPTION",
                "error": rec.error,
                "timestamp": rec.timestamp,
            }
        )


def _process_health(
    rec: EvidenceRecord,
    report: ValidationReport,
    failures: list[dict],
) -> None:
    report.health_snapshots += 1
    if rec.data.get("ok"):
        report.health_ok_count += 1
    else:
        failures.append(
            {
                "type": "HEALTH_FAILURE",
                "error": rec.error or rec.data.get("state", "unknown"),
                "timestamp": rec.timestamp,
            }
        )


def _process_reconciliation(
    rec: EvidenceRecord,
    report: ValidationReport,
    failures: list[dict],
) -> None:
    report.reconciliation_count += 1
    mismatch_count = rec.data.get("mismatch_count", 0)
    report.mismatch_count += mismatch_count

    if rec.data.get("ok"):
        report.reconciliation_ok_count += 1
    else:
        detail = f"{mismatch_count} mismatch(es)"
        mismatches = rec.data.get("mismatches", [])
        if mismatches:
            detail = "; ".join(f"{m.get('type', '?')} {m.get('symbol', '?')}" for m in mismatches[:3])
        failures.append(
            {
                "type": "RECONCILIATION_MISMATCH",
                "detail": detail,
                "error": detail,
                "timestamp": rec.timestamp,
            }
        )
