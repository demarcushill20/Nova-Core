"""Phase 7.6 — Multi-Agent Observability and Heartbeat Upgrade.

Provides:
  1. Multi-agent metric aggregation from repository-native state
  2. Health detection rules (stuck/orphan/budget/stale-dependency)
  3. HEARTBEAT_MULTIAGENT.md report generation
  4. JSON dashboard companion

All metrics are derived deterministically from STATE/, WORK/, and LOGS/ —
no in-memory counters, no LLM calls.

State sources:
  STATE/workflows/<id>.json         — workflow state + node_states
  STATE/delegations/<id>.json       — delegation records
  STATE/leases/<wf>_<node>.json     — active leases
  STATE/verifications/<id>.json     — verifier reports
  STATE/reviews/<id>.json           — critic reviews
  STATE/agents/runtime/<id>.json    — agent runtime state
  STATE/budgets/<id>.json           — budget tracking
  STATE/tool_audit.jsonl            — tool call audit trail
  STATE/metrics.json                — contract success/failure counters
  STATE/replans/<id>.json           — replan signals
  WORK/agents/contracts/<id>.json   — child contracts

Health rules use SLA thresholds — deterministic, auditable, documented.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
STATE = BASE / "STATE"

# ---------------------------------------------------------------------------
# SLA thresholds — all times in seconds
# ---------------------------------------------------------------------------

# Workflow stuck in "executing" longer than this → warning
WORKFLOW_EXECUTING_SLA_S = 1800  # 30 minutes

# Agent in "executing" longer than this → stuck
AGENT_EXECUTING_SLA_S = 600  # 10 minutes

# Node waiting on dependency longer than this → stale wait
DEPENDENCY_WAIT_SLA_S = 900  # 15 minutes

# Budget remaining below this fraction → near-exhaustion warning
BUDGET_WARN_FRACTION = 0.15

# Lease older than TTL without renewal → orphan
LEASE_ORPHAN_TTL_S = 600  # 10 minutes (matches coordination.py default)


# ---------------------------------------------------------------------------
# Health severity
# ---------------------------------------------------------------------------

class Severity:
    HEALTHY = "healthy"
    WARNING = "warning"
    UNHEALTHY = "unhealthy"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class HealthFinding:
    """A single health observation."""
    category: str          # workflow_stuck | agent_stuck | orphan | budget
                           # | dependency_wait | verifier_failure
                           # | contract_gap | policy_violation
    severity: str          # healthy | warning | unhealthy
    subject: str           # workflow_id or agent_id
    detail: str
    metric_value: Any = None  # numeric or string context

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MultiAgentMetrics:
    """Aggregated multi-agent operational metrics."""
    active_workflows: int = 0
    completed_workflows: int = 0
    failed_workflows: int = 0
    halted_workflows: int = 0
    total_delegations: int = 0
    completed_delegations: int = 0
    failed_delegations: int = 0
    agent_spawn_count: int = 0
    mean_subtask_latency_s: float | None = None
    verifier_rejection_count: int = 0
    verifier_approval_count: int = 0
    verifier_rejection_rate: float | None = None
    contract_success_count: int = 0
    contract_failure_count: int = 0
    contract_failure_rate: float | None = None
    retry_count: int = 0
    retry_rate: float | None = None
    budget_exhaustion_count: int = 0
    policy_violation_count: int = 0
    orphaned_agent_count: int = 0
    active_lease_count: int = 0
    stale_lease_count: int = 0
    collected_at: str = ""

    def __post_init__(self):
        if not self.collected_at:
            self.collected_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthReport:
    """Complete multi-agent health report."""
    overall: str = Severity.HEALTHY  # healthy | warning | unhealthy
    metrics: MultiAgentMetrics = field(default_factory=MultiAgentMetrics)
    findings: list[HealthFinding] = field(default_factory=list)
    generated_at: str = ""
    workflow_summaries: list[dict] = field(default_factory=list)
    top_bottlenecks: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _list_json_files(directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    results = []
    for f in sorted(directory.glob("*.json")):
        data = _read_json(f)
        if data and isinstance(data, dict):
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


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

def collect_metrics(base: Path | None = None) -> MultiAgentMetrics:
    """Derive multi-agent metrics from repository-native state.

    All values are computed deterministically from files in STATE/ and WORK/.
    """
    root = base or BASE
    state = root / "STATE"
    work = root / "WORK"

    m = MultiAgentMetrics()
    now = time.time()

    # --- Workflows ---
    workflows = _list_json_files(state / "workflows")
    for wf in workflows:
        status = wf.get("status", "")
        if status in ("created", "planning", "executing"):
            m.active_workflows += 1
        elif status == "completed":
            m.completed_workflows += 1
        elif status == "failed":
            m.failed_workflows += 1
        elif status == "halted":
            m.halted_workflows += 1
            halt = wf.get("halt_reason", "")
            if "budget" in halt.lower():
                m.budget_exhaustion_count += 1

    # --- Delegations ---
    delegations = _list_json_files(state / "delegations")
    m.total_delegations = len(delegations)
    latencies = []
    for d in delegations:
        status = d.get("status", "")
        if status == "completed":
            m.completed_delegations += 1
            claimed = d.get("claimed_at")
            completed = d.get("completed_at")
            if claimed and completed:
                latencies.append(completed - claimed)
        elif status == "failed":
            m.failed_delegations += 1

    if latencies:
        m.mean_subtask_latency_s = round(sum(latencies) / len(latencies), 2)

    # --- Agent spawns ---
    agent_states = _list_json_files(state / "agents" / "runtime")
    m.agent_spawn_count = len(agent_states)

    # --- Retries (from node_states in workflows) ---
    for wf in workflows:
        node_states = wf.get("node_states", {})
        for ns in node_states.values():
            if isinstance(ns, dict):
                m.retry_count += ns.get("retry_count", 0)

    if m.total_delegations > 0:
        m.retry_rate = round(m.retry_count / m.total_delegations, 3)

    # --- Verifier reports ---
    verifications = _list_json_files(state / "verifications")
    for v in verifications:
        verdict = v.get("verdict", "")
        if verdict == "rejected":
            m.verifier_rejection_count += 1
        elif verdict == "approved":
            m.verifier_approval_count += 1

    total_verifications = m.verifier_rejection_count + m.verifier_approval_count
    if total_verifications > 0:
        m.verifier_rejection_rate = round(
            m.verifier_rejection_count / total_verifications, 3
        )

    # --- Contract failure rate (from metrics.json) ---
    metrics_data = _read_json(state / "metrics.json")
    if metrics_data and isinstance(metrics_data, dict):
        cf = metrics_data.get("contract_failure", {})
        cs = metrics_data.get("contract_success", {})
        m.contract_failure_count = (
            cf.get("_total", 0) if isinstance(cf, dict) else int(cf or 0)
        )
        m.contract_success_count = (
            cs.get("_total", 0) if isinstance(cs, dict) else int(cs or 0)
        )
        total_contracts = m.contract_failure_count + m.contract_success_count
        if total_contracts > 0:
            m.contract_failure_rate = round(
                m.contract_failure_count / total_contracts, 3
            )

    # --- Policy violations (from tool_audit.jsonl) ---
    audit_records = _read_jsonl(state / "tool_audit.jsonl")
    for rec in audit_records:
        if not rec.get("ok", True):
            # A denied tool call is a policy violation proxy
            reason = str(rec.get("error", ""))
            if ("denied" in reason.lower() or "blocked" in reason.lower()
                    or rec.get("exit_code") == -1):
                m.policy_violation_count += 1

    # --- Leases ---
    leases = _list_json_files(state / "leases")
    m.active_lease_count = len(leases)
    for lease in leases:
        expires = lease.get("expires_at", 0)
        if expires and expires < now:
            m.stale_lease_count += 1

    # --- Orphaned agents ---
    for agent in agent_states:
        status = agent.get("status", "")
        if status == "executing":
            started = agent.get("started_at") or agent.get("updated_at", 0)
            if started and (now - started) > AGENT_EXECUTING_SLA_S:
                m.orphaned_agent_count += 1

    return m


# ---------------------------------------------------------------------------
# Health detection
# ---------------------------------------------------------------------------

def detect_health_issues(base: Path | None = None) -> list[HealthFinding]:
    """Detect unhealthy conditions from repository-native state.

    All rules are threshold/SLA-based — deterministic and documented.
    """
    root = base or BASE
    state = root / "STATE"
    now = time.time()
    findings: list[HealthFinding] = []

    # --- 1. Stuck workflows (executing past SLA) ---
    workflows = _list_json_files(state / "workflows")
    for wf in workflows:
        wf_id = wf.get("workflow_id", "unknown")
        status = wf.get("status", "")
        created = wf.get("created_at", 0)

        if status == "executing" and created:
            elapsed = now - created
            if elapsed > WORKFLOW_EXECUTING_SLA_S:
                findings.append(HealthFinding(
                    category="workflow_stuck",
                    severity=Severity.UNHEALTHY,
                    subject=wf_id,
                    detail=(f"Workflow executing for {elapsed:.0f}s "
                            f"(SLA: {WORKFLOW_EXECUTING_SLA_S}s)"),
                    metric_value=round(elapsed),
                ))
            elif elapsed > WORKFLOW_EXECUTING_SLA_S * 0.75:
                findings.append(HealthFinding(
                    category="workflow_stuck",
                    severity=Severity.WARNING,
                    subject=wf_id,
                    detail=(f"Workflow executing for {elapsed:.0f}s "
                            f"(approaching SLA: {WORKFLOW_EXECUTING_SLA_S}s)"),
                    metric_value=round(elapsed),
                ))

        # --- Budget near-exhaustion ---
        if status in ("created", "planning", "executing"):
            budget = wf.get("budget", {})
            max_runtime = budget.get("max_runtime_s", WORKFLOW_EXECUTING_SLA_S)
            if created and max_runtime:
                remaining_fraction = max(
                    0, 1 - (now - created) / max_runtime
                )
                if remaining_fraction <= 0:
                    findings.append(HealthFinding(
                        category="budget_exhausted",
                        severity=Severity.UNHEALTHY,
                        subject=wf_id,
                        detail="Budget runtime exhausted",
                        metric_value=0.0,
                    ))
                elif remaining_fraction <= BUDGET_WARN_FRACTION:
                    findings.append(HealthFinding(
                        category="budget_near_exhaustion",
                        severity=Severity.WARNING,
                        subject=wf_id,
                        detail=(f"Budget {remaining_fraction:.0%} remaining "
                                f"(warn threshold: {BUDGET_WARN_FRACTION:.0%})"),
                        metric_value=round(remaining_fraction, 3),
                    ))

        # --- Unresolved child contracts ---
        if status == "completed":
            delegation_ids = wf.get("delegations", [])
            work = root / "WORK"
            for sub_id in delegation_ids:
                contract_path = work / "agents" / "contracts" / f"{sub_id}.json"
                if not contract_path.exists():
                    findings.append(HealthFinding(
                        category="contract_gap",
                        severity=Severity.WARNING,
                        subject=wf_id,
                        detail=f"No child contract for delegation {sub_id}",
                    ))

        # --- Repeated verifier rejections ---
        verif_dir = state / "verifications"
        if verif_dir.exists():
            rejections_for_wf = 0
            for vf in verif_dir.glob("*.json"):
                vdata = _read_json(vf)
                if (vdata and vdata.get("workflow_id") == wf_id
                        and vdata.get("verdict") == "rejected"):
                    rejections_for_wf += 1
            if rejections_for_wf >= 2:
                findings.append(HealthFinding(
                    category="verifier_failure",
                    severity=Severity.UNHEALTHY,
                    subject=wf_id,
                    detail=f"Verifier rejected {rejections_for_wf} times",
                    metric_value=rejections_for_wf,
                ))

    # --- 2. Stuck agents ---
    agent_states = _list_json_files(state / "agents" / "runtime")
    for agent in agent_states:
        agent_id = agent.get("agent_id", "unknown")
        status = agent.get("status", "")
        started = agent.get("started_at") or agent.get("updated_at", 0)

        if status == "executing" and started:
            elapsed = now - started
            if elapsed > AGENT_EXECUTING_SLA_S:
                findings.append(HealthFinding(
                    category="agent_stuck",
                    severity=Severity.UNHEALTHY,
                    subject=agent_id,
                    detail=(f"Agent executing for {elapsed:.0f}s "
                            f"(SLA: {AGENT_EXECUTING_SLA_S}s)"),
                    metric_value=round(elapsed),
                ))

        # Waiting on dependency too long
        if status == "waiting":
            updated = agent.get("updated_at", 0)
            if updated:
                wait_time = now - updated
                if wait_time > DEPENDENCY_WAIT_SLA_S:
                    findings.append(HealthFinding(
                        category="dependency_wait",
                        severity=Severity.WARNING,
                        subject=agent_id,
                        detail=(f"Waiting on dependency for {wait_time:.0f}s "
                                f"(SLA: {DEPENDENCY_WAIT_SLA_S}s)"),
                        metric_value=round(wait_time),
                    ))

    # --- 3. Orphaned leases ---
    leases = _list_json_files(state / "leases")
    for lease in leases:
        holder = lease.get("holder", "unknown")
        expires = lease.get("expires_at", 0)
        if expires and expires < now:
            findings.append(HealthFinding(
                category="orphan",
                severity=Severity.UNHEALTHY,
                subject=holder,
                detail=(f"Lease expired at {expires:.0f}, "
                        f"now {now:.0f} (orphan)"),
                metric_value=round(now - expires),
            ))

    # --- 4. Missing runtime records ---
    # Delegations claimed by agents with no runtime record
    delegations = _list_json_files(state / "delegations")
    known_agents = {a.get("agent_id") for a in agent_states}
    for d in delegations:
        if d.get("status") in ("claimed", "executing"):
            agent_id = d.get("agent_id", "")
            if agent_id and agent_id not in known_agents:
                findings.append(HealthFinding(
                    category="orphan",
                    severity=Severity.WARNING,
                    subject=agent_id,
                    detail=f"Delegation references agent with no runtime record",
                ))

    return findings


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_health_report(base: Path | None = None) -> HealthReport:
    """Generate a complete multi-agent health report.

    Combines metric collection + health detection into a single report.
    """
    root = base or BASE
    metrics = collect_metrics(base=root)
    findings = detect_health_issues(base=root)

    # Determine overall severity
    severities = [f.severity for f in findings]
    if Severity.UNHEALTHY in severities:
        overall = Severity.UNHEALTHY
    elif Severity.WARNING in severities:
        overall = Severity.WARNING
    else:
        overall = Severity.HEALTHY

    # Workflow summaries
    workflows = _list_json_files((root / "STATE") / "workflows")
    summaries = []
    for wf in workflows:
        summaries.append({
            "workflow_id": wf.get("workflow_id", "?"),
            "task_id": wf.get("task_id", "?"),
            "status": wf.get("status", "?"),
            "halt_reason": wf.get("halt_reason"),
        })

    # Top bottlenecks
    bottlenecks = []
    category_counts: dict[str, int] = {}
    for f in findings:
        category_counts[f.category] = category_counts.get(f.category, 0) + 1
    for cat, count in sorted(category_counts.items(),
                              key=lambda x: x[1], reverse=True)[:5]:
        bottlenecks.append(f"{cat}: {count} issue(s)")

    if metrics.contract_failure_rate and metrics.contract_failure_rate > 0.3:
        bottlenecks.append(
            f"High contract failure rate: {metrics.contract_failure_rate:.1%}"
        )
    if metrics.verifier_rejection_rate and metrics.verifier_rejection_rate > 0.3:
        bottlenecks.append(
            f"High verifier rejection rate: {metrics.verifier_rejection_rate:.1%}"
        )

    return HealthReport(
        overall=overall,
        metrics=metrics,
        findings=findings,
        workflow_summaries=summaries,
        top_bottlenecks=bottlenecks,
    )


def render_report_markdown(report: HealthReport) -> str:
    """Render a HealthReport as HEARTBEAT_MULTIAGENT.md content."""
    lines = [
        "# NovaCore Multi-Agent Heartbeat",
        f"Generated: {report.generated_at}",
        "",
        f"## Overall: {report.overall.upper()}",
        "",
    ]

    # --- Metrics summary ---
    m = report.metrics
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Active workflows | {m.active_workflows} |")
    lines.append(f"| Completed workflows | {m.completed_workflows} |")
    lines.append(f"| Failed workflows | {m.failed_workflows} |")
    lines.append(f"| Halted workflows | {m.halted_workflows} |")
    lines.append(f"| Total delegations | {m.total_delegations} |")
    lines.append(f"| Completed delegations | {m.completed_delegations} |")
    lines.append(f"| Failed delegations | {m.failed_delegations} |")
    lines.append(f"| Agent spawn count | {m.agent_spawn_count} |")
    lines.append(f"| Mean subtask latency | "
                 f"{m.mean_subtask_latency_s or 'N/A'}s |")
    lines.append(f"| Retry count | {m.retry_count} |")
    lines.append(f"| Retry rate | {_pct(m.retry_rate)} |")
    lines.append(f"| Verifier rejections | {m.verifier_rejection_count} |")
    lines.append(f"| Verifier approvals | {m.verifier_approval_count} |")
    lines.append(f"| Verifier rejection rate | {_pct(m.verifier_rejection_rate)} |")
    lines.append(f"| Contract successes | {m.contract_success_count} |")
    lines.append(f"| Contract failures | {m.contract_failure_count} |")
    lines.append(f"| Contract failure rate | {_pct(m.contract_failure_rate)} |")
    lines.append(f"| Budget exhaustions | {m.budget_exhaustion_count} |")
    lines.append(f"| Policy violations | {m.policy_violation_count} |")
    lines.append(f"| Orphaned agents | {m.orphaned_agent_count} |")
    lines.append(f"| Active leases | {m.active_lease_count} |")
    lines.append(f"| Stale leases | {m.stale_lease_count} |")
    lines.append("")

    # --- Active workflows ---
    if report.workflow_summaries:
        lines.append("## Workflows")
        lines.append("")
        lines.append("| ID | Task | Status | Halt Reason |")
        lines.append("|----|------|--------|-------------|")
        for ws in report.workflow_summaries:
            lines.append(
                f"| {ws['workflow_id']} | {ws['task_id']} "
                f"| {ws['status']} | {ws.get('halt_reason') or '-'} |"
            )
        lines.append("")

    # --- Health findings ---
    if report.findings:
        lines.append("## Health Findings")
        lines.append("")
        for f in report.findings:
            icon = {"unhealthy": "X", "warning": "!", "healthy": "-"}.get(
                f.severity, "?"
            )
            lines.append(f"- [{icon}] **{f.category}** ({f.subject}): {f.detail}")
        lines.append("")

    # --- Top bottlenecks ---
    if report.top_bottlenecks:
        lines.append("## Top Bottlenecks")
        lines.append("")
        for b in report.top_bottlenecks:
            lines.append(f"- {b}")
        lines.append("")

    # --- No findings = explicit healthy ---
    if not report.findings:
        lines.append("## Health Findings")
        lines.append("")
        lines.append("No issues detected.")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_report_json(report: HealthReport) -> str:
    """Render a HealthReport as a JSON string."""
    return json.dumps(report.to_dict(), indent=2, default=str) + "\n"


def _pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1%}"


# ---------------------------------------------------------------------------
# Write to disk
# ---------------------------------------------------------------------------

def write_heartbeat_multiagent(
    report: HealthReport,
    base: Path | None = None,
) -> tuple[Path, Path]:
    """Write HEARTBEAT_MULTIAGENT.md and companion JSON to base directory.

    Returns (md_path, json_path).
    """
    root = base or BASE
    md_path = root / "HEARTBEAT_MULTIAGENT.md"
    json_path = root / "STATE" / "heartbeat_multiagent.json"

    md_content = render_report_markdown(report)
    md_path.write_text(md_content)

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_content = render_report_json(report)
    json_path.write_text(json_content)

    return md_path, json_path


# ---------------------------------------------------------------------------
# Single entry point
# ---------------------------------------------------------------------------

def run_multiagent_heartbeat(base: Path | None = None) -> HealthReport:
    """Run the full multi-agent heartbeat: collect → detect → report → write.

    This is the integration entry point called from heartbeat.py.
    """
    report = generate_health_report(base=base)
    write_heartbeat_multiagent(report, base=base)
    return report
