"""Tests for P11 RuntimeBridge — integration layer between P11 and existing subsystems."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.runtime_bridge import RuntimeBridge

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeBlackboard:
    """Minimal Blackboard stub for testing."""

    def __init__(self):
        self.agent_states = {}
        self.workflow_updates = {}

    def set_agent_state(self, state):
        self.agent_states[state.agent_id] = state

    def update_workflow(self, workflow_id, updates):
        self.workflow_updates[workflow_id] = updates


class FakeCoordination:
    """Minimal CoordinationLayer stub for testing."""

    def __init__(self):
        self.claimed = {}
        self.completed = {}
        self.failed = {}
        self.checkpoints = {}

    def claim_node(self, workflow_id, node_id, agent_id):
        self.claimed[(workflow_id, node_id)] = agent_id

    def complete_node(self, workflow_id, node_id, agent_id, output_ref=None):
        self.completed[(workflow_id, node_id)] = (agent_id, output_ref)

    def fail_node(self, workflow_id, node_id, agent_id, error):
        self.failed[(workflow_id, node_id)] = (agent_id, error)

    def save_checkpoint(self, workflow_id, checkpoint):
        self.checkpoints[workflow_id] = checkpoint


class FakePolicyDecision:
    def __init__(self, allowed):
        self.allowed = allowed


class FakePolicyEngine:
    """Minimal PolicyEngine stub for testing."""

    def __init__(self):
        self.denied_tools = set()

    def check_tool_access(self, agent_id, tool_name):
        if tool_name in self.denied_tools:
            raise RuntimeError(f"Tool {tool_name} denied")
        return FakePolicyDecision(allowed=True)


class FakeAgentRuntimeState:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.agent_id = kwargs.get("agent_id", "")


# ---------------------------------------------------------------------------
# Construction / discovery
# ---------------------------------------------------------------------------


class TestBridgeConstruction:
    def test_all_none_when_no_subsystems(self):
        """Bridge works with no subsystems available."""
        with (
            patch("agents.runtime_bridge._try_import_blackboard", return_value=(None, None, None)),
            patch("agents.runtime_bridge._try_import_coordination", return_value=None),
            patch("agents.runtime_bridge._try_import_policy_engine", return_value=None),
        ):
            bridge = RuntimeBridge()
        assert not bridge.has_blackboard
        assert not bridge.has_coordination
        assert not bridge.has_policy_engine

    def test_explicit_blackboard(self):
        bb = FakeBlackboard()
        with (
            patch("agents.runtime_bridge._try_import_blackboard", return_value=(None, FakeAgentRuntimeState, None)),
            patch("agents.runtime_bridge._try_import_coordination", return_value=None),
            patch("agents.runtime_bridge._try_import_policy_engine", return_value=None),
        ):
            bridge = RuntimeBridge(blackboard=bb)
        assert bridge.has_blackboard

    def test_explicit_coordination(self):
        coord = FakeCoordination()
        with (
            patch("agents.runtime_bridge._try_import_blackboard", return_value=(None, None, None)),
            patch("agents.runtime_bridge._try_import_coordination", return_value=None),
            patch("agents.runtime_bridge._try_import_policy_engine", return_value=None),
        ):
            bridge = RuntimeBridge(coordination=coord)
        assert bridge.has_coordination

    def test_explicit_policy_engine(self):
        pe = FakePolicyEngine()
        with (
            patch("agents.runtime_bridge._try_import_blackboard", return_value=(None, None, None)),
            patch("agents.runtime_bridge._try_import_coordination", return_value=None),
            patch("agents.runtime_bridge._try_import_policy_engine", return_value=None),
        ):
            bridge = RuntimeBridge(policy_engine=pe)
        assert bridge.has_policy_engine

    def test_get_status(self):
        bb = FakeBlackboard()
        pe = FakePolicyEngine()
        with (
            patch("agents.runtime_bridge._try_import_blackboard", return_value=(None, FakeAgentRuntimeState, None)),
            patch("agents.runtime_bridge._try_import_coordination", return_value=None),
            patch("agents.runtime_bridge._try_import_policy_engine", return_value=None),
        ):
            bridge = RuntimeBridge(blackboard=bb, policy_engine=pe)
        status = bridge.get_status()
        assert status["blackboard"] is True
        assert status["coordination"] is False
        assert status["policy_engine"] is True


# ---------------------------------------------------------------------------
# Blackboard sync
# ---------------------------------------------------------------------------


def _make_bridge(bb=None, coord=None, pe=None):
    """Helper to create a bridge with mocked imports."""
    with (
        patch("agents.runtime_bridge._try_import_blackboard", return_value=(None, FakeAgentRuntimeState, None)),
        patch("agents.runtime_bridge._try_import_coordination", return_value=None),
        patch("agents.runtime_bridge._try_import_policy_engine", return_value=None),
    ):
        return RuntimeBridge(blackboard=bb, coordination=coord, policy_engine=pe)


class TestBlackboardSync:
    def test_sync_agent_state(self):
        bb = FakeBlackboard()
        bridge = _make_bridge(bb=bb)
        bridge.sync_agent_state("agent-1", "wf-1", "executing")
        assert "agent-1" in bb.agent_states
        state = bb.agent_states["agent-1"]
        assert state.status == "executing"
        assert state.workflow_id == "wf-1"

    def test_sync_agent_state_no_blackboard(self):
        """Should not raise when blackboard is absent."""
        bridge = _make_bridge()
        bridge.sync_agent_state("agent-1", "wf-1", "executing")  # no-op

    def test_sync_agent_state_with_error(self):
        bb = FakeBlackboard()
        bridge = _make_bridge(bb=bb)
        bridge.sync_agent_state("agent-1", "wf-1", "failed", error="boom")
        state = bb.agent_states["agent-1"]
        assert state.error == "boom"

    def test_sync_agent_state_blackboard_exception(self):
        """Should swallow exceptions from blackboard."""
        bb = MagicMock()
        bb.set_agent_state.side_effect = RuntimeError("disk full")
        bridge = _make_bridge(bb=bb)
        bridge.sync_agent_state("agent-1", "wf-1", "executing")  # should not raise

    def test_sync_workflow_state(self):
        bb = FakeBlackboard()
        bridge = _make_bridge(bb=bb)
        bridge.sync_workflow_state("wf-1", "running")
        assert bb.workflow_updates["wf-1"] == {"status": "running"}

    def test_sync_workflow_state_with_halt_reason(self):
        bb = FakeBlackboard()
        bridge = _make_bridge(bb=bb)
        bridge.sync_workflow_state("wf-1", "halted", halt_reason="timeout")
        assert bb.workflow_updates["wf-1"] == {"status": "halted", "halt_reason": "timeout"}

    def test_sync_workflow_state_no_blackboard(self):
        bridge = _make_bridge()
        bridge.sync_workflow_state("wf-1", "running")  # no-op

    def test_sync_workflow_state_exception(self):
        bb = MagicMock()
        bb.update_workflow.side_effect = RuntimeError("oops")
        bridge = _make_bridge(bb=bb)
        bridge.sync_workflow_state("wf-1", "running")  # should not raise


# ---------------------------------------------------------------------------
# CoordinationLayer delegation
# ---------------------------------------------------------------------------


class TestCoordinationDelegation:
    def test_claim_node_success(self):
        coord = FakeCoordination()
        bridge = _make_bridge(coord=coord)
        assert bridge.claim_node("wf-1", "node-1", "agent-1") is True
        assert coord.claimed[("wf-1", "node-1")] == "agent-1"

    def test_claim_node_no_coordination(self):
        """No coordination → always returns True."""
        bridge = _make_bridge()
        assert bridge.claim_node("wf-1", "node-1", "agent-1") is True

    def test_claim_node_exception(self):
        """Coordination failure → returns False."""
        coord = MagicMock()
        coord.claim_node.side_effect = RuntimeError("lease conflict")
        bridge = _make_bridge(coord=coord)
        assert bridge.claim_node("wf-1", "node-1", "agent-1") is False

    def test_complete_node(self):
        coord = FakeCoordination()
        bridge = _make_bridge(coord=coord)
        bridge.complete_node("wf-1", "node-1", "agent-1", output_ref="ref-123")
        assert coord.completed[("wf-1", "node-1")] == ("agent-1", "ref-123")

    def test_complete_node_no_coordination(self):
        bridge = _make_bridge()
        bridge.complete_node("wf-1", "node-1", "agent-1")  # no-op

    def test_complete_node_exception(self):
        coord = MagicMock()
        coord.complete_node.side_effect = RuntimeError("oops")
        bridge = _make_bridge(coord=coord)
        bridge.complete_node("wf-1", "node-1", "agent-1")  # should not raise

    def test_fail_node(self):
        coord = FakeCoordination()
        bridge = _make_bridge(coord=coord)
        bridge.fail_node("wf-1", "node-1", "agent-1", "timeout")
        assert coord.failed[("wf-1", "node-1")] == ("agent-1", "timeout")

    def test_fail_node_no_coordination(self):
        bridge = _make_bridge()
        bridge.fail_node("wf-1", "node-1", "agent-1", "timeout")  # no-op

    def test_fail_node_exception(self):
        coord = MagicMock()
        coord.fail_node.side_effect = RuntimeError("oops")
        bridge = _make_bridge(coord=coord)
        bridge.fail_node("wf-1", "node-1", "agent-1", "timeout")  # should not raise

    def test_save_checkpoint(self):
        coord = FakeCoordination()
        bridge = _make_bridge(coord=coord)
        bridge.save_checkpoint("wf-1", {"nodes": ["a", "b"]})
        assert coord.checkpoints["wf-1"] == {"nodes": ["a", "b"]}

    def test_save_checkpoint_no_coordination(self):
        bridge = _make_bridge()
        bridge.save_checkpoint("wf-1", {"nodes": []})  # no-op

    def test_save_checkpoint_exception(self):
        coord = MagicMock()
        coord.save_checkpoint.side_effect = RuntimeError("oops")
        bridge = _make_bridge(coord=coord)
        bridge.save_checkpoint("wf-1", {"nodes": []})  # should not raise


# ---------------------------------------------------------------------------
# PolicyEngine delegation
# ---------------------------------------------------------------------------


class TestPolicyDelegation:
    def test_check_tool_allowed(self):
        pe = FakePolicyEngine()
        bridge = _make_bridge(pe=pe)
        assert bridge.check_tool_access("agent-1", "read_file") is True

    def test_check_tool_denied(self):
        pe = FakePolicyEngine()
        pe.denied_tools.add("delete_file")
        bridge = _make_bridge(pe=pe)
        assert bridge.check_tool_access("agent-1", "delete_file") is False

    def test_check_tool_no_policy_engine(self):
        """No policy engine → always returns True."""
        bridge = _make_bridge()
        assert bridge.check_tool_access("agent-1", "anything") is True


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_all_methods_no_op_without_subsystems(self):
        """Every method should work without any subsystem connected."""
        bridge = _make_bridge()
        bridge.sync_agent_state("a", "w", "idle")
        bridge.sync_workflow_state("w", "created")
        assert bridge.claim_node("w", "n", "a") is True
        bridge.complete_node("w", "n", "a")
        bridge.fail_node("w", "n", "a", "err")
        bridge.save_checkpoint("w", {})
        assert bridge.check_tool_access("a", "tool") is True
        status = bridge.get_status()
        assert all(v is False for v in status.values())

    def test_mixed_subsystems(self):
        """Some subsystems present, others absent."""
        coord = FakeCoordination()
        bridge = _make_bridge(coord=coord)
        # Coordination works
        assert bridge.claim_node("w", "n", "a") is True
        # Blackboard no-ops
        bridge.sync_agent_state("a", "w", "idle")
        # Policy no-ops (returns True)
        assert bridge.check_tool_access("a", "tool") is True
