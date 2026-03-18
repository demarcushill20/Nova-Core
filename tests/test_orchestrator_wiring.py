"""Tests for P11 full integration wiring.

Verifies that TimeoutManager, ContextAssembler, and RuntimePolicyEnforcer
are properly wired into the OrchestratorRuntime execute loop.
"""

from __future__ import annotations

import json

import pytest

from agents.context_assembler import ContextAssembler
from agents.message_bus import MessageBus
from agents.orchestrator_runtime import (
    DAGNode,
    NodeStatus,
    OrchestratorRuntime,
    WorkflowDAG,
    WorkflowStatus,
)
from agents.runtime_policy import RuntimePolicyEnforcer
from agents.spawner import AgentProcess, AgentSpawner
from agents.timeout_manager import RetryConfig, TimeoutManager

# ---------------------------------------------------------------------------
# Shared registry and fixtures
# ---------------------------------------------------------------------------

SAMPLE_REGISTRY = {
    "schema_version": "1.0",
    "agents": [
        {
            "agent_id": "research_001",
            "role": "research",
            "policy_profile": "default",
            "allowed_tools": ["web.search"],
            "denied_tools": [],
            "max_runtime_seconds": 10,
            "max_actions": 5,
            "max_retries": 2,
        },
        {
            "agent_id": "coder_001",
            "role": "coder",
            "policy_profile": "default",
            "allowed_tools": ["repo.files.write"],
            "denied_tools": [],
            "max_runtime_seconds": 10,
            "max_actions": 10,
            "max_retries": 2,
        },
        {
            "agent_id": "critic_001",
            "role": "critic",
            "policy_profile": "default",
            "allowed_tools": ["repo.files.read"],
            "denied_tools": [],
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
    monkeypatch.setattr("agents.spawner.RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr("agents.spawner.SPAWNER_STATE_FILE", tmp_path / "spawner.json")
    return AgentSpawner(registry_path=registry_path, max_concurrent_per_role=2, max_total_concurrent=4)


@pytest.fixture
def bus(tmp_path):
    return MessageBus(max_queue_size=100, persist_dir=tmp_path / "messages")


# ---------------------------------------------------------------------------
# TimeoutManager wiring tests
# ---------------------------------------------------------------------------


class TestTimeoutManagerWiring:
    """Verify TimeoutManager is consulted during execute loop."""

    @pytest.mark.asyncio
    async def test_success_records_to_breaker(self, spawner, bus, tmp_path):
        """Successful node completion calls record_success on TimeoutManager."""
        timeout_mgr = TimeoutManager(compensation_dir=tmp_path / "comp")
        runtime = OrchestratorRuntime(spawner, bus, persist_dir=tmp_path / "wf", timeout_mgr=timeout_mgr)

        dag = WorkflowDAG(workflow_id="wf-success01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))

        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED

        # Breaker for "research" should have recorded a success
        breaker = timeout_mgr.get_breaker("research")
        assert len(breaker._successes) >= 1

    @pytest.mark.asyncio
    async def test_failure_records_to_breaker(self, spawner, bus, tmp_path):
        """Failed node records failure on TimeoutManager breaker."""
        timeout_mgr = TimeoutManager(compensation_dir=tmp_path / "comp")

        def failing_work_fn(node, dag):
            async def work(proc: AgentProcess):
                raise RuntimeError("simulated failure")

            return work

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            timeout_mgr=timeout_mgr,
            work_fn_factory=failing_work_fn,
        )

        dag = WorkflowDAG(workflow_id="wf-fail01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="fail", max_retries=0))

        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.FAILED

        breaker = timeout_mgr.get_breaker("research")
        assert len(breaker._failures) >= 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_spawn(self, spawner, bus, tmp_path):
        """Open circuit breaker prevents node from spawning — workflow times out."""
        timeout_mgr = TimeoutManager(compensation_dir=tmp_path / "comp")

        # Manually open the breaker for "research"
        breaker = timeout_mgr.get_breaker("research")
        breaker._is_open = True
        breaker._opened_at = 9999999999.0  # far future so it stays open

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            timeout_mgr=timeout_mgr,
            max_workflow_runtime_s=0.5,  # short timeout so test completes quickly
        )

        dag = WorkflowDAG(workflow_id="wf-blocked01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="blocked"))

        # With breaker open, node never spawns → workflow times out
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.HALTED
        assert "timeout" in (result.error or "").lower()
        assert dag.nodes["n1"].status == NodeStatus.PENDING

    @pytest.mark.asyncio
    async def test_non_retryable_error_skips_retry(self, spawner, bus, tmp_path):
        """TimeoutManager.should_retry returns False for non-retryable errors."""
        # Only retry TimeoutError, not RuntimeError
        config = RetryConfig(retryable_errors={"TimeoutError"})
        timeout_mgr = TimeoutManager(retry_config=config, compensation_dir=tmp_path / "comp")

        def failing_work_fn(node, dag):
            async def work(proc: AgentProcess):
                raise RuntimeError("not retryable")

            return work

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            timeout_mgr=timeout_mgr,
            work_fn_factory=failing_work_fn,
        )

        dag = WorkflowDAG(workflow_id="wf-noretry01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="fail", max_retries=3))

        result = await runtime.execute(dag)
        # Should fail immediately without retrying (max_retries=3 but error is non-retryable)
        assert result.status == WorkflowStatus.FAILED
        assert dag.nodes["n1"].retries == 0  # never retried

    @pytest.mark.asyncio
    async def test_retry_storm_halts_workflow(self, spawner, bus, tmp_path):
        """Retry storm detection halts the workflow."""
        config = RetryConfig(max_retries=50, retryable_errors={"RuntimeError"})
        timeout_mgr = TimeoutManager(retry_config=config, compensation_dir=tmp_path / "comp")

        # Pre-populate storm detector to be on the edge
        for _ in range(9):
            timeout_mgr._storm_detector.record_retry()

        def failing_work_fn(node, dag):
            async def work(proc: AgentProcess):
                raise RuntimeError("keep failing")

            return work

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            timeout_mgr=timeout_mgr,
            work_fn_factory=failing_work_fn,
        )

        dag = WorkflowDAG(workflow_id="wf-storm01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="storm", max_retries=50))

        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.HALTED
        assert "storm" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# ContextAssembler wiring tests
# ---------------------------------------------------------------------------


class TestContextAssemblerWiring:
    """Verify ContextAssembler scopes context in the default work_fn."""

    @pytest.mark.asyncio
    async def test_context_assembled_for_research_role(self, spawner, bus, tmp_path):
        """Research role gets only research-scoped context keys."""
        assembler = ContextAssembler(token_budget=8000, persist_dir=tmp_path / "handoffs")
        captured_messages = []

        original_send = bus.send

        async def capture_send(msg):
            captured_messages.append(msg)
            return await original_send(msg)

        bus.send = capture_send

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            context_assembler=assembler,
        )

        dag = WorkflowDAG(workflow_id="wf-ctx01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="find info"))

        await runtime.execute(dag)

        # Verify the task_assign message was sent with context
        assert len(captured_messages) >= 1
        payload = captured_messages[0].payload
        ctx = payload.get("context", {})
        # Research role scope includes "query" but NOT "source_files" (coder-only)
        assert "query" in ctx
        assert "source_files" not in ctx

    @pytest.mark.asyncio
    async def test_upstream_results_in_context(self, spawner, bus, tmp_path):
        """Downstream nodes receive upstream results in context."""
        assembler = ContextAssembler(token_budget=8000, persist_dir=tmp_path / "handoffs")
        captured_messages = []

        original_send = bus.send

        async def capture_send(msg):
            captured_messages.append(msg)
            return await original_send(msg)

        bus.send = capture_send

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            context_assembler=assembler,
        )

        dag = WorkflowDAG(workflow_id="wf-ctx02")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="research X"))
        dag.add_node(DAGNode(node_id="n2", agent_id="coder_001", role="coder", goal="code Y", depends_on=["n1"]))

        await runtime.execute(dag)

        # Find the message sent to the coder (n2)
        coder_msgs = [m for m in captured_messages if m.to_role == "coder"]
        assert len(coder_msgs) >= 1
        ctx = coder_msgs[0].payload.get("context", {})
        # Coder scope includes "upstream_summary"
        assert "upstream_summary" in ctx

    @pytest.mark.asyncio
    async def test_no_assembler_sends_raw_context(self, spawner, bus, tmp_path):
        """Without assembler, full unscoped context is sent."""
        captured_messages = []
        original_send = bus.send

        async def capture_send(msg):
            captured_messages.append(msg)
            return await original_send(msg)

        bus.send = capture_send

        runtime = OrchestratorRuntime(spawner, bus, persist_dir=tmp_path / "wf")

        dag = WorkflowDAG(workflow_id="wf-ctx03")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))

        await runtime.execute(dag)

        assert len(captured_messages) >= 1
        ctx = captured_messages[0].payload.get("context", {})
        # Without assembler, all keys are present (no scoping)
        assert "query" in ctx
        assert "task_description" in ctx
        assert "spec" in ctx


# ---------------------------------------------------------------------------
# RuntimePolicyEnforcer wiring tests
# ---------------------------------------------------------------------------


class TestPolicyEnforcerWiring:
    """Verify RuntimePolicyEnforcer is registered at spawn time."""

    @pytest.mark.asyncio
    async def test_agent_registered_on_spawn(self, spawner, bus, tmp_path):
        """Policy enforcer registers agent when node spawns."""
        enforcer = RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")
        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            policy_enforcer=enforcer,
        )

        dag = WorkflowDAG(workflow_id="wf-pol01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))

        await runtime.execute(dag)

        # Verify the agent was registered in the enforcer's workflow budget
        report = enforcer.get_budget_report("wf-pol01")
        assert report is not None
        assert "research_001" in report["agents"]

    @pytest.mark.asyncio
    async def test_check_tool_proxy_with_enforcer(self, spawner, bus, tmp_path):
        """Runtime.check_tool delegates to enforcer."""
        enforcer = RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")
        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            policy_enforcer=enforcer,
        )

        # Register an agent first
        enforcer.register_agent("research_001", "wf-pol02", max_actions=10)

        result = runtime.check_tool("research_001", "wf-pol02", "web.search")
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_check_tool_proxy_without_enforcer(self, spawner, bus, tmp_path):
        """Runtime.check_tool returns allowed when no enforcer is wired."""
        runtime = OrchestratorRuntime(spawner, bus, persist_dir=tmp_path / "wf")

        result = runtime.check_tool("any_agent", "any_wf", "any_tool")
        assert result.allowed is True
        assert result.reason == "no enforcer"


# ---------------------------------------------------------------------------
# get_metrics wiring tests
# ---------------------------------------------------------------------------


class TestMetricsWiring:
    @pytest.mark.asyncio
    async def test_metrics_include_all_components(self, spawner, bus, tmp_path):
        """get_metrics returns data from all wired components."""
        timeout_mgr = TimeoutManager(compensation_dir=tmp_path / "comp")
        assembler = ContextAssembler(persist_dir=tmp_path / "handoffs")
        enforcer = RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "wf",
            timeout_mgr=timeout_mgr,
            context_assembler=assembler,
            policy_enforcer=enforcer,
        )

        metrics = runtime.get_metrics()
        assert "spawner" in metrics
        assert "bus" in metrics
        assert "timeout" in metrics
        assert "policy" in metrics
        assert "context_budget" in metrics
        assert metrics["context_budget"] == 8000

    @pytest.mark.asyncio
    async def test_metrics_without_optional_components(self, spawner, bus, tmp_path):
        """get_metrics works with no optional components."""
        runtime = OrchestratorRuntime(spawner, bus, persist_dir=tmp_path / "wf")

        metrics = runtime.get_metrics()
        assert "spawner" in metrics
        assert "bus" in metrics
        assert "timeout" not in metrics
        assert "policy" not in metrics
        assert "context_budget" not in metrics


# ---------------------------------------------------------------------------
# Factory function test
# ---------------------------------------------------------------------------


class TestCreateRuntime:
    def test_factory_creates_fully_wired_runtime(self, registry_path, tmp_path, monkeypatch):
        """create_runtime() produces a runtime with all components wired."""
        monkeypatch.setattr("agents.spawner.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agents.spawner.SPAWNER_STATE_FILE", tmp_path / "spawner.json")

        from agents import create_runtime

        runtime = create_runtime(
            registry_path=registry_path,
            persist_dir=tmp_path / "wf",
            use_bridge=False,  # don't auto-discover subsystems in test
        )

        assert isinstance(runtime, OrchestratorRuntime)

        # Verify all components are wired via metrics
        metrics = runtime.get_metrics()
        assert "spawner" in metrics
        assert "bus" in metrics
        assert "timeout" in metrics
        assert "policy" in metrics
        assert "context_budget" in metrics

    @pytest.mark.asyncio
    async def test_factory_runtime_executes_dag(self, registry_path, tmp_path, monkeypatch):
        """Runtime from factory can execute a simple DAG."""
        monkeypatch.setattr("agents.spawner.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agents.spawner.SPAWNER_STATE_FILE", tmp_path / "spawner.json")

        from agents import create_runtime

        runtime = create_runtime(
            registry_path=registry_path,
            persist_dir=tmp_path / "wf",
            use_bridge=False,
        )

        dag = WorkflowDAG(workflow_id="wf-factory01")
        dag.add_node(DAGNode(node_id="n1", agent_id="research_001", role="research", goal="test"))

        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
