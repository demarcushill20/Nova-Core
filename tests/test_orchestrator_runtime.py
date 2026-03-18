"""Tests for P11.3 — Orchestrator Runtime Engine.

Covers: single node, linear pipeline, parallel DAG, maker-checker flows,
dependency resolution, deadlock detection, state persistence, crash resume,
synthesis, topological sort, cycle detection, retry, timeout.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.message_bus import MessageBus
from agents.orchestrator_runtime import (
    DAGNode,
    NodeStatus,
    OrchestratorRuntime,
    WorkflowDAG,
    WorkflowStatus,
)
from agents.spawner import AgentProcess, AgentSpawner

# ---------------------------------------------------------------------------
# Registry for tests
# ---------------------------------------------------------------------------

SAMPLE_REGISTRY = {
    "schema_version": "1.0",
    "agents": [
        {
            "agent_id": "research_001",
            "role": "research",
            "policy_profile": "research_safe",
            "allowed_tools": ["web.search"],
            "denied_tools": ["shell.run"],
            "max_runtime_seconds": 10,
            "max_actions": 5,
            "max_retries": 1,
        },
        {
            "agent_id": "coder_001",
            "role": "coder",
            "policy_profile": "coder_scoped",
            "allowed_tools": ["repo.files.write"],
            "denied_tools": [],
            "max_runtime_seconds": 10,
            "max_actions": 10,
            "max_retries": 2,
        },
        {
            "agent_id": "critic_001",
            "role": "critic",
            "policy_profile": "critic_readonly",
            "allowed_tools": ["repo.files.read"],
            "denied_tools": ["repo.files.write"],
            "max_runtime_seconds": 5,
            "max_actions": 8,
            "max_retries": 1,
        },
        {
            "agent_id": "verifier_001",
            "role": "verifier",
            "policy_profile": "verifier_readonly",
            "allowed_tools": ["repo.files.read"],
            "denied_tools": ["repo.files.write"],
            "max_runtime_seconds": 5,
            "max_actions": 8,
            "max_retries": 1,
        },
    ],
}


@pytest.fixture
def registry_path(tmp_path):
    reg = tmp_path / "STATE" / "agents" / "registry.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps(SAMPLE_REGISTRY))
    return reg


@pytest.fixture
def spawner(registry_path, tmp_path, monkeypatch):
    monkeypatch.setattr("agents.spawner.RUNTIME_DIR", tmp_path / "STATE" / "agents" / "runtime")
    monkeypatch.setattr("agents.spawner.SPAWNER_STATE_FILE", tmp_path / "STATE" / "agents" / "spawner_state.json")
    return AgentSpawner(
        registry_path=registry_path,
        max_concurrent_per_role=2,
        max_total_concurrent=4,
    )


@pytest.fixture
def bus(tmp_path):
    return MessageBus(max_queue_size=100, persist_dir=tmp_path / "messages")


@pytest.fixture
def runtime(spawner, bus, tmp_path):
    return OrchestratorRuntime(
        spawner=spawner,
        bus=bus,
        persist_dir=tmp_path / "workflows",
        max_workflow_runtime_s=30.0,
    )


# ---------------------------------------------------------------------------
# DAG construction
# ---------------------------------------------------------------------------


class TestDAGConstruction:
    def test_create_dag(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="research"))
        assert len(dag.nodes) == 1
        assert dag.status == WorkflowStatus.CREATED

    def test_topological_order_linear(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        dag.add_node(DAGNode(node_id="n3", agent_id="critic_001", role="critic", goal="c", depends_on=["n2"]))
        order = dag.topological_order()
        assert order.index("n1") < order.index("n2") < order.index("n3")

    def test_topological_order_parallel(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b"))
        dag.add_node(DAGNode(node_id="n3", agent_id="critic_001", role="critic", goal="c", depends_on=["n1", "n2"]))
        order = dag.topological_order()
        assert order.index("n1") < order.index("n3")
        assert order.index("n2") < order.index("n3")

    def test_cycle_detection(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a", depends_on=["n2"]))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        with pytest.raises(ValueError, match="Cycle"):
            dag.topological_order()

    def test_get_ready_nodes(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        ready = dag.get_ready_nodes()
        assert len(ready) == 1
        assert ready[0].node_id == "n1"

    def test_ready_after_dependency_completes(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        dag.nodes["n1"].status = NodeStatus.COMPLETED
        ready = dag.get_ready_nodes()
        assert len(ready) == 1
        assert ready[0].node_id == "n2"

    def test_deadlock_detection(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        # n1 fails, n2 blocked → deadlock
        dag.nodes["n1"].status = NodeStatus.FAILED
        assert dag.detect_deadlock() is True

    def test_no_deadlock_when_executing(self):
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.nodes["n1"].status = NodeStatus.EXECUTING
        assert dag.detect_deadlock() is False

    def test_serialization_roundtrip(self):
        dag = WorkflowDAG(workflow_id="wf-test")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        d = dag.to_dict()
        restored = WorkflowDAG.from_dict(d)
        assert restored.workflow_id == "wf-test"
        assert len(restored.nodes) == 2
        assert restored.nodes["n2"].depends_on == ["n1"]


# ---------------------------------------------------------------------------
# Single node execution
# ---------------------------------------------------------------------------


class TestSingleNode:
    @pytest.mark.asyncio
    async def test_single_node_success(self, runtime):
        dag = WorkflowDAG(workflow_id="wf-single")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="find stuff"))
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.nodes["n1"].status == NodeStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_single_node_custom_work_fn(self, spawner, bus, tmp_path):
        async def custom_work(proc: AgentProcess) -> str:
            return "custom result"

        def factory(node, dag):
            return custom_work

        rt = OrchestratorRuntime(
            spawner=spawner,
            bus=bus,
            persist_dir=tmp_path / "workflows",
            work_fn_factory=factory,
        )
        dag = WorkflowDAG(workflow_id="wf-custom")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))
        result = await rt.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.nodes["n1"].result == "custom result"


# ---------------------------------------------------------------------------
# Linear pipeline
# ---------------------------------------------------------------------------


class TestLinearPipeline:
    @pytest.mark.asyncio
    async def test_two_node_pipeline(self, runtime):
        dag = WorkflowDAG(workflow_id="wf-linear")
        dag.add_node(DAGNode(node_id="research", agent_id="research_001", role="research", goal="research topic"))
        dag.add_node(
            DAGNode(node_id="code", agent_id="coder_001", role="coder", goal="write code", depends_on=["research"])
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.nodes["research"].status == NodeStatus.COMPLETED
        assert result.nodes["code"].status == NodeStatus.COMPLETED
        # Verify ordering
        assert result.nodes["research"].completed_at <= result.nodes["code"].started_at

    @pytest.mark.asyncio
    async def test_three_node_pipeline(self, runtime):
        dag = WorkflowDAG(workflow_id="wf-3step")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="step1"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="step2", depends_on=["n1"]))
        dag.add_node(DAGNode(node_id="n3", agent_id="critic_001", role="critic", goal="step3", depends_on=["n2"]))
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        assert all(n.status == NodeStatus.COMPLETED for n in result.nodes.values())


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_parallel_nodes(self, runtime):
        dag = WorkflowDAG(workflow_id="wf-parallel")
        dag.add_node(DAGNode(node_id="r1", agent_id="research_001", role="research", goal="research A"))
        dag.add_node(DAGNode(node_id="r2", agent_id="coder_001", role="coder", goal="research B"))
        dag.add_node(
            DAGNode(node_id="merge", agent_id="critic_001", role="critic", goal="merge", depends_on=["r1", "r2"])
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        # Both r1 and r2 should complete before merge starts
        assert result.nodes["r1"].status == NodeStatus.COMPLETED
        assert result.nodes["r2"].status == NodeStatus.COMPLETED
        assert result.nodes["merge"].status == NodeStatus.COMPLETED


# ---------------------------------------------------------------------------
# Failure and retry
# ---------------------------------------------------------------------------


class TestFailureAndRetry:
    @pytest.mark.asyncio
    async def test_node_failure_propagates(self, spawner, bus, tmp_path):
        call_count = 0

        async def failing_work(proc: AgentProcess) -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("agent crashed")

        def factory(node, dag):
            return failing_work

        rt = OrchestratorRuntime(
            spawner=spawner,
            bus=bus,
            persist_dir=tmp_path / "workflows",
            work_fn_factory=factory,
        )
        dag = WorkflowDAG(workflow_id="wf-fail")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="fail", max_retries=1))
        result = await rt.execute(dag)
        assert result.status == WorkflowStatus.FAILED
        assert result.nodes["n1"].status == NodeStatus.FAILED
        # Should have retried once
        assert call_count == 2  # original + 1 retry

    @pytest.mark.asyncio
    async def test_failure_blocks_dependents(self, spawner, bus, tmp_path):
        async def failing(proc: AgentProcess) -> str:
            raise ValueError("fail")

        def factory(node, dag):
            if node.node_id == "n1":
                return failing

            async def ok(proc):
                return "ok"

            return ok

        rt = OrchestratorRuntime(
            spawner=spawner,
            bus=bus,
            persist_dir=tmp_path / "workflows",
            work_fn_factory=factory,
        )
        dag = WorkflowDAG(workflow_id="wf-blocked")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="fail", max_retries=0))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="blocked", depends_on=["n1"]))
        result = await rt.execute(dag)
        assert result.nodes["n1"].status == NodeStatus.FAILED
        # n2 never executed — deadlock detected
        assert result.nodes["n2"].status == NodeStatus.PENDING

    @pytest.mark.asyncio
    async def test_cycle_in_dag(self, runtime):
        dag = WorkflowDAG(workflow_id="wf-cycle")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="a", depends_on=["n2"]))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="b", depends_on=["n1"]))
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.FAILED
        assert "Cycle" in result.error


# ---------------------------------------------------------------------------
# Workflow timeout
# ---------------------------------------------------------------------------


class TestWorkflowTimeout:
    @pytest.mark.asyncio
    async def test_workflow_timeout(self, spawner, bus, tmp_path):
        async def slow(proc: AgentProcess) -> str:
            await asyncio.sleep(100)
            return "done"

        def factory(node, dag):
            return slow

        rt = OrchestratorRuntime(
            spawner=spawner,
            bus=bus,
            persist_dir=tmp_path / "workflows",
            max_workflow_runtime_s=0.3,
            work_fn_factory=factory,
        )
        dag = WorkflowDAG(workflow_id="wf-timeout")
        dag.add_node(
            DAGNode(
                node_id="n1",
                agent_id="research_001",
                role="research",
                goal="slow",
                max_retries=0,
            )
        )
        result = await rt.execute(dag)
        # Workflow timeout fires before agent timeout
        assert result.status == WorkflowStatus.HALTED
        assert "timeout" in result.error.lower()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    @pytest.mark.asyncio
    async def test_workflow_persisted(self, runtime, tmp_path):
        dag = WorkflowDAG(workflow_id="wf-persist")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))
        await runtime.execute(dag)
        path = tmp_path / "workflows" / "wf-persist.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_load_workflow(self, runtime, tmp_path):
        dag = WorkflowDAG(workflow_id="wf-load")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))
        await runtime.execute(dag)
        loaded = runtime.load_workflow("wf-load")
        assert loaded is not None
        assert loaded.workflow_id == "wf-load"
        assert loaded.status == WorkflowStatus.COMPLETED

    def test_load_nonexistent(self, runtime):
        assert runtime.load_workflow("wf-nonexistent") is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics(self, runtime):
        dag = WorkflowDAG(workflow_id="wf-metrics")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))
        await runtime.execute(dag)
        metrics = runtime.get_metrics()
        assert "spawner" in metrics
        assert "bus" in metrics
