"""Basic tests for agents/blackboard.py - critical state management with 0% coverage."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the blackboard module
from agents.blackboard import AgentRuntimeState, Blackboard, Delegation


@pytest.fixture
def temp_blackboard():
    """Create a blackboard instance with temporary directories."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        state_dir = temp_path / "STATE"
        work_dir = temp_path / "WORK"

        # Create required directories
        state_dir.mkdir()
        work_dir.mkdir()
        (state_dir / "delegations").mkdir()
        (state_dir / "agents" / "runtime").mkdir(parents=True)
        (state_dir / "workflows").mkdir()
        (state_dir / "contracts").mkdir()
        (work_dir / "agents" / "messages").mkdir(parents=True)
        (work_dir / "agents" / "contracts").mkdir(parents=True)

        # Patch the module paths
        with (
            patch("agents.blackboard.BASE", temp_path),
            patch("agents.blackboard.STATE", state_dir),
            patch("agents.blackboard.WORK", work_dir),
        ):
            yield Blackboard()


class TestAgentRuntimeState:
    """Test AgentRuntimeState data model."""

    def test_creation(self):
        """Test basic AgentRuntimeState creation."""
        state = AgentRuntimeState("agent-001")
        assert state.agent_id == "agent-001"
        assert state.status == "idle"
        assert state.workflow_id is None
        assert state.action_count == 0

    def test_to_dict_excludes_none(self):
        """Test to_dict excludes None values."""
        state = AgentRuntimeState("agent-001")
        result = state.to_dict()
        assert "agent_id" in result
        assert "status" in result
        assert "action_count" in result
        assert "workflow_id" not in result  # Should exclude None values


class TestDelegation:
    """Test Delegation data model."""

    def test_creation(self):
        """Test basic Delegation creation."""
        delegation = Delegation(
            workflow_id="wf-001", subtask_id="task-001", agent_id="agent-001", role="executor", goal="complete task"
        )
        assert delegation.workflow_id == "wf-001"
        assert delegation.subtask_id == "task-001"
        assert delegation.agent_id == "agent-001"
        assert delegation.role == "executor"
        assert delegation.goal == "complete task"


class TestBlackboard:
    """Test Blackboard operations."""

    def test_get_agent_state_missing(self, temp_blackboard):
        """Test getting non-existent agent state returns None."""
        result = temp_blackboard.get_agent_state("nonexistent")
        assert result is None

    def test_set_and_get_agent_state(self, temp_blackboard):
        """Test setting and getting agent state."""
        state = AgentRuntimeState("agent-001", status="executing")
        temp_blackboard.set_agent_state(state)

        read_state_dict = temp_blackboard.get_agent_state("agent-001")
        assert read_state_dict is not None
        assert read_state_dict["agent_id"] == "agent-001"
        assert read_state_dict["status"] == "executing"

    def test_list_delegations_empty(self, temp_blackboard):
        """Test listing delegations when none exist."""
        result = temp_blackboard.list_delegations()
        assert result == []

    def test_create_and_list_delegations(self, temp_blackboard):
        """Test creating and listing delegations."""
        delegation = Delegation(
            workflow_id="wf-001", subtask_id="task-001", agent_id="agent-001", role="executor", goal="complete task"
        )
        temp_blackboard.create_delegation(delegation)

        delegations = temp_blackboard.list_delegations()
        assert len(delegations) == 1
        assert delegations[0]["workflow_id"] == "wf-001"
        assert delegations[0]["subtask_id"] == "task-001"
