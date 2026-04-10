"""MVP readiness and remediation package for NovaTrade.

Takes a ValidationReport + MvpDecision and produces an operator-facing
ReadinessPackage with actionable remediation guidance.

Remediation mappings are deterministic and explicit — each issue pattern
maps to a specific category, description, and action.

Provider-neutral — only uses NovaTrade-native models.
"""

from __future__ import annotations

from novatrade.models import (
    MvpDecision,
    MvpVerdict,
    ReadinessPackage,
    RemediationItem,
    RemediationSeverity,
    ValidationReport,
)


def build_readiness_package(
    report: ValidationReport,
    decision: MvpDecision,
) -> ReadinessPackage:
    """Build a final readiness package from report and decision.

    Pure function — no I/O, fully deterministic.
    """
    items: list[RemediationItem] = []
    do_not_proceed: list[str] = []
    evidence_gaps: list[str] = []

    # -- Map blockers to remediation items ---------------------------------
    if report.exception_count > 0:
        items.append(
            RemediationItem(
                category="exceptions",
                description=(f"{report.exception_count} unexpected exception(s) during execution"),
                action=(
                    "Check execution logs for stack traces. Fix the root cause "
                    "before retrying. Common causes: import errors, network "
                    "timeouts, unhandled edge cases in adapter translation."
                ),
                severity=RemediationSeverity.BLOCKER,
            )
        )
        do_not_proceed.append("Do not proceed to live-capital validation while exceptions occur.")

    if report.adapter_error_count > 0:
        items.append(
            RemediationItem(
                category="adapter_errors",
                description=(
                    f"{report.adapter_error_count} adapter error(s) — broker "
                    "communication failed during order placement"
                ),
                action=(
                    "Review adapter error details in evidence. Common causes: "
                    "invalid symbol, insufficient margin, broker-side rejection, "
                    "connection timeout. Verify broker account health and "
                    "symbol configuration."
                ),
                severity=RemediationSeverity.BLOCKER,
            )
        )
        do_not_proceed.append("Do not proceed while adapter errors are unresolved — orders may be silently failing.")

    if report.mismatch_count > 0:
        items.append(
            RemediationItem(
                category="reconciliation",
                description=(
                    f"{report.mismatch_count} reconciliation mismatch(es) — expected positions differ from broker state"
                ),
                action=(
                    "Review mismatch details in evidence. Check if positions "
                    "were closed externally, if position IDs differ between "
                    "tracker and broker, or if volume/SL/TP were modified "
                    "outside NovaTrade. Clear tracker state and re-validate."
                ),
                severity=RemediationSeverity.BLOCKER,
            )
        )
        do_not_proceed.append(
            "Do not proceed while position state is divergent — risk calculations may be based on stale data."
        )

    if report.health_snapshots > 0:
        health_failures = report.health_snapshots - report.health_ok_count
        if health_failures > 0:
            failure_ratio = health_failures / report.health_snapshots
            if failure_ratio >= 0.5:
                items.append(
                    RemediationItem(
                        category="health",
                        description=(
                            f"{health_failures}/{report.health_snapshots} health snapshots failed ({failure_ratio:.0%})"
                        ),
                        action=(
                            "Verify adapter connection stability. Check network "
                            "connectivity, API token validity, and account status. "
                            "Run scripts/novatrade_connect_test.py to diagnose."
                        ),
                        severity=RemediationSeverity.BLOCKER,
                    )
                )
            else:
                items.append(
                    RemediationItem(
                        category="health",
                        description=(
                            f"{health_failures}/{report.health_snapshots} health snapshots failed ({failure_ratio:.0%})"
                        ),
                        action=(
                            "Monitor health trend. Intermittent failures may "
                            "indicate network instability or broker maintenance "
                            "windows. Run more validation cycles to confirm."
                        ),
                        severity=RemediationSeverity.WARNING,
                    )
                )

    # -- Map warnings to remediation items ---------------------------------
    if report.execution_count > 0 and report.filled_count == 0:
        if report.denied_count > 0:
            items.append(
                RemediationItem(
                    category="all_denied",
                    description=(f"All {report.denied_count} execution(s) denied by risk gate"),
                    action=(
                        "Verify this is expected behavior (e.g. demo mode). "
                        "If testing live execution, check: "
                        "kill switch state, symbol allowlist, volume bounds, "
                        "stop-loss requirement."
                    ),
                    severity=RemediationSeverity.WARNING,
                )
            )
        else:
            items.append(
                RemediationItem(
                    category="no_fills",
                    description="No orders have been filled",
                    action=("Run at least one demo execution attempt to validate the full gate → adapter → fill path."),
                    severity=RemediationSeverity.WARNING,
                )
            )

    # -- Evidence gaps -----------------------------------------------------
    if report.total_records < 10:
        evidence_gaps.append(
            f"Only {report.total_records} evidence records — more demo runs will increase confidence in stability."
        )

    if report.health_snapshots == 0:
        evidence_gaps.append(
            "No health snapshots recorded. Run 'python3 scripts/novatrade_review.py --live-check' to capture."
        )

    if report.reconciliation_count == 0:
        evidence_gaps.append(
            "No reconciliation runs recorded. Run 'python3 scripts/novatrade_review.py --live-check' to capture."
        )

    if report.execution_count > 0 and report.filled_count == 0:
        evidence_gaps.append(
            "No filled executions — the adapter → fill path is untested. "
            "Consider a live demo execution on a demo account."
        )

    # -- Determine next step -----------------------------------------------
    if decision.verdict == MvpVerdict.NOT_READY:
        next_step = (
            "Fix all blockers, then rerun validation: python3 scripts/novatrade_review.py --live-check --readiness"
        )
    elif decision.verdict == MvpVerdict.NEEDS_REMEDIATION:
        next_step = (
            "Address blockers listed above, then rerun validation: "
            "python3 scripts/novatrade_review.py --live-check --readiness"
        )
    elif decision.verdict == MvpVerdict.INSUFFICIENT_DATA:
        next_step = (
            "Run more demo executions and validation cycles, then reassess: "
            "python3 scripts/novatrade_review.py --readiness"
        )
    else:
        next_step = (
            "MVP demo path is stable. Continue demo validation or proceed "
            "to next phase (live-capital readiness assessment)."
        )

    return ReadinessPackage(
        verdict=decision.verdict,
        ready=decision.verdict == MvpVerdict.READY,
        items=items,
        do_not_proceed=do_not_proceed,
        evidence_gaps=evidence_gaps,
        next_step=next_step,
    )


def format_readiness(package: ReadinessPackage) -> str:
    """Render a ReadinessPackage as a human-readable text summary."""
    lines = [
        "NovaTrade MVP Readiness Package",
        "=" * 44,
        "",
        f"Verdict:    {package.verdict.value}",
        f"Ready:      {'YES' if package.ready else 'NO'}",
        f"Blockers:   {package.blocker_count}",
        f"Warnings:   {package.warning_count}",
        "",
    ]

    # -- Blockers ----------------------------------------------------------
    blockers = [i for i in package.items if i.severity == RemediationSeverity.BLOCKER]
    if blockers:
        lines.append("Blockers (must fix before proceeding)")
        lines.append("-" * 44)
        for item in blockers:
            lines.append(f"  [{item.category}] {item.description}")
            lines.append(f"    Action: {item.action}")
            lines.append("")

    # -- Warnings ----------------------------------------------------------
    warns = [i for i in package.items if i.severity == RemediationSeverity.WARNING]
    if warns:
        lines.append("Warnings (monitor, not blocking)")
        lines.append("-" * 44)
        for item in warns:
            lines.append(f"  [{item.category}] {item.description}")
            lines.append(f"    Action: {item.action}")
            lines.append("")

    # -- Do not proceed ----------------------------------------------------
    if package.do_not_proceed:
        lines.append("Do Not Proceed")
        lines.append("-" * 44)
        for cond in package.do_not_proceed:
            lines.append(f"  [!] {cond}")
        lines.append("")

    # -- Evidence gaps -----------------------------------------------------
    if package.evidence_gaps:
        lines.append("Evidence Gaps")
        lines.append("-" * 44)
        for gap in package.evidence_gaps:
            lines.append(f"  [?] {gap}")
        lines.append("")

    # -- Next step ---------------------------------------------------------
    lines.append("Next Step")
    lines.append("-" * 44)
    lines.append(f"  {package.next_step}")
    lines.append("")

    return "\n".join(lines)
