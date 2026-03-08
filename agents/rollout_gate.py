"""Phase 7.11 — Rollout Evaluation Gate.

Deterministic, repository-native evaluation of Stage 2 rollout readiness.
Reads existing heartbeat, metrics, and workflow state to classify rollout
status as ready_to_expand, hold, or rollback_recommended.

All criteria are threshold-based and auditable.
No LLM judgments — pure metric evaluation.

State sources:
  STATE/heartbeat_multiagent.json   — latest health report
  STATE/workflows/*.json            — workflow history
  STATE/config/feature_flags.json   — current rollout config
  STATE/policy_denials.jsonl        — policy denial records
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
