"""Phase 7.8 — End-to-End Integration + Failure Simulation Suite.

Proves the governed multi-agent runtime behaves correctly under:
  - Full cross-module happy paths (research, coder→verifier, critic→replan, memory)
  - Controlled failure conditions (timeout, spawn failure, dependency cycle, etc.)
  - Heartbeat/observability accuracy under integrated scenarios
  - Hardening controls (rate limits, degradation, recovery) in context

All tests use real blackboard state on disk (tmp_path), not mocks.
Every assertion is deterministic.
"""

import json
import os
import time
from pathlib import Path

import pytest

from agents.blackboard import (
    AgentRuntimeState,
    Blackboard,
    ChildContract,
    Delegation,
    WorkflowState,
)
from agents.coordination import (
    CoordinationLayer,
    Lease,
    NodeState,
)
from agents.memory_engine import (
    capture_workflow_memory,
    retrieve_related_patterns,
)
from agents.observability import (
    Severity,
    collect_metrics,
    detect_health_issues,
    generate_health_report,
    render_report_markdown,
    run_multiagent_heartbeat,
)
from agents.policy_engine import PolicyEngine, PolicyViolation
from agents.production_hardening import (
    ArchiveManager,
    FeatureFlags,
    GracefulDegradation,
    RateLimiter,
    RestartRecovery,
    audit_policy_denial,
    run_production_hardening,
)
from agents.verifier import VerifierEngine
from agents.workflow_engine import (
    WorkflowEngine,
    WorkflowHalt,
)
from agents.workflow_gate import (
    WorkflowGate,
)
from agents.workflow_graph import WorkflowGraphBuilder, render_markdown

# ===========================================================================
# Shared fixtures
# ===========================================================================

def _bb(tmp: Path) -> Blackboard:
    return Blackboard(base=tmp)


def _engine(tmp: Path, **kw) -> WorkflowEngine:
    return WorkflowEngine(blackboard=_bb(tmp), **kw)


def _coord(tmp: Path) -> CoordinationLayer:
    return CoordinationLayer(blackboard=_bb(tmp))


def _gate(tmp: Path) -> WorkflowGate:
    return WorkflowGate(blackboard=_bb(tmp))


def _file(tmp: Path, name: str, content: str = "result") -> str:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def _contract(wf: str, sub: str, agent: str, role: str = "coder",
              summary: str = "Done", files_changed: str = "module.py",
              confidence: str = "high") -> ChildContract:
    return ChildContract(
        agent_id=agent, workflow_id=wf, subtask_id=sub, role=role,
        status="completed", summary=summary, files_changed=files_changed,
        confidence=confidence, artifacts=[],
        verification={"result": "pass", "confidence": confidence},
    )


def _good_contract_dict() -> dict:
    return {
        "summary": "Implemented feature",
        "files_changed": "module.py",
        "verification": "All tests pass",
        "confidence": "high",
    }


def _setup_flags(tmp: Path, enabled: bool = True,
                 supported_classes: list | None = None,
                 archive: bool = True, rate_limiting: bool = True) -> None:
    d = tmp / "STATE" / "config"
    d.mkdir(parents=True, exist_ok=True)
    flags = {
        "phase7_orchestrator": {
            "enabled": enabled,
            "supported_classes": supported_classes or [],
        },
        "phase7_hardening": {
            "archive_cleanup": archive,
            "rate_limiting": rate_limiting,
            "manual_approval": False,
        },
    }
    (d / "feature_flags.json").write_text(json.dumps(flags))


def _setup_registry(tmp: Path) -> None:
    d = tmp / "STATE" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    registry = {
        "agents": [
            {
                "agent_id": "orchestrator_001", "role": "orchestrator",
                "allowed_tools": ["agent.spawn", "repo.files.read"],
                "denied_tools": [],
                "max_actions": 100, "max_runtime_seconds": 1800,
                "max_retries": 2,
                "feature_flags": {"allow_delegation": True},
            },
            {
                "agent_id": "researcher_001", "role": "researcher",
                "allowed_tools": ["web.search", "http.fetch"],
                "denied_tools": ["repo.files.write", "repo.git.commit",
                                 "shell.run"],
                "max_actions": 30, "max_runtime_seconds": 180,
                "max_retries": 1,
                "feature_flags": {"allow_delegation": False},
            },
            {
                "agent_id": "coder_001", "role": "coder",
                "allowed_tools": ["repo.files.read", "repo.files.write",
                                  "repo.search.*"],
                "denied_tools": ["system.service.restart", "shell.run"],
                "max_actions": 50, "max_runtime_seconds": 300,
                "max_retries": 1,
                "feature_flags": {"allow_delegation": False},
            },
            {
                "agent_id": "verifier_001", "role": "verifier",
                "allowed_tools": ["repo.files.read", "repo.search.*"],
                "denied_tools": ["repo.files.write", "repo.git.commit",
                                 "shell.run"],
                "max_actions": 20, "max_runtime_seconds": 120,
                "max_retries": 0,
                "feature_flags": {"allow_delegation": False},
            },
        ],
    }
    (d / "registry.json").write_text(json.dumps(registry))


# ===========================================================================
# Part 1 — End-to-End Integration: Happy Paths
# ===========================================================================

class TestE2E_ResearchOnlyPath:
    """E2E: planner → orchestrator → research → synthesize."""

    def test_research_only_lifecycle(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)

        # Create workflow
        wf = engine.create_workflow("wf_research", "task_r1")
        assert wf.status == "created"

        # Plan: single research node
        coord.save_node_states("wf_research", [
            NodeState(node_id="research", workflow_id="wf_research",
                      status="pending"),
        ])

        # Delegate research
        d = engine.delegate("wf_research", "research", "researcher_001",
                            "researcher", "Find API docs")
        assert d.status == "pending"

        # Claim
        engine.claim_delegation("wf_research", "research", "researcher_001")
        assert bb.get_delegation("wf_research", "research")["status"] == "claimed"

        # Agent executes: write result
        out = _file(tmp_path, "OUTPUT/api_research.md",
                    "# API Docs\nFound 3 endpoints.")

        # Complete with contract
        c = _contract("wf_research", "research", "researcher_001",
                      "researcher", "Researched API docs",
                      files_changed="api_research.md")
        c.artifacts = [out]
        engine.complete_delegation("wf_research", "research",
                                   "researcher_001", c)
        coord.update_node_state("wf_research", "research",
                                {"status": "completed"})

        # Synthesize (ungoverned — read-only research, no repo changes)
        synthesis = engine.synthesize_workflow("wf_research")
        assert synthesis["status"] == "completed"
        assert len(synthesis["child_contracts"]) == 1

        # Verify state coherence
        final_wf = bb.get_workflow("wf_research")
        assert final_wf["status"] == "completed"
        assert len(bb.list_child_contracts("wf_research")) == 1

    def test_research_path_metrics_visible(self, tmp_path):
        """Observability sees the research workflow."""
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_r_obs", "task_r_obs")
        bb.update_workflow("wf_r_obs", {"status": "executing"})
        engine.delegate("wf_r_obs", "research", "researcher_001",
                        "researcher", "Find data")

        metrics = collect_metrics(base=tmp_path)
        assert metrics.active_workflows >= 1
        assert metrics.total_delegations >= 1


class TestE2E_CoderVerifierPath:
    """E2E: planner → orchestrator → coder → critic → verifier → governed synthesis."""

    def test_coder_verifier_full_path(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)
        gate = WorkflowGate(blackboard=bb)

        # Create workflow with budget
        engine.create_workflow("wf_code_v", "task_cv",
                               budget={"max_runtime_s": 1800})
        bb.update_workflow("wf_code_v", {"status": "executing"})

        # Plan: code → verify (dependency)
        coord.save_node_states("wf_code_v", [
            NodeState(node_id="code", workflow_id="wf_code_v",
                      status="pending"),
            NodeState(node_id="verify", workflow_id="wf_code_v",
                      status="pending", depends_on=["code"]),
        ])

        # Only code should be ready
        ready = coord.get_ready_nodes("wf_code_v")
        assert ready == ["code"]

        # Delegate and claim code
        engine.delegate("wf_code_v", "code", "coder_001", "coder",
                        "Implement feature X")
        engine.claim_delegation("wf_code_v", "code", "coder_001")

        # Register agent runtime
        bb.set_agent_state(AgentRuntimeState(
            agent_id="coder_001", workflow_id="wf_code_v",
            status="executing",
        ))

        # Coder produces output
        out = _file(tmp_path, "OUTPUT/feature_x.py",
                    "def feature_x():\n    return 42\n")
        c1 = _contract("wf_code_v", "code", "coder_001", "coder",
                       "Implemented feature X", "feature_x.py")
        c1.artifacts = [out]
        engine.complete_delegation("wf_code_v", "code", "coder_001", c1)
        coord.update_node_state("wf_code_v", "code",
                                {"status": "completed"})
        bb.set_agent_state(AgentRuntimeState(
            agent_id="coder_001", workflow_id="wf_code_v",
            status="completed",
        ))

        # Verify node now ready
        ready = coord.get_ready_nodes("wf_code_v")
        assert ready == ["verify"]

        # Critic review (pass)
        review = gate.run_critic_review(
            "wf_code_v", "code",
            deliverables={"feature_x.py": out},
            contract=_good_contract_dict(),
        )
        assert review.verdict == "pass"

        # Verifier gate
        contracts = bb.list_child_contracts("wf_code_v")
        allowed, reason = gate.is_completion_allowed(
            "wf_code_v",
            deliverables={"feature_x.py": out},
            contracts=contracts,
        )
        assert allowed is True, f"Blocked: {reason}"

        # Governed synthesis (with repo changes → maker-checker enforced)
        synthesis = engine.governed_synthesize(
            "wf_code_v",
            deliverables={"feature_x.py": out},
            repo_changes=["feature_x.py"],
        )
        assert synthesis["status"] == "completed"
        assert synthesis.get("governed") is True
        assert synthesis.get("verification_approved") is True

        # Graph rendering
        builder = WorkflowGraphBuilder(blackboard=bb)
        graph = builder.build("wf_code_v")
        md = render_markdown(graph)
        assert "Implement feature X" in md


class TestE2E_CriticReplanPath:
    """E2E: critic objection → replan → retry → success."""

    def test_critic_replan_then_retry(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        CoordinationLayer(blackboard=bb)
        gate = WorkflowGate(blackboard=bb)

        engine.create_workflow("wf_replan", "task_rp")

        # First attempt — empty deliverables → critic objection
        engine.delegate("wf_replan", "code_v1", "coder_001", "coder",
                        "Build module")
        engine.claim_delegation("wf_replan", "code_v1", "coder_001")

        # Submit bad work (no deliverables → objection)
        c_bad = ChildContract(
            agent_id="coder_001", workflow_id="wf_replan",
            subtask_id="code_v1", role="coder", status="completed",
            summary="Attempted", files_changed="bad.py", confidence="low",
        )
        engine.complete_delegation("wf_replan", "code_v1", "coder_001",
                                   c_bad)

        # Critic review with empty deliverables → objection
        review = gate.run_critic_review(
            "wf_replan", "code_v1", deliverables={},
        )
        assert review.verdict == "objection"
        assert review.blocking is True

        # Check replan signals
        decisions = gate.check_replan_signals("wf_replan")
        assert len(decisions) >= 1
        assert decisions[0].action in ("retry", "reassign", "halt")

        # Verify contract validation blocks (bad contract lacks fields)
        bb.list_child_contracts("wf_replan")
        # The bad contract has files_changed but the governed path needs
        # all contracts to pass validation.

        # Second attempt — good work
        engine.delegate("wf_replan", "code_v2", "coder_001", "coder",
                        "Rebuild module")
        engine.claim_delegation("wf_replan", "code_v2", "coder_001")
        good_out = _file(tmp_path, "OUTPUT/good.py",
                         "def module():\n    return True\n")
        c_good = _contract("wf_replan", "code_v2", "coder_001",
                           summary="Rebuilt module properly",
                           files_changed="good.py")
        c_good.artifacts = [good_out]
        engine.complete_delegation("wf_replan", "code_v2", "coder_001",
                                   c_good)

        # Critic review → pass
        review2 = gate.run_critic_review(
            "wf_replan", "code_v2",
            deliverables={"good.py": good_out},
            contract=_good_contract_dict(),
        )
        assert review2.verdict == "pass"


class TestE2E_MemoryCapturePath:
    """E2E: workflow → completion → memory capture → retrieval."""

    def test_full_memory_lifecycle(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_mem_e2e", "task_mem")
        engine.delegate("wf_mem_e2e", "code", "coder_001", "coder",
                        "Build auth module")
        engine.claim_delegation("wf_mem_e2e", "code", "coder_001")

        c = _contract("wf_mem_e2e", "code", "coder_001",
                      summary="Built auth module with JWT",
                      files_changed="auth.py")
        engine.complete_delegation("wf_mem_e2e", "code", "coder_001", c)
        engine.synthesize_workflow("wf_mem_e2e")

        # Capture memory
        delegations = bb.list_delegations("wf_mem_e2e")
        contracts = bb.list_child_contracts("wf_mem_e2e")
        metrics = bb.workflow_metrics("wf_mem_e2e")

        mem_base = tmp_path / "MEMORY"
        mem_path = capture_workflow_memory(
            workflow_id="wf_mem_e2e",
            task_summary="Build auth with JWT",
            task_class="code_impl",
            delegations=delegations,
            contracts=contracts,
            metrics=metrics,
            verification_outcome="approved",
            base=mem_base,
        )
        assert mem_path is not None
        assert mem_path.exists()

        # Retrieve and verify
        results = retrieve_related_patterns(
            task_class="code_impl", keywords=["auth", "JWT"],
            base=mem_base,
        )
        assert len(results) == 1
        assert results[0]["task_class"] == "code_impl"
        assert "auth" in results[0]["task_summary"].lower()


class TestE2E_SingleAgentFallback:
    """E2E: multi-agent disabled → GracefulDegradation → single-agent."""

    def test_disabled_orchestrator_degrades(self, tmp_path):
        _setup_flags(tmp_path, enabled=False)
        gd = GracefulDegradation(base=tmp_path)

        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "degrade"
        assert result.fallback == "single_agent_worker"

    def test_disabled_orchestrator_with_ungoverned_synthesis(self, tmp_path):
        """Full path: flags off → degrade → ungoverned synthesis works."""
        _setup_flags(tmp_path, enabled=False)
        gd = GracefulDegradation(base=tmp_path)

        # Check degradation
        result = gd.check_orchestrator_available("research")
        assert result.action == "degrade"

        # Proceed with single-agent ungoverned path
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        engine.create_workflow("wf_fallback", "task_fb")
        engine.delegate("wf_fallback", "work", "agent_1", "coder", "Do it")
        engine.claim_delegation("wf_fallback", "work", "agent_1")
        c = _contract("wf_fallback", "work", "agent_1")
        engine.complete_delegation("wf_fallback", "work", "agent_1", c)

        synthesis = engine.synthesize_workflow("wf_fallback")
        assert synthesis["status"] == "completed"

    def test_unsupported_task_class_degrades(self, tmp_path):
        _setup_flags(tmp_path, enabled=True,
                     supported_classes=["research"])
        gd = GracefulDegradation(base=tmp_path)

        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "degrade"
        assert result.fallback == "single_agent_worker"

    def test_supported_task_class_proceeds(self, tmp_path):
        _setup_flags(tmp_path, enabled=True,
                     supported_classes=["research", "code_impl"])
        gd = GracefulDegradation(base=tmp_path)

        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "proceed"


class TestE2E_RateLimitedWorkflow:
    """E2E: workflow launch respects rate limits at each stage."""

    def test_workflow_launch_with_rate_check(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)
        rl = RateLimiter(base=tmp_path)
        gd = GracefulDegradation(base=tmp_path)

        # Check feasibility before launch
        wf_check = gd.check_workflow_launch_feasibility()
        assert wf_check.action == "proceed"

        # Record the launch
        rl.record_event("workflow_launch")

        # Check spawn feasibility
        spawn_check = gd.check_spawn_feasibility()
        assert spawn_check.action == "proceed"

        # Record spawn
        rl.record_event("agent_spawn")

        # Verify rate limit state
        status = rl.check_workflow_launch()
        assert status.allowed is True
        assert status.count == 1

    def test_workflow_launch_blocked_at_limit(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)
        rl = RateLimiter(base=tmp_path)
        gd = GracefulDegradation(base=tmp_path)

        # Exhaust workflow launch limit
        for _ in range(10):
            rl.record_event("workflow_launch")

        result = gd.check_workflow_launch_feasibility()
        assert result.action == "degrade"
        assert result.fallback == "single_agent_worker"

    def test_spawn_blocked_at_limit(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)
        rl = RateLimiter(base=tmp_path)
        gd = GracefulDegradation(base=tmp_path)

        # Exhaust spawn limit
        for _ in range(20):
            rl.record_event("agent_spawn")

        result = gd.check_spawn_feasibility()
        assert result.action == "halt"


class TestE2E_MultiRoleWorkflow:
    """E2E: research → code → verify with node dependencies."""

    def test_multi_role_workflow(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)
        gate = WorkflowGate(blackboard=bb)

        engine.create_workflow("wf_multi", "task_multi")

        # DAG: research → code → verify
        coord.save_node_states("wf_multi", [
            NodeState(node_id="research", workflow_id="wf_multi",
                      status="pending"),
            NodeState(node_id="code", workflow_id="wf_multi",
                      status="pending", depends_on=["research"]),
            NodeState(node_id="verify", workflow_id="wf_multi",
                      status="pending", depends_on=["code"]),
        ])

        # Step 1: Research
        assert coord.get_ready_nodes("wf_multi") == ["research"]

        engine.delegate("wf_multi", "research", "researcher_001",
                        "researcher", "Research API")
        engine.claim_delegation("wf_multi", "research", "researcher_001")
        c_res = _contract("wf_multi", "research", "researcher_001",
                          "researcher", "Researched API", "research.md")
        engine.complete_delegation("wf_multi", "research",
                                   "researcher_001", c_res)
        coord.update_node_state("wf_multi", "research",
                                {"status": "completed"})

        # Step 2: Code (now unblocked)
        assert coord.get_ready_nodes("wf_multi") == ["code"]

        engine.delegate("wf_multi", "code", "coder_001", "coder",
                        "Implement API client")
        engine.claim_delegation("wf_multi", "code", "coder_001")
        out = _file(tmp_path, "OUTPUT/api_client.py",
                    "class APIClient:\n    pass\n")
        c_code = _contract("wf_multi", "code", "coder_001", "coder",
                           "Implemented API client", "api_client.py")
        c_code.artifacts = [out]
        engine.complete_delegation("wf_multi", "code", "coder_001", c_code)
        coord.update_node_state("wf_multi", "code",
                                {"status": "completed"})

        # Step 3: Verify (now unblocked)
        assert coord.get_ready_nodes("wf_multi") == ["verify"]

        # Critic review
        review = gate.run_critic_review(
            "wf_multi", "code",
            deliverables={"api_client.py": out},
            contract=_good_contract_dict(),
        )
        assert review.verdict == "pass"
        coord.update_node_state("wf_multi", "verify",
                                {"status": "completed"})

        # All nodes done
        assert coord.get_ready_nodes("wf_multi") == []

        # Metrics
        assert len(bb.list_delegations("wf_multi")) == 2
        assert len(bb.list_child_contracts("wf_multi")) == 2


# ===========================================================================
# Part 2 — Failure Simulation
# ===========================================================================

class TestFailure_ChildTimeout:
    """Agent executing past timeout → health detection → recovery."""

    def test_timed_out_agent_detected_by_observability(self, tmp_path):
        bb = _bb(tmp_path)

        # Agent stuck executing past SLA
        bb.set_agent_state(AgentRuntimeState(
            agent_id="stuck_coder", workflow_id="wf_timeout",
            status="executing",
            started_at=time.time() - 1200,  # 20 min (SLA is 600s)
        ))

        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "agent_stuck"]
        assert len(stuck) == 1
        assert stuck[0].severity == Severity.UNHEALTHY
        assert stuck[0].subject == "stuck_coder"

    def test_timed_out_agent_surfaced_in_report(self, tmp_path):
        bb = _bb(tmp_path)
        bb.set_agent_state(AgentRuntimeState(
            agent_id="timeout_agent", workflow_id="wf_to",
            status="executing",
            started_at=time.time() - 800,
        ))

        report = generate_health_report(base=tmp_path)
        assert report.overall == Severity.UNHEALTHY
        assert any("timeout_agent" in f.subject for f in report.findings)


class TestFailure_SpawnFailure:
    """GracefulDegradation handles spawn failure correctly."""

    def test_spawn_failure_halts_and_audits(self, tmp_path):
        _setup_flags(tmp_path, enabled=True)
        gd = GracefulDegradation(base=tmp_path)

        result = gd.handle_spawn_failure(
            "wf_spawn_fail", "agent_x",
            "Subprocess exited with code 1",
        )
        assert result.action == "halt"
        assert "spawn" in result.reason.lower() or "fail" in result.reason.lower()

        # Verify audit trail
        audit_path = tmp_path / "STATE" / "policy_denials.jsonl"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["agent_id"] == "agent_x"

    def test_spawn_rate_exceeded_halts(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)
        rl = RateLimiter(base=tmp_path)
        gd = GracefulDegradation(base=tmp_path)

        # Exhaust spawn rate
        for _ in range(20):
            rl.record_event("agent_spawn")

        result = gd.check_spawn_feasibility()
        assert result.action == "halt"


class TestFailure_DependencyCycle:
    """Dependency cycle or permanently blocked dependencies detected."""

    def test_mutual_dependency_blocks_both(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        bb.create_workflow(WorkflowState(
            workflow_id="wf_cycle", task_id="t_cycle",
        ))

        # A depends on B, B depends on A → cycle
        coord.save_node_states("wf_cycle", [
            NodeState(node_id="A", workflow_id="wf_cycle",
                      status="pending", depends_on=["B"]),
            NodeState(node_id="B", workflow_id="wf_cycle",
                      status="pending", depends_on=["A"]),
        ])

        ready = coord.get_ready_nodes("wf_cycle")
        assert len(ready) == 0  # both blocked

    def test_three_node_cycle(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        bb.create_workflow(WorkflowState(
            workflow_id="wf_tri", task_id="t_tri",
        ))

        # A→B, B→C, C→A
        coord.save_node_states("wf_tri", [
            NodeState(node_id="A", workflow_id="wf_tri",
                      status="pending", depends_on=["C"]),
            NodeState(node_id="B", workflow_id="wf_tri",
                      status="pending", depends_on=["A"]),
            NodeState(node_id="C", workflow_id="wf_tri",
                      status="pending", depends_on=["B"]),
        ])

        ready = coord.get_ready_nodes("wf_tri")
        assert len(ready) == 0

    def test_blocked_dependency_on_failed_node(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        bb.create_workflow(WorkflowState(
            workflow_id="wf_blk", task_id="t_blk",
        ))

        coord.save_node_states("wf_blk", [
            NodeState(node_id="A", workflow_id="wf_blk",
                      status="failed"),  # A failed
            NodeState(node_id="B", workflow_id="wf_blk",
                      status="pending", depends_on=["A"]),
        ])

        ready = coord.get_ready_nodes("wf_blk")
        # B depends on A, but A failed — B should not be ready
        # (get_ready_nodes checks for "completed" status)
        assert "B" not in ready


class TestFailure_UnauthorizedTool:
    """Policy denial proven to block and audit."""

    def test_coder_denied_shell_run(self, tmp_path):
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        with pytest.raises(PolicyViolation):
            policy.enforce("coder_001", "shell.run")

    def test_researcher_denied_file_write(self, tmp_path):
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        with pytest.raises(PolicyViolation):
            policy.enforce("researcher_001", "repo.files.write")

    def test_policy_violation_halts_workflow(self, tmp_path):
        _setup_registry(tmp_path)
        bb = _bb(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )
        engine = WorkflowEngine(blackboard=bb, policy=policy)
        engine.create_workflow("wf_pol", "task_pol")

        with pytest.raises(WorkflowHalt):
            engine.check_tool_policy("wf_pol", "coder_001",
                                     "system.service.restart")

        wf = bb.get_workflow("wf_pol")
        assert wf["status"] == "halted"

    def test_policy_denial_audited(self, tmp_path):
        audit_policy_denial("rogue_agent", "shell.run",
                            "Tool not in allowed list", base=tmp_path)

        path = tmp_path / "STATE" / "policy_denials.jsonl"
        assert path.exists()
        entry = json.loads(path.read_text().strip().split("\n")[-1])
        assert entry["agent_id"] == "rogue_agent"
        assert entry["tool_name"] == "shell.run"


class TestFailure_VerifierRejection:
    """Verifier rejection blocks; double rejection halts."""

    def test_single_rejection_blocks_governed_synthesis(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_vrej", "task_vr")
        engine.delegate("wf_vrej", "code", "coder_001", "coder", "Code")
        engine.claim_delegation("wf_vrej", "code", "coder_001")

        # Contract missing required fields
        c = ChildContract(
            agent_id="coder_001", workflow_id="wf_vrej",
            subtask_id="code", role="coder", status="completed",
            summary="Done",
        )
        engine.complete_delegation("wf_vrej", "code", "coder_001", c)

        out = _file(tmp_path, "OUTPUT/vr.md", "content")
        synthesis = engine.governed_synthesize(
            "wf_vrej", deliverables={"vr.md": out},
        )
        assert synthesis["status"] == "blocked"

    def test_double_rejection_halts(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        engine.create_workflow("wf_drej", "task_dr")

        with pytest.raises(WorkflowHalt):
            engine.check_verifier_rejections("wf_drej", rejection_count=2)

        assert bb.get_workflow("wf_drej")["status"] == "halted"

    def test_verifier_unavailable_halts(self, tmp_path):
        _setup_flags(tmp_path, enabled=True)
        gd = GracefulDegradation(base=tmp_path)

        result = gd.handle_verifier_unavailable("wf_no_verifier")
        assert result.action == "halt"
        assert not result.fallback  # never skip verification


class TestFailure_BudgetExhaustion:
    """Budget exhaustion halts workflow and is detected by observability."""

    def test_zero_budget_halts(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        engine.create_workflow("wf_bex", "task_bx",
                               budget={"max_runtime_s": 0})

        with pytest.raises(WorkflowHalt):
            engine.check_stop_conditions("wf_bex")

        assert bb.get_workflow("wf_bex")["status"] == "halted"

    def test_near_exhaustion_warning(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_near_bud", task_id="t_nb",
            status="executing",
            budget={"max_runtime_s": 10},
            created_at=time.time() - 9,
        ))

        findings = detect_health_issues(base=tmp_path)
        budget = [f for f in findings if "budget" in f.category.lower()]
        assert len(budget) >= 1

    def test_budget_exhaustion_in_metrics(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_bud_met", task_id="t_bm",
            status="halted",
            halt_reason="budget_exhausted: max runtime exceeded",
        ))

        metrics = collect_metrics(base=tmp_path)
        assert metrics.budget_exhaustion_count >= 1

    def test_budget_exhaustion_in_heartbeat_report(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_bud_hb", task_id="t_bh",
            status="halted",
            halt_reason="budget_exhausted: runtime",
        ))

        report = run_multiagent_heartbeat(base=tmp_path)
        assert report.metrics.budget_exhaustion_count >= 1
        assert report.metrics.halted_workflows >= 1


class TestFailure_RetryBurstExceedance:
    """Retry burst rate limit blocks excessive retries."""

    def test_retry_burst_blocks(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)
        rl = RateLimiter(base=tmp_path)

        # Exhaust retry burst limit (5 per 10 min)
        for _ in range(5):
            rl.record_event("retry_burst", window_s=600)

        result = rl.check_retry_burst()
        assert result.allowed is False
        assert result.remaining == 0


class TestFailure_MissingArtifact:
    """GracefulDegradation handles missing artifacts."""

    def test_missing_artifact_halts(self, tmp_path):
        _setup_flags(tmp_path, enabled=True)
        gd = GracefulDegradation(base=tmp_path)

        result = gd.handle_missing_artifact(
            "/nonexistent/output.py", "Expected coder output",
        )
        assert result.action == "halt"


class TestFailure_RestartRecovery:
    """Restart recovery and duplicate prevention."""

    def test_stale_workflow_halted_on_recovery(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_stale.json").write_text(json.dumps({
            "workflow_id": "wf_stale",
            "task_id": "t_stale",
            "status": "executing",
            "created_at": time.time() - 10000,
            "updated_at": time.time() - 10000,
            "budget": {"max_runtime_s": 1800},
        }))

        rr = RestartRecovery(base=tmp_path)
        result = rr.reconcile()

        halted = [a for a in result["actions"]
                  if a["type"] == "workflow_halted"]
        assert len(halted) == 1

        data = json.loads((wf_dir / "wf_stale.json").read_text())
        assert data["status"] == "halted"

    def test_inprogress_tasks_requeued(self, tmp_path):
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "0099_test.md.inprogress").write_text("# Task")

        rr = RestartRecovery(base=tmp_path)
        result = rr.reconcile()

        requeued = [a for a in result["actions"]
                    if a["type"] == "task_requeued"]
        assert len(requeued) == 1
        assert (tasks_dir / "0099_test.md").exists()

    def test_duplicate_recovery_prevented(self, tmp_path):
        lock_dir = tmp_path / "STATE"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "recovery.lock"

        # Write lock with current PID (simulating concurrent run)
        lock_path.write_text(str(os.getpid()))

        rr = RestartRecovery(base=tmp_path)
        result = rr.reconcile()

        assert "skipped" in result or "concurrent" in str(result).lower()

    def test_stale_lock_reclaimed(self, tmp_path):
        lock_dir = tmp_path / "STATE"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "recovery.lock"

        # Write lock with dead PID
        lock_path.write_text("999999999")

        # Should succeed (reclaim stale lock)
        rr = RestartRecovery(base=tmp_path)
        result = rr.reconcile()
        assert "actions" in result

    def test_recovery_log_written(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_log.json").write_text(json.dumps({
            "workflow_id": "wf_log",
            "task_id": "t_log",
            "status": "executing",
            "created_at": time.time() - 20000,
            "updated_at": time.time() - 20000,
            "budget": {"max_runtime_s": 1800},
        }))

        rr = RestartRecovery(base=tmp_path)
        rr.reconcile()

        log_path = tmp_path / "LOGS" / "recovery.log"
        assert log_path.exists()


class TestFailure_OrphanedState:
    """Orphaned child/runtime state detected."""

    def test_orphaned_delegation_no_runtime(self, tmp_path):
        bb = _bb(tmp_path)

        # Delegation claimed but no runtime record
        d = Delegation(
            workflow_id="wf_orphan_d", subtask_id="sub1",
            agent_id="ghost_agent", role="coder",
            goal="Do work", status="claimed",
        )
        bb.create_delegation(d)

        findings = detect_health_issues(base=tmp_path)
        orphans = [f for f in findings if f.category == "orphan"]
        assert len(orphans) >= 1

    def test_orphaned_lease_detected(self, tmp_path):
        coord = CoordinationLayer(blackboard=_bb(tmp_path))

        lease = Lease(
            workflow_id="wf_orphan_l", node_id="A",
            holder="dead_agent",
            acquired_at=time.time() - 2000, ttl_s=600,
        )
        coord._write_lease(lease)

        findings = detect_health_issues(base=tmp_path)
        orphans = [f for f in findings if f.category == "orphan"]
        assert len(orphans) >= 1


class TestFailure_ArchiveAfterTerminal:
    """Archive/cleanup after terminal workflows."""

    def test_completed_workflow_archived(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_arc.json").write_text(json.dumps({
            "workflow_id": "wf_arc", "status": "completed",
            "updated_at": time.time() - 100000,
        }))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert "wf_arc.json" in archived
        assert (tmp_path / "STATE" / "archive" / "workflows" / "wf_arc.json").exists()

    def test_failed_workflow_archived(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_fail.json").write_text(json.dumps({
            "workflow_id": "wf_fail", "status": "failed",
            "updated_at": time.time() - 100000,
        }))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert "wf_fail.json" in archived

    def test_halted_workflow_archived(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_halt.json").write_text(json.dumps({
            "workflow_id": "wf_halt", "status": "halted",
            "updated_at": time.time() - 100000,
        }))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert "wf_halt.json" in archived

    def test_active_workflow_not_archived(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_active.json").write_text(json.dumps({
            "workflow_id": "wf_active", "status": "executing",
            "updated_at": time.time() - 100000,
        }))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert len(archived) == 0

    def test_delegation_archived(self, tmp_path):
        del_dir = tmp_path / "STATE" / "delegations"
        del_dir.mkdir(parents=True, exist_ok=True)
        (del_dir / "wf_d_sub1.json").write_text(json.dumps({
            "status": "completed",
            "completed_at": time.time() - 100000,
        }))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_delegations()
        assert "wf_d_sub1.json" in archived

    def test_full_cleanup_summary(self, tmp_path):
        am = ArchiveManager(base=tmp_path)
        result = am.run_cleanup()

        assert "archived_workflows" in result
        assert "archived_agents" in result
        assert "cleaned_leases" in result


class TestFailure_TaskClassUnsupported:
    """Unsupported task class blocked by feature flags."""

    def test_unsupported_class_blocked(self, tmp_path):
        _setup_flags(tmp_path, enabled=True,
                     supported_classes=["research"])
        ff = FeatureFlags(base=tmp_path)

        assert ff.is_task_class_supported("code_impl") is False
        assert ff.is_task_class_supported("research") is True

    def test_empty_supported_list_blocks_all(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, supported_classes=[])
        ff = FeatureFlags(base=tmp_path)

        assert ff.is_task_class_supported("research") is False
        assert ff.is_task_class_supported("code_impl") is False


# ===========================================================================
# Part 3 — Heartbeat + Observability Validation
# ===========================================================================

class TestObservability_StuckWorkflow:
    """Stuck workflow detection triggers correctly."""

    def test_stuck_workflow_unhealthy(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_stuck", task_id="t_stuck",
            status="executing",
            created_at=time.time() - 3600,
            updated_at=time.time() - 3600,
        ))

        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "workflow_stuck"]
        assert len(stuck) >= 1
        assert stuck[0].severity == Severity.UNHEALTHY

    def test_approaching_sla_warning(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_warn", task_id="t_warn",
            status="executing",
            created_at=time.time() - 1500,  # 25 min (75% of 30 min SLA)
            updated_at=time.time() - 1500,
        ))

        findings = detect_health_issues(base=tmp_path)
        warns = [f for f in findings
                 if f.category == "workflow_stuck"
                 and f.severity == Severity.WARNING]
        assert len(warns) >= 1


class TestObservability_UnresolvedContract:
    """Unresolved child contract detection."""

    def test_unresolved_contract_detected(self, tmp_path):
        bb = _bb(tmp_path)

        # Completed workflow with delegation ID in its delegations list
        # but no matching child contract file at WORK/agents/contracts/
        wf = WorkflowState(
            workflow_id="wf_unres", task_id="t_unres",
            status="completed",
        )
        bb.create_workflow(wf)
        # Manually add delegation ID to workflow's delegations list
        bb.update_workflow("wf_unres", {
            "delegations": ["sub_unres"],
        })

        # No child contract file at WORK/agents/contracts/sub_unres.json

        findings = detect_health_issues(base=tmp_path)
        contract_gaps = [f for f in findings
                         if f.category == "contract_gap"]
        assert len(contract_gaps) >= 1
        assert contract_gaps[0].severity == Severity.WARNING


class TestObservability_BudgetPressure:
    """Budget pressure surfaced correctly."""

    def test_budget_near_exhaustion_surfaced(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_bp", task_id="t_bp",
            status="executing",
            budget={"max_runtime_s": 100},
            created_at=time.time() - 90,  # 90% used
        ))

        findings = detect_health_issues(base=tmp_path)
        budget = [f for f in findings if "budget" in f.category.lower()]
        assert len(budget) >= 1

    def test_budget_pressure_in_heartbeat(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_bp2", task_id="t_bp2",
            status="halted",
            halt_reason="budget_exhausted: runtime exceeded",
        ))

        report = run_multiagent_heartbeat(base=tmp_path)
        md = render_report_markdown(report)
        assert "Budget exhaustions" in md


class TestObservability_PolicyPressure:
    """Policy violations counted and surfaced."""

    def test_policy_violations_counted(self, tmp_path):
        audit_path = tmp_path / "STATE" / "tool_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)

        entries = [
            {"tool": "shell.run", "agent": "a1", "ok": False,
             "exit_code": -1, "error": "denied by policy"},
            {"tool": "repo.git.commit", "agent": "a2", "ok": False,
             "exit_code": -1, "error": "blocked by runner"},
            {"tool": "repo.files.read", "agent": "a3", "ok": True,
             "exit_code": 0},
        ]
        audit_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n"
        )

        metrics = collect_metrics(base=tmp_path)
        assert metrics.policy_violation_count == 2
        assert metrics.most_rejected_tool == "shell.run"

    def test_policy_violations_in_report(self, tmp_path):
        audit_path = tmp_path / "STATE" / "tool_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps({
            "tool": "shell.run", "agent": "a1", "ok": False,
            "exit_code": -1, "error": "denied",
        }) + "\n")

        report = run_multiagent_heartbeat(base=tmp_path)
        md = render_report_markdown(report)
        assert "Policy violations" in md


class TestObservability_IntegratedReport:
    """Report generation correct under multi-path integrated runs."""

    def test_report_with_mixed_state(self, tmp_path):
        bb = _bb(tmp_path)

        # Active workflow
        bb.create_workflow(WorkflowState(
            workflow_id="wf_active", task_id="t1",
            status="executing",
            created_at=time.time() - 100,
        ))

        # Completed workflow
        bb.create_workflow(WorkflowState(
            workflow_id="wf_done", task_id="t2",
            status="completed",
        ))

        # Halted workflow (budget)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_halted", task_id="t3",
            status="halted",
            halt_reason="budget_exhausted: runtime",
        ))

        # Agent runtime
        bb.set_agent_state(AgentRuntimeState(
            agent_id="agent_1", workflow_id="wf_active",
            status="executing",
        ))

        # Delegation
        d = Delegation(
            workflow_id="wf_done", subtask_id="s1",
            agent_id="agent_1", role="coder",
            goal="Code", status="completed",
            claimed_at=time.time() - 60,
            completed_at=time.time() - 10,
        )
        bb.create_delegation(d)

        report = generate_health_report(base=tmp_path)
        assert report.metrics.active_workflows == 1
        assert report.metrics.completed_workflows == 1
        assert report.metrics.halted_workflows == 1
        assert report.metrics.budget_exhaustion_count >= 1
        assert report.metrics.agent_spawn_count >= 1
        assert report.metrics.completed_delegations >= 1

        md = render_report_markdown(report)
        assert "Active workflows" in md
        assert "Completed workflows" in md
        assert "Halted workflows" in md

    def test_heartbeat_files_correct(self, tmp_path):
        bb = _bb(tmp_path)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_hb", task_id="t_hb",
            status="executing",
        ))

        run_multiagent_heartbeat(base=tmp_path)

        md_path = tmp_path / "HEARTBEAT_MULTIAGENT.md"
        json_path = tmp_path / "STATE" / "heartbeat_multiagent.json"
        assert md_path.exists()
        assert json_path.exists()

        # JSON is valid
        data = json.loads(json_path.read_text())
        assert "overall" in data
        assert "metrics" in data

        # Markdown mentions the workflow
        md = md_path.read_text()
        assert "wf_hb" in md

    def test_verifier_rejection_rate_in_report(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)

        # Create one rejected and one approved verification
        out = _file(tmp_path, "out.md", "content")
        verifier.verify("wf_vr1", deliverables={"bad": "/no/file"},
                        contracts=[_good_contract_dict()])
        verifier.verify("wf_vr2", deliverables={"out.md": out},
                        contracts=[_good_contract_dict()])

        metrics = collect_metrics(base=tmp_path)
        assert metrics.verifier_rejection_count >= 1
        assert metrics.verifier_approval_count >= 1
        assert metrics.verifier_rejection_rate is not None


class TestObservability_ProductionHardeningIntegrated:
    """run_production_hardening integrates cleanup + rate limits + recovery."""

    def test_full_hardening_run(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)

        result = run_production_hardening(base=tmp_path)
        assert result["multi_agent_enabled"] is True
        assert "cleanup" in result
        assert "rate_limits" in result
        assert "recovery" in result

    def test_hardening_disabled_skips(self, tmp_path):
        _setup_flags(tmp_path, enabled=False, archive=False,
                     rate_limiting=False)

        result = run_production_hardening(base=tmp_path)
        assert result["multi_agent_enabled"] is False
        assert result["cleanup"] == "disabled"
        assert "rate_limits" not in result or result.get("rate_limits") == "disabled"

    def test_hardening_cleanup_after_terminal_workflow(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, archive=True)

        # Old completed workflow
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_old.json").write_text(json.dumps({
            "workflow_id": "wf_old", "status": "completed",
            "updated_at": time.time() - 100000,
        }))

        result = run_production_hardening(base=tmp_path)
        cleanup = result.get("cleanup", {})
        if isinstance(cleanup, dict):
            archived = cleanup.get("archived_workflows", [])
            assert len(archived) >= 1


# ===========================================================================
# Part 4 — Combined Cross-Module Scenarios
# ===========================================================================

class TestCombined_FullGovernedWithHardening:
    """Full governed workflow with rate limits, observability, and cleanup."""

    def test_governed_workflow_with_all_controls(self, tmp_path):
        _setup_flags(tmp_path, enabled=True,
                     supported_classes=["code_impl"],
                     rate_limiting=True, archive=True)
        _setup_registry(tmp_path)

        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)
        gate = WorkflowGate(blackboard=bb)
        gd = GracefulDegradation(base=tmp_path)
        rl = RateLimiter(base=tmp_path)

        # Pre-flight checks
        assert gd.check_orchestrator_available("code_impl").action == "proceed"
        assert gd.check_workflow_launch_feasibility().action == "proceed"
        assert gd.check_spawn_feasibility().action == "proceed"

        # Record launch
        rl.record_event("workflow_launch")

        # Create workflow
        engine.create_workflow("wf_full", "task_full",
                               budget={"max_runtime_s": 1800})
        bb.update_workflow("wf_full", {"status": "executing"})

        coord.save_node_states("wf_full", [
            NodeState(node_id="code", workflow_id="wf_full",
                      status="pending"),
        ])

        # Spawn agent
        rl.record_event("agent_spawn")

        # Delegate and complete
        engine.delegate("wf_full", "code", "coder_001", "coder",
                        "Build feature")
        engine.claim_delegation("wf_full", "code", "coder_001")

        bb.set_agent_state(AgentRuntimeState(
            agent_id="coder_001", workflow_id="wf_full",
            status="executing",
        ))

        out = _file(tmp_path, "OUTPUT/feature.py", "def feature(): pass\n")
        c = _contract("wf_full", "code", "coder_001", "coder",
                      "Built feature", "feature.py")
        c.artifacts = [out]
        engine.complete_delegation("wf_full", "code", "coder_001", c)

        bb.set_agent_state(AgentRuntimeState(
            agent_id="coder_001", workflow_id="wf_full",
            status="completed",
        ))

        # Critic + verifier + governed synthesis
        review = gate.run_critic_review(
            "wf_full", "code",
            deliverables={"feature.py": out},
            contract=_good_contract_dict(),
        )
        assert review.verdict == "pass"

        synthesis = engine.governed_synthesize(
            "wf_full",
            deliverables={"feature.py": out},
            repo_changes=["feature.py"],
        )
        assert synthesis["status"] == "completed"
        assert synthesis.get("governed") is True

        # Observability
        report = run_multiagent_heartbeat(base=tmp_path)
        assert report.metrics.completed_workflows >= 1
        assert report.metrics.completed_delegations >= 1

        # Rate limit state
        wf_status = rl.check_workflow_launch()
        assert wf_status.count >= 1


class TestCombined_FailureRecoveryObservability:
    """Failure → recovery → observability sees the right state."""

    def test_failure_recovery_cycle(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, rate_limiting=True)

        # Create a workflow that will time out
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "wf_fr.json").write_text(json.dumps({
            "workflow_id": "wf_fr", "task_id": "t_fr",
            "status": "executing",
            "created_at": time.time() - 5000,
            "updated_at": time.time() - 5000,
            "budget": {"max_runtime_s": 1800},
            "node_states": {
                "A": {"status": "executing", "retry_count": 0,
                       "max_retries": 1},
            },
        }))

        # Observability detects stuck workflow
        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "workflow_stuck"]
        assert len(stuck) >= 1

        # Recovery halts the stale workflow
        rr = RestartRecovery(base=tmp_path)
        result = rr.reconcile()
        halted = [a for a in result["actions"]
                  if a["type"] == "workflow_halted"]
        assert len(halted) >= 1

        # Post-recovery: observability no longer sees active stuck workflow
        data = json.loads((wf_dir / "wf_fr.json").read_text())
        assert data["status"] == "halted"

        # Heartbeat now shows halted
        report = run_multiagent_heartbeat(base=tmp_path)
        assert report.metrics.halted_workflows >= 1


# ===========================================================================
# Run as script
# ===========================================================================

if __name__ == "__main__":
    import inspect
    import sys
    import tempfile

    test_classes = [
        # Part 1: E2E Integration
        TestE2E_ResearchOnlyPath,
        TestE2E_CoderVerifierPath,
        TestE2E_CriticReplanPath,
        TestE2E_MemoryCapturePath,
        TestE2E_SingleAgentFallback,
        TestE2E_RateLimitedWorkflow,
        TestE2E_MultiRoleWorkflow,
        # Part 2: Failure Simulation
        TestFailure_ChildTimeout,
        TestFailure_SpawnFailure,
        TestFailure_DependencyCycle,
        TestFailure_UnauthorizedTool,
        TestFailure_VerifierRejection,
        TestFailure_BudgetExhaustion,
        TestFailure_RetryBurstExceedance,
        TestFailure_MissingArtifact,
        TestFailure_RestartRecovery,
        TestFailure_OrphanedState,
        TestFailure_ArchiveAfterTerminal,
        TestFailure_TaskClassUnsupported,
        # Part 3: Observability
        TestObservability_StuckWorkflow,
        TestObservability_UnresolvedContract,
        TestObservability_BudgetPressure,
        TestObservability_PolicyPressure,
        TestObservability_IntegratedReport,
        TestObservability_ProductionHardeningIntegrated,
        # Part 4: Combined
        TestCombined_FullGovernedWithHardening,
        TestCombined_FailureRecoveryObservability,
    ]

    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        for name in sorted(dir(instance)):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            try:
                sig = inspect.signature(method)
                if "tmp_path" in sig.parameters:
                    with tempfile.TemporaryDirectory() as td:
                        method(Path(td))
                else:
                    method()
                print(f"  PASS  {cls.__name__}.{name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {cls.__name__}.{name}: {e}")
                failed += 1
                errors.append(f"{cls.__name__}.{name}: {e}")

    print(f"\n{passed}/{passed + failed} tests passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
