"""Tests for agents.workflow_graph — graph build + render."""

import json
import tempfile
import time
from pathlib import Path

import pytest

# Allow running from repo root
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.blackboard import (
    Blackboard, Delegation, WorkflowState,
    AgentRuntimeState, ChildContract,
)
from agents.workflow_graph import (
    WorkflowGraphBuilder, WorkflowGraph, GraphNode,
    render_markdown, render_json, render_ascii_tree,
    workflow_graph_markdown, workflow_graph_json,
    all_workflows_summary,
)


@pytest.fixture
def tmp_base(tmp_path):
    """Create a temp directory mimicking nova-core layout."""
    (tmp_path / "STATE" / "workflows").mkdir(parents=True)
    (tmp_path / "STATE" / "delegations").mkdir(parents=True)
    (tmp_path / "STATE" / "agents" / "runtime").mkdir(parents=True)
    (tmp_path / "WORK" / "agents" / "contracts").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def bb(tmp_base):
    return Blackboard(base=tmp_base)


def _make_workflow(bb: Blackboard, wf_id="wf_001", task_id="task_test"):
    wf = WorkflowState(
        workflow_id=wf_id,
        task_id=task_id,
        status="executing",
    )
    bb.create_workflow(wf)
    return wf


def _make_delegation(bb: Blackboard, wf_id="wf_001", subtask_id="sub_1",
                     agent_id="research_001", role="research",
                     goal="Gather data", status="completed"):
    d = Delegation(
        workflow_id=wf_id,
        subtask_id=subtask_id,
        agent_id=agent_id,
        role=role,
        goal=goal,
        status=status,
        claimed_at=time.time() - 30,
        completed_at=time.time() if status == "completed" else None,
    )
    bb.create_delegation(d)
    # Update workflow delegation list
    wf = bb.get_workflow(wf_id)
    delegs = wf.get("delegations", [])
    delegs.append(subtask_id)
    bb.update_workflow(wf_id, {"delegations": delegs})
    return d


def _make_contract(bb: Blackboard, subtask_id="sub_1",
                   agent_id="research_001", wf_id="wf_001",
                   summary="Research completed successfully"):
    c = ChildContract(
        agent_id=agent_id,
        workflow_id=wf_id,
        subtask_id=subtask_id,
        role="research",
        status="completed",
        summary=summary,
        artifacts=["OUTPUT/research_results.md"],
        verification={"result": "pass", "confidence": "high"},
    )
    bb.write_child_contract(c)
    return c


def _make_agent_state(bb: Blackboard, agent_id="research_001",
                      wf_id="wf_001", status="completed"):
    s = AgentRuntimeState(
        agent_id=agent_id,
        workflow_id=wf_id,
        status=status,
        action_count=5,
    )
    bb.set_agent_state(s)
    return s


# ---------------------------------------------------------------------------
# Build tests
# ---------------------------------------------------------------------------

class TestGraphBuild:
    def test_build_missing_workflow(self, bb):
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("nonexistent")
        assert graph.root is not None
        assert graph.root.status == "unknown"

    def test_build_empty_workflow(self, bb):
        _make_workflow(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        assert graph.root is not None
        assert graph.root.status == "executing"
        assert graph.root.children == []

    def test_build_with_delegation(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        assert len(graph.root.children) == 1
        deleg_node = graph.root.children[0]
        assert deleg_node.node_type == "delegation"
        assert deleg_node.status == "completed"

    def test_build_with_contract(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        _make_contract(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        deleg_node = graph.root.children[0]
        contract_nodes = [c for c in deleg_node.children
                          if c.node_type == "contract"]
        assert len(contract_nodes) == 1
        assert contract_nodes[0].status == "completed"
        assert "research_results.md" in str(contract_nodes[0].metadata)

    def test_build_with_agent_state(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        _make_agent_state(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        deleg_node = graph.root.children[0]
        agent_nodes = [c for c in deleg_node.children
                       if c.node_type == "agent"]
        assert len(agent_nodes) == 1
        assert agent_nodes[0].metadata["action_count"] == 5

    def test_build_multiple_delegations(self, bb):
        _make_workflow(bb)
        _make_delegation(bb, subtask_id="sub_1", agent_id="research_001",
                         role="research", goal="Research phase")
        _make_delegation(bb, subtask_id="sub_2", agent_id="coder_001",
                         role="coder", goal="Implement changes", status="executing")
        _make_contract(bb, subtask_id="sub_1")
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        assert len(graph.root.children) == 2
        # First delegation has contract, second doesn't
        d1 = graph.root.children[0]
        d2 = graph.root.children[1]
        assert any(c.node_type == "contract" for c in d1.children)
        assert not any(c.node_type == "contract" for c in d2.children)

    def test_build_failed_delegation(self, bb):
        _make_workflow(bb)
        d = _make_delegation(bb, status="failed")
        bb.update_delegation("wf_001", "sub_1", {"error": "timeout"})
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        deleg_node = graph.root.children[0]
        assert deleg_node.status == "failed"
        assert deleg_node.metadata.get("error") == "timeout"

    def test_edges_created(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        _make_contract(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        assert len(graph.edges) == 2  # workflow→delegation, delegation→contract
        assert graph.edges[0].edge_type == "delegates"
        assert graph.edges[1].edge_type == "produces"

    def test_build_all(self, bb):
        _make_workflow(bb, wf_id="wf_001")
        _make_workflow(bb, wf_id="wf_002", task_id="task_2")
        builder = WorkflowGraphBuilder(bb)
        graphs = builder.build_all()
        assert len(graphs) == 2


# ---------------------------------------------------------------------------
# Render tests
# ---------------------------------------------------------------------------

class TestRenderMarkdown:
    def test_render_empty_workflow(self, bb):
        _make_workflow(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        md = render_markdown(graph)
        assert "Workflow" in md
        assert "executing" in md
        assert "No delegations" in md

    def test_render_with_results(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        _make_contract(bb)
        _make_agent_state(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        md = render_markdown(graph)
        assert "Research completed successfully" in md
        assert "[OK]" in md
        assert "research_results.md" in md
        assert "Verification" in md

    def test_render_failed_subtask(self, bb):
        _make_workflow(bb)
        _make_delegation(bb, status="failed")
        bb.update_delegation("wf_001", "sub_1", {"error": "budget exceeded"})
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        md = render_markdown(graph)
        assert "[FAIL]" in md
        assert "budget exceeded" in md


class TestRenderJSON:
    def test_json_roundtrip(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        _make_contract(bb)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        j = render_json(graph)
        data = json.loads(j)
        assert data["workflow_id"] == "wf_001"
        assert data["root"]["type"] == "workflow"
        assert len(data["root"]["children"]) == 1
        assert len(data["edges"]) == 2


class TestRenderASCII:
    def test_ascii_tree(self, bb):
        _make_workflow(bb)
        _make_delegation(bb, subtask_id="sub_1", goal="Research task")
        _make_delegation(bb, subtask_id="sub_2", agent_id="coder_001",
                         role="coder", goal="Code task", status="executing")
        _make_contract(bb, subtask_id="sub_1")
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        tree = render_ascii_tree(graph)
        assert "└──" in tree or "├──" in tree
        assert "[OK]" in tree
        assert "[RUN]" in tree


class TestConvenienceFunctions:
    def test_one_shot_markdown(self, bb):
        _make_workflow(bb)
        _make_delegation(bb)
        _make_contract(bb)
        md = workflow_graph_markdown("wf_001", bb)
        assert "Research completed" in md

    def test_one_shot_json(self, bb):
        _make_workflow(bb)
        j = workflow_graph_json("wf_001", bb)
        data = json.loads(j)
        assert data["workflow_id"] == "wf_001"

    def test_all_workflows_summary(self, bb):
        _make_workflow(bb, wf_id="wf_001")
        _make_workflow(bb, wf_id="wf_002")
        md = all_workflows_summary(bb)
        assert "wf_001" in md
        assert "wf_002" in md


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_contract_without_delegation(self, bb):
        """Contract exists but delegation is missing — should not crash."""
        _make_workflow(bb)
        _make_contract(bb, subtask_id="orphan_sub")
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        # Graph builds without error, orphan contract just not shown
        assert graph.root is not None

    def test_none_graph_root(self):
        graph = WorkflowGraph(workflow_id="empty")
        graph.root = None
        md = render_markdown(graph)
        assert "No data available" in md
        tree = render_ascii_tree(graph)
        assert "empty" in tree

    def test_long_goal_truncated(self, bb):
        _make_workflow(bb)
        long_goal = "A" * 200
        _make_delegation(bb, goal=long_goal)
        builder = WorkflowGraphBuilder(bb)
        graph = builder.build("wf_001")
        label = graph.root.children[0].label
        # Goal is truncated to 60 chars in the label
        assert len(label) < 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
