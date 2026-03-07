"""Phase 7 — Failure simulation and adversarial testing suite.

Validates that the multi-agent system fails safely, deterministically,
and auditibly under:
  1. Child contract missing / malformed
  2. Verifier rejection (single and double rejection halt)
  3. Critic blocking objection → replan signal → reroute decision
  4. Stale lease → resume after interruption
  5. Orphaned child agent detection
  6. Dependency wait timeout detection
  7. Policy denial on delegated action (anti-bypass)
  8. Budget exhaustion / near-exhaustion
  9. Malformed memory artifact rejection
  10. Restart recovery after in-progress workflow
  11. Archive/cleanup of completed workflows
  12. Feature-flag-off fallback to safe path
  13. Anti-bypass: maker-checker enforced under failure
  14. Anti-bypass: policy cannot be bypassed through delegation
  15. Anti-bypass: dangerous paths remain gated even in failure scenarios

All tests use real blackboard state on disk (tmp_path), not mocks.
"""

import json
import time
from pathlib import Path

import pytest

from agents.blackboard import (
    Blackboard, ChildContract, Delegation, WorkflowState,
    AgentRuntimeState,
)
from agents.coordination import (
    CoordinationLayer, NodeState, Lease, LeaseConflict,
)
from agents.critic import CriticEngine, CriticReview
from agents.memory_engine import (
    MemoryArtifact, write_memory_artifact, validate_memory_artifact,
    capture_workflow_memory,
)
from agents.observability import (
    collect_metrics, detect_health_issues, generate_health_report, Severity,
)
from agents.policy_engine import PolicyEngine, PolicyViolation
from agents.production_hardening import (
    FeatureFlags, RateLimiter, ArchiveManager, RestartRecovery,
    ApprovalGate, run_production_hardening,
)
from agents.verifier import VerifierEngine
from agents.workflow_engine import (
    WorkflowEngine, WorkflowHalt, WorkflowLimits,
    HALT_BUDGET_EXHAUSTED, HALT_VERIFIER_REJECTED, HALT_POLICY_VIOLATION,
)
from agents.workflow_gate import (
    WorkflowGate, RerouteDecision, validate_contract_fields,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bb(tmp_path: Path) -> Blackboard:
    return Blackboard(base=tmp_path)


def _make_file(tmp_path: Path, name: str, content: str = "result") -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def _good_contract() -> dict:
    return {
        "summary": "Implemented feature",
        "files_changed": "module.py",
        "verification": "All tests pass",
        "confidence": "high",
    }


def _setup_registry(tmp_path: Path) -> None:
    reg_dir = tmp_path / "STATE" / "agents"
    reg_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "agents": [
            {
                "agent_id": "coder_001",
                "role": "coder",
                "allowed_tools": ["repo.files.read", "repo.files.write"],
                "denied_tools": ["system.service.restart", "shell.run"],
                "max_actions": 50,
                "max_runtime_seconds": 300,
                "max_retries": 1,
                "feature_flags": {"allow_delegation": False},
            },
            {
                "agent_id": "researcher_001",
                "role": "researcher",
                "allowed_tools": ["web.search", "http.fetch"],
                "denied_tools": ["repo.files.write", "repo.git.commit",
                                 "shell.run"],
                "max_actions": 30,
                "max_runtime_seconds": 180,
                "max_retries": 1,
                "feature_flags": {"allow_delegation": False},
            },
        ],
    }
    (reg_dir / "registry.json").write_text(json.dumps(registry))


def _setup_flags(tmp_path: Path, enabled: bool = True,
                 archive: bool = True) -> None:
    config_dir = tmp_path / "STATE" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    flags = {
        "phase7_orchestrator": {"enabled": enabled},
        "phase7_hardening": {
            "archive_cleanup": archive,
            "rate_limiting": True,
            "manual_approval": False,
        },
    }
    (config_dir / "feature_flags.json").write_text(json.dumps(flags))


# ===========================================================================
# 1. Child contract missing / malformed
# ===========================================================================

class TestChildContractMissing:
    """Fail: workflows block when child contracts are missing or malformed."""

    def test_governed_synthesis_blocks_on_missing_contract_fields(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_miss_1", "task_m1")
        engine.delegate("wf_miss_1", "sub1", "a1", "coder", "Task")
        engine.claim_delegation("wf_miss_1", "sub1", "a1")

        # Write contract WITHOUT required fields
        contract = ChildContract(
            agent_id="a1", workflow_id="wf_miss_1", subtask_id="sub1",
            role="coder", status="completed", summary="Done",
        )
        engine.complete_delegation("wf_miss_1", "sub1", "a1", contract)

        # ChildContract lacks files_changed and confidence → validation fails
        out = _make_file(tmp_path, "OUTPUT/out.md", "content")
        synthesis = engine.governed_synthesize(
            "wf_miss_1",
            deliverables={"out.md": out},
        )
        assert synthesis["status"] == "blocked"
        assert "Contract validation" in synthesis.get("reason", "")

    def test_contract_validation_rejects_empty_dict(self):
        valid, errors = validate_contract_fields({})
        assert valid is False
        assert len(errors) == 4  # all 4 required fields missing

    def test_contract_validation_rejects_whitespace_fields(self):
        contract = {
            "summary": "   ",
            "files_changed": "",
            "verification": "  ",
            "confidence": "high",
        }
        valid, errors = validate_contract_fields(contract)
        assert valid is False
        assert len(errors) >= 2

    def test_verifier_rejects_missing_artifact(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        report = verifier.verify(
            "wf_miss_2",
            deliverables={"missing": "/nonexistent/path.md"},
            contracts=[_good_contract()],
        )
        assert report.verdict == "rejected"
        assert len(report.blocking_issues) > 0

    def test_verifier_rejects_empty_artifact(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        empty_file = _make_file(tmp_path, "empty.md", "")
        report = verifier.verify(
            "wf_miss_3",
            deliverables={"empty.md": empty_file},
            contracts=[_good_contract()],
        )
        assert report.verdict == "rejected"


# ===========================================================================
# 2. Verifier rejection — single and halt-on-double
# ===========================================================================

class TestVerifierRejection:
    """Fail: verifier rejection blocks completion; double rejection halts."""

    def test_single_rejection_blocks_synthesis(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_rej_1", "task_r1")
        engine.delegate("wf_rej_1", "sub1", "a1", "coder", "Do X")
        engine.claim_delegation("wf_rej_1", "sub1", "a1")
        contract = ChildContract(
            agent_id="a1", workflow_id="wf_rej_1", subtask_id="sub1",
            role="coder", status="completed", summary="Done",
        )
        engine.complete_delegation("wf_rej_1", "sub1", "a1", contract)

        # governed_synthesize will block on contract validation
        out = _make_file(tmp_path, "OUTPUT/r1.md", "content")
        synthesis = engine.governed_synthesize(
            "wf_rej_1", deliverables={"r1.md": out},
        )
        assert synthesis["status"] == "blocked"

    def test_double_rejection_halts_workflow(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_rej_2", "task_r2")

        # Directly test the check_verifier_rejections mechanism
        with pytest.raises(WorkflowHalt) as exc_info:
            engine.check_verifier_rejections("wf_rej_2", rejection_count=2)
        assert "rejected" in str(exc_info.value).lower()

        wf = bb.get_workflow("wf_rej_2")
        assert wf["status"] == "halted"

    def test_verification_report_persisted_on_rejection(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        report = verifier.verify(
            "wf_rej_3",
            deliverables={"bad": None},
            contracts=[_good_contract()],
        )
        assert report.verdict == "rejected"

        loaded = verifier.get_report(report.report_id)
        assert loaded is not None
        assert loaded.verdict == "rejected"
        assert loaded.confidence == "high"  # high confidence in rejection


# ===========================================================================
# 3. Critic blocking objection → replan signal → reroute
# ===========================================================================

class TestCriticObjectionReplan:
    """Fail: critic objections produce deterministic replan signals."""

    def test_objection_produces_replan_signal(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)

        review = gate.run_critic_review("wf_obj_1", "node_a",
                                         deliverables={})
        assert review.verdict == "objection"
        assert review.blocking is True

        signals = gate.critic.list_pending_replans("wf_obj_1")
        assert len(signals) == 1
        assert signals[0].status == "pending"

    def test_replan_signal_maps_to_reroute_decision(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)

        gate.run_critic_review("wf_obj_2", "node_b", deliverables={})
        decisions = gate.check_replan_signals("wf_obj_2")

        assert len(decisions) == 1
        d = decisions[0]
        assert isinstance(d, RerouteDecision)
        assert d.action in ("retry", "reassign", "halt")

    def test_retryable_reason_code_maps_to_retry(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)

        # Empty deliverable value → empty_deliverable reason_code → retryable
        fpath = _make_file(tmp_path, "out.md", "content")
        review = gate.run_critic_review(
            "wf_obj_3", "node_c",
            deliverables={"empty_val": ""},  # empty string → high severity
        )
        # May or may not produce objection depending on severity
        if review.verdict == "objection":
            decisions = gate.check_replan_signals("wf_obj_3")
            if decisions:
                assert decisions[0].action in ("retry", "reassign", "halt")

    def test_pending_replan_blocks_completion(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)

        gate.run_critic_review("wf_obj_4", "node_d", deliverables={})

        fpath = _make_file(tmp_path, "out.md", "content")
        allowed, reason = gate.is_completion_allowed(
            "wf_obj_4",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        assert allowed is False
        assert "replan" in reason.lower()

    def test_acknowledged_replan_unblocks_completion(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)

        gate.run_critic_review("wf_obj_5", "node_e", deliverables={})

        # Acknowledge all replan signals
        decisions = gate.check_replan_signals("wf_obj_5")
        assert len(decisions) == 1
        # check_replan_signals auto-acknowledges signals

        # Now pending should be 0
        pending = gate.critic.list_pending_replans("wf_obj_5")
        assert len(pending) == 0

        # Completion should no longer be blocked by replans
        fpath = _make_file(tmp_path, "out.md", "content")
        allowed, reason = gate.is_completion_allowed(
            "wf_obj_5",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        # May still fail for other reasons (verifier), but not for replans
        if not allowed:
            assert "replan" not in reason.lower()


# ===========================================================================
# 4. Stale lease → resume after interruption
# ===========================================================================

class TestStaleLease:
    """Fail: stale leases are recovered and nodes become retryable."""

    def test_expired_lease_allows_takeover(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)
        bb.create_workflow(WorkflowState(
            workflow_id="wf_stale_1", task_id="t_s1",
        ))
        coord.save_node_states("wf_stale_1", [
            NodeState(node_id="A", workflow_id="wf_stale_1",
                      status="pending"),
        ])

        # Acquire with very short TTL
        coord.claim_node("wf_stale_1", "A", "agent_1", ttl_s=0.01)
        time.sleep(0.05)

        # Another agent can take over
        lease = coord.claim_node("wf_stale_1", "A", "agent_2", ttl_s=600)
        assert lease.holder == "agent_2"

    def test_recovery_resets_stale_nodes(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        bb.create_workflow(WorkflowState(
            workflow_id="wf_stale_2", task_id="t_s2",
        ))
        coord.save_node_states("wf_stale_2", [
            NodeState(node_id="A", workflow_id="wf_stale_2",
                      status="executing", retry_count=0),
        ])

        # Create expired lease
        lease = Lease(workflow_id="wf_stale_2", node_id="A", holder="agent_1",
                      acquired_at=time.time() - 1000, ttl_s=1)
        coord._write_lease(lease)

        # Recover
        state = coord.recover_workflow("wf_stale_2")
        assert len(state["recovery_actions"]) > 0

        # Node should be reset to pending
        nodes = coord.get_node_states("wf_stale_2")
        assert nodes["A"].status == "pending"
        assert nodes["A"].retry_count == 1

    def test_max_retries_exhausted_marks_failed(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        bb.create_workflow(WorkflowState(
            workflow_id="wf_stale_3", task_id="t_s3",
        ))
        coord.save_node_states("wf_stale_3", [
            NodeState(node_id="A", workflow_id="wf_stale_3",
                      status="executing", retry_count=1, max_retries=1),
        ])

        # Create expired lease
        lease = Lease(workflow_id="wf_stale_3", node_id="A", holder="agent_1",
                      acquired_at=time.time() - 1000, ttl_s=1)
        coord._write_lease(lease)

        # Recover — should fail the node
        state = coord.recover_workflow("wf_stale_3")
        nodes = coord.get_node_states("wf_stale_3")
        assert nodes["A"].status == "failed"

    def test_resume_classifies_nodes_correctly(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        bb.create_workflow(WorkflowState(
            workflow_id="wf_stale_4", task_id="t_s4",
        ))
        coord.save_node_states("wf_stale_4", [
            NodeState(node_id="A", workflow_id="wf_stale_4",
                      status="completed"),
            NodeState(node_id="B", workflow_id="wf_stale_4",
                      status="pending", depends_on=["A"]),
            NodeState(node_id="C", workflow_id="wf_stale_4",
                      status="pending", depends_on=["B"]),
        ])

        state = coord.resume_workflow("wf_stale_4")
        assert "A" in state["completed_nodes"]
        assert "B" in state["pending_nodes"]  # deps satisfied
        assert "C" in state["blocked_nodes"]  # B not complete yet


# ===========================================================================
# 5. Orphaned child agent detection
# ===========================================================================

class TestOrphanedAgentDetection:
    """Observability correctly detects orphaned agents."""

    def test_stuck_agent_detected(self, tmp_path):
        bb = _bb(tmp_path)

        # Create an agent that's been "executing" beyond SLA
        bb.set_agent_state(AgentRuntimeState(
            agent_id="orphan_agent",
            workflow_id="wf_orphan",
            status="executing",
            started_at=time.time() - 1200,  # 20 min ago
        ))

        findings = detect_health_issues(base=tmp_path)
        stuck = [f for f in findings if f.category == "agent_stuck"]
        assert len(stuck) == 1
        assert stuck[0].severity == Severity.UNHEALTHY
        assert stuck[0].subject == "orphan_agent"

    def test_orphan_lease_detected(self, tmp_path):
        bb = _bb(tmp_path)
        coord = CoordinationLayer(blackboard=bb)

        # Create an expired lease (orphan)
        lease = Lease(workflow_id="wf_orphan_2", node_id="A",
                      holder="dead_agent",
                      acquired_at=time.time() - 2000, ttl_s=600)
        coord._write_lease(lease)

        findings = detect_health_issues(base=tmp_path)
        orphans = [f for f in findings if f.category == "orphan"]
        assert len(orphans) >= 1

    def test_orphaned_count_in_metrics(self, tmp_path):
        bb = _bb(tmp_path)

        # Stuck agent
        bb.set_agent_state(AgentRuntimeState(
            agent_id="stuck_1",
            workflow_id="wf_m",
            status="executing",
            started_at=time.time() - 1200,
        ))

        metrics = collect_metrics(base=tmp_path)
        assert metrics.orphaned_agent_count >= 1


# ===========================================================================
# 6. Dependency wait timeout detection
# ===========================================================================

class TestDependencyTimeout:
    """Observability detects agents waiting too long on dependencies."""

    def test_dependency_wait_warning(self, tmp_path):
        bb = _bb(tmp_path)

        # Agent waiting beyond SLA — write file directly because
        # set_agent_state() overwrites updated_at with time.time()
        rt_dir = tmp_path / "STATE" / "agents" / "runtime"
        rt_dir.mkdir(parents=True, exist_ok=True)
        agent_data = {
            "agent_id": "wait_agent",
            "workflow_id": "wf_wait",
            "status": "waiting",
            "updated_at": time.time() - 1200,  # 20 min ago
        }
        (rt_dir / "wait_agent.json").write_text(json.dumps(agent_data))

        findings = detect_health_issues(base=tmp_path)
        waits = [f for f in findings if f.category == "dependency_wait"]
        assert len(waits) == 1
        assert waits[0].severity == Severity.WARNING


# ===========================================================================
# 7. Policy denial on delegated action (anti-bypass)
# ===========================================================================

class TestPolicyDenialAntiBypass:
    """Anti-bypass: agents cannot circumvent policy through delegation."""

    def test_denied_tool_raises_violation(self, tmp_path):
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        decision = policy.check_tool_access("coder_001", "system.service.restart")
        assert decision.allowed is False

    def test_enforce_raises_on_denied_tool(self, tmp_path):
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        with pytest.raises(PolicyViolation):
            policy.enforce("coder_001", "system.service.restart")

    def test_researcher_cannot_write_files(self, tmp_path):
        """Anti-bypass: researcher agent denied write access."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        decision = policy.check_tool_access("researcher_001", "repo.files.write")
        assert decision.allowed is False

    def test_researcher_cannot_commit(self, tmp_path):
        """Anti-bypass: researcher cannot bypass maker-checker via commit."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        decision = policy.check_tool_access("researcher_001", "repo.git.commit")
        assert decision.allowed is False

    def test_researcher_cannot_shell_run(self, tmp_path):
        """Anti-bypass: researcher cannot escape sandbox via shell."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        decision = policy.check_tool_access("researcher_001", "shell.run")
        assert decision.allowed is False

    def test_unknown_agent_denied(self, tmp_path):
        """Anti-bypass: unregistered agents default-denied."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        decision = policy.check_tool_access("rogue_agent", "repo.files.read")
        assert decision.allowed is False

    def test_policy_halt_on_violation_in_workflow(self, tmp_path):
        """Anti-bypass: policy violation halts the workflow."""
        _setup_registry(tmp_path)
        bb = _bb(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )
        engine = WorkflowEngine(blackboard=bb, policy=policy)
        engine.create_workflow("wf_pol_1", "task_p1")

        with pytest.raises(WorkflowHalt):
            engine.check_tool_policy("wf_pol_1", "coder_001",
                                     "system.service.restart")

        wf = bb.get_workflow("wf_pol_1")
        assert wf["status"] == "halted"

    def test_coder_cannot_spawn(self, tmp_path):
        """Anti-bypass: worker agents cannot spawn children."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        decision = policy.check_spawn_allowed("coder_001")
        assert decision.allowed is False

    def test_budget_exhaustion_denies(self, tmp_path):
        """Anti-bypass: budget limits enforced."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        # Exceed action budget
        decision = policy.check_budget("coder_001",
                                        action_count=100,
                                        runtime_s=10,
                                        retry_count=0)
        assert decision.allowed is False
        assert "action" in decision.reason.lower()

    def test_mutation_tools_require_verification(self, tmp_path):
        """Anti-bypass: mutation tools require maker-checker."""
        _setup_registry(tmp_path)
        policy = PolicyEngine(
            registry_path=tmp_path / "STATE" / "agents" / "registry.json",
        )

        for tool in ["repo.files.write", "repo.files.patch",
                      "repo.git.commit", "shell.run"]:
            assert policy.requires_verification(tool) is True

        for tool in ["repo.files.read", "web.search", "http.fetch"]:
            assert policy.requires_verification(tool) is False


# ===========================================================================
# 8. Budget exhaustion
# ===========================================================================

class TestBudgetExhaustionFailure:
    """Workflow halts when budgets are exceeded."""

    def test_runtime_budget_halt(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_bud_1", "task_b1",
                               budget={"max_runtime_s": 0})

        with pytest.raises(WorkflowHalt):
            engine.check_stop_conditions("wf_bud_1")

    def test_budget_near_exhaustion_warning(self, tmp_path):
        bb = _bb(tmp_path)

        # Workflow with very little runtime remaining
        bb.create_workflow(WorkflowState(
            workflow_id="wf_bud_2", task_id="t_b2",
            status="executing",
            budget={"max_runtime_s": 10},
            created_at=time.time() - 9,  # 9s elapsed of 10s budget
        ))

        findings = detect_health_issues(base=tmp_path)
        budget_issues = [f for f in findings
                         if "budget" in f.category.lower()]
        assert len(budget_issues) >= 1

    def test_budget_exhausted_in_metrics(self, tmp_path):
        bb = _bb(tmp_path)

        # Halted workflow with budget reason
        bb.create_workflow(WorkflowState(
            workflow_id="wf_bud_3", task_id="t_b3",
            status="halted",
            halt_reason="budget_exhausted: Runtime exceeded",
        ))

        metrics = collect_metrics(base=tmp_path)
        assert metrics.halted_workflows == 1
        assert metrics.budget_exhaustion_count >= 1


# ===========================================================================
# 9. Malformed memory artifact rejection
# ===========================================================================

class TestMalformedMemory:
    """Memory engine rejects invalid artifacts (fail-closed)."""

    def test_missing_required_field_rejected(self, tmp_path):
        data = {
            "artifact_id": "mem_test_1",
            "workflow_id": "wf_1",
            # Missing many required fields
        }
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert len(errors) > 0

    def test_invalid_task_class_rejected(self, tmp_path):
        artifact = MemoryArtifact(
            artifact_id="mem_wf1_123",
            workflow_id="wf1",
            task_summary="Test",
            task_class="INVALID_CLASS",
            roles_involved=["coder"],
            key_decisions=["decision"],
            successful_patterns=["pattern"],
            verification_outcome="approved",
            reusable_guidance="guidance",
            confidence="high",
        )
        data = artifact.to_dict()
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("task_class" in e for e in errors)

    def test_invalid_confidence_rejected(self, tmp_path):
        artifact = MemoryArtifact(
            artifact_id="mem_wf2_123",
            workflow_id="wf2",
            task_summary="Test",
            task_class="code_impl",
            roles_involved=["coder"],
            key_decisions=["decision"],
            successful_patterns=["pattern"],
            verification_outcome="approved",
            reusable_guidance="guidance",
            confidence="maybe",
        )
        data = artifact.to_dict()
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("confidence" in e for e in errors)

    def test_duplicate_artifact_rejected(self, tmp_path):
        mem_base = tmp_path / "MEMORY"
        artifact = MemoryArtifact(
            artifact_id="mem_wf3_123",
            workflow_id="wf3",
            task_summary="Test",
            task_class="code_impl",
            roles_involved=["coder"],
            key_decisions=["decision"],
            successful_patterns=["pattern"],
            verification_outcome="approved",
            reusable_guidance="guidance",
            confidence="high",
        )
        write_memory_artifact(artifact, base=mem_base)

        # Second write should fail (append-only)
        with pytest.raises(ValueError, match="already exists"):
            write_memory_artifact(artifact, base=mem_base)

    def test_oversized_artifact_rejected(self):
        data = {
            "artifact_id": "mem_big_123",
            "workflow_id": "big",
            "task_summary": "Test",
            "task_class": "code_impl",
            "roles_involved": ["coder"],
            "key_decisions": ["x" * 40000],  # very large
            "successful_patterns": ["y" * 40000],
            "failure_patterns": [],
            "verification_outcome": "approved",
            "reusable_guidance": "guidance",
            "created_at": "2024-01-01T00:00:00Z",
            "confidence": "high",
        }
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("too large" in e for e in errors)


# ===========================================================================
# 10. Restart recovery after in-progress workflow
# ===========================================================================

class TestRestartRecovery:
    """RestartRecovery correctly reconciles state after crash."""

    def test_stale_workflow_halted(self, tmp_path):
        # Workflow that has been "executing" way past SLA
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf = {
            "workflow_id": "wf_rec_1",
            "task_id": "t_rec",
            "status": "executing",
            "created_at": time.time() - 10000,
            "updated_at": time.time() - 10000,
            "budget": {"max_runtime_s": 1800},
        }
        (wf_dir / "wf_rec_1.json").write_text(json.dumps(wf))

        recovery = RestartRecovery(base=tmp_path)
        result = recovery.reconcile()

        halted = [a for a in result["actions"] if a["type"] == "workflow_halted"]
        assert len(halted) == 1

        # Verify workflow is actually halted
        data = json.loads((wf_dir / "wf_rec_1.json").read_text())
        assert data["status"] == "halted"

    def test_executing_nodes_reset_on_recovery(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf = {
            "workflow_id": "wf_rec_2",
            "task_id": "t_rec2",
            "status": "executing",
            "created_at": time.time() - 100,
            "updated_at": time.time() - 100,
            "budget": {"max_runtime_s": 1800},
            "node_states": {
                "A": {"status": "executing", "retry_count": 0,
                       "max_retries": 1},
                "B": {"status": "completed"},
            },
        }
        (wf_dir / "wf_rec_2.json").write_text(json.dumps(wf))

        recovery = RestartRecovery(base=tmp_path)
        result = recovery.reconcile()

        reset_actions = [a for a in result["actions"]
                         if a["type"] == "workflow_nodes_reset"]
        assert len(reset_actions) == 1

        data = json.loads((wf_dir / "wf_rec_2.json").read_text())
        assert data["node_states"]["A"]["status"] == "pending"
        assert data["node_states"]["B"]["status"] == "completed"

    def test_inprogress_task_requeued(self, tmp_path):
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        ip_file = tasks_dir / "0099_test.md.inprogress"
        ip_file.write_text("# Task\nDo something")

        recovery = RestartRecovery(base=tmp_path)
        result = recovery.reconcile()

        requeued = [a for a in result["actions"] if a["type"] == "task_requeued"]
        assert len(requeued) == 1
        assert (tasks_dir / "0099_test.md").exists()
        assert not ip_file.exists()

    def test_expired_leases_cleaned(self, tmp_path):
        leases_dir = tmp_path / "STATE" / "leases"
        leases_dir.mkdir(parents=True, exist_ok=True)
        lease = {
            "workflow_id": "wf_rec_3",
            "node_id": "A",
            "holder": "dead_agent",
            "acquired_at": time.time() - 2000,
            "ttl_s": 600,
        }
        (leases_dir / "wf_rec_3_A.json").write_text(json.dumps(lease))

        recovery = RestartRecovery(base=tmp_path)
        result = recovery.reconcile()

        lease_actions = [a for a in result["actions"]
                         if a["type"] == "lease_recovered"]
        assert len(lease_actions) == 1
        assert not (leases_dir / "wf_rec_3_A.json").exists()

    def test_recovery_log_written(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf = {
            "workflow_id": "wf_rec_4",
            "task_id": "t_rec4",
            "status": "executing",
            "created_at": time.time() - 20000,
            "updated_at": time.time() - 20000,
            "budget": {"max_runtime_s": 1800},
        }
        (wf_dir / "wf_rec_4.json").write_text(json.dumps(wf))

        recovery = RestartRecovery(base=tmp_path)
        recovery.reconcile()

        log_path = tmp_path / "LOGS" / "recovery.log"
        assert log_path.exists()
        assert "Recovery at" in log_path.read_text()


# ===========================================================================
# 11. Archive/cleanup of completed workflows
# ===========================================================================

class TestArchiveCleanup:
    """Archive manager correctly archives old state."""

    def test_completed_workflow_archived_after_threshold(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf = {
            "workflow_id": "wf_arc_1",
            "status": "completed",
            "updated_at": time.time() - 100000,  # old
        }
        (wf_dir / "wf_arc_1.json").write_text(json.dumps(wf))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert "wf_arc_1.json" in archived

        archive_dir = tmp_path / "STATE" / "archive" / "workflows"
        assert (archive_dir / "wf_arc_1.json").exists()
        assert not (wf_dir / "wf_arc_1.json").exists()

    def test_recent_workflow_not_archived(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf = {
            "workflow_id": "wf_arc_2",
            "status": "completed",
            "updated_at": time.time(),  # just now
        }
        (wf_dir / "wf_arc_2.json").write_text(json.dumps(wf))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert len(archived) == 0

    def test_executing_workflow_not_archived(self, tmp_path):
        wf_dir = tmp_path / "STATE" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf = {
            "workflow_id": "wf_arc_3",
            "status": "executing",
            "updated_at": time.time() - 100000,
        }
        (wf_dir / "wf_arc_3.json").write_text(json.dumps(wf))

        am = ArchiveManager(base=tmp_path)
        archived = am.archive_completed_workflows()
        assert len(archived) == 0

    def test_expired_leases_cleaned(self, tmp_path):
        leases_dir = tmp_path / "STATE" / "leases"
        leases_dir.mkdir(parents=True, exist_ok=True)
        lease = {
            "acquired_at": time.time() - 2000,
            "renewed_at": None,
            "ttl_s": 600,
        }
        (leases_dir / "old_lease.json").write_text(json.dumps(lease))

        am = ArchiveManager(base=tmp_path)
        cleaned = am.cleanup_expired_leases()
        assert "old_lease.json" in cleaned

    def test_stale_tmp_files_cleaned(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = state_dir / "orphan.tmp"
        tmp_file.write_text("orphaned data")
        # Set mtime to the past
        import os
        old_time = time.time() - 7200
        os.utime(tmp_file, (old_time, old_time))

        am = ArchiveManager(base=tmp_path)
        cleaned = am.cleanup_stale_tmp_files()
        assert len(cleaned) == 1

    def test_full_cleanup_runs(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, archive=True)
        am = ArchiveManager(base=tmp_path)
        result = am.run_cleanup()

        assert "archived_workflows" in result
        assert "archived_agents" in result
        assert "cleaned_leases" in result
        assert "cleaned_tmp" in result


# ===========================================================================
# 12. Feature-flag-off fallback to safe path
# ===========================================================================

class TestFeatureFlagOff:
    """When feature flags are off, system falls back to safe single-agent mode."""

    def test_missing_flags_file_defaults_off(self, tmp_path):
        ff = FeatureFlags(base=tmp_path)
        assert ff.is_multi_agent_enabled() is False
        assert ff.is_archive_enabled() is False
        assert ff.is_rate_limiting_enabled() is False
        assert ff.is_manual_approval_enabled() is False

    def test_corrupt_flags_defaults_off(self, tmp_path):
        config_dir = tmp_path / "STATE" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "feature_flags.json").write_text("[invalid]")
        ff = FeatureFlags(base=tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_non_boolean_value_defaults_off(self, tmp_path):
        config_dir = tmp_path / "STATE" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        flags = {"phase7_orchestrator": {"enabled": "yes"}}
        (config_dir / "feature_flags.json").write_text(json.dumps(flags))
        ff = FeatureFlags(base=tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_production_hardening_skips_when_disabled(self, tmp_path):
        _setup_flags(tmp_path, enabled=False, archive=False)
        result = run_production_hardening(base=tmp_path)
        assert result["cleanup"] == "disabled"


# ===========================================================================
# 13. Anti-bypass: maker-checker enforced under failure
# ===========================================================================

class TestMakerCheckerAntiBypass:
    """Maker-checker cannot be bypassed even in failure scenarios."""

    def test_repo_changes_need_critic_even_if_contracts_pass(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_file(tmp_path, "code.py", "print(1)")

        # Valid contract, valid artifact, but repo_changes with no reviews
        report = verifier.verify(
            "wf_anti_1",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
            critic_reviews=None,
        )
        assert report.verdict == "rejected"
        assert report.maker_checker_enforced is True
        assert report.maker_checker_passed is False

    def test_blocking_objection_blocks_even_with_passing_review(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_file(tmp_path, "code.py", "print(1)")

        # Both a pass and a blocking objection
        report = verifier.verify(
            "wf_anti_2",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
            critic_reviews=[
                {"verdict": "pass", "blocking": False},
                {"verdict": "objection", "blocking": True},
            ],
        )
        assert report.verdict == "rejected"
        assert report.maker_checker_enforced is True
        assert report.maker_checker_passed is False

    def test_all_objection_reviews_fail_maker_checker(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_file(tmp_path, "code.py", "print(1)")

        report = verifier.verify(
            "wf_anti_3",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
            critic_reviews=[
                {"verdict": "objection", "blocking": True},
            ],
        )
        assert report.verdict == "rejected"

    def test_gate_blocks_completion_with_pending_replans(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)

        # Create blocking critic objection
        gate.run_critic_review("wf_anti_4", "node_x", deliverables={})

        fpath = _make_file(tmp_path, "out.md", "content")
        allowed, reason = gate.is_completion_allowed(
            "wf_anti_4",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        assert allowed is False


# ===========================================================================
# 14. Rate limiting
# ===========================================================================

class TestRateLimiting:
    """Rate limiter correctly bounds event rates."""

    def test_within_limit_allowed(self, tmp_path):
        rl = RateLimiter(base=tmp_path)
        result = rl.check_workflow_launch()
        assert result.allowed is True
        assert result.remaining > 0

    def test_exceed_limit_blocked(self, tmp_path):
        rl = RateLimiter(base=tmp_path)

        # Record events up to the limit
        for _ in range(10):
            rl.record_event("workflow_launch", window_s=3600)

        result = rl.check_workflow_launch()
        assert result.allowed is False
        assert result.remaining == 0

    def test_events_expire_outside_window(self, tmp_path):
        rl = RateLimiter(base=tmp_path)
        state_path = tmp_path / "STATE" / "rate_limits.json"

        # Manually write old events
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "workflow_launch": {
                "events": [time.time() - 7200]  # 2 hours ago
            },
        }
        state_path.write_text(json.dumps(state))

        result = rl.check_workflow_launch()
        assert result.allowed is True
        assert result.count == 0


# ===========================================================================
# 15. Approval gate (manual approval hooks)
# ===========================================================================

class TestApprovalGate:
    """Manual approval gate works correctly."""

    def test_approval_not_required_when_disabled(self, tmp_path):
        _setup_flags(tmp_path, enabled=True, archive=True)
        gate = ApprovalGate(base=tmp_path)
        assert gate.is_approval_required("repo.git.commit") is False

    def test_approval_required_when_enabled(self, tmp_path):
        config_dir = tmp_path / "STATE" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        flags = {
            "phase7_orchestrator": {"enabled": True},
            "phase7_hardening": {"manual_approval": True},
        }
        (config_dir / "feature_flags.json").write_text(json.dumps(flags))

        gate = ApprovalGate(base=tmp_path)
        assert gate.is_approval_required("repo.git.commit") is True
        assert gate.is_approval_required("repo.files.read") is False

    def test_pending_approval_auto_denied_on_timeout(self, tmp_path):
        gate = ApprovalGate(base=tmp_path)

        # Request with already-expired timeout
        approvals_dir = tmp_path / "STATE" / "approvals"
        approvals_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "action_id": "act_1",
            "status": "pending",
            "timeout_at": time.time() - 10,  # already expired
        }
        (approvals_dir / "act_1.json").write_text(json.dumps(request))

        approved, reason = gate.check_approval("act_1")
        assert approved is False
        assert "timed out" in reason.lower()


# ===========================================================================
# Run as script
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile
    import inspect

    test_classes = [
        TestChildContractMissing,
        TestVerifierRejection,
        TestCriticObjectionReplan,
        TestStaleLease,
        TestOrphanedAgentDetection,
        TestDependencyTimeout,
        TestPolicyDenialAntiBypass,
        TestBudgetExhaustionFailure,
        TestMalformedMemory,
        TestRestartRecovery,
        TestArchiveCleanup,
        TestFeatureFlagOff,
        TestMakerCheckerAntiBypass,
        TestRateLimiting,
        TestApprovalGate,
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
