"""Tests for Phase 7.3 — Shared Coordination Layer.

Acceptance criteria tested:
  - child results visible through workflow graph
  - no duplicate execution of same subtask
  - orchestrator can resume after restart from state

Specific tests:
  - resume after interruption
  - lock contention
  - stale lease recovery
  - duplicate claim rejection
  - node dependency resolution
  - checkpoint save/restore
  - workflow recovery after crash
"""

import json
import os
import tempfile
import time

import pytest

# Set NOVACORE_ROOT before imports so modules use the temp dir
_tmpdir = None


@pytest.fixture(autouse=True)
def setup_tmpdir(tmp_path):
    """Set up a temporary nova-core root for each test."""
    global _tmpdir
    _tmpdir = tmp_path
    os.environ["NOVACORE_ROOT"] = str(tmp_path)

    # Create required directories
    (tmp_path / "STATE" / "workflows").mkdir(parents=True)
    (tmp_path / "STATE" / "delegations").mkdir(parents=True)
    (tmp_path / "STATE" / "leases").mkdir(parents=True)
    (tmp_path / "STATE" / "agents" / "runtime").mkdir(parents=True)
    (tmp_path / "WORK" / "agents" / "messages").mkdir(parents=True)
    (tmp_path / "WORK" / "agents" / "contracts").mkdir(parents=True)

    yield tmp_path

    if "NOVACORE_ROOT" in os.environ:
        del os.environ["NOVACORE_ROOT"]


def make_blackboard(tmp_path):
    from agents.blackboard import Blackboard
    return Blackboard(base=tmp_path)


def make_coord(tmp_path):
    from agents.coordination import CoordinationLayer
    bb = make_blackboard(tmp_path)
    return CoordinationLayer(blackboard=bb)


def create_test_workflow(tmp_path, workflow_id="wf-001", task_id="task-001"):
    from agents.blackboard import WorkflowState
    bb = make_blackboard(tmp_path)
    wf = WorkflowState(workflow_id=workflow_id, task_id=task_id, status="executing")
    bb.create_workflow(wf)
    return bb


# =========================================================================
# Lease tests
# =========================================================================

class TestLeaseAcquisition:
    """Test basic lease acquire/release lifecycle."""

    def test_acquire_lease(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        lease = coord.acquire_lease("wf-001", "node-A", "agent-1")
        assert lease.holder == "agent-1"
        assert lease.workflow_id == "wf-001"
        assert lease.node_id == "node-A"
        assert not lease.is_expired

    def test_release_lease(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")
        coord.release_lease("wf-001", "node-A", "agent-1")

        assert coord.get_lease("wf-001", "node-A") is None

    def test_release_nonexistent_is_idempotent(self, tmp_path):
        coord = make_coord(tmp_path)
        # Should not raise
        coord.release_lease("wf-001", "node-X", "agent-1")

    def test_renew_lease(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        lease = coord.acquire_lease("wf-001", "node-A", "agent-1", ttl_s=60)
        original_expires = lease.expires_at

        time.sleep(0.05)
        renewed = coord.renew_lease("wf-001", "node-A", "agent-1")
        assert renewed.renewed_at is not None
        assert renewed.expires_at > original_expires

    def test_get_lease(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")
        lease = coord.get_lease("wf-001", "node-A")
        assert lease is not None
        assert lease.holder == "agent-1"

    def test_list_leases(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")
        coord.acquire_lease("wf-001", "node-B", "agent-2")

        leases = coord.list_leases("wf-001")
        assert len(leases) == 2
        holders = {l.holder for l in leases}
        assert holders == {"agent-1", "agent-2"}

    def test_list_leases_filters_by_workflow(self, tmp_path):
        create_test_workflow(tmp_path, "wf-001")
        create_test_workflow(tmp_path, "wf-002", "task-002")
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")
        coord.acquire_lease("wf-002", "node-A", "agent-2")

        leases = coord.list_leases("wf-001")
        assert len(leases) == 1
        assert leases[0].holder == "agent-1"


class TestDuplicateClaimRejection:
    """No duplicate execution of same subtask."""

    def test_duplicate_claim_raises(self, tmp_path):
        from agents.coordination import LeaseConflict

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")

        with pytest.raises(LeaseConflict) as exc_info:
            coord.acquire_lease("wf-001", "node-A", "agent-2")

        assert "agent-1" in str(exc_info.value)

    def test_same_agent_double_claim_raises(self, tmp_path):
        from agents.coordination import LeaseConflict

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")

        with pytest.raises(LeaseConflict):
            coord.acquire_lease("wf-001", "node-A", "agent-1")

    def test_claim_after_release_succeeds(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")
        coord.release_lease("wf-001", "node-A", "agent-1")

        lease = coord.acquire_lease("wf-001", "node-A", "agent-2")
        assert lease.holder == "agent-2"

    def test_wrong_agent_cannot_release(self, tmp_path):
        from agents.coordination import LeaseConflict

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")

        with pytest.raises(LeaseConflict):
            coord.release_lease("wf-001", "node-A", "agent-2")

    def test_wrong_agent_cannot_renew(self, tmp_path):
        from agents.coordination import LeaseConflict

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1")

        with pytest.raises(LeaseConflict):
            coord.renew_lease("wf-001", "node-A", "agent-2")

    def test_renew_nonexistent_raises(self, tmp_path):
        from agents.coordination import LeaseNotFound

        coord = make_coord(tmp_path)

        with pytest.raises(LeaseNotFound):
            coord.renew_lease("wf-001", "node-X", "agent-1")


class TestStaleLease:
    """Stale lease recovery."""

    def test_stale_lease_allows_takeover(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Acquire with very short TTL
        coord.acquire_lease("wf-001", "node-A", "agent-1", ttl_s=0.01)
        time.sleep(0.05)  # Wait for expiry

        # Another agent can take over
        lease = coord.acquire_lease("wf-001", "node-A", "agent-2")
        assert lease.holder == "agent-2"

    def test_stale_recovery_logged(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1", ttl_s=0.01)
        time.sleep(0.05)
        coord.acquire_lease("wf-001", "node-A", "agent-2")

        # Check recovery log exists
        log_path = tmp_path / "STATE" / "leases" / "recovery.jsonl"
        assert log_path.exists()
        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["old_holder"] == "agent-1"
        assert records[0]["new_holder"] == "agent-2"

    def test_recover_stale_leases(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1", ttl_s=0.01)
        coord.acquire_lease("wf-001", "node-B", "agent-2", ttl_s=600)
        time.sleep(0.05)

        recovered = coord.recover_stale_leases("wf-001")
        assert len(recovered) == 1
        assert recovered[0].node_id == "node-A"

        # node-B should still be leased
        assert coord.get_lease("wf-001", "node-B") is not None
        # node-A lease should be gone
        assert coord.get_lease("wf-001", "node-A") is None

    def test_expired_lease_is_expired(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.acquire_lease("wf-001", "node-A", "agent-1", ttl_s=0.01)
        time.sleep(0.05)

        lease = coord.get_lease("wf-001", "node-A")
        assert lease is not None
        assert lease.is_expired


# =========================================================================
# Node state tests
# =========================================================================

class TestNodeStates:
    """Test node-level state persistence within workflows."""

    def test_save_and_get_node_states(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="pending"),
            NodeState(node_id="n2", workflow_id="wf-001", status="pending",
                      depends_on=["n1"]),
        ]
        coord.save_node_states("wf-001", nodes)

        result = coord.get_node_states("wf-001")
        assert "n1" in result
        assert "n2" in result
        assert result["n1"].status == "pending"
        assert result["n2"].depends_on == ["n1"]

    def test_update_single_node(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [NodeState(node_id="n1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)

        updated = coord.update_node_state("wf-001", "n1", {
            "status": "executing",
            "assigned_agent": "agent-1",
        })

        assert updated.status == "executing"
        assert updated.assigned_agent == "agent-1"

        # Verify persisted
        states = coord.get_node_states("wf-001")
        assert states["n1"].status == "executing"

    def test_get_node_states_empty_workflow(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)
        result = coord.get_node_states("wf-001")
        assert result == {}

    def test_get_node_states_missing_workflow(self, tmp_path):
        coord = make_coord(tmp_path)
        result = coord.get_node_states("nonexistent")
        assert result == {}


# =========================================================================
# Checkpoint tests
# =========================================================================

class TestCheckpoints:
    """Test checkpoint save/restore for resume support."""

    def test_save_and_get_checkpoint(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.save_checkpoint("wf-001", {
            "step_index": 2,
            "completed_nodes": ["n1", "n2"],
            "phase": "execution",
        })

        cp = coord.get_latest_checkpoint("wf-001")
        assert cp is not None
        assert cp["step_index"] == 2
        assert "saved_at" in cp

    def test_multiple_checkpoints_returns_latest(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        coord.save_checkpoint("wf-001", {"step_index": 1})
        coord.save_checkpoint("wf-001", {"step_index": 2})
        coord.save_checkpoint("wf-001", {"step_index": 3})

        cp = coord.get_latest_checkpoint("wf-001")
        assert cp["step_index"] == 3

    def test_no_checkpoint_returns_none(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)
        assert coord.get_latest_checkpoint("wf-001") is None

    def test_checkpoint_missing_workflow(self, tmp_path):
        coord = make_coord(tmp_path)
        assert coord.get_latest_checkpoint("nonexistent") is None


# =========================================================================
# Resume after interruption
# =========================================================================

class TestResumeAfterInterruption:
    """Orchestrator can resume after restart from state."""

    def test_resume_workflow_classifies_nodes(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Set up a workflow with nodes in different states
        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed"),
            NodeState(node_id="n2", workflow_id="wf-001", status="pending"),
            NodeState(node_id="n3", workflow_id="wf-001", status="pending",
                      depends_on=["n2"]),
            NodeState(node_id="n4", workflow_id="wf-001", status="failed",
                      error="timeout"),
        ]
        coord.save_node_states("wf-001", nodes)

        state = coord.resume_workflow("wf-001")

        assert "n1" in state["completed_nodes"]
        assert "n2" in state["pending_nodes"]  # deps satisfied (none)
        assert "n3" in state["blocked_nodes"]  # depends on n2
        assert "n4" in state["failed_nodes"]

    def test_resume_detects_stale_executing_nodes(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Node is "executing" but has no valid lease
        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001",
                      status="executing", assigned_agent="agent-1"),
        ]
        coord.save_node_states("wf-001", nodes)

        state = coord.resume_workflow("wf-001")
        assert "n1" in state["stale_nodes"]

    def test_resume_with_valid_executing_node(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001",
                      status="executing", assigned_agent="agent-1"),
        ]
        coord.save_node_states("wf-001", nodes)

        # Give it a valid lease
        coord.acquire_lease("wf-001", "n1", "agent-1", ttl_s=600)

        state = coord.resume_workflow("wf-001")
        assert "n1" in state["executing_nodes"]
        assert "n1" not in state["stale_nodes"]

    def test_resume_includes_checkpoint(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [NodeState(node_id="n1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)
        coord.save_checkpoint("wf-001", {"step_index": 5})

        state = coord.resume_workflow("wf-001")
        assert state["latest_checkpoint"]["step_index"] == 5

    def test_resume_includes_delegations(self, tmp_path):
        from agents.blackboard import Delegation

        bb = create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        bb.create_delegation(Delegation(
            workflow_id="wf-001", subtask_id="sub-1",
            agent_id="agent-1", role="coder", goal="write code",
        ))

        from agents.coordination import NodeState
        nodes = [NodeState(node_id="sub-1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)

        state = coord.resume_workflow("wf-001")
        assert len(state["delegations"]) == 1

    def test_resume_missing_workflow_raises(self, tmp_path):
        coord = make_coord(tmp_path)

        with pytest.raises(FileNotFoundError):
            coord.resume_workflow("nonexistent")


# =========================================================================
# Workflow recovery after crash
# =========================================================================

class TestWorkflowRecovery:
    """Recover workflow state after crash: stale leases, reset nodes."""

    def test_recovery_resets_stale_nodes(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Node executing with expired lease
        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001",
                      status="executing", assigned_agent="agent-1",
                      retry_count=0, max_retries=1),
        ]
        coord.save_node_states("wf-001", nodes)
        coord.acquire_lease("wf-001", "n1", "agent-1", ttl_s=0.01)
        time.sleep(0.05)

        state = coord.recover_workflow("wf-001")

        # Node should have been reset to pending
        assert "n1" in state["pending_nodes"]
        assert any(a["action"] == "node_reset_for_retry"
                   for a in state["recovery_actions"])

    def test_recovery_fails_exhausted_retries(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001",
                      status="executing", assigned_agent="agent-1",
                      retry_count=1, max_retries=1),
        ]
        coord.save_node_states("wf-001", nodes)
        coord.acquire_lease("wf-001", "n1", "agent-1", ttl_s=0.01)
        time.sleep(0.05)

        state = coord.recover_workflow("wf-001")

        assert "n1" in state["failed_nodes"]
        assert any(a["action"] == "node_failed_max_retries"
                   for a in state["recovery_actions"])

    def test_recovery_preserves_healthy_nodes(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed"),
            NodeState(node_id="n2", workflow_id="wf-001", status="pending"),
        ]
        coord.save_node_states("wf-001", nodes)

        state = coord.recover_workflow("wf-001")
        assert "n1" in state["completed_nodes"]
        assert "n2" in state["pending_nodes"]
        assert len(state["recovery_actions"]) == 0


# =========================================================================
# Coordinated node claiming
# =========================================================================

class TestCoordinatedClaiming:
    """Test claim_node / complete_node / fail_node integration."""

    def test_claim_node_acquires_lease_and_updates_state(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [NodeState(node_id="n1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)

        lease = coord.claim_node("wf-001", "n1", "agent-1")
        assert lease.holder == "agent-1"

        states = coord.get_node_states("wf-001")
        assert states["n1"].status == "claimed"
        assert states["n1"].assigned_agent == "agent-1"

    def test_complete_node_releases_lease(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [NodeState(node_id="n1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)

        coord.claim_node("wf-001", "n1", "agent-1")
        coord.complete_node("wf-001", "n1", "agent-1", output_ref="OUTPUT/result.md")

        states = coord.get_node_states("wf-001")
        assert states["n1"].status == "completed"
        assert states["n1"].output_ref == "OUTPUT/result.md"
        assert coord.get_lease("wf-001", "n1") is None

    def test_fail_node_releases_lease(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [NodeState(node_id="n1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)

        coord.claim_node("wf-001", "n1", "agent-1")
        coord.fail_node("wf-001", "n1", "agent-1", "something broke")

        states = coord.get_node_states("wf-001")
        assert states["n1"].status == "failed"
        assert states["n1"].error == "something broke"
        assert coord.get_lease("wf-001", "n1") is None

    def test_double_claim_rejected(self, tmp_path):
        from agents.coordination import NodeState, LeaseConflict

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [NodeState(node_id="n1", workflow_id="wf-001")]
        coord.save_node_states("wf-001", nodes)

        coord.claim_node("wf-001", "n1", "agent-1")

        with pytest.raises(LeaseConflict):
            coord.claim_node("wf-001", "n1", "agent-2")


# =========================================================================
# Dependency resolution
# =========================================================================

class TestDependencyResolution:
    """Test get_ready_nodes for dependency-aware scheduling."""

    def test_no_deps_all_ready(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001"),
            NodeState(node_id="n2", workflow_id="wf-001"),
        ]
        coord.save_node_states("wf-001", nodes)

        ready = coord.get_ready_nodes("wf-001")
        assert set(ready) == {"n1", "n2"}

    def test_deps_block_nodes(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001"),
            NodeState(node_id="n2", workflow_id="wf-001",
                      depends_on=["n1"]),
        ]
        coord.save_node_states("wf-001", nodes)

        ready = coord.get_ready_nodes("wf-001")
        assert ready == ["n1"]

    def test_completed_deps_unblock(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed"),
            NodeState(node_id="n2", workflow_id="wf-001",
                      depends_on=["n1"]),
        ]
        coord.save_node_states("wf-001", nodes)

        ready = coord.get_ready_nodes("wf-001")
        assert ready == ["n2"]

    def test_multi_dep_chain(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed"),
            NodeState(node_id="n2", workflow_id="wf-001", status="completed",
                      depends_on=["n1"]),
            NodeState(node_id="n3", workflow_id="wf-001",
                      depends_on=["n1", "n2"]),
            NodeState(node_id="n4", workflow_id="wf-001",
                      depends_on=["n3"]),
        ]
        coord.save_node_states("wf-001", nodes)

        ready = coord.get_ready_nodes("wf-001")
        assert ready == ["n3"]  # n3 deps satisfied, n4 blocked by n3

    def test_executing_node_not_ready(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="executing"),
        ]
        coord.save_node_states("wf-001", nodes)

        ready = coord.get_ready_nodes("wf-001")
        assert ready == []


# =========================================================================
# Integration: child results visible through workflow graph
# =========================================================================

class TestWorkflowGraphVisibility:
    """Child results should be visible through the workflow graph."""

    def test_node_states_persisted_in_workflow_json(self, tmp_path):
        from agents.coordination import NodeState

        bb = create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed",
                      output_ref="OUTPUT/n1_result.md"),
        ]
        coord.save_node_states("wf-001", nodes)

        # Read raw workflow JSON to verify node states are persisted
        wf = bb.get_workflow("wf-001")
        assert "node_states" in wf
        assert "n1" in wf["node_states"]
        assert wf["node_states"]["n1"]["status"] == "completed"

    def test_complete_lifecycle_visible(self, tmp_path):
        """Full lifecycle: create → claim → complete, all visible in state."""
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Create nodes
        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001"),
            NodeState(node_id="n2", workflow_id="wf-001", depends_on=["n1"]),
        ]
        coord.save_node_states("wf-001", nodes)

        # Claim and complete n1
        coord.claim_node("wf-001", "n1", "agent-1")
        coord.complete_node("wf-001", "n1", "agent-1", "OUTPUT/n1.md")

        # Now n2 should be ready
        ready = coord.get_ready_nodes("wf-001")
        assert ready == ["n2"]

        # Claim and complete n2
        coord.claim_node("wf-001", "n2", "agent-2")
        coord.complete_node("wf-001", "n2", "agent-2", "OUTPUT/n2.md")

        # Final state
        state = coord.resume_workflow("wf-001")
        assert set(state["completed_nodes"]) == {"n1", "n2"}
        assert len(state["pending_nodes"]) == 0
        assert len(state["stale_nodes"]) == 0

    def test_graph_builder_shows_coordination_node_states(self, tmp_path):
        """WorkflowGraphBuilder surfaces node_states from CoordinationLayer."""
        from agents.coordination import NodeState
        from agents.workflow_graph import WorkflowGraphBuilder, render_markdown

        bb = create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Coordination-only nodes (no delegation records)
        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed",
                      assigned_agent="agent-1", output_ref="OUTPUT/n1.md"),
            NodeState(node_id="n2", workflow_id="wf-001", status="executing",
                      assigned_agent="agent-2"),
        ]
        coord.save_node_states("wf-001", nodes)

        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf-001")

        # Both coordination nodes should appear in the graph
        assert graph.root is not None
        child_ids = {c.node_id for c in graph.root.children}
        assert "n1" in child_ids
        assert "n2" in child_ids

        # Check status propagation
        n1_node = next(c for c in graph.root.children if c.node_id == "n1")
        assert n1_node.status == "completed"
        assert n1_node.node_type == "coordination"
        assert n1_node.metadata.get("output_ref") == "OUTPUT/n1.md"

        n2_node = next(c for c in graph.root.children if c.node_id == "n2")
        assert n2_node.status == "executing"

        # Edges should exist
        edge_targets = {e.target for e in graph.edges}
        assert "n1" in edge_targets
        assert "n2" in edge_targets

    def test_graph_builder_merges_coordination_into_delegations(self, tmp_path):
        """When both delegation and node_state exist, coordination status wins."""
        from agents.blackboard import Delegation
        from agents.coordination import NodeState
        from agents.workflow_graph import WorkflowGraphBuilder

        bb = create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Create a delegation (status=pending)
        bb.create_delegation(Delegation(
            workflow_id="wf-001", subtask_id="sub-1",
            agent_id="agent-1", role="coder", goal="write code",
            status="pending",
        ))

        # Coordination layer advances it to completed
        nodes = [
            NodeState(node_id="sub-1", workflow_id="wf-001",
                      status="completed", assigned_agent="agent-1",
                      output_ref="OUTPUT/sub1.md"),
        ]
        coord.save_node_states("wf-001", nodes)

        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf-001")

        # Delegation node should show coordination status (completed, not pending)
        sub1 = next(c for c in graph.root.children if c.node_id == "sub-1")
        assert sub1.status == "completed"
        assert sub1.node_type == "delegation"
        assert sub1.metadata.get("output_ref") == "OUTPUT/sub1.md"

    def test_graph_markdown_renders_coordination_output_ref(self, tmp_path):
        """Markdown render includes output_ref from coordination layer."""
        from agents.coordination import NodeState
        from agents.workflow_graph import WorkflowGraphBuilder, render_markdown

        bb = create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="completed",
                      assigned_agent="agent-1", output_ref="OUTPUT/result.md"),
        ]
        coord.save_node_states("wf-001", nodes)

        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf-001")
        md = render_markdown(graph)

        assert "OUTPUT/result.md" in md
        assert "[OK]" in md

    def test_graph_shows_failed_node_with_error(self, tmp_path):
        """Failed coordination nodes show error in the graph."""
        from agents.coordination import NodeState
        from agents.workflow_graph import WorkflowGraphBuilder, render_markdown

        bb = create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        nodes = [
            NodeState(node_id="n1", workflow_id="wf-001", status="failed",
                      assigned_agent="agent-1", error="timeout after 300s"),
        ]
        coord.save_node_states("wf-001", nodes)

        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf-001")

        n1 = next(c for c in graph.root.children if c.node_id == "n1")
        assert n1.status == "failed"
        assert n1.metadata.get("error") == "timeout after 300s"

        md = render_markdown(graph)
        assert "timeout after 300s" in md
        assert "[FAIL]" in md


# =========================================================================
# Edge cases
# =========================================================================

class TestEdgeCases:

    def test_update_node_creates_if_not_exists(self, tmp_path):
        create_test_workflow(tmp_path)
        coord = make_coord(tmp_path)

        # Update a node that doesn't exist yet in node_states
        ns = coord.update_node_state("wf-001", "new-node", {
            "status": "pending",
        })
        assert ns.node_id == "new-node"
        assert ns.workflow_id == "wf-001"

    def test_lease_persists_across_instances(self, tmp_path):
        """Leases survive CoordinationLayer re-instantiation (simulate restart)."""
        create_test_workflow(tmp_path)
        coord1 = make_coord(tmp_path)
        coord1.acquire_lease("wf-001", "n1", "agent-1")

        # Simulate restart: new instance
        coord2 = make_coord(tmp_path)
        lease = coord2.get_lease("wf-001", "n1")
        assert lease is not None
        assert lease.holder == "agent-1"

    def test_node_states_persist_across_instances(self, tmp_path):
        from agents.coordination import NodeState

        create_test_workflow(tmp_path)
        coord1 = make_coord(tmp_path)
        coord1.save_node_states("wf-001", [
            NodeState(node_id="n1", workflow_id="wf-001", status="executing"),
        ])

        coord2 = make_coord(tmp_path)
        states = coord2.get_node_states("wf-001")
        assert states["n1"].status == "executing"

    def test_checkpoint_persists_across_instances(self, tmp_path):
        create_test_workflow(tmp_path)
        coord1 = make_coord(tmp_path)
        coord1.save_checkpoint("wf-001", {"step": 42})

        coord2 = make_coord(tmp_path)
        cp = coord2.get_latest_checkpoint("wf-001")
        assert cp["step"] == 42
