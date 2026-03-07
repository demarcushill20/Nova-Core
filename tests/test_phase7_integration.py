"""Phase 7 — End-to-end integration tests across the full governed workflow path.

Validates:
  1. Full governed workflow lifecycle: create → delegate → claim → complete
     → critic review → verifier gate → governed synthesis → memory capture
  2. Multi-delegation workflow with node dependencies
  3. Maker-checker enforcement for repo-changing paths
  4. Feature-flag-disabled fallback to ungoverned path
  5. Observability/heartbeat visibility of live workflow state
  6. Coordination layer + lease lifecycle in integrated context
  7. Workflow graph rendering from integrated state

These tests exercise the complete cross-module integration path using
real blackboard state on disk (tmp_path), not mocks.
"""

import json
import time
from pathlib import Path

import pytest

from agents.blackboard import (
    Blackboard, ChildContract, Delegation, WorkflowState,
)
from agents.coordination import CoordinationLayer, NodeState, LeaseConflict
from agents.critic import CriticEngine
from agents.memory_engine import (
    capture_workflow_memory, retrieve_related_patterns,
    write_memory_artifact, MemoryArtifact,
)
from agents.observability import (
    collect_metrics, detect_health_issues, generate_health_report,
    run_multiagent_heartbeat,
)
from agents.production_hardening import FeatureFlags, run_production_hardening
from agents.verifier import VerifierEngine
from agents.workflow_engine import WorkflowEngine, WorkflowHalt, WorkflowLimits
from agents.workflow_gate import WorkflowGate, validate_contract_fields
from agents.workflow_graph import WorkflowGraphBuilder, render_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bb(tmp_path: Path) -> Blackboard:
    return Blackboard(base=tmp_path)


def _engine(tmp_path: Path, **kw) -> WorkflowEngine:
    bb = _bb(tmp_path)
    return WorkflowEngine(blackboard=bb, **kw)


def _coord(tmp_path: Path) -> CoordinationLayer:
    return CoordinationLayer(blackboard=_bb(tmp_path))


def _gate(tmp_path: Path) -> WorkflowGate:
    return WorkflowGate(blackboard=_bb(tmp_path))


def _make_file(tmp_path: Path, name: str, content: str = "result") -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def _good_contract_dict() -> dict:
    return {
        "summary": "Implemented feature X",
        "files_changed": "module.py",
        "verification": "All tests pass",
        "confidence": "high",
    }


def _child_contract(wf_id: str, subtask_id: str, agent_id: str,
                     role: str = "coder", summary: str = "Done") -> ChildContract:
    return ChildContract(
        agent_id=agent_id,
        workflow_id=wf_id,
        subtask_id=subtask_id,
        role=role,
        status="completed",
        summary=summary,
        artifacts=[],
        verification={"result": "pass", "confidence": "high"},
    )


def _enrich_child_contracts(bb: Blackboard, wf_id: str) -> None:
    """Add files_changed + confidence to persisted child contracts.

    The ChildContract dataclass doesn't include these fields, but
    validate_contract_fields() requires them for governed synthesis.
    In production, the orchestrator adapter populates these from the
    worker's ## CONTRACT block. Here we simulate that post-processing.
    """
    contracts_dir = bb.work / "agents" / "contracts"
    if not contracts_dir.exists():
        return
    for f in contracts_dir.glob("*.json"):
        data = json.loads(f.read_text())
        if data.get("workflow_id") != wf_id:
            continue
        if "files_changed" not in data:
            data["files_changed"] = data.get("artifacts", ["module.py"]) or ["module.py"]
        if "confidence" not in data:
            data["confidence"] = "high"
        f.write_text(json.dumps(data, indent=2))


def _setup_feature_flags(tmp_path: Path, enabled: bool = True) -> None:
    config_dir = tmp_path / "STATE" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    flags = {
        "phase7_orchestrator": {"enabled": enabled},
        "phase7_hardening": {
            "archive_cleanup": True,
            "rate_limiting": True,
            "manual_approval": False,
        },
    }
    (config_dir / "feature_flags.json").write_text(json.dumps(flags))


def _setup_policy_registry(tmp_path: Path) -> None:
    reg_dir = tmp_path / "STATE" / "agents"
    reg_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "agents": [
            {
                "agent_id": "coder_001",
                "role": "coder",
                "allowed_tools": ["repo.files.read", "repo.files.write",
                                  "repo.search.*"],
                "denied_tools": ["system.service.restart"],
                "max_actions": 50,
                "max_runtime_seconds": 300,
                "max_retries": 1,
                "feature_flags": {"allow_delegation": False},
            },
            {
                "agent_id": "researcher_001",
                "role": "researcher",
                "allowed_tools": ["web.*", "http.fetch"],
                "denied_tools": ["repo.files.write", "repo.git.commit",
                                 "shell.run"],
                "max_actions": 30,
                "max_runtime_seconds": 180,
                "max_retries": 1,
                "feature_flags": {"allow_delegation": False},
            },
            {
                "agent_id": "orchestrator_001",
                "role": "orchestrator",
                "allowed_tools": ["agent.spawn", "repo.files.read"],
                "denied_tools": [],
                "max_actions": 100,
                "max_runtime_seconds": 1800,
                "max_retries": 2,
                "feature_flags": {"allow_delegation": True},
            },
        ],
    }
    (reg_dir / "registry.json").write_text(json.dumps(registry))


# ===========================================================================
# 1. Full governed workflow lifecycle (happy path)
# ===========================================================================

class TestGovernedWorkflowLifecycle:
    """End-to-end: create → delegate → complete → critic → verifier → synthesize → memory."""

    def test_full_lifecycle(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)
        gate = WorkflowGate(blackboard=bb)

        # 1. Create workflow
        wf = engine.create_workflow("wf_e2e_1", "task_001")
        assert wf.workflow_id == "wf_e2e_1"
        assert wf.status == "created"

        # Verify persisted
        wf_data = bb.get_workflow("wf_e2e_1")
        assert wf_data is not None
        assert wf_data["status"] == "created"

        # 2. Save node states
        nodes = [
            NodeState(node_id="research", workflow_id="wf_e2e_1",
                      status="pending"),
            NodeState(node_id="code", workflow_id="wf_e2e_1",
                      status="pending", depends_on=["research"]),
        ]
        coord.save_node_states("wf_e2e_1", nodes)

        # 3. Delegate subtask
        d1 = engine.delegate("wf_e2e_1", "research", "researcher_001",
                             "researcher", "Research the API")
        assert d1.status == "pending"

        # 4. Claim delegation
        engine.claim_delegation("wf_e2e_1", "research", "researcher_001")
        d1_data = bb.get_delegation("wf_e2e_1", "research")
        assert d1_data["status"] == "claimed"

        # 5. Complete delegation with child contract
        contract = _child_contract("wf_e2e_1", "research", "researcher_001",
                                   "researcher", "Researched API docs")
        engine.complete_delegation("wf_e2e_1", "research",
                                   "researcher_001", contract)
        d1_data = bb.get_delegation("wf_e2e_1", "research")
        assert d1_data["status"] == "completed"

        # Complete research node
        coord.update_node_state("wf_e2e_1", "research",
                                {"status": "completed"})

        # 6. Delegate second subtask (depends on research)
        d2 = engine.delegate("wf_e2e_1", "code", "coder_001",
                             "coder", "Implement the feature")

        # 7. Claim and complete second subtask
        engine.claim_delegation("wf_e2e_1", "code", "coder_001")
        out_file = _make_file(tmp_path, "OUTPUT/feature.py",
                              "def feature(): pass")
        contract2 = _child_contract("wf_e2e_1", "code", "coder_001",
                                    "coder", "Implemented feature")
        contract2.artifacts = [out_file]
        engine.complete_delegation("wf_e2e_1", "code", "coder_001", contract2)
        coord.update_node_state("wf_e2e_1", "code", {"status": "completed"})

        # 8. Enrich child contracts for validation
        _enrich_child_contracts(bb, "wf_e2e_1")

        # 9. Critic review (pass)
        deliverables = {"feature.py": out_file}
        review = gate.run_critic_review(
            "wf_e2e_1", "code", deliverables,
            contract=_good_contract_dict(),
        )
        assert review.verdict == "pass"

        # 10. Verifier gate (approve)
        contracts = bb.list_child_contracts("wf_e2e_1")
        allowed, reason = gate.is_completion_allowed(
            "wf_e2e_1",
            deliverables=deliverables,
            contracts=contracts,
        )
        assert allowed is True, f"Completion blocked: {reason}"

        # 11. Governed synthesis
        synthesis = engine.governed_synthesize(
            "wf_e2e_1",
            deliverables=deliverables,
            repo_changes=None,  # no repo changes
        )
        assert synthesis["status"] == "completed"
        assert synthesis.get("governed") is True

        # 12. Memory capture
        delegations = bb.list_delegations("wf_e2e_1")
        metrics = bb.workflow_metrics("wf_e2e_1")
        mem_path = capture_workflow_memory(
            workflow_id="wf_e2e_1",
            task_summary="Build feature X",
            task_class="code_impl",
            delegations=delegations,
            contracts=contracts,
            metrics=metrics,
            verification_outcome="approved",
            base=tmp_path / "MEMORY",
        )
        assert mem_path is not None
        assert mem_path.exists()

        # 13. Verify full state coherence
        final_wf = bb.get_workflow("wf_e2e_1")
        assert final_wf["status"] == "completed"
        assert len(bb.list_child_contracts("wf_e2e_1")) == 2
        assert len(bb.list_delegations("wf_e2e_1")) == 2

    def test_workflow_metrics_accurate_after_lifecycle(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_metrics_1", "task_m1")
        d = engine.delegate("wf_metrics_1", "sub1", "agent_1", "coder", "Do X")
        engine.claim_delegation("wf_metrics_1", "sub1", "agent_1")
        time.sleep(0.01)  # measurable latency
        contract = _child_contract("wf_metrics_1", "sub1", "agent_1")
        engine.complete_delegation("wf_metrics_1", "sub1", "agent_1", contract)

        metrics = bb.workflow_metrics("wf_metrics_1")
        assert metrics["total_delegations"] == 1
        assert metrics["completed"] == 1
        assert metrics["failed"] == 0
        assert metrics["contracts_received"] == 1
        assert metrics["mean_subtask_latency_s"] is not None


# ===========================================================================
# 2. Multi-delegation with node dependencies
# ===========================================================================

class TestMultiDelegationDependencies:
    """Validate dependency resolution across multiple delegations."""

    def test_dependency_chain_ordering(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)

        engine.create_workflow("wf_dep_1", "task_dep")

        # DAG: A → B → C
        nodes = [
            NodeState(node_id="A", workflow_id="wf_dep_1", status="pending"),
            NodeState(node_id="B", workflow_id="wf_dep_1", status="pending",
                      depends_on=["A"]),
            NodeState(node_id="C", workflow_id="wf_dep_1", status="pending",
                      depends_on=["B"]),
        ]
        coord.save_node_states("wf_dep_1", nodes)

        # Only A should be ready
        ready = coord.get_ready_nodes("wf_dep_1")
        assert ready == ["A"]

        # Complete A
        coord.update_node_state("wf_dep_1", "A", {"status": "completed"})
        ready = coord.get_ready_nodes("wf_dep_1")
        assert ready == ["B"]

        # Complete B
        coord.update_node_state("wf_dep_1", "B", {"status": "completed"})
        ready = coord.get_ready_nodes("wf_dep_1")
        assert ready == ["C"]

    def test_parallel_independent_nodes(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)

        engine.create_workflow("wf_par_1", "task_par")

        # DAG: A and B are independent; C depends on both
        nodes = [
            NodeState(node_id="A", workflow_id="wf_par_1", status="pending"),
            NodeState(node_id="B", workflow_id="wf_par_1", status="pending"),
            NodeState(node_id="C", workflow_id="wf_par_1", status="pending",
                      depends_on=["A", "B"]),
        ]
        coord.save_node_states("wf_par_1", nodes)

        # Both A and B should be ready
        ready = coord.get_ready_nodes("wf_par_1")
        assert set(ready) == {"A", "B"}

        # Complete only A — C still blocked
        coord.update_node_state("wf_par_1", "A", {"status": "completed"})
        ready = coord.get_ready_nodes("wf_par_1")
        assert ready == ["B"]  # B still pending, C blocked

        # Complete B — C now ready
        coord.update_node_state("wf_par_1", "B", {"status": "completed"})
        ready = coord.get_ready_nodes("wf_par_1")
        assert ready == ["C"]


# ===========================================================================
# 3. Maker-checker enforcement in integrated flow
# ===========================================================================

class TestMakerCheckerIntegrated:
    """Repo-changing paths require critic review before verifier approves."""

    def test_repo_changes_blocked_without_critic(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_mc_1", "task_mc")
        engine.delegate("wf_mc_1", "code", "coder_001", "coder", "Write code")
        engine.claim_delegation("wf_mc_1", "code", "coder_001")
        code_file = _make_file(tmp_path, "OUTPUT/code.py", "print('hi')")
        contract = _child_contract("wf_mc_1", "code", "coder_001")
        engine.complete_delegation("wf_mc_1", "code", "coder_001", contract)
        _enrich_child_contracts(bb, "wf_mc_1")

        # Attempt governed synthesis WITH repo_changes but NO critic review
        synthesis = engine.governed_synthesize(
            "wf_mc_1",
            deliverables={"code.py": code_file},
            repo_changes=["code.py"],
        )
        assert synthesis["status"] == "blocked"
        assert "Maker-checker" in synthesis.get("reason", "") or \
               "Verifier" in synthesis.get("reason", "")

    def test_repo_changes_approved_with_critic(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        gate = WorkflowGate(blackboard=bb)

        engine.create_workflow("wf_mc_2", "task_mc2")
        engine.delegate("wf_mc_2", "code", "coder_001", "coder", "Write code")
        engine.claim_delegation("wf_mc_2", "code", "coder_001")
        code_file = _make_file(tmp_path, "OUTPUT/code.py", "print('hello')")
        contract = _child_contract("wf_mc_2", "code", "coder_001")
        contract.artifacts = [code_file]
        engine.complete_delegation("wf_mc_2", "code", "coder_001", contract)
        _enrich_child_contracts(bb, "wf_mc_2")

        # Run critic review (passes)
        review = gate.run_critic_review(
            "wf_mc_2", "code",
            deliverables={"code.py": code_file},
            contract=_good_contract_dict(),
        )
        assert review.verdict == "pass"

        # Now governed synthesis WITH repo_changes — should approve
        synthesis = engine.governed_synthesize(
            "wf_mc_2",
            deliverables={"code.py": code_file},
            repo_changes=["code.py"],
        )
        assert synthesis["status"] == "completed"
        assert synthesis.get("governed") is True


# ===========================================================================
# 4. Feature-flag-disabled fallback
# ===========================================================================

class TestFeatureFlagFallback:
    """When phase7_orchestrator is disabled, multi-agent features are off."""

    def test_flags_disabled_by_default(self, tmp_path):
        ff = FeatureFlags(base=tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_flags_enabled_when_configured(self, tmp_path):
        _setup_feature_flags(tmp_path, enabled=True)
        ff = FeatureFlags(base=tmp_path)
        assert ff.is_multi_agent_enabled() is True

    def test_flags_disabled_when_file_corrupt(self, tmp_path):
        config_dir = tmp_path / "STATE" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "feature_flags.json").write_text("NOT JSON")
        ff = FeatureFlags(base=tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_ungoverned_synthesis_works_without_flags(self, tmp_path):
        """Single-agent mode: synthesize_workflow works without verifier gate."""
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_single", "task_s")
        engine.delegate("wf_single", "work", "agent_1", "coder", "Do stuff")
        engine.claim_delegation("wf_single", "work", "agent_1")
        contract = _child_contract("wf_single", "work", "agent_1")
        engine.complete_delegation("wf_single", "work", "agent_1", contract)

        # Ungoverned path — no verifier needed
        synthesis = engine.synthesize_workflow("wf_single")
        assert synthesis["status"] == "completed"
        assert synthesis.get("governed") is None  # not governed

    def test_hardening_disabled_skips_archive(self, tmp_path):
        # Set both orchestrator and archive_cleanup to disabled
        config_dir = tmp_path / "STATE" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        flags = {
            "phase7_orchestrator": {"enabled": False},
            "phase7_hardening": {
                "archive_cleanup": False,
                "rate_limiting": False,
                "manual_approval": False,
            },
        }
        (config_dir / "feature_flags.json").write_text(json.dumps(flags))

        result = run_production_hardening(base=tmp_path)
        assert result["multi_agent_enabled"] is False
        assert result["cleanup"] == "disabled"


# ===========================================================================
# 5. Observability / heartbeat visibility
# ===========================================================================

class TestObservabilityIntegrated:
    """Heartbeat reports accurately reflect live workflow state."""

    def test_active_workflow_visible_in_metrics(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_obs_1", "task_obs")
        engine.delegate("wf_obs_1", "sub1", "agent_1", "coder", "Task")
        bb.update_workflow("wf_obs_1", {"status": "executing"})

        metrics = collect_metrics(base=tmp_path)
        assert metrics.active_workflows == 1
        assert metrics.total_delegations == 1

    def test_completed_workflow_metrics(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_obs_2", "task_obs2")
        engine.delegate("wf_obs_2", "sub1", "agent_1", "coder", "Done")
        engine.claim_delegation("wf_obs_2", "sub1", "agent_1")
        contract = _child_contract("wf_obs_2", "sub1", "agent_1")
        engine.complete_delegation("wf_obs_2", "sub1", "agent_1", contract)

        synthesis = engine.synthesize_workflow("wf_obs_2")

        metrics = collect_metrics(base=tmp_path)
        assert metrics.completed_workflows == 1
        assert metrics.completed_delegations == 1
        assert metrics.agent_spawn_count >= 1

    def test_health_report_generated(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_obs_3", "task_obs3")
        bb.update_workflow("wf_obs_3", {"status": "executing"})

        report = generate_health_report(base=tmp_path)
        assert report.overall in ("healthy", "warning", "unhealthy")
        assert report.metrics.active_workflows >= 1

    def test_heartbeat_writes_files(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        engine.create_workflow("wf_obs_4", "task_obs4")

        report = run_multiagent_heartbeat(base=tmp_path)
        md_path = tmp_path / "HEARTBEAT_MULTIAGENT.md"
        json_path = tmp_path / "STATE" / "heartbeat_multiagent.json"
        assert md_path.exists()
        assert json_path.exists()
        assert "wf_obs_4" in md_path.read_text()


# ===========================================================================
# 6. Coordination + lease lifecycle in integrated context
# ===========================================================================

class TestCoordinationIntegrated:
    """Lease + node state + delegation work together correctly."""

    def test_claim_node_prevents_double_execution(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)

        engine.create_workflow("wf_coord_1", "task_c1")
        coord.save_node_states("wf_coord_1", [
            NodeState(node_id="A", workflow_id="wf_coord_1", status="pending"),
        ])

        # First agent claims
        lease = coord.claim_node("wf_coord_1", "A", "agent_1")
        assert lease.holder == "agent_1"

        # Second agent blocked
        with pytest.raises(LeaseConflict):
            coord.claim_node("wf_coord_1", "A", "agent_2")

    def test_complete_node_releases_lease(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)

        engine.create_workflow("wf_coord_2", "task_c2")
        coord.save_node_states("wf_coord_2", [
            NodeState(node_id="A", workflow_id="wf_coord_2", status="pending"),
        ])

        coord.claim_node("wf_coord_2", "A", "agent_1")
        coord.complete_node("wf_coord_2", "A", "agent_1", "OUTPUT/a.md")

        # Node state should be completed
        states = coord.get_node_states("wf_coord_2")
        assert states["A"].status == "completed"

        # Lease should be released
        lease = coord.get_lease("wf_coord_2", "A")
        assert lease is None

    def test_resume_workflow_from_state(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)
        coord = CoordinationLayer(blackboard=bb)

        engine.create_workflow("wf_coord_3", "task_c3")
        coord.save_node_states("wf_coord_3", [
            NodeState(node_id="A", workflow_id="wf_coord_3",
                      status="completed"),
            NodeState(node_id="B", workflow_id="wf_coord_3",
                      status="pending", depends_on=["A"]),
        ])

        resume = coord.resume_workflow("wf_coord_3")
        assert "A" in resume["completed_nodes"]
        assert "B" in resume["pending_nodes"]


# ===========================================================================
# 7. Workflow graph rendering from integrated state
# ===========================================================================

class TestWorkflowGraphIntegrated:
    """Workflow graph accurately represents integrated state."""

    def test_graph_includes_delegations_and_contracts(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_graph_1", "task_g1")
        engine.delegate("wf_graph_1", "sub1", "agent_1", "coder", "Code it")
        engine.claim_delegation("wf_graph_1", "sub1", "agent_1")
        contract = _child_contract("wf_graph_1", "sub1", "agent_1")
        engine.complete_delegation("wf_graph_1", "sub1", "agent_1", contract)

        builder = WorkflowGraphBuilder(blackboard=bb)
        graph = builder.build("wf_graph_1")

        # Tree-based model: root has children (delegation nodes)
        assert graph.root is not None
        assert len(graph.root.children) >= 1
        assert len(graph.edges) >= 1

        md = render_markdown(graph)
        assert "task_g1" in md
        assert "Code it" in md  # goal is rendered in the graph


# ===========================================================================
# 8. Memory capture and retrieval in integrated context
# ===========================================================================

class TestMemoryCaptureIntegrated:
    """Memory artifacts correctly capture workflow learnings."""

    def test_capture_and_retrieve(self, tmp_path):
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb)

        engine.create_workflow("wf_mem_1", "task_m1")
        engine.delegate("wf_mem_1", "sub1", "agent_1", "coder", "Code task")
        engine.claim_delegation("wf_mem_1", "sub1", "agent_1")
        contract = _child_contract("wf_mem_1", "sub1", "agent_1",
                                   summary="Built auth module")
        engine.complete_delegation("wf_mem_1", "sub1", "agent_1", contract)

        delegations = bb.list_delegations("wf_mem_1")
        contracts = bb.list_child_contracts("wf_mem_1")
        metrics = bb.workflow_metrics("wf_mem_1")

        mem_base = tmp_path / "MEMORY"
        mem_path = capture_workflow_memory(
            workflow_id="wf_mem_1",
            task_summary="Build auth module",
            task_class="code_impl",
            delegations=delegations,
            contracts=contracts,
            metrics=metrics,
            verification_outcome="approved",
            base=mem_base,
        )
        assert mem_path is not None
        assert mem_path.exists()

        # Retrieve
        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["auth"],
            base=mem_base,
        )
        assert len(results) == 1
        assert results[0]["task_class"] == "code_impl"
        assert results[0]["_relevance_score"] > 0

    def test_no_delegations_skips_memory(self, tmp_path):
        mem_base = tmp_path / "MEMORY"
        result = capture_workflow_memory(
            workflow_id="wf_empty",
            task_summary="Empty",
            task_class="simple",
            delegations=[],
            contracts=[],
            metrics={},
            base=mem_base,
        )
        assert result is None


# ===========================================================================
# 9. Full lifecycle with concurrent limit enforcement
# ===========================================================================

class TestConcurrentLimitEnforcement:
    """Workflow engine enforces max concurrent agent limit."""

    def test_exceeding_concurrent_limit_raises(self, tmp_path):
        limits = WorkflowLimits(max_concurrent_agents=2)
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb, limits=limits)

        engine.create_workflow("wf_lim_1", "task_lim")
        engine.delegate("wf_lim_1", "sub1", "a1", "coder", "Task 1")
        engine.delegate("wf_lim_1", "sub2", "a2", "coder", "Task 2")

        with pytest.raises(WorkflowHalt):
            engine.delegate("wf_lim_1", "sub3", "a3", "coder", "Task 3")

    def test_completed_delegation_frees_slot(self, tmp_path):
        limits = WorkflowLimits(max_concurrent_agents=2)
        bb = _bb(tmp_path)
        engine = WorkflowEngine(blackboard=bb, limits=limits)

        engine.create_workflow("wf_lim_2", "task_lim2")
        engine.delegate("wf_lim_2", "sub1", "a1", "coder", "Task 1")
        engine.delegate("wf_lim_2", "sub2", "a2", "coder", "Task 2")

        # Complete one
        engine.claim_delegation("wf_lim_2", "sub1", "a1")
        contract = _child_contract("wf_lim_2", "sub1", "a1")
        engine.complete_delegation("wf_lim_2", "sub1", "a1", contract)

        # Now should be able to add another
        engine.delegate("wf_lim_2", "sub3", "a3", "coder", "Task 3")
        delegations = bb.list_delegations("wf_lim_2")
        assert len(delegations) == 3


# ===========================================================================
# 10. Stop condition: budget exhaustion (time-based)
# ===========================================================================

class TestBudgetExhaustion:
    """Workflow halts when runtime budget is exceeded."""

    def test_runtime_budget_halt(self, tmp_path):
        bb = _bb(tmp_path)
        limits = WorkflowLimits(max_workflow_runtime_s=0)
        engine = WorkflowEngine(blackboard=bb, limits=limits)

        wf = engine.create_workflow("wf_budget_1", "task_b1",
                                     budget={"max_runtime_s": 0})

        # Any stop-condition check should halt
        with pytest.raises(WorkflowHalt) as exc_info:
            engine.check_stop_conditions("wf_budget_1")
        assert "budget" in str(exc_info.value).lower() or \
               "runtime" in str(exc_info.value).lower()

        # Workflow should be halted in state
        wf_data = bb.get_workflow("wf_budget_1")
        assert wf_data["status"] == "halted"


# ===========================================================================
# Run as script
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile
    import inspect

    test_classes = [
        TestGovernedWorkflowLifecycle,
        TestMultiDelegationDependencies,
        TestMakerCheckerIntegrated,
        TestFeatureFlagFallback,
        TestObservabilityIntegrated,
        TestCoordinationIntegrated,
        TestWorkflowGraphIntegrated,
        TestMemoryCaptureIntegrated,
        TestConcurrentLimitEnforcement,
        TestBudgetExhaustion,
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
