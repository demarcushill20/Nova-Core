"""MVP go/no-go decision layer for NovaTrade.

Evaluates a ValidationReport and produces an explicit MvpDecision with
verdict, reasons, blockers, warnings, and recommended next action.

Rules are conservative, transparent, and hardcoded for the MVP phase.
All thresholds are defined as module-level constants for easy inspection.

Provider-neutral — only uses NovaTrade-native models.
"""

from __future__ import annotations

from novatrade.models import MvpDecision, MvpVerdict, ValidationReport

# ---------------------------------------------------------------------------
# Thresholds (MVP — hardcoded, operator-readable)
# ---------------------------------------------------------------------------

# Minimum evidence records needed to make any judgment.
MIN_RECORDS = 3

# Minimum execution attempts needed for a READY verdict.
MIN_EXECUTIONS = 1

# Maximum adapter errors allowed for READY.  Any > 0 triggers remediation.
MAX_ADAPTER_ERRORS = 0

# Maximum exceptions allowed for READY.  Any > 0 triggers NOT_READY.
MAX_EXCEPTIONS = 0

# Maximum reconciliation mismatches allowed for READY.
MAX_MISMATCHES = 0

# Health failure ratio that triggers a warning (degraded but not blocking).
HEALTH_FAILURE_WARN_RATIO = 0.3

# Health failure ratio that triggers remediation.
HEALTH_FAILURE_BLOCK_RATIO = 0.5


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


def evaluate_mvp(report: ValidationReport) -> MvpDecision:
    """Evaluate a ValidationReport and produce an MVP verdict.

    Pure function — no I/O, fully deterministic from the report.

    Decision priority (first match wins for verdict escalation):
    1. INSUFFICIENT_DATA — too few records to judge
    2. NOT_READY — exceptions occurred (fundamental instability)
    3. NEEDS_REMEDIATION — adapter errors, mismatches, or health failures
    4. READY — all checks pass
    """
    reasons: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    # -- Insufficient data check -------------------------------------------
    if report.total_records < MIN_RECORDS:
        return MvpDecision(
            verdict=MvpVerdict.INSUFFICIENT_DATA,
            reasons=[
                f"only {report.total_records} evidence record(s) collected (minimum {MIN_RECORDS})",
            ],
            next_action=("Run more demo executions and validation cycles to build a sufficient evidence base."),
        )

    if report.execution_count < MIN_EXECUTIONS:
        return MvpDecision(
            verdict=MvpVerdict.INSUFFICIENT_DATA,
            reasons=[
                f"only {report.execution_count} execution(s) recorded (minimum {MIN_EXECUTIONS})",
            ],
            next_action="Run at least one demo execution attempt.",
        )

    # -- Exception check (NOT_READY) ---------------------------------------
    if report.exception_count > MAX_EXCEPTIONS:
        blockers.append(f"{report.exception_count} unexpected exception(s) — indicates fundamental instability")
        reasons.append("exceptions occurred during execution")

    # -- Adapter error check (NEEDS_REMEDIATION) ---------------------------
    if report.adapter_error_count > MAX_ADAPTER_ERRORS:
        blockers.append(f"{report.adapter_error_count} adapter error(s) — broker communication failed")
        reasons.append("adapter returned errors during execution")

    # -- Reconciliation mismatch check (NEEDS_REMEDIATION) -----------------
    if report.mismatch_count > MAX_MISMATCHES:
        blockers.append(
            f"{report.mismatch_count} reconciliation mismatch(es) — expected positions differ from broker state"
        )
        reasons.append("position state diverged from broker")

    # -- Health failure check (WARNING or NEEDS_REMEDIATION) ---------------
    if report.health_snapshots > 0:
        health_failures = report.health_snapshots - report.health_ok_count
        failure_ratio = health_failures / report.health_snapshots

        if failure_ratio >= HEALTH_FAILURE_BLOCK_RATIO:
            blockers.append(
                f"{health_failures}/{report.health_snapshots} health snapshots "
                f"failed ({failure_ratio:.0%}) — exceeds {HEALTH_FAILURE_BLOCK_RATIO:.0%} threshold"
            )
            reasons.append("adapter health is unreliable")
        elif failure_ratio >= HEALTH_FAILURE_WARN_RATIO:
            warnings.append(
                f"{health_failures}/{report.health_snapshots} health snapshots "
                f"failed ({failure_ratio:.0%}) — approaching failure threshold"
            )

    # -- Execution pattern warnings ----------------------------------------
    if report.execution_count > 0 and report.filled_count == 0:
        warnings.append("no orders have been filled — all executions were denied or failed")

    if report.denied_count > 0 and report.filled_count == 0:
        warnings.append(
            f"all {report.denied_count} execution(s) denied by risk gate — verify this is expected (e.g. dry_run mode)"
        )

    # -- Determine verdict -------------------------------------------------
    if report.exception_count > MAX_EXCEPTIONS:
        verdict = MvpVerdict.NOT_READY
    elif blockers:
        verdict = MvpVerdict.NEEDS_REMEDIATION
    else:
        verdict = MvpVerdict.READY
        reasons.append("all checks passed — system appears stable for demo validation")

    # -- Determine next action ---------------------------------------------
    if verdict == MvpVerdict.NOT_READY:
        next_action = (
            "Investigate and fix the root cause of exceptions before "
            "continuing demo validation. Check logs for stack traces."
        )
    elif verdict == MvpVerdict.NEEDS_REMEDIATION:
        next_action = "Address the blocking issues listed above, then rerun validation cycles to confirm stability."
    else:
        next_action = (
            "Continue demo validation. When confident, proceed to next phase (live-capital readiness assessment)."
        )

    return MvpDecision(
        verdict=verdict,
        reasons=reasons,
        blockers=blockers,
        warnings=warnings,
        next_action=next_action,
    )


def format_decision(decision: MvpDecision) -> str:
    """Render an MvpDecision as a human-readable text summary."""
    lines = [
        "NovaTrade MVP Decision",
        "=" * 40,
        "",
        f"Verdict: {decision.verdict.value}",
        "",
    ]

    if decision.reasons:
        lines.append("Reasons")
        lines.append("-" * 40)
        for r in decision.reasons:
            lines.append(f"  - {r}")
        lines.append("")

    if decision.blockers:
        lines.append("Blockers")
        lines.append("-" * 40)
        for b in decision.blockers:
            lines.append(f"  [!] {b}")
        lines.append("")

    if decision.warnings:
        lines.append("Warnings")
        lines.append("-" * 40)
        for w in decision.warnings:
            lines.append(f"  [~] {w}")
        lines.append("")

    lines.append("Next Action")
    lines.append("-" * 40)
    lines.append(f"  {decision.next_action}")
    lines.append("")

    return "\n".join(lines)
