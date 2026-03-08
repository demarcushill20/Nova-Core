"""Phase 7.11–7.20 — Rollout Evaluation Gate, Activation, Stability Review, Stage 4 Evaluation, Rollout Plan, Activation Gate, Controlled Activation, Stage D Monitoring, Extended Monitoring, and Broader System Scope Evaluation.

Deterministic, repository-native evaluation of rollout readiness and stability.
Reads existing heartbeat, metrics, and workflow state to classify rollout
status as ready_to_expand, hold, or rollback_recommended.

Includes:
  - Evidence collection and criterion evaluation
  - Readiness check with progress toward thresholds
  - Evaluation-gated Stage 3 activation with audit trail
  - Fail-closed activation procedure
  - Phase 7.13: Post-activation Stage 3 stability review
  - Phase 7.14: Stage 4 evaluation gate (system class readiness)
  - Phase 7.15: Stage 4 rollout plan (system class planning)
  - Phase 7.16: Stage 4 activation gate (system_inspect readiness)
  - Phase 7.17: Controlled Stage 4 activation (system_inspect live)
  - Phase 7.18: Stage D monitoring and stability review
  - Phase 7.19: Extended Stage D monitoring window
  - Phase 7.20: Broader system scope evaluation gate

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

# --- Phase 7.14: Stage 4 evaluation gate thresholds ---
# Tighter than Stage 3 — system class is highest-risk

# Minimum code_impl workflows for Stage 4 consideration (more than Stage 3's 2)
STAGE4_MIN_CODE_IMPL_RUNS = 5

# Maximum code_impl failure rate (tighter than Stage 3's 50%)
STAGE4_MAX_CODE_IMPL_FAILURE_RATE = 0.30

# Maximum overall failure rate (tighter than Stage 3's 30%)
STAGE4_MAX_FAILURE_RATE = 0.20

# Maximum verifier rejection rate (tighter than Stage 3's 50%)
STAGE4_MAX_VERIFIER_REJECTION_RATE = 0.30

# Maximum contract failure rate (tighter than Stage 3's 30%)
STAGE4_MAX_CONTRACT_FAILURE_RATE = 0.20

# Maximum recovery anomalies since activation (tighter than Stage 3's 3)
STAGE4_MAX_RECOVERY_ANOMALIES = 1

# Minimum total completed workflows for systemic confidence
STAGE4_MIN_TOTAL_COMPLETED = 6

# --- Phase 7.18: Stage D live stability review thresholds ---
# These evaluate live Stage D (system_inspect active) operational health.
# Tightest thresholds — system scope demands maximum safety.

# Minimum system_inspect completed workflows for stability opinion
STAGED_MIN_SYSTEM_INSPECT_RUNS = 3

# Maximum system_inspect failure rate (very tight — read-only should not fail)
STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE = 0.20

# Maximum overall failure rate under Stage D (inherited from Stage 4 eval)
STAGED_MAX_FAILURE_RATE = 0.20

# Maximum verifier rejection rate for system_inspect tasks
STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE = 0.20

# Maximum contract failure rate (very tight)
STAGED_MAX_CONTRACT_FAILURE_RATE = 0.10

# Hard ceiling on policy violations (zero tolerance — any = rollback)
STAGED_MAX_POLICY_VIOLATIONS = 0

# Hard ceiling on budget exhaustions (zero tolerance — any = rollback)
STAGED_MAX_BUDGET_EXHAUSTIONS = 0

# Maximum recovery anomalies (zero tolerance for system scope)
STAGED_MAX_RECOVERY_ANOMALIES = 0

# Maximum blocked mutation attempts before recommending rollback
# (a few are expected from misrouting; too many indicates a classifier problem)
STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS = 5

# Maximum unresolved contract count (unfinished contracts accumulating)
STAGED_MAX_UNRESOLVED_CONTRACTS = 3

# Heartbeat requirement (must be exactly "healthy", not just "not unhealthy")
STAGED_HEARTBEAT_REQUIRED = "healthy"


# --- Phase 7.19: Extended Stage D monitoring window thresholds ---
# Tighter thresholds and longer observation horizon than Phase 7.18.
# Validates sustained Stage D safety before any future scope expansion.

# Minimum elapsed time since Stage D activation (seconds) — 4 hours
EXTENDED_MIN_ELAPSED_SECONDS = 4 * 3600

# Minimum system_inspect completed workflows for sustained opinion
EXTENDED_MIN_SYSTEM_INSPECT_RUNS = 10

# Maximum system_inspect failure rate (tighter than initial)
EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE = 0.10

# Maximum overall failure rate under extended window
EXTENDED_MAX_FAILURE_RATE = 0.15

# Maximum verifier rejection rate for system_inspect tasks
EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE = 0.10

# Hard ceiling on policy violations (zero tolerance — unchanged)
EXTENDED_MAX_POLICY_VIOLATIONS = 0

# Hard ceiling on budget exhaustions (zero tolerance — unchanged)
EXTENDED_MAX_BUDGET_EXHAUSTIONS = 0

# Maximum recovery anomalies (zero tolerance — unchanged)
EXTENDED_MAX_RECOVERY_ANOMALIES = 0

# Maximum blocked mutation attempts (tighter — by now classifier should be clean)
EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS = 3

# Maximum contract failure rate for system_inspect (new)
EXTENDED_MAX_CONTRACT_FAILURE_RATE = 0.10

# Heartbeat requirement (unchanged — strict healthy)
EXTENDED_HEARTBEAT_REQUIRED = "healthy"


# --- Phase 7.20: Broader system scope evaluation gate thresholds ---
# Tightest thresholds yet — gate before even *planning* scope expansion.
# All must pass before broader system scope planning can be considered.

# Minimum system_inspect completed workflows (more than extended's 10)
BROADER_MIN_SYSTEM_INSPECT_RUNS = 15

# Minimum elapsed time in Stage D (seconds) — 8 hours (more than extended's 4)
BROADER_MIN_ELAPSED_SECONDS = 8 * 3600

# Maximum overall failure rate (tighter than extended's 15%)
BROADER_MAX_FAILURE_RATE = 0.10

# Maximum system_inspect failure rate (tighter than extended's 10%)
BROADER_MAX_SYSTEM_INSPECT_FAILURE_RATE = 0.05

# Maximum verifier rejection rate (tighter than extended's 10%)
BROADER_MAX_SYSTEM_VERIFIER_REJECTION_RATE = 0.05

# Hard ceiling on policy violations (zero tolerance — unchanged)
BROADER_MAX_POLICY_VIOLATIONS = 0

# Hard ceiling on budget exhaustions (zero tolerance — unchanged)
BROADER_MAX_BUDGET_EXHAUSTIONS = 0

# Maximum unresolved recovery anomalies (zero tolerance — unchanged)
BROADER_MAX_RECOVERY_ANOMALIES = 0

# Maximum blocked mutation attempts (tighter than extended's 3)
BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS = 2

# Maximum contract failure rate (tighter than extended's 10%)
BROADER_MAX_CONTRACT_FAILURE_RATE = 0.05

# Heartbeat requirement (strict healthy — unchanged)
BROADER_HEARTBEAT_REQUIRED = "healthy"

# Required extended monitoring decision (prerequisite gate)
BROADER_REQUIRED_EXTENDED_DECISION = "stageD_sustained_stable"


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
# Decision string normalisation
# ---------------------------------------------------------------------------

# Canonical decision values used throughout the rollout gate.
VALID_DECISIONS = frozenset({"ready_to_expand", "hold", "rollback_recommended"})


def normalize_decision(raw: str) -> str:
    """Normalise a rollout decision string to canonical lowercase form.

    Accepts common variations (mixed case, extra whitespace, hyphens instead
    of underscores) and returns one of the three canonical decision strings:
      "ready_to_expand", "hold", "rollback_recommended"

    Raises ``ValueError`` for unrecognisable inputs.
    """
    cleaned = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in VALID_DECISIONS:
        return cleaned
    raise ValueError(
        f"Unknown rollout decision: {raw!r} "
        f"(normalised to {cleaned!r}, expected one of {sorted(VALID_DECISIONS)})"
    )


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


def _classify_post_activation_recoveries(
    base: Path, activation_ts: str,
) -> dict:
    """Classify post-activation recovery events as resolved or unresolved.

    A recovery event is "resolved" when the requeued task later completed
    successfully (matching workflow in STATE/workflows/ with status
    "completed").  Resolved events represent benign watcher-level requeues
    where the watcher correctly detected a dead worker and the task
    succeeded on retry.

    Unresolved events — tasks that were requeued but never completed —
    represent genuine recovery anomalies that warrant investigation.

    Returns dict with keys: total, resolved, unresolved, details.
    """
    empty = {"total": 0, "resolved": 0, "unresolved": 0, "details": []}
    if not activation_ts:
        return empty

    recovery_log = base / "LOGS" / "recovery.log"
    if not recovery_log.exists():
        return empty
    try:
        content = recovery_log.read_text()
    except OSError:
        return empty

    # Parse recovery events that are post-activation
    events: list[dict] = []
    current_ts = ""
    for line in content.splitlines():
        if "--- Recovery at" in line:
            parts = line.split("--- Recovery at ", 1)
            if len(parts) == 2:
                current_ts = parts[1].strip().rstrip(" ---")
        elif "[task_requeued]" in line and current_ts:
            if current_ts >= activation_ts:
                task_match = line.split("[task_requeued] ", 1)
                if len(task_match) == 2:
                    task_part = task_match[1].split(" \u2014 ")[0].strip()
                    # Also handle ASCII dash
                    if " -- " in task_part:
                        task_part = task_part.split(" -- ")[0].strip()
                    # Strip common suffixes to get the workflow stem
                    task_stem = task_part
                    for suffix in (".md.inprogress", ".md.done", ".md"):
                        if task_stem.endswith(suffix):
                            task_stem = task_stem[: -len(suffix)]
                            break
                    events.append({"ts": current_ts, "task": task_stem})

    if not events:
        return empty

    # Cross-reference with workflow completions
    workflows_dir = base / "STATE" / "workflows"
    resolved = 0
    unresolved = 0
    details: list[dict] = []
    for event in events:
        task = event["task"]
        wf_path = workflows_dir / f"{task}.json"
        is_resolved = False
        if wf_path.exists():
            try:
                wf = json.loads(wf_path.read_text())
                if wf.get("status") == "completed":
                    is_resolved = True
            except (json.JSONDecodeError, OSError):
                pass
        if is_resolved:
            resolved += 1
            details.append({
                "task": task, "ts": event["ts"],
                "outcome": "resolved",
                "reason": "task completed successfully after requeue",
            })
        else:
            unresolved += 1
            details.append({
                "task": task, "ts": event["ts"],
                "outcome": "unresolved",
                "reason": "task did not complete after requeue",
            })

    return {
        "total": len(events),
        "resolved": resolved,
        "unresolved": unresolved,
        "details": details,
    }


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


# ---------------------------------------------------------------------------
# Phase 7.14 — Stage 4 Evaluation Gate (system class readiness)
# ---------------------------------------------------------------------------

@dataclass
class Stage4Evaluation:
    """Deterministic evaluation of whether Stage 4 (system class) should be
    considered for rollout planning."""
    decision: str          # "ready_for_stage4_planning" | "hold_stage4" | "block_stage4"
    rollout_stage: str
    enabled_classes: list[str]
    criteria: list[RolloutCriterion] = field(default_factory=list)
    code_impl_metrics: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    stage3_stability_decision: str = ""
    generated_at: str = ""
    next_action: str = ""
    remaining_requirements: list[str] = field(default_factory=list)

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
            "stage3_stability_decision": self.stage3_stability_decision,
            "generated_at": self.generated_at,
            "next_action": self.next_action,
            "remaining_requirements": self.remaining_requirements,
        }


def evaluate_stage4_criteria(
    evidence: dict,
    code_impl_metrics: dict,
    post_activation_recoveries: int,
    stage3_decision: str,
) -> list[RolloutCriterion]:
    """Evaluate Stage 4 readiness criteria.

    Tighter thresholds than Stage 3 because system is the highest-risk class.
    Requires Stage 3 to be confirmed stable first.
    """
    criteria: list[RolloutCriterion] = []

    # 1. Stage 3 stability must be stable_continue (hard — prerequisite)
    criteria.append(RolloutCriterion(
        name="stage3_stable",
        passed=stage3_decision == "stable_continue",
        value=stage3_decision,
        threshold="stable_continue",
        detail=(
            "Stage 3 stability confirmed"
            if stage3_decision == "stable_continue"
            else f"Stage 3 stability is '{stage3_decision}' — must be stable_continue"
        ),
        severity="hard",
    ))

    # 2. Heartbeat healthy (hard)
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb != "unhealthy",
        value=hb or "(no data)",
        threshold="not unhealthy",
        detail=(
            f"Current heartbeat: {hb or '(no data)'}"
            if hb != "unhealthy"
            else "Heartbeat is UNHEALTHY — Stage 4 blocked"
        ),
        severity="hard",
    ))

    # 3. Overall failure rate (hard — tighter for Stage 4)
    failure_rate = evidence.get("failure_rate")
    if failure_rate is not None:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=failure_rate <= STAGE4_MAX_FAILURE_RATE,
            value=failure_rate,
            threshold=STAGE4_MAX_FAILURE_RATE,
            detail=f"Overall failure rate {failure_rate:.1%} (max {STAGE4_MAX_FAILURE_RATE:.0%})",
            severity="hard",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=True,
            value=None,
            threshold=STAGE4_MAX_FAILURE_RATE,
            detail="No terminal workflows yet",
            severity="soft",
        ))

    # 4. No policy violations (hard — zero tolerance)
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations <= MAX_POLICY_VIOLATIONS,
        value=violations,
        threshold=MAX_POLICY_VIOLATIONS,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) — Stage 4 blocked"
        ),
        severity="hard",
    ))

    # 5. No budget exhaustions (hard — zero tolerance)
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

    # 6. System class currently blocked (hard — sanity check)
    supported = evidence.get("supported_classes", [])
    system_blocked = "system" not in supported
    criteria.append(RolloutCriterion(
        name="system_class_blocked",
        passed=system_blocked,
        value="blocked" if system_blocked else "enabled",
        threshold="blocked",
        detail=(
            "system class correctly blocked"
            if system_blocked
            else "system class already enabled — evaluation invalid"
        ),
        severity="hard",
    ))

    # 7. Minimum total completed workflows (soft — systemic confidence)
    completed = evidence.get("completed_workflows", 0)
    criteria.append(RolloutCriterion(
        name="minimum_total_completed",
        passed=completed >= STAGE4_MIN_TOTAL_COMPLETED,
        value=completed,
        threshold=STAGE4_MIN_TOTAL_COMPLETED,
        detail=f"{completed} total completed (need >= {STAGE4_MIN_TOTAL_COMPLETED})",
        severity="soft",
    ))

    # 8. code_impl minimum observation (soft — needs sustained evidence)
    impl_total = code_impl_metrics.get("total_runs", 0)
    criteria.append(RolloutCriterion(
        name="code_impl_minimum_runs",
        passed=impl_total >= STAGE4_MIN_CODE_IMPL_RUNS,
        value=impl_total,
        threshold=STAGE4_MIN_CODE_IMPL_RUNS,
        detail=f"{impl_total} code_impl runs (need >= {STAGE4_MIN_CODE_IMPL_RUNS})",
        severity="soft",
    ))

    # 9. code_impl failure rate (soft — tighter than Stage 3)
    impl_fr = code_impl_metrics.get("failure_rate")
    if impl_fr is not None:
        criteria.append(RolloutCriterion(
            name="code_impl_failure_rate",
            passed=impl_fr <= STAGE4_MAX_CODE_IMPL_FAILURE_RATE,
            value=impl_fr,
            threshold=STAGE4_MAX_CODE_IMPL_FAILURE_RATE,
            detail=(
                f"code_impl failure rate {impl_fr:.1%} "
                f"(max {STAGE4_MAX_CODE_IMPL_FAILURE_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="code_impl_failure_rate",
            passed=True,
            value=None,
            threshold=STAGE4_MAX_CODE_IMPL_FAILURE_RATE,
            detail="No code_impl terminal workflows yet",
            severity="soft",
        ))

    # 10. Verifier rejection rate (soft — tighter)
    vr_rate = evidence.get("verifier_rejection_rate")
    if vr_rate is not None:
        criteria.append(RolloutCriterion(
            name="verifier_rejection_rate",
            passed=vr_rate <= STAGE4_MAX_VERIFIER_REJECTION_RATE,
            value=vr_rate,
            threshold=STAGE4_MAX_VERIFIER_REJECTION_RATE,
            detail=(
                f"Verifier rejection rate {vr_rate:.1%} "
                f"(max {STAGE4_MAX_VERIFIER_REJECTION_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="verifier_rejection_rate",
            passed=True,
            value=None,
            threshold=STAGE4_MAX_VERIFIER_REJECTION_RATE,
            detail="No verifier reports yet — N/A",
            severity="soft",
        ))

    # 11. Contract failure rate (soft — tighter)
    cf_rate = evidence.get("contract_failure_rate")
    if cf_rate is not None:
        criteria.append(RolloutCriterion(
            name="contract_failure_rate",
            passed=cf_rate <= STAGE4_MAX_CONTRACT_FAILURE_RATE,
            value=cf_rate,
            threshold=STAGE4_MAX_CONTRACT_FAILURE_RATE,
            detail=(
                f"Contract failure rate {cf_rate:.1%} "
                f"(max {STAGE4_MAX_CONTRACT_FAILURE_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="contract_failure_rate",
            passed=True,
            value=None,
            threshold=STAGE4_MAX_CONTRACT_FAILURE_RATE,
            detail="No contract metrics yet — N/A",
            severity="soft",
        ))

    # 12. Recovery anomalies (soft — tighter)
    criteria.append(RolloutCriterion(
        name="recovery_anomalies",
        passed=post_activation_recoveries <= STAGE4_MAX_RECOVERY_ANOMALIES,
        value=post_activation_recoveries,
        threshold=STAGE4_MAX_RECOVERY_ANOMALIES,
        detail=(
            f"{post_activation_recoveries} recovery events since activation "
            f"(max {STAGE4_MAX_RECOVERY_ANOMALIES})"
        ),
        severity="soft",
    ))

    # 13. No orphaned agents (soft)
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

    # 14. No stale leases (soft)
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

    # 15. No rollback events in activation history (soft — no instability)
    activation_log = _read_jsonl(
        (evidence.get("_base_path") or BASE) / "STATE" / "activation_log.jsonl"
    )
    rollback_events = sum(
        1 for r in activation_log if r.get("outcome") == "rollback"
    )
    criteria.append(RolloutCriterion(
        name="no_rollback_history",
        passed=rollback_events == 0,
        value=rollback_events,
        threshold=0,
        detail=(
            "No rollback events in activation history"
            if rollback_events == 0
            else f"{rollback_events} rollback event(s) — indicates instability"
        ),
        severity="soft",
    ))

    return criteria


def decide_stage4_readiness(
    criteria: list[RolloutCriterion],
) -> tuple[str, str]:
    """Determine Stage 4 readiness from evaluated criteria.

    Returns (decision, next_action) where decision is one of:
      - "ready_for_stage4_planning"
      - "hold_stage4"
      - "block_stage4"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure → block
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "block_stage4",
            f"Hard failures blocking Stage 4: {reasons}. "
            f"Resolve before re-evaluating. "
            f"system class must remain blocked.",
        )

    # Insufficient evidence → hold
    evidence_criteria = {"code_impl_minimum_runs", "minimum_total_completed"}
    insufficient = [
        c for c in soft_failures if c.name in evidence_criteria
    ]
    if insufficient:
        names = ", ".join(c.name for c in insufficient)
        return (
            "hold_stage4",
            f"Insufficient evidence: {names}. "
            f"Continue operating Stage 3 to accumulate more evidence. "
            f"Re-evaluate after more workflows complete.",
        )

    # Other soft failures → hold
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "hold_stage4",
            f"Soft concerns: {reasons}. "
            f"Continue monitoring Stage 3. system remains blocked.",
        )

    # All clear → ready for planning
    return (
        "ready_for_stage4_planning",
        "Stage 3 is stable with sufficient evidence. "
        "system class rollout planning can begin. "
        "This does NOT activate system — a separate activation step is required.",
    )


def evaluate_stage4(
    base: Path | None = None,
) -> Stage4Evaluation:
    """Run the Stage 4 evaluation gate.

    Evaluates whether the current validated Stage 3 system is sufficiently
    stable, governed, and low-risk to consider controlled rollout planning
    for the system class.

    This is an evaluation gate only — it does NOT activate system.

    Decisions:
      - ready_for_stage4_planning: all criteria pass, sufficient evidence
      - hold_stage4: insufficient evidence or soft concerns
      - block_stage4: hard failures (Stage 3 unstable, policy violations, etc.)
    """
    root = base or BASE
    state = root / "STATE"

    # Collect general evidence (reuse existing collector)
    evidence = collect_evidence(root)
    # Stash base path for activation log access in criteria evaluation
    evidence["_base_path"] = root

    # Collect code_impl-specific metrics
    workflows = _list_json_files(state / "workflows")
    code_impl_metrics = _collect_code_impl_metrics(workflows)

    # Get activation record for observation window
    activation = _get_latest_activation(root)
    activation_ts = activation.get("attempted_at", "")

    # Count post-activation recovery anomalies
    post_recoveries = _count_post_activation_recoveries(root, activation_ts)

    # Run Stage 3 stability review to get current stability decision
    stage3_review = review_stage3_stability(root)
    stage3_decision = stage3_review.decision

    # Evaluate Stage 4 criteria
    criteria = evaluate_stage4_criteria(
        evidence, code_impl_metrics, post_recoveries, stage3_decision,
    )
    decision, next_action = decide_stage4_readiness(criteria)

    # Identify remaining requirements
    remaining = []
    for c in criteria:
        if not c.passed:
            remaining.append(f"{c.name}: {c.detail}")

    return Stage4Evaluation(
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
            "stage3_stability_decision": stage3_decision,
        },
        stage3_stability_decision=stage3_decision,
        next_action=next_action,
        remaining_requirements=remaining,
    )


def render_stage4_evaluation_markdown(evaluation: Stage4Evaluation) -> str:
    """Render Stage 4 evaluation gate as an operator-facing markdown report."""
    icon = {
        "ready_for_stage4_planning": "READY",
        "hold_stage4": "HOLD",
        "block_stage4": "BLOCKED",
    }.get(evaluation.decision, "?")

    lines = [
        "# Phase 7.14 — Stage 4 Evaluation Gate (system class)",
        f"Generated: {evaluation.generated_at}",
        "",
        f"## Decision: {icon} — {evaluation.decision}",
        "",
        f"**Rollout stage**: {evaluation.rollout_stage}",
        f"**Enabled classes**: {', '.join(evaluation.enabled_classes)}",
        f"**Stage 3 stability**: {evaluation.stage3_stability_decision}",
        "",
        f"**Next action**: {evaluation.next_action}",
        "",
    ]

    # code_impl metrics
    m = evaluation.code_impl_metrics
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
    lines.append("## Evaluation Criteria")
    lines.append("")
    lines.append("| # | Criterion | Status | Value | Threshold | Severity | Detail |")
    lines.append("|---|-----------|--------|-------|-----------|----------|--------|")
    for i, c in enumerate(evaluation.criteria, 1):
        status = "PASS" if c.passed else "FAIL"
        val = c.value if c.value is not None else "N/A"
        lines.append(
            f"| {i} | {c.name} | {status} | {val} | {c.threshold} "
            f"| {c.severity} | {c.detail} |"
        )
    lines.append("")

    # Remaining requirements
    if evaluation.remaining_requirements:
        lines.append("## Remaining Requirements")
        lines.append("")
        for req in evaluation.remaining_requirements:
            lines.append(f"- {req}")
        lines.append("")

    # Evidence summary
    ev = evaluation.evidence_summary
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

    # Stage 4 vs Stage 3 threshold comparison
    lines.append("## Threshold Comparison (Stage 3 → Stage 4)")
    lines.append("")
    lines.append("| Criterion | Stage 3 Threshold | Stage 4 Threshold |")
    lines.append("|-----------|-------------------|-------------------|")
    lines.append(f"| code_impl minimum runs | {STAGE3_MIN_CODE_IMPL_RUNS} | {STAGE4_MIN_CODE_IMPL_RUNS} |")
    lines.append(f"| code_impl failure rate | {STAGE3_MAX_CODE_IMPL_FAILURE_RATE:.0%} | {STAGE4_MAX_CODE_IMPL_FAILURE_RATE:.0%} |")
    lines.append(f"| Overall failure rate | {STAGE3_MAX_FAILURE_RATE:.0%} | {STAGE4_MAX_FAILURE_RATE:.0%} |")
    lines.append(f"| Verifier rejection rate | {STAGE3_MAX_VERIFIER_REJECTION_RATE:.0%} | {STAGE4_MAX_VERIFIER_REJECTION_RATE:.0%} |")
    lines.append(f"| Contract failure rate | {MAX_CONTRACT_FAILURE_RATE:.0%} | {STAGE4_MAX_CONTRACT_FAILURE_RATE:.0%} |")
    lines.append(f"| Recovery anomalies | {STAGE3_MAX_RECOVERY_ANOMALIES} | {STAGE4_MAX_RECOVERY_ANOMALIES} |")
    lines.append("")

    # system status
    lines.append("## system Class Status")
    lines.append("")
    lines.append("**system remains BLOCKED.** This evaluation gate does not activate system.")
    lines.append("If the decision is `ready_for_stage4_planning`, a separate activation step")
    lines.append("with its own safety gates would be required.")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_stage4_evaluation(
    evaluation: Stage4Evaluation,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write Stage 4 evaluation to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stage4_evaluation_gate.md"
    json_path = root / "STATE" / "stage4_evaluation.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_stage4_evaluation_markdown(evaluation))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(evaluation.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path


# ---------------------------------------------------------------------------
# Phase 7.15 — Stage 4 Rollout Plan (system class)
# ---------------------------------------------------------------------------

# --- Stage 4 scope: allowed and blocked system operations ---
# The "system" task class covers a wide range of operations.
# Stage 4 initial rollout restricts to inspection/read-only operations only.

STAGE4_ALLOWED_OPERATIONS = frozenset({
    # Read-only system inspection
    "status_check",        # check service status, systemctl status
    "log_inspection",      # read/tail log files
    "config_review",       # read config files, inspect settings
    "health_audit",        # run health checks, heartbeat review
    "resource_monitoring",  # disk, memory, process listing
    "architecture_review",  # review codebase structure, patterns
    "dependency_audit",    # check dependencies, versions
    "security_scan",       # read-only security review
})

STAGE4_BLOCKED_OPERATIONS = frozenset({
    # Mutation-capable — blocked in initial Stage 4
    "service_modification",  # systemctl start/stop/restart/enable/disable
    "config_modification",   # edit config files, feature flags
    "deployment",            # deploy, release, promote
    "package_management",    # pip install, apt install
    "cron_modification",     # add/edit/remove cron jobs
    "file_system_mutation",  # create/delete/move files outside sandbox
    "permission_changes",    # chmod, chown, access control
    "process_management",    # kill, signal, spawn services
    "network_changes",       # firewall, ports, DNS
    "self_modification",     # modify own code, bootstrap, self-improve
    "git_operations",        # push, force-push, branch operations
    "user_management",       # add/remove users, sudo operations
})

# --- Stage 4 allowed skills (narrower than Stage C) ---
STAGE4_ALLOWED_SKILLS = frozenset({
    "web-research",
    "file-ops",           # read-only use for inspection
    "self-verification",
    "http-fetch",
    "reading-obsidian-memory",
})

STAGE4_BLOCKED_SKILLS = frozenset({
    "shell-ops",          # blocked — mutation-capable
    "git-ops",            # blocked — mutation-capable
    "task-execution",     # blocked — unconstrained execution
})

# --- Stage 4 signal patterns for scope filtering ---
# Tasks matching these patterns are system-inspect (allowed in initial Stage 4)
STAGE4_INSPECT_SIGNALS = [
    r"\bstatus\b", r"\bcheck\b", r"\binspect\b", r"\breview\b",
    r"\baudit\b", r"\blist\b", r"\bshow\b", r"\blog[s]?\b",
    r"\bhealth\b", r"\bmonitor\b", r"\bdiagnos\w+\b",
    r"\bdependenc\w+\b", r"\bversion\b", r"\bscan\b",
    r"\bread\b", r"\bexamine\b", r"\banalyze\b", r"\banalyse\b",
]

# Tasks matching these patterns are system-mutate (blocked in initial Stage 4)
STAGE4_MUTATE_SIGNALS = [
    r"\bdeploy\b", r"\bconfigure\b", r"\binstall\b", r"\bmodify\b",
    r"\bstart\b", r"\bstop\b", r"\brestart\b", r"\benable\b", r"\bdisable\b",
    r"\bcreate\b", r"\bdelete\b", r"\bremove\b", r"\bkill\b",
    r"\bchmod\b", r"\bchown\b", r"\bsudo\b",
    r"\bpip\s+install\b", r"\bapt\b",
    r"\bsystemctl\s+(?:start|stop|restart|enable|disable)\b",
    r"\bcron\b", r"\bbootstrap\b", r"\bself[\s-]*improv\w+\b",
    r"\bpromote\b", r"\brollout\b", r"\bpush\b",
]

# --- Stage 4 activation prerequisites ---
STAGE4_ACTIVATION_PREREQUISITES = {
    "stage4_evaluation_ready": "Stage 4 evaluation gate returns ready_for_stage4_planning",
    "stage3_stable": "Stage 3 stability review returns stable_continue",
    "operator_approval": "Explicit operator confirmation via Telegram or manual trigger",
    "rollout_plan_reviewed": "This rollout plan has been reviewed and accepted",
    "verifier_required": "Maker-checker (verifier) mandatory for all system tasks",
    "manual_approval_enabled": "Manual approval hook active for system paths",
}

# --- Stage 4 success criteria for future activation monitoring ---
STAGE4_SUCCESS_CRITERIA = {
    "min_system_inspect_runs": 3,        # minimum clean inspection runs
    "max_system_failure_rate": 0.20,     # maximum 20% failure rate
    "max_verifier_rejection_rate": 0.20,  # tighter than Stage 3
    "max_contract_failure_rate": 0.10,   # very tight
    "max_policy_violations": 0,           # zero tolerance
    "max_budget_exhaustions": 0,          # zero tolerance
    "max_recovery_anomalies": 0,          # zero tolerance for system tasks
    "heartbeat_required": "healthy",     # must be healthy, not just "not unhealthy"
}

# --- Stage 4 abort conditions ---
STAGE4_ABORT_CONDITIONS = [
    "Any policy violation involving a system task",
    "Any budget exhaustion during system task execution",
    "Heartbeat transitions to unhealthy while system tasks are active",
    "Any system-mutate pattern detected in a task routed to system inspection path",
    "Verifier rejects a system task output",
    "Any system task produces side-effects outside ~/nova-core",
    "Operator issues manual abort via Telegram /cancel or equivalent",
]


@dataclass
class Stage4RolloutPlan:
    """Repository-native rollout plan for Stage 4 (system class)."""
    plan_status: str                   # "approved" | "draft" | "superseded"
    initial_scope: str                 # description of initial rollout slice
    allowed_operations: list[str]
    blocked_operations: list[str]
    allowed_skills: list[str]
    blocked_skills: list[str]
    activation_prerequisites: dict     # name -> description
    success_criteria: dict             # name -> threshold
    abort_conditions: list[str]
    rollback_procedure: list[str]
    system_class_status: str           # "blocked" | "inspect_only" | "full"
    stage4_evaluation_decision: str    # from evaluate_stage4()
    generated_at: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        return {
            "plan_status": self.plan_status,
            "initial_scope": self.initial_scope,
            "allowed_operations": self.allowed_operations,
            "blocked_operations": self.blocked_operations,
            "allowed_skills": self.allowed_skills,
            "blocked_skills": self.blocked_skills,
            "activation_prerequisites": self.activation_prerequisites,
            "success_criteria": self.success_criteria,
            "abort_conditions": self.abort_conditions,
            "rollback_procedure": self.rollback_procedure,
            "system_class_status": self.system_class_status,
            "stage4_evaluation_decision": self.stage4_evaluation_decision,
            "generated_at": self.generated_at,
        }


# Standard rollback procedure for Stage 4
_STAGE4_ROLLBACK_STEPS = [
    "Remove 'system' from supported_classes in STATE/config/feature_flags.json",
    "Set rollout_stage back to stage3_research_code_review_code_impl",
    "Restart novacore-watcher (sudo systemctl restart novacore-watcher)",
    "Write rollback record to STATE/activation_log.jsonl",
    "Verify system tasks no longer route to orchestrator",
    "Run stage3 stability review to confirm post-rollback health",
]


def build_stage4_rollout_plan(
    base: Path | None = None,
) -> Stage4RolloutPlan:
    """Build the Stage 4 rollout plan for the system class.

    Evaluates current Stage 4 readiness and produces a bounded rollout plan
    that defines scope, protections, success criteria, abort conditions,
    and rollback procedure.

    This is a planning artifact — it does NOT activate system.
    """
    root = base or BASE

    # Get Stage 4 evaluation decision
    stage4_eval = evaluate_stage4(root)

    # Read current feature flags for status
    ff = _read_json(root / "STATE" / "config" / "feature_flags.json") or {}
    orch = ff.get("phase7_orchestrator", {})
    supported = orch.get("supported_classes", [])
    system_status = "blocked" if "system" not in supported else "inspect_only"

    return Stage4RolloutPlan(
        plan_status="draft",
        initial_scope=(
            "system_inspect ONLY — read-only system inspection tasks. "
            "All mutation-capable system operations remain blocked. "
            "Only status checks, log inspection, config review, health audits, "
            "resource monitoring, architecture review, dependency audits, "
            "and security scans are permitted."
        ),
        allowed_operations=sorted(STAGE4_ALLOWED_OPERATIONS),
        blocked_operations=sorted(STAGE4_BLOCKED_OPERATIONS),
        allowed_skills=sorted(STAGE4_ALLOWED_SKILLS),
        blocked_skills=sorted(STAGE4_BLOCKED_SKILLS),
        activation_prerequisites=dict(STAGE4_ACTIVATION_PREREQUISITES),
        success_criteria=dict(STAGE4_SUCCESS_CRITERIA),
        abort_conditions=list(STAGE4_ABORT_CONDITIONS),
        rollback_procedure=list(_STAGE4_ROLLBACK_STEPS),
        system_class_status=system_status,
        stage4_evaluation_decision=stage4_eval.decision,
    )


def render_stage4_plan_markdown(plan: Stage4RolloutPlan) -> str:
    """Render Stage 4 rollout plan as operator-facing markdown."""
    lines = [
        "# Phase 7.15 — Stage 4 Rollout Plan (system class)",
        f"Generated: {plan.generated_at}",
        "",
        f"## Plan Status: {plan.plan_status.upper()}",
        "",
        f"**Stage 4 evaluation**: {plan.stage4_evaluation_decision}",
        f"**system class**: {plan.system_class_status}",
        "",
    ]

    # Why Stage 4 planning is now allowed
    lines.append("## Why Stage 4 Planning Is Allowed")
    lines.append("")
    if plan.stage4_evaluation_decision == "ready_for_stage4_planning":
        lines.append("The Stage 4 evaluation gate passes all 15 criteria:")
        lines.append("- Stage 3 stability confirmed (stable_continue)")
        lines.append("- Sufficient code_impl evidence accumulated")
        lines.append("- All hard criteria (heartbeat, failure rate, policy, budget) pass")
        lines.append("- All soft criteria (thresholds, anomalies, orphans, leases) pass")
    else:
        lines.append(f"**WARNING**: Stage 4 evaluation is `{plan.stage4_evaluation_decision}` —")
        lines.append("this plan is informational only. Prerequisites not yet met.")
    lines.append("")

    # Initial scope
    lines.append("## Proposed Initial Rollout Slice")
    lines.append("")
    lines.append(f"**Scope**: {plan.initial_scope}")
    lines.append("")

    # Allowed operations
    lines.append("### Allowed Operations (system_inspect)")
    lines.append("")
    lines.append("| Operation | Description |")
    lines.append("|-----------|-------------|")
    _op_descriptions = {
        "architecture_review": "Review codebase structure, patterns, and architecture",
        "config_review": "Read configuration files, inspect settings (read-only)",
        "dependency_audit": "Check installed dependencies, versions, compatibility",
        "health_audit": "Run health checks, review heartbeat, verify system state",
        "log_inspection": "Read and tail log files for diagnostics",
        "resource_monitoring": "Check disk, memory, CPU usage, list processes",
        "security_scan": "Read-only security review, vulnerability scanning",
        "status_check": "Check service status, systemctl status (read-only)",
    }
    for op in plan.allowed_operations:
        desc = _op_descriptions.get(op, "")
        lines.append(f"| {op} | {desc} |")
    lines.append("")

    # Blocked operations
    lines.append("### Blocked Operations (system_mutate — NOT allowed)")
    lines.append("")
    lines.append("| Operation | Why Blocked |")
    lines.append("|-----------|-------------|")
    _block_reasons = {
        "config_modification": "Mutates system configuration — high risk",
        "cron_modification": "Changes scheduled jobs — persistence risk",
        "deployment": "Production deployment — highest risk category",
        "file_system_mutation": "Creates/deletes/moves files outside sandbox",
        "git_operations": "Push, force-push — irreversible shared state changes",
        "network_changes": "Firewall, ports, DNS — infrastructure mutation",
        "package_management": "Installs/removes packages — system state mutation",
        "permission_changes": "chmod/chown — security-sensitive",
        "process_management": "kill/signal/spawn — affects running services",
        "self_modification": "Modifies own code/config — self-referential risk",
        "service_modification": "Start/stop/restart services — availability risk",
        "user_management": "Add/remove users, sudo — privilege escalation risk",
    }
    for op in plan.blocked_operations:
        reason = _block_reasons.get(op, "Mutation-capable — blocked in initial rollout")
        lines.append(f"| {op} | {reason} |")
    lines.append("")

    # Skills
    lines.append("### Skill Allowlist")
    lines.append("")
    lines.append("| Skill | Status |")
    lines.append("|-------|--------|")
    for s in plan.allowed_skills:
        lines.append(f"| {s} | ALLOWED |")
    for s in plan.blocked_skills:
        lines.append(f"| {s} | BLOCKED |")
    lines.append("")

    # Protections
    lines.append("## Required Protections")
    lines.append("")
    lines.append("1. **Verifier (maker-checker) mandatory** — every system task output must be verified")
    lines.append("2. **Operator approval required** — manual confirmation before activation")
    lines.append("3. **Inspect-only scope enforcement** — mutate-pattern filter rejects mutation tasks")
    lines.append("4. **Heartbeat prerequisite** — must be `healthy` (not just `not unhealthy`)")
    lines.append("5. **Zero-tolerance abort** — any policy violation or budget exhaustion triggers immediate rollback")
    lines.append("6. **shell-ops blocked** — no shell execution in system inspection path")
    lines.append("7. **Tighter success criteria** — failure rate, rejection rate, and contract failure thresholds lower than Stage 3")
    lines.append("")

    # Activation prerequisites
    lines.append("## Activation Prerequisites")
    lines.append("")
    lines.append("All must be met before any future activation step:")
    lines.append("")
    for name, desc in plan.activation_prerequisites.items():
        lines.append(f"- **{name}**: {desc}")
    lines.append("")

    # Success criteria
    lines.append("## Success Criteria (for future monitoring)")
    lines.append("")
    lines.append("| Criterion | Threshold |")
    lines.append("|-----------|-----------|")
    for name, threshold in plan.success_criteria.items():
        display = f"{threshold:.0%}" if isinstance(threshold, float) else str(threshold)
        lines.append(f"| {name} | {display} |")
    lines.append("")

    # Abort conditions
    lines.append("## Abort Conditions")
    lines.append("")
    lines.append("Any of the following triggers immediate rollback:")
    lines.append("")
    for i, condition in enumerate(plan.abort_conditions, 1):
        lines.append(f"{i}. {condition}")
    lines.append("")

    # Rollback procedure
    lines.append("## Rollback Procedure")
    lines.append("")
    for i, step in enumerate(plan.rollback_procedure, 1):
        lines.append(f"{i}. {step}")
    lines.append("")

    # Rollback command
    lines.append("### Quick Rollback Command")
    lines.append("```bash")
    lines.append('python3 -c "')
    lines.append("import json; p='STATE/config/feature_flags.json'")
    lines.append("d=json.loads(open(p).read())")
    lines.append("d['phase7_orchestrator']['supported_classes']=['research','code_review','code_impl']")
    lines.append("d['phase7_orchestrator']['rollout_stage']='stage3_research_code_review_code_impl'")
    lines.append("open(p,'w').write(json.dumps(d,indent=2))")
    lines.append("print('Rolled back to Stage 3')")
    lines.append('"')
    lines.append("sudo systemctl restart novacore-watcher")
    lines.append("```")
    lines.append("")

    # What this plan does NOT do
    lines.append("## What This Plan Does NOT Do")
    lines.append("")
    lines.append("- Does NOT activate system class")
    lines.append("- Does NOT add system to supported_classes")
    lines.append("- Does NOT modify feature flags")
    lines.append("- Does NOT route any tasks to system orchestrator path")
    lines.append("- Does NOT expand rollout beyond Stage 3")
    lines.append("")
    lines.append("A separate activation step (Phase 7.16+) would be required")
    lines.append("to implement this plan. That step must re-evaluate all")
    lines.append("prerequisites at activation time.")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_stage4_rollout_plan(
    plan: Stage4RolloutPlan,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write Stage 4 rollout plan to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stage4_rollout_plan.md"
    json_path = root / "STATE" / "stage4_rollout_plan.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_stage4_plan_markdown(plan))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(plan.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path


# ---------------------------------------------------------------------------
# Phase 7.16 — Stage 4 Activation Gate (system_inspect)
# ---------------------------------------------------------------------------

# Required fields in a valid Stage 4 rollout plan
_PLAN_REQUIRED_FIELDS = frozenset({
    "plan_status",
    "allowed_operations",
    "blocked_operations",
    "allowed_skills",
    "blocked_skills",
    "activation_prerequisites",
    "success_criteria",
    "abort_conditions",
    "rollback_procedure",
    "system_class_status",
})

# Required sections that must be non-empty lists/dicts
_PLAN_REQUIRED_NONEMPTY = frozenset({
    "allowed_operations",
    "blocked_operations",
    "activation_prerequisites",
    "success_criteria",
    "abort_conditions",
    "rollback_procedure",
})


def validate_stage4_plan(base: Path | None = None) -> tuple[bool, list[str]]:
    """Validate that the Stage 4 rollout plan is structurally valid.

    Checks:
      - Plan JSON exists and is parseable
      - All required fields present
      - Required sections are non-empty
      - Allowed operations are read-only (subset of STAGE4_ALLOWED_OPERATIONS)
      - Blocked operations include all mutation ops
      - system_class_status is "blocked"

    Returns (valid, errors) where errors is a list of failure reasons.
    """
    root = base or BASE
    plan_path = root / "STATE" / "stage4_rollout_plan.json"

    errors: list[str] = []

    # 1. File exists and is valid JSON
    if not plan_path.exists():
        return False, ["stage4_rollout_plan.json does not exist"]

    try:
        plan_data = json.loads(plan_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, [f"stage4_rollout_plan.json is not valid JSON: {e}"]

    if not isinstance(plan_data, dict):
        return False, ["stage4_rollout_plan.json root is not a dict"]

    # 2. Required fields present
    missing = _PLAN_REQUIRED_FIELDS - set(plan_data.keys())
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")

    # 3. Required sections non-empty
    for field_name in _PLAN_REQUIRED_NONEMPTY:
        val = plan_data.get(field_name)
        if val is not None and not val:
            errors.append(f"{field_name} is empty")

    # 4. Allowed operations must be subset of defined read-only ops
    allowed = set(plan_data.get("allowed_operations", []))
    if allowed and not allowed.issubset(STAGE4_ALLOWED_OPERATIONS):
        extra = sorted(allowed - STAGE4_ALLOWED_OPERATIONS)
        errors.append(f"allowed_operations contains non-inspect ops: {extra}")

    # 5. Blocked operations must include all mutation ops
    blocked = set(plan_data.get("blocked_operations", []))
    if blocked and not STAGE4_BLOCKED_OPERATIONS.issubset(blocked):
        missing_blocked = sorted(STAGE4_BLOCKED_OPERATIONS - blocked)
        errors.append(f"blocked_operations missing mutation ops: {missing_blocked}")

    # 6. system_class_status must be "blocked"
    status = plan_data.get("system_class_status", "")
    if status != "blocked":
        errors.append(f"system_class_status is '{status}', expected 'blocked'")

    return len(errors) == 0, errors


@dataclass
class Stage4ActivationGate:
    """Result of the Stage 4 activation gate evaluation."""
    decision: str          # "ready_to_activate_stage4" | "hold_stage4_activation" | "block_stage4_activation"
    criteria: list[RolloutCriterion] = field(default_factory=list)
    plan_validation: dict = field(default_factory=dict)  # {"valid": bool, "errors": [...]}
    evidence_summary: dict = field(default_factory=dict)
    stage3_stability_decision: str = ""
    stage4_evaluation_decision: str = ""
    generated_at: str = ""
    next_action: str = ""
    blocking_criteria: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "criteria": [c.to_dict() for c in self.criteria],
            "plan_validation": self.plan_validation,
            "evidence_summary": self.evidence_summary,
            "stage3_stability_decision": self.stage3_stability_decision,
            "stage4_evaluation_decision": self.stage4_evaluation_decision,
            "generated_at": self.generated_at,
            "next_action": self.next_action,
            "blocking_criteria": self.blocking_criteria,
        }


def evaluate_activation_gate_criteria(
    evidence: dict,
    stage3_decision: str,
    stage4_eval_decision: str,
    plan_valid: bool,
    plan_errors: list[str],
) -> list[RolloutCriterion]:
    """Evaluate Stage 4 activation gate criteria.

    These criteria determine whether the planned system_inspect slice
    is safe to activate. All criteria use repository-native evidence.
    """
    criteria: list[RolloutCriterion] = []

    # 1. Stage 3 stability must be stable_continue (hard)
    criteria.append(RolloutCriterion(
        name="stage3_stable",
        passed=stage3_decision == "stable_continue",
        value=stage3_decision,
        threshold="stable_continue",
        detail=(
            "Stage 3 stability confirmed"
            if stage3_decision == "stable_continue"
            else f"Stage 3 stability is '{stage3_decision}' — must be stable_continue"
        ),
        severity="hard",
    ))

    # 2. Stage 4 evaluation must be ready_for_stage4_planning (hard)
    criteria.append(RolloutCriterion(
        name="stage4_evaluation_ready",
        passed=stage4_eval_decision == "ready_for_stage4_planning",
        value=stage4_eval_decision,
        threshold="ready_for_stage4_planning",
        detail=(
            "Stage 4 evaluation gate passed"
            if stage4_eval_decision == "ready_for_stage4_planning"
            else f"Stage 4 evaluation is '{stage4_eval_decision}' — must be ready_for_stage4_planning"
        ),
        severity="hard",
    ))

    # 3. Heartbeat must be healthy (hard — stricter than "not unhealthy")
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb == "healthy",
        value=hb or "(no data)",
        threshold="healthy",
        detail=(
            "Heartbeat is healthy"
            if hb == "healthy"
            else f"Heartbeat is '{hb or '(no data)'}' — must be 'healthy' for system activation"
        ),
        severity="hard",
    ))

    # 4. No policy violations (hard — zero tolerance)
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations == 0,
        value=violations,
        threshold=0,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) — activation blocked"
        ),
        severity="hard",
    ))

    # 5. No budget exhaustions (hard — zero tolerance)
    budget = evidence.get("budget_exhaustions", 0)
    criteria.append(RolloutCriterion(
        name="no_budget_exhaustions",
        passed=budget == 0,
        value=budget,
        threshold=0,
        detail=(
            "No budget exhaustions"
            if budget == 0
            else f"{budget} budget exhaustion(s) — activation blocked"
        ),
        severity="hard",
    ))

    # 6. System class currently blocked (hard — must not already be active)
    supported = evidence.get("supported_classes", [])
    system_blocked = "system" not in supported
    criteria.append(RolloutCriterion(
        name="system_class_blocked",
        passed=system_blocked,
        value="blocked" if system_blocked else "enabled",
        threshold="blocked",
        detail=(
            "system class correctly blocked — ready for activation"
            if system_blocked
            else "system class already enabled — activation invalid"
        ),
        severity="hard",
    ))

    # 7. Stage 4 rollout plan is structurally valid (hard)
    criteria.append(RolloutCriterion(
        name="plan_valid",
        passed=plan_valid,
        value="valid" if plan_valid else f"invalid ({len(plan_errors)} error(s))",
        threshold="valid",
        detail=(
            "Stage 4 rollout plan is structurally valid"
            if plan_valid
            else f"Plan validation errors: {'; '.join(plan_errors)}"
        ),
        severity="hard",
    ))

    # 8. No rollback events in activation history (soft — indicates instability)
    activation_log = _read_jsonl(
        (evidence.get("_base_path") or BASE) / "STATE" / "activation_log.jsonl"
    )
    rollback_events = sum(
        1 for r in activation_log if r.get("outcome") == "rollback"
    )
    criteria.append(RolloutCriterion(
        name="no_rollback_history",
        passed=rollback_events == 0,
        value=rollback_events,
        threshold=0,
        detail=(
            "No rollback events in activation history"
            if rollback_events == 0
            else f"{rollback_events} rollback event(s) — indicates instability"
        ),
        severity="soft",
    ))

    # 9. No orphaned agents (soft)
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

    # 10. No stale leases (soft)
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


def decide_activation_gate(
    criteria: list[RolloutCriterion],
) -> tuple[str, str]:
    """Determine Stage 4 activation gate decision.

    Returns (decision, next_action) where decision is one of:
      - "ready_to_activate_stage4"
      - "hold_stage4_activation"
      - "block_stage4_activation"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure → block activation
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "block_stage4_activation",
            f"Hard failures blocking activation: {reasons}. "
            f"Resolve before re-evaluating. "
            f"system_inspect must not be activated.",
        )

    # Soft failures → hold activation
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "hold_stage4_activation",
            f"Soft concerns: {reasons}. "
            f"Continue monitoring. Re-evaluate after concerns resolve.",
        )

    # All clear → ready to activate
    return (
        "ready_to_activate_stage4",
        "All activation gate criteria pass. "
        "system_inspect is safe to activate under the defined protections. "
        "A separate activation step with operator approval is required.",
    )


def evaluate_activation_gate(
    base: Path | None = None,
) -> Stage4ActivationGate:
    """Run the Stage 4 activation gate.

    Evaluates whether the planned read-only system_inspect slice is safe
    to activate. Checks prerequisite gates (Stage 3 stability, Stage 4
    evaluation), validates the rollout plan, and evaluates activation-specific
    criteria.

    This is a gate only — it does NOT activate system_inspect.

    Decisions:
      - ready_to_activate_stage4: all criteria pass, plan valid
      - hold_stage4_activation: soft concerns
      - block_stage4_activation: hard failures or invalid plan
    """
    root = base or BASE

    # Collect general evidence
    evidence = collect_evidence(root)
    evidence["_base_path"] = root

    # Run prerequisite gates
    stage3_review = review_stage3_stability(root)
    stage4_eval = evaluate_stage4(root)

    # Validate the rollout plan
    plan_valid, plan_errors = validate_stage4_plan(root)

    # Evaluate activation gate criteria
    criteria = evaluate_activation_gate_criteria(
        evidence,
        stage3_review.decision,
        stage4_eval.decision,
        plan_valid,
        plan_errors,
    )
    decision, next_action = decide_activation_gate(criteria)

    # Identify blocking criteria
    blocking = [c.name for c in criteria if not c.passed]

    return Stage4ActivationGate(
        decision=decision,
        criteria=criteria,
        plan_validation={"valid": plan_valid, "errors": plan_errors},
        evidence_summary={
            "total_workflows": evidence.get("total_workflows", 0),
            "completed_workflows": evidence.get("completed_workflows", 0),
            "failed_workflows": evidence.get("failed_workflows", 0),
            "halted_workflows": evidence.get("halted_workflows", 0),
            "heartbeat_overall": evidence.get("heartbeat_overall", ""),
            "policy_violations": evidence.get("policy_violations", 0),
            "budget_exhaustions": evidence.get("budget_exhaustions", 0),
            "orphaned_agents": evidence.get("orphaned_agents", 0),
            "stale_leases": evidence.get("stale_leases", 0),
            "recovery_events": evidence.get("recovery_events", 0),
            "rollout_stage": evidence.get("rollout_stage", "unknown"),
            "supported_classes": evidence.get("supported_classes", []),
        },
        stage3_stability_decision=stage3_review.decision,
        stage4_evaluation_decision=stage4_eval.decision,
        next_action=next_action,
        blocking_criteria=blocking,
    )


def render_activation_gate_markdown(gate: Stage4ActivationGate) -> str:
    """Render Stage 4 activation gate as an operator-facing markdown report."""
    icon = {
        "ready_to_activate_stage4": "READY",
        "hold_stage4_activation": "HOLD",
        "block_stage4_activation": "BLOCKED",
    }.get(gate.decision, "?")

    lines = [
        "# Phase 7.16 — Stage 4 Activation Gate (system_inspect)",
        f"Generated: {gate.generated_at}",
        "",
        f"## Decision: {icon} — {gate.decision}",
        "",
        f"**Stage 3 stability**: {gate.stage3_stability_decision}",
        f"**Stage 4 evaluation**: {gate.stage4_evaluation_decision}",
        f"**Plan valid**: {'YES' if gate.plan_validation.get('valid') else 'NO'}",
        "",
        f"**Next action**: {gate.next_action}",
        "",
    ]

    # Plan validation
    lines.append("## Plan Validation")
    lines.append("")
    if gate.plan_validation.get("valid"):
        lines.append("Stage 4 rollout plan is structurally valid.")
    else:
        lines.append("**Plan validation FAILED:**")
        for err in gate.plan_validation.get("errors", []):
            lines.append(f"- {err}")
    lines.append("")

    # Activation gate criteria
    lines.append("## Activation Gate Criteria")
    lines.append("")
    lines.append("| # | Criterion | Status | Value | Threshold | Severity | Detail |")
    lines.append("|---|-----------|--------|-------|-----------|----------|--------|")
    for i, c in enumerate(gate.criteria, 1):
        status = "PASS" if c.passed else "FAIL"
        val = c.value if c.value is not None else "N/A"
        lines.append(
            f"| {i} | {c.name} | {status} | {val} | {c.threshold} "
            f"| {c.severity} | {c.detail} |"
        )
    lines.append("")

    # Blocking criteria
    if gate.blocking_criteria:
        lines.append("## Blocking Criteria")
        lines.append("")
        for name in gate.blocking_criteria:
            c = next((x for x in gate.criteria if x.name == name), None)
            if c:
                lines.append(f"- **{name}** ({c.severity}): {c.detail}")
        lines.append("")

    # Evidence summary
    ev = gate.evidence_summary
    lines.append("## Evidence Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for k, v in ev.items():
        if isinstance(v, float):
            display = f"{v:.1%}"
        elif isinstance(v, list):
            display = ", ".join(str(x) for x in v) or "(none)"
        elif v is not None:
            display = str(v)
        else:
            display = "N/A"
        lines.append(f"| {k} | {display} |")
    lines.append("")

    # What this gate does and does not do
    lines.append("## Scope")
    lines.append("")
    lines.append("This gate determines whether the planned `system_inspect` slice")
    lines.append("is safe to activate. It does NOT:")
    lines.append("- Activate system_inspect")
    lines.append("- Modify feature flags")
    lines.append("- Add system to supported_classes")
    lines.append("- Route any tasks to the system orchestrator path")
    lines.append("")
    if gate.decision == "ready_to_activate_stage4":
        lines.append("### Activation")
        lines.append("")
        lines.append("All criteria pass. A separate activation step with explicit")
        lines.append("operator approval is required to proceed.")
        lines.append("```python")
        lines.append("# Activation step (Phase 7.17+) would:")
        lines.append("# 1. Re-evaluate all prerequisites at activation time")
        lines.append("# 2. Add system to supported_classes (inspect-only scope)")
        lines.append("# 3. Enable mutate-signal filter")
        lines.append("# 4. Write activation record to audit trail")
        lines.append("# 5. Enable manual approval hook for system paths")
        lines.append("```")
    else:
        lines.append("### Next Steps")
        lines.append("")
        lines.append("Resolve blocking criteria before re-evaluating.")
        lines.append("```python")
        lines.append("from agents.rollout_gate import evaluate_activation_gate")
        lines.append("gate = evaluate_activation_gate()")
        lines.append("print(gate.decision, gate.blocking_criteria)")
        lines.append("```")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_activation_gate(
    gate: Stage4ActivationGate,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write Stage 4 activation gate result to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stage4_activation_gate.md"
    json_path = root / "STATE" / "stage4_activation_gate.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_activation_gate_markdown(gate))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(gate.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path


# ---------------------------------------------------------------------------
# Phase 7.17 — Controlled Stage 4 Activation (system_inspect)
# ---------------------------------------------------------------------------

# Stage 4 target configuration
STAGE4_CLASSES = ["research", "code_review", "code_impl", "system"]
STAGE4_ROLLOUT_STAGE = "stage4_research_code_review_code_impl_system_inspect"
STAGE4_ALLOWED_ROLES = ["research", "coding", "system_inspect"]


@dataclass
class Stage4ActivationRecord:
    """Audit record of a Stage 4 activation attempt."""
    attempted_at: str
    outcome: str           # "activated" | "blocked" | "error"
    gate_decision: str     # activation gate decision at time of activation
    reason: str
    pre_config: dict       # config snapshot before attempt
    post_config: dict      # config snapshot after attempt (same if blocked)
    blocking_criteria: list[str]
    activated_scope: str   # "system_inspect" or ""
    plan_validation: dict  # plan validation result at activation time

    def to_dict(self) -> dict:
        return {
            "attempted_at": self.attempted_at,
            "outcome": self.outcome,
            "gate_decision": self.gate_decision,
            "reason": self.reason,
            "pre_config": self.pre_config,
            "post_config": self.post_config,
            "blocking_criteria": self.blocking_criteria,
            "activated_scope": self.activated_scope,
            "plan_validation": self.plan_validation,
        }


def activate_stage4(base: Path | None = None) -> Stage4ActivationRecord:
    """Safe, auditable Stage 4 activation procedure for system_inspect.

    1. Snapshots current config
    2. Re-runs Stage 4 activation gate (fresh evidence)
    3. Only activates if gate returns ready_to_activate_stage4
    4. Updates feature_flags.json with system in inspect-only scope
    5. Writes activation audit record to STATE/activation_log.jsonl
    6. Returns Stage4ActivationRecord with full details

    Fail-closed: any gate failure, config error, or non-ready decision
    blocks activation and preserves current state.

    Scope: system_inspect ONLY. Mutation operations remain blocked
    via Stage D denylist in task_classifier and orchestrator_adapter.
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
        record = Stage4ActivationRecord(
            attempted_at=now_iso,
            outcome="error",
            gate_decision="unknown",
            reason="cannot_read_feature_flags",
            pre_config={},
            post_config={},
            blocking_criteria=[],
            activated_scope="",
            plan_validation={},
        )
        _append_activation_log(log_path, record)
        return record

    # 2. Re-run activation gate with fresh evidence
    gate = evaluate_activation_gate(root)

    if gate.decision != "ready_to_activate_stage4":
        record = Stage4ActivationRecord(
            attempted_at=now_iso,
            outcome="blocked",
            gate_decision=gate.decision,
            reason=gate.next_action,
            pre_config=pre_config.get("phase7_orchestrator", {}),
            post_config=pre_config.get("phase7_orchestrator", {}),
            blocking_criteria=gate.blocking_criteria,
            activated_scope="",
            plan_validation=gate.plan_validation,
        )
        _append_activation_log(log_path, record)
        return record

    # 3. Gate approved — update feature flags for Stage 4 (system_inspect)
    try:
        # Re-read for freshest version
        flags_data = json.loads(flags_path.read_text())
    except (json.JSONDecodeError, OSError):
        record = Stage4ActivationRecord(
            attempted_at=now_iso,
            outcome="error",
            gate_decision=gate.decision,
            reason="cannot_read_feature_flags_for_update",
            pre_config=pre_config.get("phase7_orchestrator", {}),
            post_config=pre_config.get("phase7_orchestrator", {}),
            blocking_criteria=[],
            activated_scope="",
            plan_validation=gate.plan_validation,
        )
        _append_activation_log(log_path, record)
        return record

    orch = flags_data.get("phase7_orchestrator", {})
    orch["supported_classes"] = list(STAGE4_CLASSES)
    orch["rollout_stage"] = STAGE4_ROLLOUT_STAGE
    orch["stage"] = "D"
    orch["allowed_roles"] = list(STAGE4_ALLOWED_ROLES)
    orch["verifier_required"] = True  # mandatory for system tasks
    orch["system_scope"] = "inspect_only"  # scope qualifier
    orch["system_allowed_operations"] = sorted(STAGE4_ALLOWED_OPERATIONS)
    orch["system_blocked_operations"] = sorted(STAGE4_BLOCKED_OPERATIONS)
    orch["system_allowed_skills"] = sorted(STAGE4_ALLOWED_SKILLS)
    orch["system_blocked_skills"] = sorted(STAGE4_BLOCKED_SKILLS)
    orch["stage_description"] = (
        "Stage 4 rollout — research + code_review + code_impl + system_inspect. "
        "system_inspect is read-only: status checks, log inspection, config review, "
        "health audits, resource monitoring, architecture review, dependency audits, "
        "security scans. All mutation-capable system operations remain blocked. "
        "Verifier gate mandatory. Maker-checker enforced. "
        "Shell-ops blocked for system path."
    )
    orch["stage4_activation_notes"] = (
        "Activated via Phase 7.17 controlled activation. "
        "Scope: system_inspect (read-only only). "
        "Mutation operations blocked via Stage D denylist. "
        "Abort conditions: policy violation, budget exhaustion, "
        "heartbeat unhealthy, mutate pattern detected, verifier rejection."
    )
    flags_data["phase7_orchestrator"] = orch
    flags_data["version"] = flags_data.get("version", 0) + 1
    flags_data["updated_at"] = now_iso

    # Atomic write
    flags_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = flags_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(flags_data, indent=2))
    tmp.rename(flags_path)

    # Read post-activation config
    try:
        post_config = json.loads(flags_path.read_text())
    except (json.JSONDecodeError, OSError):
        post_config = flags_data

    # 4. Write activation audit record
    record = Stage4ActivationRecord(
        attempted_at=now_iso,
        outcome="activated",
        gate_decision=gate.decision,
        reason="Stage 4 activation applied — system_inspect added to supported_classes (read-only scope)",
        pre_config=pre_config.get("phase7_orchestrator", {}),
        post_config=post_config.get("phase7_orchestrator", {}),
        blocking_criteria=[],
        activated_scope="system_inspect",
        plan_validation=gate.plan_validation,
    )
    _append_activation_log(log_path, record)

    return record


def render_stage4_activation_markdown(record: Stage4ActivationRecord) -> str:
    """Render Stage 4 activation result as an operator-facing markdown report."""
    icon = {
        "activated": "ACTIVATED",
        "blocked": "BLOCKED",
        "error": "ERROR",
    }.get(record.outcome, "?")

    lines = [
        "# Phase 7.17 — Stage 4 Activation Result (system_inspect)",
        f"Attempted: {record.attempted_at}",
        "",
        f"## Outcome: {icon}",
        "",
        f"**Gate decision**: {record.gate_decision}",
        f"**Activated scope**: {record.activated_scope or '(none)'}",
        f"**Reason**: {record.reason}",
        "",
    ]

    if record.outcome == "activated":
        # Enabled scope
        lines.append("## Enabled Scope: system_inspect (read-only)")
        lines.append("")
        lines.append("### Allowed Operations")
        lines.append("")
        for op in sorted(STAGE4_ALLOWED_OPERATIONS):
            lines.append(f"- {op}")
        lines.append("")
        lines.append("### Allowed Skills")
        lines.append("")
        for skill in sorted(STAGE4_ALLOWED_SKILLS):
            lines.append(f"- {skill}")
        lines.append("")

        # Still-blocked scope
        lines.append("## Still-Blocked Scope: system_mutate")
        lines.append("")
        lines.append("### Blocked Operations")
        lines.append("")
        for op in sorted(STAGE4_BLOCKED_OPERATIONS):
            lines.append(f"- {op}")
        lines.append("")
        lines.append("### Blocked Skills")
        lines.append("")
        for skill in sorted(STAGE4_BLOCKED_SKILLS):
            lines.append(f"- {skill}")
        lines.append("")

        # Monitoring checklist
        lines.append("## Monitoring Checklist")
        lines.append("")
        for name, threshold in STAGE4_SUCCESS_CRITERIA.items():
            display = f"{threshold:.0%}" if isinstance(threshold, float) else str(threshold)
            lines.append(f"- [ ] {name}: threshold = {display}")
        lines.append("")

        # Abort conditions
        lines.append("## Abort Conditions")
        lines.append("")
        for i, condition in enumerate(STAGE4_ABORT_CONDITIONS, 1):
            lines.append(f"{i}. {condition}")
        lines.append("")

        # Rollback path
        lines.append("## Rollback Path")
        lines.append("")
        for i, step in enumerate(_STAGE4_ROLLBACK_STEPS, 1):
            lines.append(f"{i}. {step}")
        lines.append("")
        lines.append("### Quick Rollback Command")
        lines.append("```bash")
        lines.append('python3 -c "')
        lines.append("import json; p='STATE/config/feature_flags.json'")
        lines.append("d=json.loads(open(p).read())")
        lines.append("d['phase7_orchestrator']['supported_classes']=['research','code_review','code_impl']")
        lines.append("d['phase7_orchestrator']['rollout_stage']='stage3_research_code_review_code_impl'")
        lines.append("d['phase7_orchestrator']['stage']='C'")
        lines.append("d['phase7_orchestrator'].pop('system_scope', None)")
        lines.append("d['phase7_orchestrator'].pop('system_allowed_operations', None)")
        lines.append("d['phase7_orchestrator'].pop('system_blocked_operations', None)")
        lines.append("d['phase7_orchestrator'].pop('system_allowed_skills', None)")
        lines.append("d['phase7_orchestrator'].pop('system_blocked_skills', None)")
        lines.append("open(p,'w').write(json.dumps(d,indent=2))")
        lines.append("print('Rolled back to Stage 3')")
        lines.append('"')
        lines.append("sudo systemctl restart novacore-watcher")
        lines.append("```")
        lines.append("")

        # Config diff
        lines.append("## Config Change")
        lines.append("")
        pre = record.pre_config
        post = record.post_config
        lines.append(f"- **supported_classes**: {pre.get('supported_classes', [])} → {post.get('supported_classes', [])}")
        lines.append(f"- **stage**: {pre.get('stage', '?')} → {post.get('stage', '?')}")
        lines.append(f"- **rollout_stage**: {pre.get('rollout_stage', '?')} → {post.get('rollout_stage', '?')}")
        lines.append(f"- **system_scope**: (new) → {post.get('system_scope', '?')}")
        lines.append("")

    elif record.blocking_criteria:
        lines.append("## Blocking Criteria")
        lines.append("")
        for name in record.blocking_criteria:
            lines.append(f"- {name}")
        lines.append("")

    # Plan validation at activation time
    pv = record.plan_validation
    if pv:
        lines.append("## Plan Validation at Activation Time")
        lines.append("")
        lines.append(f"- **Valid**: {'YES' if pv.get('valid') else 'NO'}")
        if pv.get("errors"):
            for err in pv["errors"]:
                lines.append(f"  - {err}")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_stage4_activation_result(
    record: Stage4ActivationRecord,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write Stage 4 activation result to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stage4_activation_result.md"
    json_path = root / "STATE" / "stage4_activation_result.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_stage4_activation_markdown(record))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(record.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path


# ---------------------------------------------------------------------------
# Phase 7.18 — Stage D Monitoring and Stability Review
# ---------------------------------------------------------------------------

@dataclass
class StageDStabilityReview:
    """Deterministic stability review for live Stage D rollout.

    Evaluates whether the system_inspect read-only slice is operating
    safely under real conditions.
    """
    decision: str          # "stable_continue_stageD" | "hold_stageD" | "rollback_system_inspect_recommended"
    rollout_stage: str
    enabled_classes: list[str] = field(default_factory=list)
    criteria: list[RolloutCriterion] = field(default_factory=list)
    system_inspect_metrics: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    generated_at: str = ""
    next_action: str = ""
    activation_record: dict = field(default_factory=dict)
    observation_window: dict = field(default_factory=dict)
    scope_integrity: dict = field(default_factory=dict)

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
            "system_inspect_metrics": self.system_inspect_metrics,
            "evidence_summary": self.evidence_summary,
            "generated_at": self.generated_at,
            "next_action": self.next_action,
            "activation_record": self.activation_record,
            "observation_window": self.observation_window,
            "scope_integrity": self.scope_integrity,
        }


def _collect_system_inspect_metrics(
    workflows: list[dict],
) -> dict:
    """Extract system_inspect-specific metrics from workflow records."""
    sys_workflows = [
        w for w in workflows if w.get("task_class") == "system"
    ]
    sys_completed = [w for w in sys_workflows if w.get("status") == "completed"]
    sys_failed = [
        w for w in sys_workflows
        if w.get("status") in ("failed", "halted")
    ]
    sys_rejected = [
        w for w in sys_workflows
        if w.get("halt_reason") == "verifier_rejected"
    ]

    total_terminal = len(sys_completed) + len(sys_failed)
    failure_rate = (
        round(len(sys_failed) / total_terminal, 3) if total_terminal > 0 else None
    )

    return {
        "total_runs": len(sys_workflows),
        "completed": len(sys_completed),
        "failed": len(sys_failed),
        "verifier_rejected": len(sys_rejected),
        "failure_rate": failure_rate,
    }


def _count_blocked_mutation_attempts(base: Path, since_ts: str = "") -> int:
    """Count mutation attempts that were blocked by Stage D enforcement.

    Reads policy_denials.jsonl for entries with system/mutate signals
    since the given timestamp.
    """
    denials = _read_jsonl(base / "STATE" / "policy_denials.jsonl")
    count = 0
    for d in denials:
        deny_class = d.get("task_class", "")
        deny_reason = d.get("reason", "")
        deny_ts = d.get("timestamp", d.get("denied_at", ""))
        if since_ts and deny_ts < since_ts:
            continue
        if deny_class == "system" or "mutate" in deny_reason.lower():
            count += 1
    return count


def _verify_scope_integrity(base: Path) -> dict:
    """Verify that system scope has remained inspect_only.

    Returns a dict with scope integrity checks.
    """
    root = base
    ff = _read_json(root / "STATE" / "config" / "feature_flags.json")
    if not ff:
        return {
            "intact": False,
            "reason": "feature_flags.json not found",
            "system_scope": "unknown",
            "system_in_classes": False,
        }

    orch = ff.get("phase7_orchestrator", {})
    scope = orch.get("system_scope", "")
    classes = orch.get("supported_classes", [])
    stage = orch.get("stage", "")
    allowed_ops = set(orch.get("system_allowed_operations", []))
    blocked_ops = set(orch.get("system_blocked_operations", []))

    issues = []

    if scope != "inspect_only":
        issues.append(f"system_scope is '{scope}', expected 'inspect_only'")

    if stage != "D":
        issues.append(f"stage is '{stage}', expected 'D'")

    if "system" not in classes:
        issues.append("'system' not in supported_classes")

    if allowed_ops and not allowed_ops.issubset(STAGE4_ALLOWED_OPERATIONS):
        extra = sorted(allowed_ops - STAGE4_ALLOWED_OPERATIONS)
        issues.append(f"unexpected allowed operations: {extra}")

    if blocked_ops and not STAGE4_BLOCKED_OPERATIONS.issubset(blocked_ops):
        missing = sorted(STAGE4_BLOCKED_OPERATIONS - blocked_ops)
        issues.append(f"missing blocked operations: {missing}")

    return {
        "intact": len(issues) == 0,
        "reason": "; ".join(issues) if issues else "scope intact",
        "system_scope": scope,
        "system_in_classes": "system" in classes,
        "stage": stage,
    }


def evaluate_stageD_stability(
    evidence: dict,
    system_inspect_metrics: dict,
    post_activation_recoveries: int,
    blocked_mutation_attempts: int,
    scope_integrity: dict,
) -> list[RolloutCriterion]:
    """Evaluate Stage D live stability criteria.

    13 criteria total: 7 hard, 6 soft.
    """
    criteria: list[RolloutCriterion] = []

    # 1. Heartbeat healthy (hard — strict: must be exactly "healthy")
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb == STAGED_HEARTBEAT_REQUIRED,
        value=hb or "(no data)",
        threshold=STAGED_HEARTBEAT_REQUIRED,
        detail=(
            f"Current heartbeat: {hb or '(no data)'}"
            if hb == STAGED_HEARTBEAT_REQUIRED
            else f"Heartbeat is '{hb or '(no data)'}', required '{STAGED_HEARTBEAT_REQUIRED}'"
        ),
        severity="hard",
    ))

    # 2. No policy violations (hard — zero tolerance)
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations <= STAGED_MAX_POLICY_VIOLATIONS,
        value=violations,
        threshold=STAGED_MAX_POLICY_VIOLATIONS,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) — review immediately"
        ),
        severity="hard",
    ))

    # 3. No budget exhaustions (hard — zero tolerance)
    budget = evidence.get("budget_exhaustions", 0)
    criteria.append(RolloutCriterion(
        name="no_budget_exhaustions",
        passed=budget <= STAGED_MAX_BUDGET_EXHAUSTIONS,
        value=budget,
        threshold=STAGED_MAX_BUDGET_EXHAUSTIONS,
        detail=(
            "No budget exhaustions"
            if budget == 0
            else f"{budget} budget exhaustion(s)"
        ),
        severity="hard",
    ))

    # 4. Overall failure rate (hard)
    failure_rate = evidence.get("failure_rate")
    if failure_rate is not None:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=failure_rate <= STAGED_MAX_FAILURE_RATE,
            value=failure_rate,
            threshold=STAGED_MAX_FAILURE_RATE,
            detail=f"Overall failure rate {failure_rate:.1%} (max {STAGED_MAX_FAILURE_RATE:.0%})",
            severity="hard",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=True,
            value=None,
            threshold=STAGED_MAX_FAILURE_RATE,
            detail="No terminal workflows yet",
            severity="soft",
        ))

    # 5. Scope integrity (hard — system_inspect must remain read-only)
    criteria.append(RolloutCriterion(
        name="scope_integrity",
        passed=scope_integrity.get("intact", False),
        value=scope_integrity.get("reason", "unknown"),
        threshold="intact",
        detail=(
            "Scope integrity verified: system_inspect remains read-only"
            if scope_integrity.get("intact")
            else f"Scope integrity violated: {scope_integrity.get('reason', 'unknown')}"
        ),
        severity="hard",
    ))

    # 6. Stage is D (hard — must still be in Stage D)
    stage = evidence.get("rollout_stage", "unknown")
    is_stage_d = "stage4" in stage or "system_inspect" in stage
    criteria.append(RolloutCriterion(
        name="stage_is_D",
        passed=is_stage_d,
        value=stage,
        threshold="stage4_*",
        detail=(
            f"Rollout stage confirmed: {stage}"
            if is_stage_d
            else f"Unexpected rollout stage: {stage}"
        ),
        severity="hard",
    ))

    # 7. system class in supported_classes (hard)
    classes = evidence.get("supported_classes", [])
    criteria.append(RolloutCriterion(
        name="system_class_active",
        passed="system" in classes,
        value=classes,
        threshold="system in supported_classes",
        detail=(
            f"system class active in: {classes}"
            if "system" in classes
            else f"system class not found in: {classes}"
        ),
        severity="hard",
    ))

    # 8. system_inspect minimum observation (soft — insufficient evidence = hold)
    sys_total = system_inspect_metrics.get("total_runs", 0)
    criteria.append(RolloutCriterion(
        name="system_inspect_minimum_runs",
        passed=sys_total >= STAGED_MIN_SYSTEM_INSPECT_RUNS,
        value=sys_total,
        threshold=STAGED_MIN_SYSTEM_INSPECT_RUNS,
        detail=(
            f"{sys_total} system_inspect runs "
            f"(need >= {STAGED_MIN_SYSTEM_INSPECT_RUNS})"
        ),
        severity="soft",
    ))

    # 9. system_inspect failure rate (soft)
    sys_fr = system_inspect_metrics.get("failure_rate")
    if sys_fr is not None:
        criteria.append(RolloutCriterion(
            name="system_inspect_failure_rate",
            passed=sys_fr <= STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
            value=sys_fr,
            threshold=STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
            detail=(
                f"system_inspect failure rate {sys_fr:.1%} "
                f"(max {STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="system_inspect_failure_rate",
            passed=True,
            value=None,
            threshold=STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
            detail="No system_inspect terminal workflows yet",
            severity="soft",
        ))

    # 10. Verifier rejection rate for system tasks (soft)
    sys_rejected = system_inspect_metrics.get("verifier_rejected", 0)
    sys_completed = system_inspect_metrics.get("completed", 0)
    sys_total_for_vr = sys_rejected + sys_completed
    if sys_total_for_vr > 0:
        vr_rate = round(sys_rejected / sys_total_for_vr, 3)
        criteria.append(RolloutCriterion(
            name="system_verifier_rejection_rate",
            passed=vr_rate <= STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
            value=vr_rate,
            threshold=STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
            detail=(
                f"system_inspect verifier rejection rate {vr_rate:.1%} "
                f"(max {STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="system_verifier_rejection_rate",
            passed=True,
            value=None,
            threshold=STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
            detail="No system verifier reports yet — N/A",
            severity="soft",
        ))

    # 11. Recovery anomalies (soft — zero tolerance for system scope)
    criteria.append(RolloutCriterion(
        name="recovery_anomalies",
        passed=post_activation_recoveries <= STAGED_MAX_RECOVERY_ANOMALIES,
        value=post_activation_recoveries,
        threshold=STAGED_MAX_RECOVERY_ANOMALIES,
        detail=(
            f"{post_activation_recoveries} recovery events since Stage D activation "
            f"(max {STAGED_MAX_RECOVERY_ANOMALIES})"
        ),
        severity="soft",
    ))

    # 12. Blocked mutation attempts within bounds (soft)
    criteria.append(RolloutCriterion(
        name="blocked_mutation_attempts",
        passed=blocked_mutation_attempts <= STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS,
        value=blocked_mutation_attempts,
        threshold=STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS,
        detail=(
            f"{blocked_mutation_attempts} blocked mutation attempt(s) "
            f"(max {STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS})"
        ),
        severity="soft",
    ))

    # 13. No orphaned agents or stale leases (soft)
    orphaned = evidence.get("orphaned_agents", 0)
    stale = evidence.get("stale_leases", 0)
    anomaly_count = orphaned + stale
    criteria.append(RolloutCriterion(
        name="no_workflow_anomalies",
        passed=anomaly_count == 0,
        value=anomaly_count,
        threshold=0,
        detail=(
            "No orphaned agents or stale leases"
            if anomaly_count == 0
            else f"{orphaned} orphaned agent(s), {stale} stale lease(s)"
        ),
        severity="soft",
    ))

    return criteria


def decide_stageD_stability(
    criteria: list[RolloutCriterion],
) -> tuple[str, str]:
    """Determine Stage D live stability from evaluated criteria.

    Returns (decision, next_action) where decision is one of:
      - "stable_continue_stageD"
      - "hold_stageD"
      - "rollback_system_inspect_recommended"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure -> rollback system_inspect
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "rollback_system_inspect_recommended",
            f"Hard failures detected: {reasons}. "
            f"Remove system from supported_classes and revert to Stage C. "
            f"Investigate scope integrity and policy compliance before re-enabling.",
        )

    # Insufficient system_inspect evidence -> hold
    sys_runs = next(
        (c for c in criteria if c.name == "system_inspect_minimum_runs"), None
    )
    if sys_runs and not sys_runs.passed:
        return (
            "hold_stageD",
            f"Insufficient system_inspect evidence: {sys_runs.value} runs "
            f"(need >= {sys_runs.threshold}). "
            f"Continue operating Stage D and re-evaluate after more system_inspect tasks.",
        )

    # Any soft failure -> hold
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "hold_stageD",
            f"Soft concerns: {reasons}. "
            f"Continue monitoring Stage D. Do not expand system scope.",
        )

    # All clear -> stable
    return (
        "stable_continue_stageD",
        "Stage D is stable. system_inspect operating within all thresholds. "
        "Scope integrity confirmed. "
        "Safe to continue current Stage D rollout. "
        "Broader system scope expansion may be evaluated in a future phase.",
    )


def review_stageD_stability(
    base: Path | None = None,
) -> StageDStabilityReview:
    """Run the Stage D post-activation stability review.

    Collects evidence, evaluates Stage D-specific criteria with extra
    attention to system_inspect scope integrity, and returns a
    deterministic decision.

    Decisions:
      - stable_continue_stageD: Stage D operating safely
      - hold_stageD: insufficient evidence or soft concerns
      - rollback_system_inspect_recommended: hard failures detected
    """
    root = base or BASE
    state = root / "STATE"

    # Collect general evidence (reuse existing collector)
    evidence = collect_evidence(root)

    # Collect system_inspect-specific metrics
    workflows = _list_json_files(state / "workflows")
    system_inspect_metrics = _collect_system_inspect_metrics(workflows)

    # Get Stage D activation record for observation window
    activation_log = _read_jsonl(state / "activation_log.jsonl")
    stage4_activations = [
        r for r in activation_log
        if r.get("activated_scope") == "system_inspect"
        and r.get("outcome") == "activated"
    ]
    activation = stage4_activations[-1] if stage4_activations else {}
    activation_ts = activation.get("attempted_at", "")

    # Observation window
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    observation_window = {
        "activation_at": activation_ts or "N/A",
        "review_at": now_iso,
        "scope": "system_inspect (read-only)",
    }

    # Count post-activation recovery anomalies
    post_recoveries = _count_post_activation_recoveries(root, activation_ts)

    # Count blocked mutation attempts since activation
    blocked_mutations = _count_blocked_mutation_attempts(root, activation_ts)

    # Verify scope integrity
    scope_integrity = _verify_scope_integrity(root)

    # Evaluate Stage D stability criteria
    criteria = evaluate_stageD_stability(
        evidence,
        system_inspect_metrics,
        post_recoveries,
        blocked_mutations,
        scope_integrity,
    )
    decision, next_action = decide_stageD_stability(criteria)

    return StageDStabilityReview(
        decision=decision,
        rollout_stage=evidence.get("rollout_stage", "unknown"),
        enabled_classes=evidence.get("supported_classes", []),
        criteria=criteria,
        system_inspect_metrics=system_inspect_metrics,
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
            "blocked_mutation_attempts": blocked_mutations,
        },
        next_action=next_action,
        activation_record=activation,
        observation_window=observation_window,
        scope_integrity=scope_integrity,
    )


def render_stageD_stability_markdown(review: StageDStabilityReview) -> str:
    """Render Stage D stability review as an operator-facing markdown report."""
    icon = {
        "stable_continue_stageD": "STABLE",
        "hold_stageD": "HOLD",
        "rollback_system_inspect_recommended": "ROLLBACK",
    }.get(review.decision, "?")

    lines = [
        "# Phase 7.18 — Stage D Stability Review (system_inspect)",
        f"Generated: {review.generated_at}",
        "",
        f"## Decision: {icon} — {review.decision}",
        "",
        f"**Rollout stage**: {review.rollout_stage}",
        f"**Enabled classes**: {', '.join(review.enabled_classes)}",
        f"**System scope**: inspect_only (read-only)",
        "",
        f"**Next action**: {review.next_action}",
        "",
    ]

    # Observation window
    ow = review.observation_window
    lines.append("## Observation Window")
    lines.append("")
    lines.append(f"- **Activation at**: {ow.get('activation_at', 'N/A')}")
    lines.append(f"- **Review at**: {ow.get('review_at', 'N/A')}")
    lines.append(f"- **Scope under review**: {ow.get('scope', 'N/A')}")
    lines.append("")

    # Scope integrity
    si = review.scope_integrity
    lines.append("## Scope Integrity")
    lines.append("")
    si_status = "INTACT" if si.get("intact") else "VIOLATED"
    lines.append(f"- **Status**: {si_status}")
    lines.append(f"- **Detail**: {si.get('reason', 'N/A')}")
    lines.append(f"- **system_scope**: {si.get('system_scope', 'N/A')}")
    lines.append(f"- **system in classes**: {si.get('system_in_classes', False)}")
    lines.append("")

    # system_inspect metrics
    m = review.system_inspect_metrics
    lines.append("## system_inspect Metrics")
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

    # Blocked mutation attempts detail
    bma = ev.get("blocked_mutation_attempts", 0)
    lines.append("## Blocked Mutation Attempts")
    lines.append("")
    if bma == 0:
        lines.append("No mutation attempts were blocked during the observation window.")
    else:
        lines.append(
            f"{bma} mutation attempt(s) blocked by Stage D enforcement. "
            f"Threshold: {STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS}."
        )
    lines.append("")

    # Required operator action
    lines.append("## Required Operator Action")
    lines.append("")
    if review.decision == "stable_continue_stageD":
        lines.append("- No action required. Stage D is operating safely.")
        lines.append("- Continue monitoring system_inspect behavior.")
        lines.append("- Do NOT expand system scope without a future Phase evaluation.")
    elif review.decision == "hold_stageD":
        lines.append("- Do NOT expand system scope.")
        lines.append("- Continue current Stage D operation and gather more evidence.")
        lines.append("- Re-run this stability review after additional system_inspect tasks.")
    else:
        lines.append("- **ROLLBACK RECOMMENDED**: Revert to Stage C immediately.")
        lines.append("- Remove 'system' from supported_classes in feature_flags.json.")
        lines.append("- Set stage back to 'C' and rollout_stage to 'stage3_research_code_review_code_impl'.")
        lines.append("- Remove system_scope and system_allowed/blocked configs.")
        lines.append("- Restart watcher service: `sudo systemctl restart novacore-watcher`")
        lines.append("- Investigate root cause before re-enabling system_inspect.")
    lines.append("")

    # Rollback path
    lines.append("## Rollback Path (Stage D -> Stage C)")
    lines.append("")
    lines.append("```bash")
    lines.append('python3 -c "')
    lines.append("import json; p='STATE/config/feature_flags.json'")
    lines.append("d=json.loads(open(p).read())")
    lines.append("d['phase7_orchestrator']['supported_classes']=['research','code_review','code_impl']")
    lines.append("d['phase7_orchestrator']['rollout_stage']='stage3_research_code_review_code_impl'")
    lines.append("d['phase7_orchestrator']['stage']='C'")
    lines.append("d['phase7_orchestrator'].pop('system_scope', None)")
    lines.append("d['phase7_orchestrator'].pop('system_allowed_operations', None)")
    lines.append("d['phase7_orchestrator'].pop('system_blocked_operations', None)")
    lines.append("d['phase7_orchestrator'].pop('system_allowed_skills', None)")
    lines.append("d['phase7_orchestrator'].pop('system_blocked_skills', None)")
    lines.append("open(p,'w').write(json.dumps(d,indent=2))")
    lines.append("print('Rolled back to Stage C')")
    lines.append('"')
    lines.append("sudo systemctl restart novacore-watcher")
    lines.append("```")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_stageD_stability_review(
    review: StageDStabilityReview,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write Stage D stability review to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stageD_stability_review.md"
    json_path = root / "STATE" / "stageD_stability_review.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_stageD_stability_markdown(review))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(review.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path


# ============================================================================
# Phase 7.19 — Extended Stage D Monitoring Window
# ============================================================================


@dataclass
class StageDExtendedMonitoring:
    """Deterministic extended monitoring review for sustained Stage D safety.

    Evaluates whether system_inspect has remained stably safe over a longer
    observation window with tighter thresholds than the initial Phase 7.18
    stability review.
    """
    decision: str          # "stageD_sustained_stable" | "stageD_continue_monitoring" | "rollback_system_inspect_recommended"
    rollout_stage: str
    enabled_classes: list[str] = field(default_factory=list)
    criteria: list[RolloutCriterion] = field(default_factory=list)
    system_inspect_metrics: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    generated_at: str = ""
    next_action: str = ""
    activation_record: dict = field(default_factory=dict)
    observation_window: dict = field(default_factory=dict)
    scope_integrity: dict = field(default_factory=dict)
    monitoring_progress: dict = field(default_factory=dict)

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
            "system_inspect_metrics": self.system_inspect_metrics,
            "evidence_summary": self.evidence_summary,
            "generated_at": self.generated_at,
            "next_action": self.next_action,
            "activation_record": self.activation_record,
            "observation_window": self.observation_window,
            "scope_integrity": self.scope_integrity,
            "monitoring_progress": self.monitoring_progress,
        }


def _compute_elapsed_seconds(activation_ts: str, now_ts: str) -> float:
    """Compute seconds elapsed between two ISO timestamps.

    Returns 0.0 if either timestamp is empty or unparseable.
    """
    if not activation_ts or not now_ts:
        return 0.0
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        t_start = datetime.strptime(activation_ts, fmt).replace(
            tzinfo=timezone.utc
        )
        t_end = datetime.strptime(now_ts, fmt).replace(tzinfo=timezone.utc)
        delta = (t_end - t_start).total_seconds()
        return max(0.0, delta)
    except (ValueError, TypeError):
        return 0.0


def _compute_contract_failure_rate(workflows: list[dict]) -> float | None:
    """Compute contract failure rate for system_inspect workflows.

    Returns None if no system workflows have contract data.
    """
    sys_workflows = [
        w for w in workflows if w.get("task_class") == "system"
    ]
    if not sys_workflows:
        return None
    total_with_contract = 0
    contract_failures = 0
    for w in sys_workflows:
        steps = w.get("steps", [])
        for s in steps:
            contract = s.get("contract_status", s.get("contract"))
            if contract is not None:
                total_with_contract += 1
                if contract not in ("valid", True):
                    contract_failures += 1
    if total_with_contract == 0:
        return None
    return round(contract_failures / total_with_contract, 3)


def evaluate_stageD_extended(
    evidence: dict,
    system_inspect_metrics: dict,
    post_activation_recoveries: int,
    blocked_mutation_attempts: int,
    scope_integrity: dict,
    elapsed_seconds: float,
    contract_failure_rate: float | None,
    recovery_classification: dict | None = None,
) -> list[RolloutCriterion]:
    """Evaluate extended Stage D monitoring criteria.

    15 criteria total: 8 hard, 7 soft.
    Tighter thresholds and longer horizon than Phase 7.18.

    If recovery_classification is provided (from
    _classify_post_activation_recoveries), criterion #8 uses the
    ``unresolved`` count rather than the raw post_activation_recoveries
    total.  This allows benign watcher task-requeues that later completed
    successfully to be distinguished from genuine safety-significant
    recovery anomalies.  When recovery_classification is None, the
    criterion falls back to the raw count (fail-closed).
    """
    criteria: list[RolloutCriterion] = []

    # --- HARD CRITERIA (8) — any failure triggers rollback ---

    # 1. Heartbeat healthy (hard — strict)
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb == EXTENDED_HEARTBEAT_REQUIRED,
        value=hb or "(no data)",
        threshold=EXTENDED_HEARTBEAT_REQUIRED,
        detail=(
            f"Current heartbeat: {hb or '(no data)'}"
            if hb == EXTENDED_HEARTBEAT_REQUIRED
            else f"Heartbeat is '{hb or '(no data)'}', required '{EXTENDED_HEARTBEAT_REQUIRED}'"
        ),
        severity="hard",
    ))

    # 2. No policy violations (hard — zero tolerance)
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations <= EXTENDED_MAX_POLICY_VIOLATIONS,
        value=violations,
        threshold=EXTENDED_MAX_POLICY_VIOLATIONS,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) — review immediately"
        ),
        severity="hard",
    ))

    # 3. No budget exhaustions (hard — zero tolerance)
    budget = evidence.get("budget_exhaustions", 0)
    criteria.append(RolloutCriterion(
        name="no_budget_exhaustions",
        passed=budget <= EXTENDED_MAX_BUDGET_EXHAUSTIONS,
        value=budget,
        threshold=EXTENDED_MAX_BUDGET_EXHAUSTIONS,
        detail=(
            "No budget exhaustions"
            if budget == 0
            else f"{budget} budget exhaustion(s)"
        ),
        severity="hard",
    ))

    # 4. Overall failure rate (hard — tighter than initial)
    failure_rate = evidence.get("failure_rate")
    if failure_rate is not None:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=failure_rate <= EXTENDED_MAX_FAILURE_RATE,
            value=failure_rate,
            threshold=EXTENDED_MAX_FAILURE_RATE,
            detail=f"Overall failure rate {failure_rate:.1%} (max {EXTENDED_MAX_FAILURE_RATE:.0%})",
            severity="hard",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="overall_failure_rate",
            passed=True,
            value=None,
            threshold=EXTENDED_MAX_FAILURE_RATE,
            detail="No terminal workflows yet",
            severity="soft",
        ))

    # 5. Scope integrity (hard — system_inspect must remain read-only)
    criteria.append(RolloutCriterion(
        name="scope_integrity",
        passed=scope_integrity.get("intact", False),
        value=scope_integrity.get("reason", "unknown"),
        threshold="intact",
        detail=(
            "Scope integrity verified: system_inspect remains read-only"
            if scope_integrity.get("intact")
            else f"Scope integrity violated: {scope_integrity.get('reason', 'unknown')}"
        ),
        severity="hard",
    ))

    # 6. Stage is D (hard)
    stage = evidence.get("rollout_stage", "unknown")
    is_stage_d = "stage4" in stage or "system_inspect" in stage
    criteria.append(RolloutCriterion(
        name="stage_is_D",
        passed=is_stage_d,
        value=stage,
        threshold="stage4_*",
        detail=(
            f"Rollout stage confirmed: {stage}"
            if is_stage_d
            else f"Unexpected rollout stage: {stage}"
        ),
        severity="hard",
    ))

    # 7. system class in supported_classes (hard)
    classes = evidence.get("supported_classes", [])
    criteria.append(RolloutCriterion(
        name="system_class_active",
        passed="system" in classes,
        value=classes,
        threshold="system in supported_classes",
        detail=(
            f"system class active in: {classes}"
            if "system" in classes
            else f"system class not found in: {classes}"
        ),
        severity="hard",
    ))

    # 8. No unresolved recovery anomalies during observation window
    #    (hard — promoted from soft).
    #    When recovery_classification is available, only *unresolved*
    #    recoveries (requeued tasks that never completed) count toward
    #    the hard threshold.  Resolved requeues (task completed after
    #    watcher recovery) are benign and logged for audit only.
    #    When classification is unavailable, fall back to raw count
    #    (fail-closed).
    if recovery_classification is not None:
        unresolved = recovery_classification.get("unresolved", post_activation_recoveries)
        resolved = recovery_classification.get("resolved", 0)
        total_rec = recovery_classification.get("total", post_activation_recoveries)
    else:
        unresolved = post_activation_recoveries
        resolved = 0
        total_rec = post_activation_recoveries

    if unresolved == 0 and resolved > 0:
        rec_detail = (
            f"0 unresolved recovery events since activation "
            f"({resolved} benign requeue(s) resolved by successful completion)"
        )
    elif unresolved == 0:
        rec_detail = "0 recovery events since activation"
    else:
        rec_detail = (
            f"{unresolved} unresolved recovery event(s) since activation — investigate"
            + (f" ({resolved} resolved)" if resolved > 0 else "")
        )

    criteria.append(RolloutCriterion(
        name="no_recovery_anomalies",
        passed=unresolved <= EXTENDED_MAX_RECOVERY_ANOMALIES,
        value=unresolved,
        threshold=EXTENDED_MAX_RECOVERY_ANOMALIES,
        detail=rec_detail,
        severity="hard",
    ))

    # --- SOFT CRITERIA (7) — any failure triggers continue_monitoring ---

    # 9. Minimum elapsed time since activation (soft — insufficient time = continue)
    elapsed_hours = elapsed_seconds / 3600.0
    min_hours = EXTENDED_MIN_ELAPSED_SECONDS / 3600.0
    criteria.append(RolloutCriterion(
        name="minimum_elapsed_time",
        passed=elapsed_seconds >= EXTENDED_MIN_ELAPSED_SECONDS,
        value=round(elapsed_hours, 2),
        threshold=min_hours,
        detail=(
            f"Elapsed {elapsed_hours:.1f}h since activation "
            f"(need >= {min_hours:.0f}h)"
        ),
        severity="soft",
    ))

    # 10. Minimum system_inspect runs (soft — higher bar than initial)
    sys_total = system_inspect_metrics.get("total_runs", 0)
    criteria.append(RolloutCriterion(
        name="extended_minimum_runs",
        passed=sys_total >= EXTENDED_MIN_SYSTEM_INSPECT_RUNS,
        value=sys_total,
        threshold=EXTENDED_MIN_SYSTEM_INSPECT_RUNS,
        detail=(
            f"{sys_total} system_inspect runs "
            f"(need >= {EXTENDED_MIN_SYSTEM_INSPECT_RUNS})"
        ),
        severity="soft",
    ))

    # 11. system_inspect failure rate (soft — tighter)
    sys_fr = system_inspect_metrics.get("failure_rate")
    if sys_fr is not None:
        criteria.append(RolloutCriterion(
            name="system_inspect_failure_rate",
            passed=sys_fr <= EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
            value=sys_fr,
            threshold=EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
            detail=(
                f"system_inspect failure rate {sys_fr:.1%} "
                f"(max {EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="system_inspect_failure_rate",
            passed=True,
            value=None,
            threshold=EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
            detail="No system_inspect terminal workflows yet",
            severity="soft",
        ))

    # 12. Verifier rejection rate (soft — tighter)
    sys_rejected = system_inspect_metrics.get("verifier_rejected", 0)
    sys_completed = system_inspect_metrics.get("completed", 0)
    sys_total_for_vr = sys_rejected + sys_completed
    if sys_total_for_vr > 0:
        vr_rate = round(sys_rejected / sys_total_for_vr, 3)
        criteria.append(RolloutCriterion(
            name="system_verifier_rejection_rate",
            passed=vr_rate <= EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
            value=vr_rate,
            threshold=EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
            detail=(
                f"system_inspect verifier rejection rate {vr_rate:.1%} "
                f"(max {EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="system_verifier_rejection_rate",
            passed=True,
            value=None,
            threshold=EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
            detail="No system verifier reports yet — N/A",
            severity="soft",
        ))

    # 13. Blocked mutation attempts within tighter bounds (soft)
    criteria.append(RolloutCriterion(
        name="blocked_mutation_attempts",
        passed=blocked_mutation_attempts <= EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS,
        value=blocked_mutation_attempts,
        threshold=EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS,
        detail=(
            f"{blocked_mutation_attempts} blocked mutation attempt(s) "
            f"(max {EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS})"
        ),
        severity="soft",
    ))

    # 14. Contract failure rate for system_inspect (soft)
    if contract_failure_rate is not None:
        criteria.append(RolloutCriterion(
            name="contract_failure_rate",
            passed=contract_failure_rate <= EXTENDED_MAX_CONTRACT_FAILURE_RATE,
            value=contract_failure_rate,
            threshold=EXTENDED_MAX_CONTRACT_FAILURE_RATE,
            detail=(
                f"system_inspect contract failure rate {contract_failure_rate:.1%} "
                f"(max {EXTENDED_MAX_CONTRACT_FAILURE_RATE:.0%})"
            ),
            severity="soft",
        ))
    else:
        criteria.append(RolloutCriterion(
            name="contract_failure_rate",
            passed=True,
            value=None,
            threshold=EXTENDED_MAX_CONTRACT_FAILURE_RATE,
            detail="No system_inspect contract data yet — N/A",
            severity="soft",
        ))

    # 15. No orphaned agents or stale leases (soft)
    orphaned = evidence.get("orphaned_agents", 0)
    stale = evidence.get("stale_leases", 0)
    anomaly_count = orphaned + stale
    criteria.append(RolloutCriterion(
        name="no_workflow_anomalies",
        passed=anomaly_count == 0,
        value=anomaly_count,
        threshold=0,
        detail=(
            "No orphaned agents or stale leases"
            if anomaly_count == 0
            else f"{orphaned} orphaned agent(s), {stale} stale lease(s)"
        ),
        severity="soft",
    ))

    return criteria


def decide_stageD_extended(
    criteria: list[RolloutCriterion],
) -> tuple[str, str]:
    """Determine sustained Stage D stability from extended criteria.

    Returns (decision, next_action) where decision is one of:
      - "stageD_sustained_stable"
      - "stageD_continue_monitoring"
      - "rollback_system_inspect_recommended"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure -> rollback
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "rollback_system_inspect_recommended",
            f"Hard failures detected: {reasons}. "
            f"Remove system from supported_classes and revert to Stage C. "
            f"Investigate root cause before re-enabling system_inspect.",
        )

    # Insufficient elapsed time -> continue monitoring (priority)
    elapsed = next(
        (c for c in criteria if c.name == "minimum_elapsed_time"), None
    )
    if elapsed and not elapsed.passed:
        return (
            "stageD_continue_monitoring",
            f"Insufficient elapsed time: {elapsed.value}h "
            f"(need >= {elapsed.threshold}h). "
            f"Continue operating Stage D and re-evaluate after more time passes.",
        )

    # Insufficient runs -> continue monitoring
    runs = next(
        (c for c in criteria if c.name == "extended_minimum_runs"), None
    )
    if runs and not runs.passed:
        return (
            "stageD_continue_monitoring",
            f"Insufficient system_inspect evidence: {runs.value} runs "
            f"(need >= {runs.threshold}). "
            f"Continue operating Stage D and re-evaluate after more tasks.",
        )

    # Any other soft failure -> continue monitoring
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "stageD_continue_monitoring",
            f"Soft concerns: {reasons}. "
            f"Continue monitoring Stage D. Do not expand system scope.",
        )

    # All clear -> sustained stable
    return (
        "stageD_sustained_stable",
        "Stage D is sustained-stable. system_inspect has operated safely "
        "over the extended monitoring window with tighter thresholds. "
        "Scope integrity confirmed. "
        "Extended monitoring complete. "
        "Broader system scope expansion may be evaluated in a future phase.",
    )


def review_stageD_extended(
    base: Path | None = None,
    now_override: str = "",
) -> StageDExtendedMonitoring:
    """Run the extended Stage D monitoring review.

    Collects evidence over the full observation window since Stage D
    activation and evaluates tighter thresholds for sustained safety.

    Decisions:
      - stageD_sustained_stable: Stage D safe over extended window
      - stageD_continue_monitoring: insufficient evidence or time
      - rollback_system_inspect_recommended: hard failures detected

    Args:
        base: Repository root (defaults to BASE).
        now_override: ISO timestamp to use as "now" (for deterministic
            testing). If empty, uses actual current time.
    """
    root = base or BASE
    state = root / "STATE"

    # Collect general evidence
    evidence = collect_evidence(root)

    # Collect system_inspect-specific metrics
    workflows = _list_json_files(state / "workflows")
    system_inspect_metrics = _collect_system_inspect_metrics(workflows)

    # Get Stage D activation record
    activation_log = _read_jsonl(state / "activation_log.jsonl")
    stage4_activations = [
        r for r in activation_log
        if r.get("activated_scope") == "system_inspect"
        and r.get("outcome") == "activated"
    ]
    activation = stage4_activations[-1] if stage4_activations else {}
    activation_ts = activation.get("attempted_at", "")

    # Current time
    now_iso = now_override or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Compute elapsed time
    elapsed_seconds = _compute_elapsed_seconds(activation_ts, now_iso)
    elapsed_hours = elapsed_seconds / 3600.0

    # Observation window
    observation_window = {
        "activation_at": activation_ts or "N/A",
        "review_at": now_iso,
        "scope": "system_inspect (read-only)",
        "elapsed_hours": round(elapsed_hours, 2),
        "elapsed_seconds": round(elapsed_seconds, 1),
    }

    # Classify post-activation recovery anomalies
    # Distinguishes benign watcher requeues (task later completed) from
    # genuine unresolved anomalies.  Only unresolved count toward the
    # hard threshold — fail-closed when classification is unavailable.
    recovery_classification = _classify_post_activation_recoveries(
        root, activation_ts,
    )
    post_recoveries_unresolved = recovery_classification["unresolved"]

    # Count blocked mutation attempts since activation
    blocked_mutations = _count_blocked_mutation_attempts(root, activation_ts)

    # Verify scope integrity
    scope_integrity = _verify_scope_integrity(root)

    # Compute contract failure rate
    contract_failure_rate = _compute_contract_failure_rate(workflows)

    # Evaluate extended criteria
    criteria = evaluate_stageD_extended(
        evidence,
        system_inspect_metrics,
        post_recoveries_unresolved,
        blocked_mutations,
        scope_integrity,
        elapsed_seconds,
        contract_failure_rate,
        recovery_classification=recovery_classification,
    )
    decision, next_action = decide_stageD_extended(criteria)

    # Monitoring progress — how far toward sustained-stable
    min_runs = EXTENDED_MIN_SYSTEM_INSPECT_RUNS
    min_elapsed = EXTENDED_MIN_ELAPSED_SECONDS
    sys_total = system_inspect_metrics.get("total_runs", 0)
    monitoring_progress = {
        "runs_completed": sys_total,
        "runs_required": min_runs,
        "runs_progress_pct": round(
            min(100.0, (sys_total / min_runs) * 100), 1
        ) if min_runs > 0 else 100.0,
        "elapsed_hours": round(elapsed_hours, 2),
        "elapsed_required_hours": round(min_elapsed / 3600.0, 1),
        "elapsed_progress_pct": round(
            min(100.0, (elapsed_seconds / min_elapsed) * 100), 1
        ) if min_elapsed > 0 else 100.0,
        "hard_criteria_passed": sum(
            1 for c in criteria if c.severity == "hard" and c.passed
        ),
        "hard_criteria_total": sum(
            1 for c in criteria if c.severity == "hard"
        ),
        "soft_criteria_passed": sum(
            1 for c in criteria if c.severity == "soft" and c.passed
        ),
        "soft_criteria_total": sum(
            1 for c in criteria if c.severity == "soft"
        ),
    }

    return StageDExtendedMonitoring(
        decision=decision,
        rollout_stage=evidence.get("rollout_stage", "unknown"),
        enabled_classes=evidence.get("supported_classes", []),
        criteria=criteria,
        system_inspect_metrics=system_inspect_metrics,
        evidence_summary={
            "total_workflows": evidence.get("total_workflows", 0),
            "completed_workflows": evidence.get("completed_workflows", 0),
            "failed_workflows": evidence.get("failed_workflows", 0),
            "halted_workflows": evidence.get("halted_workflows", 0),
            "heartbeat_overall": evidence.get("heartbeat_overall", ""),
            "policy_violations": evidence.get("policy_violations", 0),
            "budget_exhaustions": evidence.get("budget_exhaustions", 0),
            "verifier_rejection_rate": evidence.get("verifier_rejection_rate"),
            "contract_failure_rate": contract_failure_rate,
            "orphaned_agents": evidence.get("orphaned_agents", 0),
            "stale_leases": evidence.get("stale_leases", 0),
            "recovery_events": evidence.get("recovery_events", 0),
            "post_activation_recoveries": post_recoveries_unresolved,
            "post_activation_recoveries_total": recovery_classification["total"],
            "post_activation_recoveries_resolved": recovery_classification["resolved"],
            "recovery_classification_details": recovery_classification.get("details", []),
            "blocked_mutation_attempts": blocked_mutations,
        },
        next_action=next_action,
        activation_record=activation,
        observation_window=observation_window,
        scope_integrity=scope_integrity,
        monitoring_progress=monitoring_progress,
    )


def render_stageD_extended_markdown(review: StageDExtendedMonitoring) -> str:
    """Render extended Stage D monitoring review as operator-facing markdown."""
    icon = {
        "stageD_sustained_stable": "SUSTAINED STABLE",
        "stageD_continue_monitoring": "CONTINUE MONITORING",
        "rollback_system_inspect_recommended": "ROLLBACK",
    }.get(review.decision, "?")

    lines = [
        "# Phase 7.19 — Extended Stage D Monitoring Window (system_inspect)",
        f"Generated: {review.generated_at}",
        "",
        f"## Decision: {icon} — {review.decision}",
        "",
        f"**Rollout stage**: {review.rollout_stage}",
        f"**Enabled classes**: {', '.join(review.enabled_classes)}",
        f"**System scope**: inspect_only (read-only)",
        "",
        f"**Next action**: {review.next_action}",
        "",
    ]

    # Monitoring progress
    mp = review.monitoring_progress
    lines.append("## Monitoring Progress")
    lines.append("")
    lines.append("| Dimension | Current | Required | Progress |")
    lines.append("|-----------|---------|----------|----------|")
    lines.append(
        f"| system_inspect runs | {mp.get('runs_completed', 0)} "
        f"| {mp.get('runs_required', 0)} "
        f"| {mp.get('runs_progress_pct', 0):.0f}% |"
    )
    lines.append(
        f"| Elapsed time | {mp.get('elapsed_hours', 0):.1f}h "
        f"| {mp.get('elapsed_required_hours', 0):.0f}h "
        f"| {mp.get('elapsed_progress_pct', 0):.0f}% |"
    )
    lines.append(
        f"| Hard criteria | {mp.get('hard_criteria_passed', 0)}"
        f"/{mp.get('hard_criteria_total', 0)} PASS | all | "
        + ("100%" if mp.get('hard_criteria_passed', 0) == mp.get('hard_criteria_total', 0) else
           f"{mp.get('hard_criteria_passed', 0)}/{mp.get('hard_criteria_total', 0)}") + " |"
    )
    lines.append(
        f"| Soft criteria | {mp.get('soft_criteria_passed', 0)}"
        f"/{mp.get('soft_criteria_total', 0)} PASS | all | "
        + ("100%" if mp.get('soft_criteria_passed', 0) == mp.get('soft_criteria_total', 0) else
           f"{mp.get('soft_criteria_passed', 0)}/{mp.get('soft_criteria_total', 0)}") + " |"
    )
    lines.append("")

    # Observation window
    ow = review.observation_window
    lines.append("## Observation Window")
    lines.append("")
    lines.append(f"- **Activation at**: {ow.get('activation_at', 'N/A')}")
    lines.append(f"- **Review at**: {ow.get('review_at', 'N/A')}")
    lines.append(f"- **Elapsed**: {ow.get('elapsed_hours', 0):.1f}h ({ow.get('elapsed_seconds', 0):.0f}s)")
    lines.append(f"- **Required**: >= {EXTENDED_MIN_ELAPSED_SECONDS / 3600:.0f}h")
    lines.append(f"- **Scope under review**: {ow.get('scope', 'N/A')}")
    lines.append("")

    # Scope integrity
    si = review.scope_integrity
    lines.append("## Scope Integrity")
    lines.append("")
    si_status = "INTACT" if si.get("intact") else "VIOLATED"
    lines.append(f"- **Status**: {si_status}")
    lines.append(f"- **Detail**: {si.get('reason', 'N/A')}")
    lines.append(f"- **system_scope**: {si.get('system_scope', 'N/A')}")
    lines.append(f"- **system in classes**: {si.get('system_in_classes', False)}")
    lines.append("")

    # system_inspect metrics
    m = review.system_inspect_metrics
    lines.append("## system_inspect Metrics")
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
    lines.append("## Extended Monitoring Criteria")
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

    # Blocked mutation attempts
    bma = ev.get("blocked_mutation_attempts", 0)
    lines.append("## Blocked Mutation Attempts")
    lines.append("")
    if bma == 0:
        lines.append("No mutation attempts were blocked during the extended observation window.")
    else:
        lines.append(
            f"{bma} mutation attempt(s) blocked by Stage D enforcement. "
            f"Extended threshold: {EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS}."
        )
    lines.append("")

    # Threshold comparison
    lines.append("## Threshold Comparison (Initial vs Extended)")
    lines.append("")
    lines.append("| Threshold | Initial (7.18) | Extended (7.19) |")
    lines.append("|-----------|---------------|-----------------|")
    lines.append(f"| Min system_inspect runs | {STAGED_MIN_SYSTEM_INSPECT_RUNS} | {EXTENDED_MIN_SYSTEM_INSPECT_RUNS} |")
    lines.append(f"| Min elapsed time | N/A | {EXTENDED_MIN_ELAPSED_SECONDS / 3600:.0f}h |")
    lines.append(f"| Max failure rate | {STAGED_MAX_FAILURE_RATE:.0%} | {EXTENDED_MAX_FAILURE_RATE:.0%} |")
    lines.append(f"| Max system_inspect failure rate | {STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE:.0%} | {EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE:.0%} |")
    lines.append(f"| Max verifier rejection rate | {STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE:.0%} | {EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE:.0%} |")
    lines.append(f"| Max blocked mutations | {STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS} | {EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS} |")
    lines.append(f"| Contract failure rate | N/A | {EXTENDED_MAX_CONTRACT_FAILURE_RATE:.0%} |")
    lines.append(f"| Recovery anomalies (severity) | soft | hard |")
    lines.append("")

    # Required operator action
    lines.append("## Required Operator Action")
    lines.append("")
    if review.decision == "stageD_sustained_stable":
        lines.append("- No action required. Stage D is sustained-stable.")
        lines.append("- Extended monitoring window complete.")
        lines.append("- system_inspect has proven safe over sustained operation.")
        lines.append("- Do NOT expand system scope without a future Phase evaluation.")
    elif review.decision == "stageD_continue_monitoring":
        lines.append("- Do NOT expand system scope.")
        lines.append("- Continue current Stage D operation and gather more evidence.")
        lines.append("- Re-run this extended monitoring review after more time/tasks.")
    else:
        lines.append("- **ROLLBACK RECOMMENDED**: Revert to Stage C immediately.")
        lines.append("- Remove 'system' from supported_classes in feature_flags.json.")
        lines.append("- Set stage back to 'C' and rollout_stage to 'stage3_research_code_review_code_impl'.")
        lines.append("- Remove system_scope and system_allowed/blocked configs.")
        lines.append("- Restart watcher service: `sudo systemctl restart novacore-watcher`")
        lines.append("- Investigate root cause before re-enabling system_inspect.")
    lines.append("")

    # Rollback path
    lines.append("## Rollback Path (Stage D -> Stage C)")
    lines.append("")
    lines.append("```bash")
    lines.append('python3 -c "')
    lines.append("import json; p='STATE/config/feature_flags.json'")
    lines.append("d=json.loads(open(p).read())")
    lines.append("d['phase7_orchestrator']['supported_classes']=['research','code_review','code_impl']")
    lines.append("d['phase7_orchestrator']['rollout_stage']='stage3_research_code_review_code_impl'")
    lines.append("d['phase7_orchestrator']['stage']='C'")
    lines.append("d['phase7_orchestrator'].pop('system_scope', None)")
    lines.append("d['phase7_orchestrator'].pop('system_allowed_operations', None)")
    lines.append("d['phase7_orchestrator'].pop('system_blocked_operations', None)")
    lines.append("d['phase7_orchestrator'].pop('system_allowed_skills', None)")
    lines.append("d['phase7_orchestrator'].pop('system_blocked_skills', None)")
    lines.append("open(p,'w').write(json.dumps(d,indent=2))")
    lines.append("print('Rolled back to Stage C')")
    lines.append('"')
    lines.append("sudo systemctl restart novacore-watcher")
    lines.append("```")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_stageD_extended_monitoring(
    review: StageDExtendedMonitoring,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write extended Stage D monitoring review to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_stageD_extended_monitoring.md"
    json_path = root / "STATE" / "stageD_extended_monitoring.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_stageD_extended_markdown(review))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(review.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path


# ---------------------------------------------------------------------------
# Phase 7.20 — Broader System Scope Evaluation Gate
# ---------------------------------------------------------------------------
# Deterministic evaluation of whether Nova-Core's sustained-stable
# inspect_only Stage D rollout justifies *planning* for a broader but
# still bounded system scope.
#
# This is evaluation only — it never activates broader scope.
# ---------------------------------------------------------------------------


@dataclass
class BroaderSystemScopeEvaluation:
    """Deterministic evaluation of broader system scope readiness.

    Decides whether inspect_only Stage D is sufficiently stable and
    governed to justify planning for a broader bounded system scope.
    Evaluation only — does not activate anything.
    """
    decision: str          # "ready_for_broader_system_scope_planning" | "hold_broader_system_scope" | "block_broader_system_scope"
    rollout_stage: str
    enabled_classes: list[str] = field(default_factory=list)
    criteria: list[RolloutCriterion] = field(default_factory=list)
    system_inspect_metrics: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    extended_monitoring_decision: str = ""
    generated_at: str = ""
    next_action: str = ""
    remaining_requirements: list[str] = field(default_factory=list)
    scope_integrity: dict = field(default_factory=dict)
    observation_window: dict = field(default_factory=dict)

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
            "system_inspect_metrics": self.system_inspect_metrics,
            "evidence_summary": self.evidence_summary,
            "extended_monitoring_decision": self.extended_monitoring_decision,
            "generated_at": self.generated_at,
            "next_action": self.next_action,
            "remaining_requirements": self.remaining_requirements,
            "scope_integrity": self.scope_integrity,
            "observation_window": self.observation_window,
        }


def evaluate_broader_system_scope(
    evidence: dict,
    system_inspect_metrics: dict,
    extended_decision: str,
    recovery_classification: dict | None,
    blocked_mutation_attempts: int,
    scope_integrity: dict,
    elapsed_seconds: float,
    contract_failure_rate: float | None,
    stageD_rollback_count: int,
) -> list[RolloutCriterion]:
    """Evaluate broader system scope readiness criteria.

    12 criteria total: 9 hard, 3 soft.
    Tighter thresholds than Phase 7.19 extended monitoring.
    All hard criteria must pass before planning can be considered.

    This function evaluates whether the data *justifies planning* for
    broader scope.  It never activates anything.
    """
    criteria: list[RolloutCriterion] = []

    # --- HARD CRITERIA (9) — any failure triggers block ---

    # 1. Extended monitoring must be stageD_sustained_stable (prerequisite)
    criteria.append(RolloutCriterion(
        name="extended_monitoring_sustained_stable",
        passed=extended_decision == BROADER_REQUIRED_EXTENDED_DECISION,
        value=extended_decision,
        threshold=BROADER_REQUIRED_EXTENDED_DECISION,
        detail=(
            "Extended Stage D monitoring confirmed sustained-stable"
            if extended_decision == BROADER_REQUIRED_EXTENDED_DECISION
            else f"Extended monitoring is '{extended_decision}', "
                 f"required '{BROADER_REQUIRED_EXTENDED_DECISION}'"
        ),
        severity="hard",
    ))

    # 2. Heartbeat healthy (hard — strict)
    hb = evidence.get("heartbeat_overall", "")
    criteria.append(RolloutCriterion(
        name="heartbeat_healthy",
        passed=hb == BROADER_HEARTBEAT_REQUIRED,
        value=hb or "(no data)",
        threshold=BROADER_HEARTBEAT_REQUIRED,
        detail=(
            f"Current heartbeat: {hb or '(no data)'}"
            if hb == BROADER_HEARTBEAT_REQUIRED
            else f"Heartbeat is '{hb or '(no data)'}', "
                 f"required '{BROADER_HEARTBEAT_REQUIRED}'"
        ),
        severity="hard",
    ))

    # 3. No policy violations (hard — zero tolerance)
    violations = evidence.get("policy_violations", 0)
    criteria.append(RolloutCriterion(
        name="no_policy_violations",
        passed=violations <= BROADER_MAX_POLICY_VIOLATIONS,
        value=violations,
        threshold=BROADER_MAX_POLICY_VIOLATIONS,
        detail=(
            "No policy violations"
            if violations == 0
            else f"{violations} policy violation(s) — broader scope blocked"
        ),
        severity="hard",
    ))

    # 4. No budget exhaustions (hard — zero tolerance)
    budget = evidence.get("budget_exhaustions", 0)
    criteria.append(RolloutCriterion(
        name="no_budget_exhaustions",
        passed=budget <= BROADER_MAX_BUDGET_EXHAUSTIONS,
        value=budget,
        threshold=BROADER_MAX_BUDGET_EXHAUSTIONS,
        detail=(
            "No budget exhaustions"
            if budget == 0
            else f"{budget} budget exhaustion(s) — broader scope blocked"
        ),
        severity="hard",
    ))

    # 5. Scope integrity (hard — system_inspect must still be inspect_only)
    criteria.append(RolloutCriterion(
        name="scope_integrity",
        passed=scope_integrity.get("intact", False),
        value=scope_integrity.get("reason", "unknown"),
        threshold="intact",
        detail=(
            "Scope integrity verified: system_inspect remains read-only"
            if scope_integrity.get("intact")
            else f"Scope integrity violated: "
                 f"{scope_integrity.get('reason', 'unknown')}"
        ),
        severity="hard",
    ))

    # 6. Stage is D (hard)
    stage = evidence.get("rollout_stage", "unknown")
    is_stage_d = "stage4" in stage or "system_inspect" in stage
    criteria.append(RolloutCriterion(
        name="stage_is_D",
        passed=is_stage_d,
        value=stage,
        threshold="stage4_*",
        detail=(
            f"Rollout stage confirmed: {stage}"
            if is_stage_d
            else f"Unexpected rollout stage: {stage}"
        ),
        severity="hard",
    ))

    # 7. System class active (hard)
    classes = evidence.get("supported_classes", [])
    criteria.append(RolloutCriterion(
        name="system_class_active",
        passed="system" in classes,
        value=classes,
        threshold="system in supported_classes",
        detail=(
            f"system class active in: {classes}"
            if "system" in classes
            else f"system class not found in: {classes}"
        ),
        severity="hard",
    ))

    # 8. No unresolved recovery anomalies (hard)
    if recovery_classification is not None:
        unresolved = recovery_classification.get("unresolved", 0)
        resolved = recovery_classification.get("resolved", 0)
    else:
        unresolved = 0
        resolved = 0
    criteria.append(RolloutCriterion(
        name="no_recovery_anomalies",
        passed=unresolved <= BROADER_MAX_RECOVERY_ANOMALIES,
        value=unresolved,
        threshold=BROADER_MAX_RECOVERY_ANOMALIES,
        detail=(
            "0 unresolved recovery events"
            + (f" ({resolved} benign resolved)" if resolved > 0 else "")
            if unresolved == 0
            else f"{unresolved} unresolved recovery event(s) — "
                 f"broader scope blocked"
        ),
        severity="hard",
    ))

    # 9. No Stage D rollback history (hard — never rolled back)
    criteria.append(RolloutCriterion(
        name="no_stageD_rollback_history",
        passed=stageD_rollback_count == 0,
        value=stageD_rollback_count,
        threshold=0,
        detail=(
            "No Stage D rollback history"
            if stageD_rollback_count == 0
            else f"{stageD_rollback_count} prior Stage D rollback(s) — "
                 f"broader scope blocked until root cause confirmed resolved"
        ),
        severity="hard",
    ))

    # --- SOFT CRITERIA (3) — any failure triggers hold ---

    # 10. Minimum system_inspect runs (soft — higher bar)
    sys_total = system_inspect_metrics.get("total_runs", 0)
    criteria.append(RolloutCriterion(
        name="minimum_system_inspect_runs",
        passed=sys_total >= BROADER_MIN_SYSTEM_INSPECT_RUNS,
        value=sys_total,
        threshold=BROADER_MIN_SYSTEM_INSPECT_RUNS,
        detail=(
            f"{sys_total} system_inspect runs "
            f"(need >= {BROADER_MIN_SYSTEM_INSPECT_RUNS})"
        ),
        severity="soft",
    ))

    # 11. Minimum elapsed time in Stage D (soft — longer window)
    elapsed_hours = elapsed_seconds / 3600.0
    min_hours = BROADER_MIN_ELAPSED_SECONDS / 3600.0
    criteria.append(RolloutCriterion(
        name="minimum_elapsed_time",
        passed=elapsed_seconds >= BROADER_MIN_ELAPSED_SECONDS,
        value=round(elapsed_hours, 2),
        threshold=min_hours,
        detail=(
            f"Elapsed {elapsed_hours:.1f}h in Stage D "
            f"(need >= {min_hours:.0f}h)"
        ),
        severity="soft",
    ))

    # 12. Blocked mutation attempts within tighter bounds (soft)
    criteria.append(RolloutCriterion(
        name="blocked_mutation_attempts",
        passed=blocked_mutation_attempts <= BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS,
        value=blocked_mutation_attempts,
        threshold=BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS,
        detail=(
            f"{blocked_mutation_attempts} blocked mutation attempt(s) "
            f"(max {BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS})"
        ),
        severity="soft",
    ))

    return criteria


def decide_broader_system_scope(
    criteria: list[RolloutCriterion],
) -> tuple[str, str]:
    """Determine broader system scope readiness from criteria.

    Returns (decision, next_action) where decision is one of:
      - "ready_for_broader_system_scope_planning"
      - "hold_broader_system_scope"
      - "block_broader_system_scope"
    """
    hard_failures = [c for c in criteria
                     if c.severity == "hard" and not c.passed]
    soft_failures = [c for c in criteria
                     if c.severity == "soft" and not c.passed]

    # Any hard failure -> block
    if hard_failures:
        reasons = ", ".join(c.name for c in hard_failures)
        return (
            "block_broader_system_scope",
            f"Broader system scope blocked: {reasons}. "
            f"Resolve hard failures before broader scope can be considered. "
            f"system remains inspect_only.",
        )

    # Any soft failure -> hold
    if soft_failures:
        reasons = ", ".join(c.name for c in soft_failures)
        return (
            "hold_broader_system_scope",
            f"Broader system scope held: {reasons}. "
            f"Continue operating Stage D to accumulate evidence. "
            f"system remains inspect_only.",
        )

    # All clear -> ready for planning (not activation)
    return (
        "ready_for_broader_system_scope_planning",
        "All broader-scope evaluation criteria pass. "
        "inspect_only Stage D has proven safe under tightened thresholds. "
        "A bounded broader-system rollout plan may be prepared in the "
        "next phase. system remains inspect_only until that plan is "
        "reviewed, approved, and separately activated.",
    )


def _count_stageD_rollbacks(base: Path) -> int:
    """Count activation log entries that indicate a Stage D rollback.

    A rollback is any activation record where pre_config has stage='D'
    and post_config has stage != 'D' (reverted to an earlier stage).
    """
    log = _read_jsonl(base / "STATE" / "activation_log.jsonl")
    count = 0
    for record in log:
        pre = record.get("pre_config", {})
        post = record.get("post_config", {})
        if pre.get("stage") == "D" and post.get("stage", "D") != "D":
            count += 1
    return count


def review_broader_system_scope(
    base: Path | None = None,
    now_override: str = "",
) -> BroaderSystemScopeEvaluation:
    """Run the broader system scope evaluation gate.

    Collects evidence from the sustained-stable Stage D observation window
    and evaluates tighter thresholds to determine whether *planning* for
    a broader system scope is justified.

    This function is evaluation-only — it never activates broader scope.

    Decisions:
      - ready_for_broader_system_scope_planning: all criteria pass
      - hold_broader_system_scope: insufficient evidence (soft)
      - block_broader_system_scope: hard failures detected
    """
    root = base or BASE
    state = root / "STATE"

    # Collect general evidence
    evidence = collect_evidence(root)

    # Collect system_inspect-specific metrics
    workflows = _list_json_files(state / "workflows")
    system_inspect_metrics = _collect_system_inspect_metrics(workflows)

    # Get extended monitoring decision
    ext_json = _read_json(state / "stageD_extended_monitoring.json")
    extended_decision = (
        ext_json.get("decision", "") if ext_json else ""
    )

    # Get Stage D activation record for elapsed time
    activation_log = _read_jsonl(state / "activation_log.jsonl")
    stage4_activations = [
        r for r in activation_log
        if r.get("activated_scope") == "system_inspect"
        and r.get("outcome") == "activated"
    ]
    activation = stage4_activations[-1] if stage4_activations else {}
    activation_ts = activation.get("attempted_at", "")

    # Current time
    now_iso = now_override or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Compute elapsed time since Stage D activation
    elapsed_seconds = _compute_elapsed_seconds(activation_ts, now_iso)
    elapsed_hours = elapsed_seconds / 3600.0

    observation_window = {
        "activation_at": activation_ts or "N/A",
        "review_at": now_iso,
        "scope": "system_inspect (read-only)",
        "elapsed_hours": round(elapsed_hours, 2),
        "elapsed_seconds": round(elapsed_seconds, 1),
    }

    # Classify post-activation recovery anomalies
    recovery_classification = _classify_post_activation_recoveries(
        root, activation_ts,
    )

    # Count blocked mutation attempts
    blocked_mutations = _count_blocked_mutation_attempts(root, activation_ts)

    # Verify scope integrity
    scope_integrity = _verify_scope_integrity(root)

    # Compute contract failure rate
    contract_failure_rate = _compute_contract_failure_rate(workflows)

    # Count Stage D rollback history
    stageD_rollbacks = _count_stageD_rollbacks(root)

    # Evaluate criteria
    criteria = evaluate_broader_system_scope(
        evidence,
        system_inspect_metrics,
        extended_decision,
        recovery_classification,
        blocked_mutations,
        scope_integrity,
        elapsed_seconds,
        contract_failure_rate,
        stageD_rollbacks,
    )
    decision, next_action = decide_broader_system_scope(criteria)

    # Remaining requirements
    remaining = []
    for c in criteria:
        if not c.passed:
            remaining.append(f"{c.name}: {c.detail}")

    return BroaderSystemScopeEvaluation(
        decision=decision,
        rollout_stage=evidence.get("rollout_stage", "unknown"),
        enabled_classes=evidence.get("supported_classes", []),
        criteria=criteria,
        system_inspect_metrics=system_inspect_metrics,
        evidence_summary={
            "total_workflows": evidence.get("total_workflows", 0),
            "completed_workflows": evidence.get("completed_workflows", 0),
            "failed_workflows": evidence.get("failed_workflows", 0),
            "halted_workflows": evidence.get("halted_workflows", 0),
            "heartbeat_overall": evidence.get("heartbeat_overall", ""),
            "policy_violations": evidence.get("policy_violations", 0),
            "budget_exhaustions": evidence.get("budget_exhaustions", 0),
            "verifier_rejection_rate": evidence.get("verifier_rejection_rate"),
            "contract_failure_rate": contract_failure_rate,
            "orphaned_agents": evidence.get("orphaned_agents", 0),
            "stale_leases": evidence.get("stale_leases", 0),
            "recovery_events": evidence.get("recovery_events", 0),
            "post_activation_recoveries_unresolved": recovery_classification["unresolved"],
            "post_activation_recoveries_resolved": recovery_classification["resolved"],
            "blocked_mutation_attempts": blocked_mutations,
            "stageD_rollback_count": stageD_rollbacks,
        },
        extended_monitoring_decision=extended_decision,
        next_action=next_action,
        remaining_requirements=remaining,
        scope_integrity=scope_integrity,
        observation_window=observation_window,
    )


def render_broader_system_scope_markdown(
    review: BroaderSystemScopeEvaluation,
) -> str:
    """Render broader system scope evaluation as operator-facing markdown."""
    icon = {
        "ready_for_broader_system_scope_planning": "READY FOR PLANNING",
        "hold_broader_system_scope": "HOLD",
        "block_broader_system_scope": "BLOCKED",
    }.get(review.decision, "?")

    lines = [
        "# Phase 7.20 — Broader System Scope Evaluation Gate",
        f"Generated: {review.generated_at}",
        "",
        f"## Decision: {icon} — {review.decision}",
        "",
        f"**Rollout stage**: {review.rollout_stage}",
        f"**Enabled classes**: {', '.join(review.enabled_classes)}",
        f"**Current system scope**: inspect_only (read-only)",
        f"**Extended monitoring**: {review.extended_monitoring_decision}",
        "",
        f"**Next action**: {review.next_action}",
        "",
    ]

    # Observation window
    ow = review.observation_window
    lines += [
        "## Observation Window",
        "",
        f"- **Stage D activation at**: {ow.get('activation_at', 'N/A')}",
        f"- **Review at**: {ow.get('review_at', 'N/A')}",
        f"- **Elapsed in Stage D**: {ow.get('elapsed_hours', 0)}h "
        f"({ow.get('elapsed_seconds', 0)}s)",
        f"- **Scope under review**: {ow.get('scope', 'N/A')}",
        "",
    ]

    # Scope integrity
    si = review.scope_integrity
    lines += [
        "## Scope Integrity",
        "",
        f"- **Status**: {'INTACT' if si.get('intact') else 'VIOLATED'}",
        f"- **system_scope**: {si.get('system_scope', 'N/A')}",
        f"- **system in classes**: {si.get('system_in_classes', 'N/A')}",
        "",
    ]

    # system_inspect metrics
    sim = review.system_inspect_metrics
    lines += [
        "## system_inspect Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total runs | {sim.get('total_runs', 0)} |",
        f"| Completed | {sim.get('completed', 0)} |",
        f"| Failed | {sim.get('failed', 0)} |",
        f"| Verifier rejected | {sim.get('verifier_rejected', 0)} |",
        f"| Failure rate | {sim.get('failure_rate', 0):.1%} |",
        "",
    ]

    # Criteria table
    lines += [
        "## Evaluation Criteria",
        "",
        "| # | Criterion | Status | Value | Threshold | Severity | Detail |",
        "|---|-----------|--------|-------|-----------|----------|--------|",
    ]
    for i, c in enumerate(review.criteria, 1):
        status = "PASS" if c.passed else "FAIL"
        val = c.value if c.value is not None else "N/A"
        lines.append(
            f"| {i} | {c.name} | {status} | {val} | "
            f"{c.threshold} | {c.severity} | {c.detail} |"
        )
    lines.append("")

    # Evidence summary
    es = review.evidence_summary
    lines += [
        "## Evidence Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for k, v in es.items():
        display = "N/A" if v is None else v
        lines.append(f"| {k} | {display} |")
    lines.append("")

    # Remaining requirements
    if review.remaining_requirements:
        lines += [
            "## Remaining Requirements",
            "",
        ]
        for req in review.remaining_requirements:
            lines.append(f"- {req}")
        lines.append("")

    # Threshold comparison
    lines += [
        "## Threshold Progression (Extended -> Broader Scope)",
        "",
        "| Threshold | Extended (7.19) | Broader (7.20) |",
        "|-----------|-----------------|----------------|",
        f"| Min system_inspect runs | {EXTENDED_MIN_SYSTEM_INSPECT_RUNS}"
        f" | {BROADER_MIN_SYSTEM_INSPECT_RUNS} |",
        f"| Min elapsed time | "
        f"{EXTENDED_MIN_ELAPSED_SECONDS // 3600}h"
        f" | {BROADER_MIN_ELAPSED_SECONDS // 3600}h |",
        f"| Max failure rate | "
        f"{EXTENDED_MAX_FAILURE_RATE:.0%}"
        f" | {BROADER_MAX_FAILURE_RATE:.0%} |",
        f"| Max system_inspect failure rate | "
        f"{EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE:.0%}"
        f" | {BROADER_MAX_SYSTEM_INSPECT_FAILURE_RATE:.0%} |",
        f"| Max verifier rejection rate | "
        f"{EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE:.0%}"
        f" | {BROADER_MAX_SYSTEM_VERIFIER_REJECTION_RATE:.0%} |",
        f"| Max blocked mutations | "
        f"{EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS}"
        f" | {BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS} |",
        f"| Max contract failure rate | "
        f"{EXTENDED_MAX_CONTRACT_FAILURE_RATE:.0%}"
        f" | {BROADER_MAX_CONTRACT_FAILURE_RATE:.0%} |",
        "| Stage D rollback history | N/A | must be 0 |",
        "| Extended monitoring prerequisite | N/A"
        " | stageD_sustained_stable |",
        "",
    ]

    # Operator action
    if review.decision == "ready_for_broader_system_scope_planning":
        lines += [
            "## Operator Action",
            "",
            "- Broader system scope planning is justified.",
            "- system remains inspect_only until a rollout plan is "
            "prepared, reviewed, and separately activated.",
            "- Next step: Phase 7.21 — Broader System Scope "
            "Rollout Plan.",
            "- No scope expansion is performed by this evaluation.",
            "",
        ]
    elif review.decision == "hold_broader_system_scope":
        lines += [
            "## Operator Action",
            "",
            "- Broader system scope is held pending more evidence.",
            "- system remains inspect_only.",
            "- Continue operating Stage D and re-run this evaluation "
            "after thresholds are met.",
            "",
        ]
    else:
        lines += [
            "## Operator Action",
            "",
            "- Broader system scope is BLOCKED.",
            "- Resolve hard failures before re-evaluation.",
            "- system remains inspect_only.",
            "- Do NOT proceed to broader scope planning.",
            "",
        ]

    return "\n".join(lines)


def write_broader_system_scope_evaluation(
    review: BroaderSystemScopeEvaluation,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write broader system scope evaluation to WORK/ and STATE/.

    Returns (md_path, json_path).
    """
    root = base or BASE

    md_path = root / "WORK" / "phase7_broader_system_scope_evaluation.md"
    json_path = root / "STATE" / "broader_system_scope_evaluation.json"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_broader_system_scope_markdown(review))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(review.to_dict(), indent=2, default=str) + "\n"
    )

    return md_path, json_path
