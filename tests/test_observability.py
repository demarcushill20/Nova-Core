"""Tests for Phase 7.6 — Multi-Agent Observability and Heartbeat Upgrade.

Acceptance criteria covered:
  1. unhealthy workflow flagged automatically
  2. stuck agent surfaced
  3. policy violations counted
  4. contract failure rate tracked
  5. simulated orphan agent detected
  6. simulated budget exhaustion surfaced
  7. stale dependency wait detected
  8. heartbeat report generated deterministically
"""

import json
import time
from pathlib import Path

import pytest

from agents.observability import (
    MultiAgentMetrics,
    HealthFinding,
    HealthReport,
    Severity,
    collect_metrics,
    detect_health_issues,
    generate_health_report,
    render_report_markdown,
    render_report_json,
    write_heartbeat_multiagent,
    run_multiagent_heartbeat,
    WORKFLOW_EXECUTING_SLA_S,
    AGENT_EXECUTING_SLA_S,
    DEPENDENCY_WAIT_SLA_S,
    BUDGET_WARN_FRACTION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")


def _make_workflow(tmp_path, workflow_id="wf01", task_id="task01",
                   status="executing", created_at=None, budget=None,
                   delegations=None, halt_reason=None, node_states=None):
    wf = {
        "workflow_id": workflow_id,
        "task_id": task_id,
        "status": status,
        "created_at": created_at or time.time(),
        "delegations": delegations or [],
    }
    if budget:
        wf["budget"] = budget
    if halt_reason:
        wf["halt_reason"] = halt_reason
    if node_states:
        wf["node_states"] = node_states
    _write_json(tmp_path / "STATE" / "workflows" / f"{workflow_id}.json", wf)
    return wf


def _make_delegation(tmp_path, workflow_id="wf01", subtask_id="sub01",
                     agent_id="agent01", role="coder", status="completed",
                     claimed_at=None, completed_at=None):
    d = {
        "workflow_id": workflow_id,
        "subtask_id": subtask_id,
        "agent_id": agent_id,
        "role": role,
        "status": status,
        "claimed_at": claimed_at or time.time() - 60,
        "completed_at": completed_at or time.time(),
    }
    _write_json(
        tmp_path / "STATE" / "delegations" / f"{workflow_id}_{subtask_id}.json",
        d,
    )
    return d


def _make_agent_state(tmp_path, agent_id="agent01", status="idle",
                      started_at=None, updated_at=None, workflow_id=None):
    a = {
        "agent_id": agent_id,
        "status": status,
        "started_at": started_at,
        "updated_at": updated_at or time.time(),
    }
    if workflow_id:
        a["workflow_id"] = workflow_id
    _write_json(
        tmp_path / "STATE" / "agents" / "runtime" / f"{agent_id}.json",
        a,
    )
    return a


def _make_verification(tmp_path, workflow_id="wf01", verdict="approved",
                       report_id=None):
    rid = report_id or f"vr_{workflow_id}_{int(time.time())}"
    v = {
        "report_id": rid,
        "workflow_id": workflow_id,
        "verdict": verdict,
        "verified_at": time.time(),
    }
    _write_json(tmp_path / "STATE" / "verifications" / f"{rid}.json", v)
    return v


def _make_lease(tmp_path, workflow_id="wf01", node_id="node01",
                holder="agent01", expires_at=None):
    lease = {
        "workflow_id": workflow_id,
        "node_id": node_id,
        "holder": holder,
        "expires_at": expires_at or (time.time() + 600),
    }
    _write_json(
        tmp_path / "STATE" / "leases" / f"{workflow_id}_{node_id}.json",
        lease,
    )
    return lease


def _make_metrics_json(tmp_path, contract_success=0, contract_failure=0):
    data = {
        "contract_success": {"_total": contract_success},
        "contract_failure": {"_total": contract_failure},
    }
    _write_json(tmp_path / "STATE" / "metrics.json", data)
    return data


def _make_audit_trail(tmp_path, records: list[dict]):
    _write_jsonl(tmp_path / "STATE" / "tool_audit.jsonl", records)


# ---------------------------------------------------------------------------
# 1. Metric collection tests
# ---------------------------------------------------------------------------

class TestCollectMetrics:
    """Test metric aggregation from repository-native state."""

    def test_empty_state(self, tmp_path):
        m = collect_metrics(base=tmp_path)
        assert m.active_workflows == 0
        assert m.total_delegations == 0
        assert m.agent_spawn_count == 0

    def test_active_workflow_counted(self, tmp_path):
        _make_workflow(tmp_path, status="executing")
        m = collect_metrics(base=tmp_path)
        assert m.active_workflows == 1

    def test_completed_and_failed_workflows(self, tmp_path):
        _make_workflow(tmp_path, workflow_id="wf1", status="completed")
        _make_workflow(tmp_path, workflow_id="wf2", status="failed")
        m = collect_metrics(base=tmp_path)
        assert m.completed_workflows == 1
        assert m.failed_workflows == 1

    def test_halted_workflow_with_budget_exhaustion(self, tmp_path):
        _make_workflow(tmp_path, status="halted",
                       halt_reason="budget_exhausted: exceeded limit")
        m = collect_metrics(base=tmp_path)
        assert m.halted_workflows == 1
        assert m.budget_exhaustion_count == 1

    def test_delegation_counts_and_latency(self, tmp_path):
        now = time.time()
        _make_delegation(tmp_path, subtask_id="s1", status="completed",
                         claimed_at=now - 100, completed_at=now - 50)
        _make_delegation(tmp_path, subtask_id="s2", status="failed")
        m = collect_metrics(base=tmp_path)
        assert m.total_delegations == 2
        assert m.completed_delegations == 1
        assert m.failed_delegations == 1
        assert m.mean_subtask_latency_s == 50.0

    def test_agent_spawn_count(self, tmp_path):
        _make_agent_state(tmp_path, agent_id="a1")
        _make_agent_state(tmp_path, agent_id="a2")
        m = collect_metrics(base=tmp_path)
        assert m.agent_spawn_count == 2

    def test_verifier_rejection_rate(self, tmp_path):
        _make_verification(tmp_path, verdict="approved", report_id="v1")
        _make_verification(tmp_path, verdict="rejected", report_id="v2")
        _make_verification(tmp_path, verdict="rejected", report_id="v3")
        m = collect_metrics(base=tmp_path)
        assert m.verifier_rejection_count == 2
        assert m.verifier_approval_count == 1
        assert m.verifier_rejection_rate == pytest.approx(0.667, abs=0.01)

    def test_contract_failure_rate(self, tmp_path):
        _make_metrics_json(tmp_path, contract_success=7, contract_failure=3)
        m = collect_metrics(base=tmp_path)
        assert m.contract_success_count == 7
        assert m.contract_failure_count == 3
        assert m.contract_failure_rate == 0.3

    def test_policy_violations_from_audit(self, tmp_path):
        _make_audit_trail(tmp_path, [
            {"tool": "shell.run", "ok": True, "exit_code": 0},
            {"tool": "shell.run", "ok": False, "exit_code": -1,
             "error": "blocked by safety"},
            {"tool": "repo.files.write", "ok": False, "exit_code": -1,
             "error": "denied by policy"},
        ])
        m = collect_metrics(base=tmp_path)
        assert m.policy_violation_count == 2

    def test_retry_count_from_node_states(self, tmp_path):
        _make_workflow(tmp_path, node_states={
            "node1": {"retry_count": 2},
            "node2": {"retry_count": 1},
        })
        _make_delegation(tmp_path)  # 1 delegation for retry_rate calc
        m = collect_metrics(base=tmp_path)
        assert m.retry_count == 3

    def test_orphaned_agent_count(self, tmp_path):
        # Agent executing since long ago
        _make_agent_state(tmp_path, agent_id="stuck_agent",
                          status="executing",
                          started_at=time.time() - AGENT_EXECUTING_SLA_S - 100)
        m = collect_metrics(base=tmp_path)
        assert m.orphaned_agent_count == 1

    def test_stale_lease_count(self, tmp_path):
        _make_lease(tmp_path, expires_at=time.time() - 100)
        m = collect_metrics(base=tmp_path)
        assert m.stale_lease_count == 1
        assert m.active_lease_count == 1


# ---------------------------------------------------------------------------
# 2. Health detection tests
# ---------------------------------------------------------------------------

class TestDetectHealthIssues:
    """Test health detection rules."""

    def test_no_issues_on_empty_state(self, tmp_path):
        findings = detect_health_issues(base=tmp_path)
        assert findings == []

    def test_stuck_workflow_detected(self, tmp_path):
        _make_workflow(
            tmp_path,
            status="executing",
            created_at=time.time() - WORKFLOW_EXECUTING_SLA_S - 100,
        )
        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "workflow_stuck"]
        assert len(stuck) == 1
        assert stuck[0].severity == Severity.UNHEALTHY

    def test_workflow_approaching_sla_is_warning(self, tmp_path):
        # 80% of SLA elapsed
        _make_workflow(
            tmp_path,
            status="executing",
            created_at=time.time() - WORKFLOW_EXECUTING_SLA_S * 0.8,
        )
        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "workflow_stuck"]
        assert len(stuck) == 1
        assert stuck[0].severity == Severity.WARNING

    def test_stuck_agent_detected(self, tmp_path):
        _make_agent_state(
            tmp_path, agent_id="slow_agent", status="executing",
            started_at=time.time() - AGENT_EXECUTING_SLA_S - 100,
        )
        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "agent_stuck"]
        assert len(stuck) == 1
        assert stuck[0].severity == Severity.UNHEALTHY
        assert stuck[0].subject == "slow_agent"

    def test_stale_dependency_wait_detected(self, tmp_path):
        _make_agent_state(
            tmp_path, agent_id="waiting_agent", status="waiting",
            updated_at=time.time() - DEPENDENCY_WAIT_SLA_S - 100,
        )
        findings = detect_health_issues(base=tmp_path)
        waits = [f for f in findings if f.category == "dependency_wait"]
        assert len(waits) == 1
        assert waits[0].severity == Severity.WARNING

    def test_orphaned_lease_detected(self, tmp_path):
        _make_lease(tmp_path, holder="dead_agent",
                    expires_at=time.time() - 300)
        findings = detect_health_issues(base=tmp_path)
        orphans = [f for f in findings if f.category == "orphan"]
        assert len(orphans) >= 1
        assert any(f.subject == "dead_agent" for f in orphans)

    def test_orphaned_delegation_no_runtime(self, tmp_path):
        # Delegation claimed by agent with no runtime record
        _make_delegation(tmp_path, agent_id="ghost_agent", status="executing")
        findings = detect_health_issues(base=tmp_path)
        orphans = [f for f in findings if f.category == "orphan"]
        assert any("ghost_agent" in f.subject for f in orphans)

    def test_budget_exhausted(self, tmp_path):
        _make_workflow(
            tmp_path, status="executing",
            created_at=time.time() - 2000,
            budget={"max_runtime_s": 1800},
        )
        findings = detect_health_issues(base=tmp_path)
        budget = [f for f in findings if f.category == "budget_exhausted"]
        assert len(budget) == 1
        assert budget[0].severity == Severity.UNHEALTHY

    def test_budget_near_exhaustion_warning(self, tmp_path):
        max_runtime = 1800
        # 90% elapsed → 10% remaining → below 15% threshold
        _make_workflow(
            tmp_path, status="executing",
            created_at=time.time() - max_runtime * 0.90,
            budget={"max_runtime_s": max_runtime},
        )
        findings = detect_health_issues(base=tmp_path)
        near = [f for f in findings if f.category == "budget_near_exhaustion"]
        assert len(near) == 1
        assert near[0].severity == Severity.WARNING

    def test_repeated_verifier_rejections(self, tmp_path):
        _make_workflow(tmp_path, workflow_id="wf_reject", status="executing")
        _make_verification(tmp_path, workflow_id="wf_reject",
                           verdict="rejected", report_id="v1")
        _make_verification(tmp_path, workflow_id="wf_reject",
                           verdict="rejected", report_id="v2")
        findings = detect_health_issues(base=tmp_path)
        verifier = [f for f in findings if f.category == "verifier_failure"]
        assert len(verifier) == 1
        assert verifier[0].severity == Severity.UNHEALTHY

    def test_unresolved_child_contract(self, tmp_path):
        _make_workflow(tmp_path, status="completed",
                       delegations=["sub_missing"])
        findings = detect_health_issues(base=tmp_path)
        gaps = [f for f in findings if f.category == "contract_gap"]
        assert len(gaps) == 1

    def test_healthy_workflow_no_findings(self, tmp_path):
        _make_workflow(tmp_path, status="completed",
                       created_at=time.time() - 60)
        findings = detect_health_issues(base=tmp_path)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# 3. Report generation tests
# ---------------------------------------------------------------------------

class TestReportGeneration:
    """Test heartbeat report generation and rendering."""

    def test_generate_empty_state(self, tmp_path):
        report = generate_health_report(base=tmp_path)
        assert report.overall == Severity.HEALTHY
        assert report.metrics.active_workflows == 0
        assert len(report.findings) == 0

    def test_generate_with_issues(self, tmp_path):
        _make_workflow(
            tmp_path, status="executing",
            created_at=time.time() - WORKFLOW_EXECUTING_SLA_S - 100,
        )
        report = generate_health_report(base=tmp_path)
        assert report.overall == Severity.UNHEALTHY
        assert len(report.findings) >= 1

    def test_report_includes_workflow_summaries(self, tmp_path):
        _make_workflow(tmp_path, workflow_id="wf_test", task_id="task_123",
                       status="completed")
        report = generate_health_report(base=tmp_path)
        assert len(report.workflow_summaries) == 1
        assert report.workflow_summaries[0]["workflow_id"] == "wf_test"

    def test_render_markdown(self, tmp_path):
        _make_workflow(tmp_path, status="executing",
                       created_at=time.time() - 100)
        _make_metrics_json(tmp_path, contract_success=5, contract_failure=2)
        report = generate_health_report(base=tmp_path)
        md = render_report_markdown(report)
        assert "# NovaCore Multi-Agent Heartbeat" in md
        assert "## Metrics" in md
        assert "Active workflows" in md
        assert "Contract failures" in md

    def test_render_json(self, tmp_path):
        report = generate_health_report(base=tmp_path)
        j = render_report_json(report)
        data = json.loads(j)
        assert "overall" in data
        assert "metrics" in data
        assert "findings" in data

    def test_write_heartbeat_files(self, tmp_path):
        report = generate_health_report(base=tmp_path)
        md_path, json_path = write_heartbeat_multiagent(report, base=tmp_path)
        assert md_path.exists()
        assert json_path.exists()
        assert "HEARTBEAT_MULTIAGENT.md" in md_path.name
        assert json.loads(json_path.read_text())["overall"] == "healthy"

    def test_run_multiagent_heartbeat_e2e(self, tmp_path):
        # Full end-to-end: state → report → files
        _make_workflow(tmp_path, status="executing",
                       created_at=time.time() - 100)
        _make_delegation(tmp_path, status="completed")
        _make_agent_state(tmp_path)
        _make_metrics_json(tmp_path, contract_success=10, contract_failure=1)

        report = run_multiagent_heartbeat(base=tmp_path)

        assert report.overall in (Severity.HEALTHY, Severity.WARNING,
                                   Severity.UNHEALTHY)
        assert report.metrics.active_workflows == 1
        assert report.metrics.contract_success_count == 10

        # Files written
        assert (tmp_path / "HEARTBEAT_MULTIAGENT.md").exists()
        assert (tmp_path / "STATE" / "heartbeat_multiagent.json").exists()

    def test_overall_warning_not_unhealthy(self, tmp_path):
        # Warning-level issue only → overall should be WARNING
        _make_agent_state(
            tmp_path, agent_id="waiter", status="waiting",
            updated_at=time.time() - DEPENDENCY_WAIT_SLA_S - 10,
        )
        report = generate_health_report(base=tmp_path)
        assert report.overall == Severity.WARNING

    def test_top_bottlenecks_populated(self, tmp_path):
        # Create multiple issues of same category
        for i in range(3):
            _make_agent_state(
                tmp_path, agent_id=f"stuck_{i}", status="executing",
                started_at=time.time() - AGENT_EXECUTING_SLA_S - 100,
            )
        report = generate_health_report(base=tmp_path)
        assert len(report.top_bottlenecks) >= 1
        assert "agent_stuck" in report.top_bottlenecks[0]

    def test_markdown_shows_no_issues_when_healthy(self, tmp_path):
        report = generate_health_report(base=tmp_path)
        md = render_report_markdown(report)
        assert "No issues detected" in md

    def test_markdown_shows_findings_when_unhealthy(self, tmp_path):
        _make_workflow(tmp_path, status="executing",
                       created_at=time.time() - WORKFLOW_EXECUTING_SLA_S - 100)
        report = generate_health_report(base=tmp_path)
        md = render_report_markdown(report)
        assert "workflow_stuck" in md
        assert "[X]" in md


# ---------------------------------------------------------------------------
# 4. Contract failure rate tracking test
# ---------------------------------------------------------------------------

class TestContractFailureRate:
    """Explicit test for contract failure rate metric."""

    def test_rate_computed_correctly(self, tmp_path):
        _make_metrics_json(tmp_path, contract_success=6, contract_failure=4)
        m = collect_metrics(base=tmp_path)
        assert m.contract_failure_rate == 0.4

    def test_zero_contracts_no_rate(self, tmp_path):
        _make_metrics_json(tmp_path, contract_success=0, contract_failure=0)
        m = collect_metrics(base=tmp_path)
        assert m.contract_failure_rate is None

    def test_high_failure_rate_in_bottlenecks(self, tmp_path):
        _make_metrics_json(tmp_path, contract_success=3, contract_failure=7)
        report = generate_health_report(base=tmp_path)
        assert any("contract failure rate" in b.lower()
                    for b in report.top_bottlenecks)
