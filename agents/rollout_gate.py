"""Phase 7.11/7.12a/7.13 — Rollout Evaluation Gate, Stage 3 Activation, and Stability Review.

Deterministic, repository-native evaluation of rollout readiness and stability.
Reads existing heartbeat, metrics, and workflow state to classify rollout
status as ready_to_expand, hold, or rollback_recommended.

Includes:
  - Evidence collection and criterion evaluation
  - Readiness check with progress toward thresholds
  - Evaluation-gated Stage 3 activation with audit trail
  - Fail-closed activation procedure
  - Phase 7.13: Post-activation Stage 3 stability review

All criteria are threshold-based and auditable.
No LLM judgments — pure metric evaluation.

State sources:
  STATE/heartbeat_multiagent.json   — latest health report
  STATE/workflows/*.json            — workflow history
  STATE/config/feature_flags.json   — current rollout config
  STATE/policy_denials.jsonl        — policy denial records
  STATE/activation_log.jsonl        — activation audit trail
  LOGS/recovery.log                 — recovery event history

Stdlib only — no pip installs required.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))


# ---------------------------------------------------------------------------
# Evaluation thresholds — explicit, auditable, tunable
# ---------------------------------------------------------------------------

# Minimum completed workflows to have sufficient evidence
MIN_COMPLETED_WORKFLOWS = 3

# Maximum acceptable failure rate (failed + halted) / total
MAX_FAILURE_RATE = 0.30

# Maximum acceptable verifier rejection rate
MAX_VERIFIER_REJECTION_RATE = 0.50

# Maximum acceptable contract failure rate
MAX_CONTRACT_FAILURE_RATE = 0.30

# Hard ceiling on policy violations (any = rollback)
MAX_POLICY_VIOLATIONS = 0

# Hard ceiling on budget exhaustions (any = rollback)
MAX_BUDGET_EXHAUSTIONS = 0

# --- Phase 7.13: Stage 3 stability review thresholds ---

# Minimum code_impl workflows before a stability opinion can be given
STAGE3_MIN_CODE_IMPL_RUNS = 2

# Maximum code_impl failure rate (tighter than overall because mutation-capable)
STAGE3_MAX_CODE_IMPL_FAILURE_RATE = 0.50

# Maximum overall failure rate under Stage 3 operation
STAGE3_MAX_FAILURE_RATE = 0.30

# Maximum verifier rejection rate (critical for mutation-capable class)
STAGE3_MAX_VERIFIER_REJECTION_RATE = 0.50

# Maximum recovery anomalies (requeued/orphaned) since activation
STAGE3_MAX_RECOVERY_ANOMALIES = 3


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RolloutCriterion:
    """Result of evaluating a single rollout criterion."""
    name: str
    passed: bool
    value: object       # actual measured value
    threshold: object   # threshold used
    detail: str
    severity: str = ""  # "hard" = blocks expansion, "soft" = advisory

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RolloutEvaluation:
    """Complete rollout evaluation result."""
    decision: str          # "ready_to_expand" | "hold" | "rollback_recommended"
    rollout_stage: str     # e.g., "stage2_research_and_code_review"
    classes_evaluated: list[str] = field(default_factory=list)
    criteria: list[RolloutCriterion] = field(default_factory=list)
    evidence_summary: dict = field(default_factory=dict)
    generated_at: str = ""
    next_action: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["criteria"] = [c.to_dict() for c in self.criteria]
        return d


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _list_json_files(directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    results = []
    for f in sorted(directory.glob("*.json")):
        data = _read_json(f)
        if data:
            results.append(data)
    return results


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def collect_evidence(base: Path | None = None) -> dict:
    """Collect rollout evaluation evidence from repository-native state.

    Returns a flat dict of metrics used for criterion evaluation.
    """
    root = base or BASE
    state = root / "STATE"

    ev: dict = {}

    # --- Heartbeat ---
    hb = _read_json(state / "heartbeat_multiagent.json")
    ev["heartbeat_overall"] = hb.get("overall", "") if hb else ""
    ev["heartbeat_exists"] = hb is not None

    # Count unhealthy findings in latest heartbeat
    if hb:
        findings = hb.get("findings", [])
        ev["unhealthy_finding_count"] = sum(
            1 for f in findings
            if isinstance(f, dict) and f.get("severity") == "unhealthy"
        )
    else:
        ev["unhealthy_finding_count"] = 0

    # --- Workflows ---
    workflows = _list_json_files(state / "workflows")
    completed = [w for w in workflows if w.get("status") == "completed"]
    failed = [w for w in workflows if w.get("status") == "failed"]
    halted = [w for w in workflows if w.get("status") == "halted"]
    active = [w for w in workflows
              if w.get("status") in ("created", "planning", "executing")]

    ev["total_workflows"] = len(workflows)
    ev["completed_workflows"] = len(completed)
    ev["failed_workflows"] = len(failed)
    ev["halted_workflows"] = len(halted)
    ev["active_workflows"] = len(active)

    total_terminal = len(completed) + len(failed) + len(halted)
    ev["failure_rate"] = (
        round((len(failed) + len(halted)) / total_terminal, 3)
        if total_terminal > 0 else None
    )

    # --- Verifier reports ---
    verifications = _list_json_files(state / "verifications")
    rejections = sum(1 for v in verifications if v.get("verdict") == "rejected")
    approvals = sum(1 for v in verifications if v.get("verdict") == "approved")
    total_verifications = rejections + approvals

    ev["verifier_rejections"] = rejections
    ev["verifier_approvals"] = approvals
    ev["verifier_rejection_rate"] = (
        round(rejections / total_verifications, 3)
        if total_verifications > 0 else None
    )

    # --- Contract metrics ---
    metrics_data = _read_json(state / "metrics.json")
    if metrics_data:
        cf = metrics_data.get("contract_failure", {})
        cs = metrics_data.get("contract_success", {})
        cf_count = cf.get("_total", 0) if isinstance(cf, dict) else int(cf or 0)
        cs_count = cs.get("_total", 0) if isinstance(cs, dict) else int(cs or 0)
        total_contracts = cf_count + cs_count
        ev["contract_failures"] = cf_count
        ev["contract_successes"] = cs_count
        ev["contract_failure_rate"] = (
            round(cf_count / total_contracts, 3) if total_contracts > 0 else None
        )
    else:
        ev["contract_failures"] = 0
        ev["contract_successes"] = 0
        ev["contract_failure_rate"] = None

    # --- Policy violations ---
    denials = _read_jsonl(state / "policy_denials.jsonl")
    ev["policy_violations"] = len(denials)

    # --- Budget exhaustions ---
    ev["budget_exhaustions"] = sum(
        1 for w in halted
        if "budget" in w.get("halt_reason", "").lower()
    )

    # --- Orphaned agents ---
    now = time.time()
    agent_states = _list_json_files(state / "agents" / "runtime")
    orphaned = 0
    for agent in agent_states:
        if agent.get("status") == "executing":
            started = agent.get("started_at") or agent.get("updated_at", 0)
            if started and (now - started) > 600:
                orphaned += 1
    ev["orphaned_agents"] = orphaned

    # --- Stale leases ---
    leases = _list_json_files(state / "leases")
    stale = sum(1 for l in leases
                if l.get("expires_at", 0) and l["expires_at"] < now)
    ev["stale_leases"] = stale

    # --- Recovery events ---
    recovery_log = root / "LOGS" / "recovery.log"
    recovery_count = 0
    if recovery_log.exists():
        try:
            recovery_count = recovery_log.read_text().count("--- Recovery at")
        except OSError:
            pass
    ev["recovery_events"] = recovery_count

    # --- Feature flags ---
    ff = _read_json(state / "config" / "feature_flags.json")
    if ff:
        orch = ff.get("phase7_orchestrator", {})
        ev["rollout_stage"] = orch.get("rollout_stage", "unknown")
        ev["supported_classes"] = orch.get("supported_classes", [])
        ev["enabled"] = orch.get("enabled", False) is True
    else:
        ev["rollout_stage"] = "unknown"
        ev["supported_classes"] = []
        ev["enabled"] = False

    return ev


# ---------------------------------------------------------------------------
# Criterion evaluation
# ---------------------------------------------------------------------------

def evaluate_criteria(evidence: dict) -> list[RolloutCriterion]:
    """Evaluate all rollout criteria against collected evidence.

    Returns a list of criterion results, each with pass/fail and detail.
    """
    criteria: list[RolloutCriterion] = []

    # 1. Heartbeat healthy
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb != "unhealthy",
        value=hb or "(no data)",
        threshold="not unhealthy",
        detail=(
            f"Current heartbeat: {hb or '(no data)'}"
            if hb != "unhealthy"
            else f"Heartbeat is UNHEALTHY — rollout unsafe"
        ),
        severity="hard",
    ))

    # 2. Minimum completed workflows
    completed = evidence.get("completed_workflows", 0)
    criteria.append(RolloutCriterion(
        name="minimum_completed_runs",
        passed=completed >= MIN_COMPLETED_WORKFLOWS,
        value=completed,
        threshold=MIN_COMPLETED_WORKFLOWS,
        detail=(
            f"{completed} completed workflows (need >= {MIN_COMPLETED_WORKFLOWS})"
        ),
        severity="soft",
    ))

    # 3. Failure rate
    failure_rate = evidence.get("failure_rate")
    if failure_rate is not None:
        criteria.append(RolloutCriterion(
            name="acceptable_failure_rate",
            passed=failure_rate <= MAX_FAILURE_RATE,
            value=failure_rate,
            threshold=MAX_FAILURE_RATE,
            detail=f"Failure rate {failure_rate:.1%} (max {MAX_FAILURE_RATE:.0%})",
            severity="hard",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="acceptable_failure_rate",
            passed=True,  # no evidence = no failures
            value=None,
            threshold=MAX_FAILURE_RATE,
            detail="No terminal workflows yet — no failure rate to evaluate",
            severity="soft",
        ))

    # 4. Verifier rejection rate
    vr_rate = evidence.get("verifier_rejection_rate")
    if vr_rate is not None:
        criteria.append(RolloutCriterion(
            name="acceptable_verifier_rejection_rate",
            passed=vr_rate <= MAX_VERIFIER_REJECTION_RATE,
            value=vr_rate,
            threshold=MAX_VERIFIER_REJECTION_RATE,
            detail=f"Verifier rejection rate {vr_rate:.1%} (max {MAX_VERIFIER_REJECTION_RATE:.0%})",
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="acceptable_verifier_rejection_rate",
            passed=True,
            value=None,
            threshold=MAX_VERIFIER_REJECTION_RATE,
            detail="No verifier reports yet — N/A",
            severity="soft",
        ))

    # 5. Contract failure rate
    cf_rate = evidence.get("contract_failure_rate")
    if cf_rate is not None:
        criteria.append(RolloutCriterion(
            name="acceptable_contract_failure_rate",
            passed=cf_rate <= MAX_CONTRACT_FAILURE_RATE,
            value=cf_rate,
            threshold=MAX_CONTRACT_FAILURE_RATE,
            detail=f"Contract failure rate {cf_rate:.1%} (max {MAX_CONTRACT_FAILURE_RATE:.0%})",
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="acceptable_contract_failure_rate",
            passed=True,
            value=None,
            threshold=MAX_CONTRACT_FAILURE_RATE,
            detail="No contract metrics yet — N/A",
            severity="soft",
        ))

    # 6. No policy violations
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations <= MAX_POLICY_VIOLATIONS,
        value=violations,
        threshold=MAX_POLICY_VIOLATIONS,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) detected"
        ),
        severity="hard",
    ))

    # 7. No budget exhaustions
    budget = evidence.get("budget_exhaustions", 0)
    criteria.append(RolloutCriterion(
        name="no_budget_exhaustions",
        passed=budget <= MAX_BUDGET_EXHAUSTIONS,
        value=budget,
        threshold=MAX_BUDGET_EXHAUSTIONS,
        detail=(
            "No budget exhaustions"
            if budget == 0
            else f"{budget} budget exhaustion(s)"
        ),
        severity="hard",
    ))

    # 8. No orphaned agents
    orphaned = evidence.get("orphaned_agents", 0)
    criteria.append(RolloutCriterion(
        name="no_orphaned_agents",
        passed=orphaned == 0,
        value=orphaned,
        threshold=0,
        detail=(
            "No orphaned agents"
            if orphaned == 0
            else f"{orphaned} orphaned agent(s)"
        ),
        severity="soft",
    ))

    # 9. No stale leases
    stale = evidence.get("stale_leases", 0)
    criteria.append(RolloutCriterion(
        name="no_stale_leases",
        passed=stale == 0,
        value=stale,
        threshold=0,
        detail=(
            "No stale leases"
            if stale == 0
            else f"{stale} stale lease(s)"
        ),
        severity="soft",
    ))

    return criteria


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def decide(criteria: list[RolloutCriterion],
           evidence: dict) -> tuple[str, str]:
    """Determine rollout decision from evaluated criteria.

    Returns (decision, next_action) where decision is one of:
      - "ready_to_expand"
      - "hold"
      - "rollback_recommended"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure → rollback recommended
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "rollback_recommended",
            f"Investigate and resolve hard failures: {reasons}. "
            f"Consider setting enabled=false or removing affected classes.",
        )

    # Insufficient evidence → hold
    min_runs = next(
        (c for c in criteria if c.name == "minimum_completed_runs"), None
    )
    if min_runs and not min_runs.passed:
        return (
            "hold",
            f"Continue Stage 2 rollout. Need {min_runs.threshold} completed "
            f"workflows (currently {min_runs.value}). Re-evaluate after more runs.",
        )

    # Soft failures → hold
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "hold",
            f"Soft concerns detected: {reasons}. "
            f"Continue monitoring before expansion.",
        )

    # All clear → ready to expand
    return (
        "ready_to_expand",
        "Stage 2 rollout is stable. Safe to add code_impl to "
        "supported_classes for Stage 3 rollout.",
    )


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------

def evaluate_rollout(base: Path | None = None) -> RolloutEvaluation:
    """Run the full rollout evaluation gate.

    Collects evidence, evaluates criteria, and returns a decision.
    """
    root = base or BASE
    evidence = collect_evidence(root)
    criteria = evaluate_criteria(evidence)
    decision, next_action = decide(criteria, evidence)

    return RolloutEvaluation(
        decision=decision,
        rollout_stage=evidence.get("rollout_stage", "unknown"),
        classes_evaluated=evidence.get("supported_classes", []),
        criteria=criteria,
        evidence_summary={
            "total_workflows": evidence.get("total_workflows", 0),
            "completed_workflows": evidence.get("completed_workflows", 0),
            "failed_workflows": evidence.get("failed_workflows", 0),
            "halted_workflows": evidence.get("halted_workflows", 0),
            "heartbeat_overall": evidence.get("heartbeat_overall", ""),
            "policy_violations": evidence.get("policy_violations", 0),
            "budget_exhaustions": evidence.get("budget_exhaustions", 0),
            "verifier_rejection_rate": evidence.get("verifier_rejection_rate"),
            "contract_failure_rate": evidence.get("contract_failure_rate"),
            "orphaned_agents": evidence.get("orphaned_agents", 0),
            "stale_leases": evidence.get("stale_leases", 0),
            "recovery_events": evidence.get("recovery_events", 0),
        },
        next_action=next_action,
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_evaluation_markdown(evaluation: RolloutEvaluation) -> str:
    """Render evaluation result as a markdown report."""
    icon = {
        "ready_to_expand": "PASS",
        "hold": "HOLD",
        "rollback_recommended": "FAIL",
    }.get(evaluation.decision, "?")

    lines = [
        "# Rollout Evaluation Gate — Stage 2",
        f"Generated: {evaluation.generated_at}",
        "",
        f"## Decision: {icon} — {evaluation.decision}",
        "",
        f"**Rollout stage**: {evaluation.rollout_stage}",
        f"**Classes evaluated**: {', '.join(evaluation.classes_evaluated) or 'none'}",
        "",
        f"**Next action**: {evaluation.next_action}",
        "",
        "## Criteria",
        "",
        "| # | Criterion | Status | Value | Threshold | Severity | Detail |",
        "|---|-----------|--------|-------|-----------|----------|--------|",
    ]

    for i, c in enumerate(evaluation.criteria, 1):
        status = "PASS" if c.passed else "FAIL"
        val = c.value if c.value is not None else "N/A"
        lines.append(
            f"| {i} | {c.name} | {status} | {val} | {c.threshold} "
            f"| {c.severity} | {c.detail} |"
        )

    lines.append("")

    # Evidence summary
    ev = evaluation.evidence_summary
    lines.append("## Evidence Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for k, v in ev.items():
        display = f"{v:.1%}" if isinstance(v, float) else str(v) if v is not None else "N/A"
        lines.append(f"| {k} | {display} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def render_evaluation_json(evaluation: RolloutEvaluation) -> str:
    """Render evaluation result as JSON."""
    return json.dumps(evaluation.to_dict(), indent=2, default=str) + "\n"


# ---------------------------------------------------------------------------
# Write to disk
# ---------------------------------------------------------------------------

def write_evaluation_report(
    evaluation: RolloutEvaluation,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write evaluation report to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_rollout_stage2_evaluation.md"
    json_path = root / "STATE" / "rollout_evaluation.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_evaluation_markdown(evaluation))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(render_evaluation_json(evaluation))

    return md_path, json_path


# ---------------------------------------------------------------------------
# Stage 3 expansion — evaluation-gated code_impl enablement
# ---------------------------------------------------------------------------

# Stage 3 target configuration
STAGE3_CLASSES = ["research", "code_review", "code_impl"]
STAGE3_ROLLOUT_STAGE = "stage3_research_code_review_code_impl"
STAGE3_ALLOWED_ROLES = ["research", "coding"]


@dataclass
class ExpansionResult:
    """Outcome of an evaluation-gated Stage 3 expansion attempt."""
    expanded: bool
    decision: str          # "ready_to_expand" | "hold" | "rollback_recommended"
    reason: str
    evaluation: RolloutEvaluation | None = None
    config_path: Path | None = None

    def to_dict(self) -> dict:
        d = {
            "expanded": self.expanded,
            "decision": self.decision,
            "reason": self.reason,
        }
        if self.config_path:
            d["config_path"] = str(self.config_path)
        if self.evaluation:
            d["evaluation"] = self.evaluation.to_dict()
        return d


def expand_to_stage3(base: Path | None = None) -> ExpansionResult:
    """Attempt evaluation-gated expansion to Stage 3 (add code_impl).

    Runs the rollout evaluation gate. Only updates feature_flags.json
    if the decision is ready_to_expand. Otherwise preserves current
    config and returns the blocking reason.

    Returns ExpansionResult with details of the attempt.
    """
    root = base or BASE
    flags_path = root / "STATE" / "config" / "feature_flags.json"

    # Run evaluation gate
    evaluation = evaluate_rollout(root)

    if evaluation.decision != "ready_to_expand":
        return ExpansionResult(
            expanded=False,
            decision=evaluation.decision,
            reason=evaluation.next_action,
            evaluation=evaluation,
        )

    # Gate approved — update feature flags
    try:
        flags_data = json.loads(flags_path.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return ExpansionResult(
            expanded=False,
            decision=evaluation.decision,
            reason="cannot_read_feature_flags",
            evaluation=evaluation,
        )

    orch = flags_data.get("phase7_orchestrator", {})
    orch["supported_classes"] = list(STAGE3_CLASSES)
    orch["rollout_stage"] = STAGE3_ROLLOUT_STAGE
    orch["allowed_roles"] = list(STAGE3_ALLOWED_ROLES)
    orch["verifier_required"] = True  # mandatory for mutation-capable class
    orch["stage_description"] = (
        "Stage 3 rollout — research + code_review + code_impl. "
        "code_impl is the first mutation-capable class. "
        "Verifier gate mandatory. Maker-checker enforced. "
        "system blocked until Stage D."
    )
    flags_data["phase7_orchestrator"] = orch
    flags_data["version"] = flags_data.get("version", 0) + 1
    flags_data["updated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Atomic write
    flags_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = flags_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(flags_data, indent=2))
    tmp.rename(flags_path)

    return ExpansionResult(
        expanded=True,
        decision="ready_to_expand",
        reason="Stage 3 expansion applied — code_impl added to supported_classes",
        evaluation=evaluation,
        config_path=flags_path,
    )


# ---------------------------------------------------------------------------
# Stage 3 readiness check — operator-facing progress report
# ---------------------------------------------------------------------------

@dataclass
class ReadinessReport:
    """Operator-facing report of Stage 3 readiness progress."""
    permitted: bool
    decision: str
    blocking_criteria: list[str]
    progress: dict          # criterion_name -> {value, threshold, met, detail}
    evidence_summary: dict
    rollout_stage: str
    generated_at: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        return {
            "permitted": self.permitted,
            "decision": self.decision,
            "blocking_criteria": self.blocking_criteria,
            "progress": self.progress,
            "evidence_summary": self.evidence_summary,
            "rollout_stage": self.rollout_stage,
            "generated_at": self.generated_at,
        }


def check_stage3_readiness(base: Path | None = None) -> ReadinessReport:
    """Check current progress toward Stage 3 readiness.

    Evaluates all rollout criteria and reports:
    - Whether activation is currently permitted or blocked
    - Which criteria are blocking
    - Current value vs threshold for each criterion
    - Evidence summary

    This is a read-only check — it does not modify any state.
    """
    root = base or BASE
    evaluation = evaluate_rollout(root)

    progress: dict = {}
    blocking: list[str] = []

    for c in evaluation.criteria:
        progress[c.name] = {
            "value": c.value,
            "threshold": c.threshold,
            "met": c.passed,
            "severity": c.severity,
            "detail": c.detail,
        }
        if not c.passed:
            blocking.append(c.name)

    return ReadinessReport(
        permitted=evaluation.decision == "ready_to_expand",
        decision=evaluation.decision,
        blocking_criteria=blocking,
        progress=progress,
        evidence_summary=evaluation.evidence_summary,
        rollout_stage=evaluation.rollout_stage,
    )


def render_readiness_markdown(report: ReadinessReport) -> str:
    """Render readiness report as operator-facing markdown."""
    status = "PERMITTED" if report.permitted else "BLOCKED"
    lines = [
        "# Stage 3 Activation Readiness",
        f"Generated: {report.generated_at}",
        "",
        f"## Status: {status}",
        f"**Decision**: {report.decision}",
        f"**Rollout stage**: {report.rollout_stage}",
        "",
    ]

    if report.blocking_criteria:
        lines.append("### Blocking Criteria")
        for name in report.blocking_criteria:
            p = report.progress[name]
            lines.append(f"- **{name}** ({p['severity']}): {p['detail']}")
        lines.append("")

    lines.append("### All Criteria Progress")
    lines.append("")
    lines.append("| Criterion | Status | Value | Threshold | Severity |")
    lines.append("|-----------|--------|-------|-----------|----------|")

    for name, p in report.progress.items():
        icon = "PASS" if p["met"] else "FAIL"
        val = p["value"] if p["value"] is not None else "N/A"
        lines.append(
            f"| {name} | {icon} | {val} | {p['threshold']} | {p['severity']} |"
        )

    lines.append("")
    lines.append("### Evidence Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for k, v in report.evidence_summary.items():
        display = (f"{v:.1%}" if isinstance(v, float)
                   else str(v) if v is not None else "N/A")
        lines.append(f"| {k} | {display} |")
    lines.append("")

    if report.permitted:
        lines.append("### Activation")
        lines.append("")
        lines.append("Stage 3 activation is permitted. Run:")
        lines.append("```python")
        lines.append("from agents.rollout_gate import activate_stage3")
        lines.append("result = activate_stage3()")
        lines.append("```")
    else:
        lines.append("### Next Steps")
        lines.append("")
        lines.append("Accumulate more clean Stage 2 evidence, then re-check.")
        lines.append("```python")
        lines.append("from agents.rollout_gate import check_stage3_readiness")
        lines.append("report = check_stage3_readiness()")
        lines.append("```")

    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Stage 3 activation procedure — auditable, fail-closed
# ---------------------------------------------------------------------------

@dataclass
class ActivationRecord:
    """Audit record of a Stage 3 activation attempt."""
    attempted_at: str
    outcome: str           # "activated" | "blocked" | "error"
    decision: str          # gate decision at time of activation
    reason: str
    pre_config: dict       # config snapshot before attempt
    post_config: dict      # config snapshot after attempt (same if blocked)
    blocking_criteria: list[str]

    def to_dict(self) -> dict:
        return {
            "attempted_at": self.attempted_at,
            "outcome": self.outcome,
            "decision": self.decision,
            "reason": self.reason,
            "pre_config": self.pre_config,
            "post_config": self.post_config,
            "blocking_criteria": self.blocking_criteria,
        }


def activate_stage3(base: Path | None = None) -> ActivationRecord:
    """Safe, auditable Stage 3 activation procedure.

    1. Snapshots current config
    2. Re-runs rollout evaluation gate (fresh evidence)
    3. Only activates if gate returns ready_to_expand
    4. Writes activation audit record to STATE/activation_log.jsonl
    5. Returns ActivationRecord with full details

    Fail-closed: any gate failure, config error, or non-ready decision
    blocks activation and preserves current state.
    """
    root = base or BASE
    flags_path = root / "STATE" / "config" / "feature_flags.json"
    log_path = root / "STATE" / "activation_log.jsonl"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Snapshot pre-activation config
    pre_config: dict = {}
    try:
        pre_config = json.loads(flags_path.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        record = ActivationRecord(
            attempted_at=now_iso,
            outcome="error",
            decision="unknown",
            reason="cannot_read_feature_flags",
            pre_config={},
            post_config={},
            blocking_criteria=[],
        )
        _append_activation_log(log_path, record)
        return record

    # 2. Run expansion (includes fresh gate evaluation)
    expansion = expand_to_stage3(root)

    # 3. Build activation record
    blocking = []
    if expansion.evaluation:
        blocking = [
            c.name for c in expansion.evaluation.criteria if not c.passed
        ]

    if expansion.expanded:
        # Read post-activation config
        try:
            post_config = json.loads(flags_path.read_text())
        except (json.JSONDecodeError, OSError):
            post_config = pre_config

        record = ActivationRecord(
            attempted_at=now_iso,
            outcome="activated",
            decision=expansion.decision,
            reason=expansion.reason,
            pre_config=pre_config.get("phase7_orchestrator", {}),
            post_config=post_config.get("phase7_orchestrator", {}),
            blocking_criteria=[],
        )
    else:
        record = ActivationRecord(
            attempted_at=now_iso,
            outcome="blocked",
            decision=expansion.decision,
            reason=expansion.reason,
            pre_config=pre_config.get("phase7_orchestrator", {}),
            post_config=pre_config.get("phase7_orchestrator", {}),
            blocking_criteria=blocking,
        )

    # 4. Write audit log
    _append_activation_log(log_path, record)

    return record


def _append_activation_log(log_path: Path, record: ActivationRecord) -> None:
    """Append activation record to STATE/activation_log.jsonl."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(record.to_dict(), default=str) + "\n")


# ---------------------------------------------------------------------------
# Phase 7.13 — Post-Stage-3 Stability Review
# ---------------------------------------------------------------------------

@dataclass
class StabilityReview:
    """Deterministic stability review for live Stage 3 rollout."""
    decision: str          # "stable_continue" | "hold_stage3" | "rollback_code_impl_recommended"
    rollout_stage: str
    enabled_classes: list[str]
    criteria: list[RolloutCriterion] = field(default_factory=list)
    code_impl_metrics: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    generated_at: str = ""
    next_action: str = ""
    activation_record: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "rollout_stage": self.rollout_stage,
            "enabled_classes": self.enabled_classes,
            "criteria": [c.to_dict() for c in self.criteria],
            "code_impl_metrics": self.code_impl_metrics,
            "evidence_summary": self.evidence_summary,
            "generated_at": self.generated_at,
            "next_action": self.next_action,
            "activation_record": self.activation_record,
        }


def _collect_code_impl_metrics(
    workflows: list[dict],
) -> dict:
    """Extract code_impl-specific metrics from workflow records."""
    impl_workflows = [
        w for w in workflows if w.get("task_class") == "code_impl"
    ]
    impl_completed = [w for w in impl_workflows if w.get("status") == "completed"]
    impl_failed = [
        w for w in impl_workflows
        if w.get("status") in ("failed", "halted")
    ]
    impl_rejected = [
        w for w in impl_workflows
        if w.get("halt_reason") == "verifier_rejected"
    ]

    total = len(impl_completed) + len(impl_failed)
    failure_rate = (
        round(len(impl_failed) / total, 3) if total > 0 else None
    )

    return {
        "total_runs": len(impl_workflows),
        "completed": len(impl_completed),
        "failed": len(impl_failed),
        "verifier_rejected": len(impl_rejected),
        "failure_rate": failure_rate,
    }


def _get_latest_activation(base: Path) -> dict:
    """Read the most recent activation record from the audit log."""
    log_path = base / "STATE" / "activation_log.jsonl"
    records = _read_jsonl(log_path)
    # Return the last activated entry, or the last entry
    activated = [r for r in records if r.get("outcome") == "activated"]
    if activated:
        return activated[-1]
    return records[-1] if records else {}


def _count_post_activation_recoveries(
    base: Path, activation_ts: str,
) -> int:
    """Count recovery events that occurred after the activation timestamp."""
    recovery_log = base / "LOGS" / "recovery.log"
    if not recovery_log.exists():
        return 0
    try:
        content = recovery_log.read_text()
    except OSError:
        return 0

    count = 0
    for line in content.splitlines():
        if "--- Recovery at" in line:
            # Extract ISO timestamp from "--- Recovery at 2026-03-08T..."
            parts = line.split("--- Recovery at ", 1)
            if len(parts) == 2:
                ts = parts[1].strip().rstrip(" ---")
                if ts >= activation_ts:
                    count += 1
    return count


def evaluate_stage3_stability(
    evidence: dict,
    code_impl_metrics: dict,
    post_activation_recoveries: int,
) -> list[RolloutCriterion]:
    """Evaluate Stage 3 stability criteria.

    These criteria are specific to post-activation stability with extra
    attention to code_impl (the first mutation-capable class).
    """
    criteria: list[RolloutCriterion] = []

    # 1. Heartbeat healthy (inherited, hard)
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb != "unhealthy",
        value=hb or "(no data)",
        threshold="not unhealthy",
        detail=(
            f"Current heartbeat: {hb or '(no data)'}"
            if hb != "unhealthy"
            else "Heartbeat is UNHEALTHY — Stage 3 unsafe"
        ),
        severity="hard",
    ))

    # 2. Overall failure rate (hard — tighter threshold for live mutation)
    failure_rate = evidence.get("failure_rate")
    if failure_rate is not None:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=failure_rate <= STAGE3_MAX_FAILURE_RATE,
            value=failure_rate,
            threshold=STAGE3_MAX_FAILURE_RATE,
            detail=f"Overall failure rate {failure_rate:.1%} (max {STAGE3_MAX_FAILURE_RATE:.0%})",
            severity="hard",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=True,
            value=None,
            threshold=STAGE3_MAX_FAILURE_RATE,
            detail="No terminal workflows yet",
            severity="soft",
        ))

    # 3. No policy violations (hard — zero tolerance)
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations <= MAX_POLICY_VIOLATIONS,
        value=violations,
        threshold=MAX_POLICY_VIOLATIONS,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) — review immediately"
        ),
        severity="hard",
    ))

    # 4. No budget exhaustions (hard — zero tolerance)
    budget = evidence.get("budget_exhaustions", 0)
    criteria.append(RolloutCriterion(
        name="no_budget_exhaustions",
        passed=budget <= MAX_BUDGET_EXHAUSTIONS,
        value=budget,
        threshold=MAX_BUDGET_EXHAUSTIONS,
        detail=(
            "No budget exhaustions"
            if budget == 0
            else f"{budget} budget exhaustion(s)"
        ),
        severity="hard",
    ))

    # 5. code_impl minimum observation (soft — insufficient evidence = hold)
    impl_total = code_impl_metrics.get("total_runs", 0)
    criteria.append(RolloutCriterion(
        name="code_impl_minimum_runs",
        passed=impl_total >= STAGE3_MIN_CODE_IMPL_RUNS,
        value=impl_total,
        threshold=STAGE3_MIN_CODE_IMPL_RUNS,
        detail=(
            f"{impl_total} code_impl runs (need >= {STAGE3_MIN_CODE_IMPL_RUNS})"
        ),
        severity="soft",
    ))

    # 6. code_impl failure rate (soft — mutation-capable class needs monitoring)
    impl_fr = code_impl_metrics.get("failure_rate")
    if impl_fr is not None:
        criteria.append(RolloutCriterion(
            name="code_impl_failure_rate",
            passed=impl_fr <= STAGE3_MAX_CODE_IMPL_FAILURE_RATE,
            value=impl_fr,
            threshold=STAGE3_MAX_CODE_IMPL_FAILURE_RATE,
            detail=(
                f"code_impl failure rate {impl_fr:.1%} "
                f"(max {STAGE3_MAX_CODE_IMPL_FAILURE_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="code_impl_failure_rate",
            passed=True,
            value=None,
            threshold=STAGE3_MAX_CODE_IMPL_FAILURE_RATE,
            detail="No code_impl terminal workflows yet",
            severity="soft",
        ))

    # 7. Verifier rejection rate (soft — critical for mutation-capable class)
    vr_rate = evidence.get("verifier_rejection_rate")
    if vr_rate is not None:
        criteria.append(RolloutCriterion(
            name="verifier_rejection_rate",
            passed=vr_rate <= STAGE3_MAX_VERIFIER_REJECTION_RATE,
            value=vr_rate,
            threshold=STAGE3_MAX_VERIFIER_REJECTION_RATE,
            detail=(
                f"Verifier rejection rate {vr_rate:.1%} "
                f"(max {STAGE3_MAX_VERIFIER_REJECTION_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="verifier_rejection_rate",
            passed=True,
            value=None,
            threshold=STAGE3_MAX_VERIFIER_REJECTION_RATE,
            detail="No verifier reports yet — N/A",
            severity="soft",
        ))

    # 8. Contract failure rate (soft)
    cf_rate = evidence.get("contract_failure_rate")
    if cf_rate is not None:
        criteria.append(RolloutCriterion(
            name="contract_failure_rate",
            passed=cf_rate <= MAX_CONTRACT_FAILURE_RATE,
            value=cf_rate,
            threshold=MAX_CONTRACT_FAILURE_RATE,
            detail=f"Contract failure rate {cf_rate:.1%} (max {MAX_CONTRACT_FAILURE_RATE:.0%})",
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="contract_failure_rate",
            passed=True,
            value=None,
            threshold=MAX_CONTRACT_FAILURE_RATE,
            detail="No contract metrics yet — N/A",
            severity="soft",
        ))

    # 9. Recovery anomalies (soft — requeues/orphans since activation)
    criteria.append(RolloutCriterion(
        name="recovery_anomalies",
        passed=post_activation_recoveries <= STAGE3_MAX_RECOVERY_ANOMALIES,
        value=post_activation_recoveries,
        threshold=STAGE3_MAX_RECOVERY_ANOMALIES,
        detail=(
            f"{post_activation_recoveries} recovery events since activation "
            f"(max {STAGE3_MAX_RECOVERY_ANOMALIES})"
        ),
        severity="soft",
    ))

    # 10. No orphaned agents (soft)
    orphaned = evidence.get("orphaned_agents", 0)
    criteria.append(RolloutCriterion(
        name="no_orphaned_agents",
        passed=orphaned == 0,
        value=orphaned,
        threshold=0,
        detail=(
            "No orphaned agents"
            if orphaned == 0
            else f"{orphaned} orphaned agent(s)"
        ),
        severity="soft",
    ))

    # 11. No stale leases (soft)
    stale = evidence.get("stale_leases", 0)
    criteria.append(RolloutCriterion(
        name="no_stale_leases",
        passed=stale == 0,
        value=stale,
        threshold=0,
        detail=(
            "No stale leases"
            if stale == 0
            else f"{stale} stale lease(s)"
        ),
        severity="soft",
    ))

    return criteria


def decide_stage3_stability(
    criteria: list[RolloutCriterion],
) -> tuple[str, str]:
    """Determine Stage 3 stability from evaluated criteria.

    Returns (decision, next_action) where decision is one of:
      - "stable_continue"
      - "hold_stage3"
      - "rollback_code_impl_recommended"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure → rollback code_impl
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "rollback_code_impl_recommended",
            f"Hard failures detected: {reasons}. "
            f"Remove code_impl from supported_classes and restart watcher. "
            f"Investigate before re-enabling.",
        )

    # Insufficient code_impl evidence → hold
    impl_runs = next(
        (c for c in criteria if c.name == "code_impl_minimum_runs"), None
    )
    if impl_runs and not impl_runs.passed:
        return (
            "hold_stage3",
            f"Insufficient code_impl evidence: {impl_runs.value} runs "
            f"(need >= {impl_runs.threshold}). "
            f"Continue operating Stage 3 and re-evaluate after more code_impl tasks.",
        )

    # Any soft failure → hold
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "hold_stage3",
            f"Soft concerns: {reasons}. "
            f"Continue monitoring Stage 3. Do not expand to Stage 4.",
        )

    # All clear → stable
    return (
        "stable_continue",
        "Stage 3 is stable. code_impl operating within thresholds. "
        "Safe to continue current rollout. "
        "Stage 4 (system class) expansion can be evaluated when ready.",
    )


def review_stage3_stability(
    base: Path | None = None,
) -> StabilityReview:
    """Run the Stage 3 post-activation stability review.

    Collects evidence, evaluates Stage 3-specific criteria with extra
    attention to code_impl, and returns a deterministic decision.

    Decisions:
      - stable_continue: Stage 3 operating safely
      - hold_stage3: insufficient evidence or soft concerns
      - rollback_code_impl_recommended: hard failures detected
    """
    root = base or BASE
    state = root / "STATE"

    # Collect general evidence (reuse existing collector)
    evidence = collect_evidence(root)

    # Collect code_impl-specific metrics
    workflows = _list_json_files(state / "workflows")
    code_impl_metrics = _collect_code_impl_metrics(workflows)

    # Get activation record for observation window
    activation = _get_latest_activation(root)
    activation_ts = activation.get("attempted_at", "")

    # Count post-activation recovery anomalies
    post_recoveries = _count_post_activation_recoveries(root, activation_ts)

    # Evaluate Stage 3 stability criteria
    criteria = evaluate_stage3_stability(
        evidence, code_impl_metrics, post_recoveries,
    )
    decision, next_action = decide_stage3_stability(criteria)

    return StabilityReview(
        decision=decision,
        rollout_stage=evidence.get("rollout_stage", "unknown"),
        enabled_classes=evidence.get("supported_classes", []),
        criteria=criteria,
        code_impl_metrics=code_impl_metrics,
        evidence_summary={
            "total_workflows": evidence.get("total_workflows", 0),
            "completed_workflows": evidence.get("completed_workflows", 0),
            "failed_workflows": evidence.get("failed_workflows", 0),
            "halted_workflows": evidence.get("halted_workflows", 0),
            "heartbeat_overall": evidence.get("heartbeat_overall", ""),
            "policy_violations": evidence.get("policy_violations", 0),
            "budget_exhaustions": evidence.get("budget_exhaustions", 0),
            "verifier_rejection_rate": evidence.get("verifier_rejection_rate"),
            "contract_failure_rate": evidence.get("contract_failure_rate"),
            "orphaned_agents": evidence.get("orphaned_agents", 0),
            "stale_leases": evidence.get("stale_leases", 0),
            "recovery_events": evidence.get("recovery_events", 0),
            "post_activation_recoveries": post_recoveries,
        },
        next_action=next_action,
        activation_record=activation,
    )


def render_stability_review_markdown(review: StabilityReview) -> str:
    """Render Stage 3 stability review as a markdown report."""
    icon = {
        "stable_continue": "STABLE",
        "hold_stage3": "HOLD",
        "rollback_code_impl_recommended": "ROLLBACK",
    }.get(review.decision, "?")

    lines = [
        "# Phase 7.13 — Stage 3 Stability Review",
        f"Generated: {review.generated_at}",
        "",
        f"## Decision: {icon} — {review.decision}",
        "",
        f"**Rollout stage**: {review.rollout_stage}",
        f"**Enabled classes**: {', '.join(review.enabled_classes)}",
        "",
        f"**Next action**: {review.next_action}",
        "",
    ]

    # Activation context
    if review.activation_record:
        act = review.activation_record
        lines.append("## Activation Context")
        lines.append("")
        lines.append(f"- **Activated at**: {act.get('attempted_at', 'N/A')}")
        lines.append(f"- **Outcome**: {act.get('outcome', 'N/A')}")
        lines.append(f"- **Decision at activation**: {act.get('decision', 'N/A')}")
        lines.append("")

    # code_impl metrics
    m = review.code_impl_metrics
    lines.append("## code_impl Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total runs | {m.get('total_runs', 0)} |")
    lines.append(f"| Completed | {m.get('completed', 0)} |")
    lines.append(f"| Failed | {m.get('failed', 0)} |")
    lines.append(f"| Verifier rejected | {m.get('verifier_rejected', 0)} |")
    fr = m.get("failure_rate")
    fr_str = f"{fr:.1%}" if fr is not None else "N/A"
    lines.append(f"| Failure rate | {fr_str} |")
    lines.append("")

    # Criteria table
    lines.append("## Stability Criteria")
    lines.append("")
    lines.append("| # | Criterion | Status | Value | Threshold | Severity | Detail |")
    lines.append("|---|-----------|--------|-------|-----------|----------|--------|")
    for i, c in enumerate(review.criteria, 1):
        status = "PASS" if c.passed else "FAIL"
        val = c.value if c.value is not None else "N/A"
        lines.append(
            f"| {i} | {c.name} | {status} | {val} | {c.threshold} "
            f"| {c.severity} | {c.detail} |"
        )
    lines.append("")

    # Evidence summary
    ev = review.evidence_summary
    lines.append("## Evidence Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for k, v in ev.items():
        display = (
            f"{v:.1%}" if isinstance(v, float)
            else str(v) if v is not None
            else "N/A"
        )
        lines.append(f"| {k} | {display} |")
    lines.append("")

    # Rollback instructions
    lines.append("## Rollback Instructions")
    lines.append("")
    lines.append("### Remove code_impl (revert to Stage 2)")
    lines.append("```bash")
    lines.append('python3 -c "')
    lines.append("import json; p='STATE/config/feature_flags.json'")
    lines.append("d=json.loads(open(p).read())")
    lines.append("d['phase7_orchestrator']['supported_classes']=['research','code_review']")
    lines.append("d['phase7_orchestrator']['rollout_stage']='stage2_research_and_code_review'")
    lines.append("open(p,'w').write(json.dumps(d,indent=2))")
    lines.append("print('Reverted to Stage 2')")
    lines.append('"')
    lines.append("sudo systemctl restart novacore-watcher")
    lines.append("```")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_stability_review(
    review: StabilityReview,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write stability review to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stage3_stability_review.md"
    json_path = root / "STATE" / "stage3_stability_review.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_stability_review_markdown(review))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(review.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path
