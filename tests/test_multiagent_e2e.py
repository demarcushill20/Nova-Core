"""P11.7 — End-to-End Integration Tests for Multi-Agent Runtime.

Validates the full stack with realistic multi-agent workflows:
  1. Simple research task (1 agent)
  2. Code implementation with review (Coder -> Critic -> Verifier)
  3. Complex multi-step (parallel Research -> Coder -> Critic -> Verifier)
  4. Failure recovery (timeout, policy violation, budget exceeded)
  5. Memory integration (Memory agent extracts patterns)
  6. Watcher integration (feature flag routing, fallback to single-agent)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.context_assembler import ContextAssembler, HandoffArtifact
from agents.message_bus import MessageBus
from agents.messages import (
    AgentMessage,
    MessageType,
    result_message,
    task_assign_message,
)
from agents.orchestrator_runtime import (
    DAGNode,
    NodeStatus,
    OrchestratorRuntime,
    WorkflowDAG,
    WorkflowStatus,
)
from agents.runtime_policy import (
    BudgetExhausted,
    PolicyViolation,
    RuntimePolicyEnforcer,
)
from agents.spawner import (
    AgentProcess,
    AgentSpawner,
    AgentStatus,
    AgentWorkFn,
)
from agents.timeout_manager import (
    CompensationEntry,
    RetryConfig,
    TimeoutManager,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

REGISTRY = {
    "schema_version": "1.0",
    "agents": [
        {
            "agent_id": "orchestrator_main",
            "role": "orchestrator",
            "policy_profile": "orchestrator_control",
            "allowed_tools": ["agent.spawn", "agent.send", "agent.await"],
            "denied_tools": ["repo.files.write", "shell.run"],
            "max_runtime_seconds": 60,
            "max_actions": 50,
            "max_retries": 2,
        },
        {
            "agent_id": "research_001",
            "role": "research",
            "policy_profile": "research_safe",
            "allowed_tools": ["repo.search", "web.search", "vault.read"],
            "denied_tools": ["repo.files.write", "shell.run"],
            "max_runtime_seconds": 30,
            "max_actions": 12,
            "max_retries": 1,
        },
        {
            "agent_id": "coder_001",
            "role": "coder",
            "policy_profile": "coder_scoped_write",
            "allowed_tools": ["repo.files.read", "repo.files.write", "shell.run"],
            "denied_tools": ["agent.spawn", "web.search"],
            "max_runtime_seconds": 30,
            "max_actions": 30,
            "max_retries": 2,
        },
        {
            "agent_id": "critic_001",
            "role": "critic",
            "policy_profile": "critic_readonly",
            "allowed_tools": ["repo.files.read", "repo.search"],
            "denied_tools": ["repo.files.write", "shell.run"],
            "max_runtime_seconds": 15,
            "max_actions": 15,
            "max_retries": 1,
        },
        {
            "agent_id": "verifier_001",
            "role": "verifier",
            "policy_profile": "verifier_readonly",
            "allowed_tools": ["repo.files.read", "shell.run"],
            "denied_tools": ["repo.files.write", "agent.spawn"],
            "max_runtime_seconds": 15,
            "max_actions": 15,
            "max_retries": 1,
        },
        {
            "agent_id": "memory_001",
            "role": "memory",
            "policy_profile": "memory_scoped_write",
            "allowed_tools": ["repo.files.read", "vault.read", "vault.write"],
            "denied_tools": ["shell.run", "agent.spawn"],
            "max_runtime_seconds": 15,
            "max_actions": 10,
            "max_retries": 1,
        },
    ],
}


@pytest.fixture
def registry_path(tmp_path):
    """Write a test registry and return the path."""
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(REGISTRY))
    return path


@pytest.fixture
def spawner(registry_path):
    return AgentSpawner(
        registry_path=registry_path,
        max_concurrent_per_role=2,
        max_total_concurrent=6,
    )


@pytest.fixture
def bus(tmp_path):
    return MessageBus(max_queue_size=100, persist_dir=tmp_path / "messages")


@pytest.fixture
def assembler(tmp_path):
    return ContextAssembler(token_budget=8000, persist_dir=tmp_path / "handoffs")


@pytest.fixture
def policy(tmp_path):
    return RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")


@pytest.fixture
def timeout_mgr(tmp_path):
    return TimeoutManager(
        retry_config=RetryConfig(max_retries=2, base_delay_s=0.01),
        compensation_dir=tmp_path / "compensations",
    )


# ---------------------------------------------------------------------------
# Work function helpers — simulate agent behaviour
# ---------------------------------------------------------------------------


def make_research_fn(result: dict | None = None) -> AgentWorkFn:
    """Simulate a research agent that returns findings."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(0.01)
        return result or {
            "findings": "Python 3.12 adds type param syntax",
            "sources": 3,
            "confidence": 0.9,
        }

    return work


def make_coder_fn(output: str = "def hello(): pass") -> AgentWorkFn:
    """Simulate a coder agent that writes code."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(0.01)
        return {"code": output, "files_changed": 1, "tests_added": 2}

    return work


def make_critic_fn(*, approve: bool = True, changes: str = "") -> AgentWorkFn:
    """Simulate a critic agent that reviews code."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(0.01)
        return {
            "decision": "approve" if approve else "request_changes",
            "issues": [] if approve else [changes or "missing docstring"],
            "confidence": 0.95 if approve else 0.6,
        }

    return work


def make_verifier_fn(*, passes: bool = True) -> AgentWorkFn:
    """Simulate a verifier agent that runs tests."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(0.01)
        return {
            "verified": passes,
            "tests_run": 5,
            "tests_passed": 5 if passes else 3,
            "tests_failed": 0 if passes else 2,
        }

    return work


def make_memory_fn() -> AgentWorkFn:
    """Simulate a memory agent that extracts patterns."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(0.01)
        return {
            "patterns_extracted": 2,
            "entries": ["pattern_async_retry", "pattern_circuit_breaker"],
        }

    return work


def make_slow_fn(delay: float = 5.0) -> AgentWorkFn:
    """Simulate a slow agent that may timeout."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(delay)
        return {"result": "done"}

    return work


def make_failing_fn(error_cls: type = RuntimeError, msg: str = "boom") -> AgentWorkFn:
    """Simulate an agent that fails."""

    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(0.01)
        raise error_cls(msg)

    return work


# ---------------------------------------------------------------------------
# Scenario 1: Simple research task (single agent)
# ---------------------------------------------------------------------------


class TestSimpleResearchTask:
    @pytest.mark.asyncio
    async def test_single_research_agent(self, spawner, bus):
        """One research agent completes a simple task."""
        findings = {"findings": "test result", "sources": 2}
        proc = await spawner.spawn(
            "research_001",
            make_research_fn(findings),
            workflow_id="wf-research-1",
            task_description="Find Python 3.12 changes",
        )
        result_proc = await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert result_proc.status == AgentStatus.COMPLETED
        assert result_proc.result == findings

    @pytest.mark.asyncio
    async def test_research_with_bus_messages(self, spawner, bus):
        """Research agent receives task via bus and result is sent back."""
        received_messages: list[AgentMessage] = []

        async def research_with_bus(proc: AgentProcess) -> dict:
            # Send task assignment
            msg = task_assign_message(
                from_role="orchestrator",
                to_role="research",
                workflow_id=proc.workflow_id or "",
                subtask_id="research-step-1",
                goal=proc.task_description,
            )
            await bus.send(msg)
            # Receive it back (simulates agent reading its queue)
            received = await bus.receive("research", timeout=1.0)
            if received:
                received_messages.append(received)
            # Send result
            res = result_message(
                from_role="research",
                to_role="orchestrator",
                workflow_id=proc.workflow_id or "",
                subtask_id="research-step-1",
                success=True,
                output={"findings": "asyncio best practices"},
                confidence=0.85,
            )
            await bus.send(res)
            return {"findings": "asyncio best practices"}

        proc = await spawner.spawn(
            "research_001",
            research_with_bus,
            workflow_id="wf-research-bus",
            task_description="Research asyncio patterns",
        )
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert proc.status == AgentStatus.COMPLETED
        assert len(received_messages) == 1
        assert received_messages[0].message_type == MessageType.TASK_ASSIGN

        # Check orchestrator received the result
        orchestrator_msg = await bus.receive("orchestrator")
        assert orchestrator_msg is not None
        assert orchestrator_msg.message_type == MessageType.RESULT
        assert orchestrator_msg.payload["success"] is True

    @pytest.mark.asyncio
    async def test_research_with_context_assembly(self, spawner, assembler):
        """Research agent gets properly scoped context."""
        context = assembler.assemble(
            role="research",
            available_context={
                "query": "Python 3.12 features",
                "existing_findings": "PEP 695 type aliases",
                "repo_structure": "src/, tests/, docs/",
                "source_files": "# should be excluded for research",
                "work_product": "# should be excluded for research",
            },
        )
        # Research role should get query, existing_findings, repo_structure
        assert "query" in context
        assert "existing_findings" in context
        assert "repo_structure" in context
        # Should NOT get coder-specific keys
        assert "source_files" not in context
        assert "work_product" not in context

    @pytest.mark.asyncio
    async def test_research_with_policy_check(self, spawner, policy):
        """Research agent policy allows reads but denies writes."""
        policy.register_agent("research_001", "wf-policy-1", max_actions=10)

        # Allowed tool
        result = policy.check_tool(
            "research_001",
            "wf-policy-1",
            "web.search",
            allowed_tools=["repo.search", "web.search", "vault.read"],
        )
        assert result.allowed is True

        # Denied tool
        with pytest.raises(PolicyViolation):
            policy.check_tool(
                "research_001",
                "wf-policy-1",
                "repo.files.write",
                denied_tools=["repo.files.write", "shell.run"],
            )


# ---------------------------------------------------------------------------
# Scenario 2: Code implementation with review (Coder -> Critic -> Verifier)
# ---------------------------------------------------------------------------


class TestCodeReviewPipeline:
    @pytest.mark.asyncio
    async def test_coder_critic_verifier_pipeline(self, spawner, bus, tmp_path):
        """Full maker-checker: Coder writes -> Critic reviews -> Verifier checks."""
        results: dict[str, Any] = {}

        # Step 1: Coder
        coder_proc = await spawner.spawn(
            "coder_001",
            make_coder_fn("def add(a, b): return a + b"),
            workflow_id="wf-pipeline-1",
            task_description="Implement add function",
        )
        await spawner.wait_for(coder_proc.spawn_id, timeout=5.0)
        assert coder_proc.status == AgentStatus.COMPLETED
        results["coder"] = coder_proc.result

        # Step 2: Critic (after coder completes)
        critic_proc = await spawner.spawn(
            "critic_001",
            make_critic_fn(approve=True),
            workflow_id="wf-pipeline-1",
            task_description="Review add function",
        )
        await spawner.wait_for(critic_proc.spawn_id, timeout=5.0)
        assert critic_proc.status == AgentStatus.COMPLETED
        results["critic"] = critic_proc.result
        assert critic_proc.result["decision"] == "approve"

        # Step 3: Verifier (after critic approves)
        verifier_proc = await spawner.spawn(
            "verifier_001",
            make_verifier_fn(passes=True),
            workflow_id="wf-pipeline-1",
            task_description="Verify add function tests",
        )
        await spawner.wait_for(verifier_proc.spawn_id, timeout=5.0)
        assert verifier_proc.status == AgentStatus.COMPLETED
        results["verifier"] = verifier_proc.result
        assert verifier_proc.result["verified"] is True

        # All three completed successfully
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_critic_rejects_code(self, spawner):
        """Critic requests changes — pipeline records rejection."""
        # Coder
        coder = await spawner.spawn(
            "coder_001",
            make_coder_fn("x = 1"),
            workflow_id="wf-reject-1",
        )
        await spawner.wait_for(coder.spawn_id, timeout=5.0)

        # Critic rejects
        critic = await spawner.spawn(
            "critic_001",
            make_critic_fn(approve=False, changes="no docstring, no types"),
            workflow_id="wf-reject-1",
        )
        await spawner.wait_for(critic.spawn_id, timeout=5.0)
        assert critic.result["decision"] == "request_changes"
        assert len(critic.result["issues"]) > 0

    @pytest.mark.asyncio
    async def test_pipeline_with_handoffs(self, spawner, assembler, tmp_path):
        """Pipeline uses handoff artifacts to pass context between stages."""
        wf_id = "wf-handoff-1"

        # Coder produces a handoff
        coder = await spawner.spawn(
            "coder_001",
            make_coder_fn("def greet(name): return f'Hello {name}'"),
            workflow_id=wf_id,
        )
        await spawner.wait_for(coder.spawn_id, timeout=5.0)

        handoff = HandoffArtifact(
            subtask_id="code-step",
            from_role="coder",
            to_role="critic",
            status="completed",
            workflow_id=wf_id,
            output_summary="Implemented greet function",
            artifacts=["/tmp/greet.py"],
            context_for_downstream={"code": coder.result["code"]},
            confidence=0.9,
        )
        path = assembler.save_handoff(handoff)
        assert path.exists()

        # Critic loads the handoff
        loaded = assembler.load_handoff(wf_id, "code-step")
        assert loaded is not None
        assert loaded.from_role == "coder"
        assert loaded.to_role == "critic"
        assert loaded.context_for_downstream["code"] == coder.result["code"]

    @pytest.mark.asyncio
    async def test_pipeline_via_dag(self, spawner, bus, tmp_path):
        """Full pipeline expressed as a DAG and executed by OrchestratorRuntime."""
        dag = WorkflowDAG(workflow_id="wf-dag-pipeline-1")
        dag.add_node(
            DAGNode(
                node_id="code",
                agent_id="coder_001",
                role="coder",
                goal="Implement function",
                depends_on=[],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="review",
                agent_id="critic_001",
                role="critic",
                goal="Review code",
                depends_on=["code"],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="verify",
                agent_id="verifier_001",
                role="verifier",
                goal="Run tests",
                depends_on=["review"],
            )
        )

        # Map node roles to work functions
        work_fns = {
            "code": make_coder_fn(),
            "review": make_critic_fn(approve=True),
            "verify": make_verifier_fn(passes=True),
        }

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return work_fns[node.node_id]

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        for node in result.nodes.values():
            assert node.status == NodeStatus.COMPLETED


# ---------------------------------------------------------------------------
# Scenario 3: Complex multi-step with parallel execution
# ---------------------------------------------------------------------------


class TestComplexParallelWorkflow:
    @pytest.mark.asyncio
    async def test_parallel_research_then_code(self, spawner, bus, tmp_path):
        """Two research agents run in parallel, then coder uses both results."""
        dag = WorkflowDAG(workflow_id="wf-parallel-1")
        dag.add_node(
            DAGNode(
                node_id="research_a",
                agent_id="research_001",
                role="research",
                goal="Research topic A",
                depends_on=[],
            )
        )
        # Need a second research agent for parallel — use coder as a stand-in
        # since spawner enforces max_concurrent_per_role=2
        dag.add_node(
            DAGNode(
                node_id="plan",
                agent_id="orchestrator_main",
                role="orchestrator",
                goal="Plan approach",
                depends_on=[],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="code",
                agent_id="coder_001",
                role="coder",
                goal="Implement based on research",
                depends_on=["research_a", "plan"],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="review",
                agent_id="critic_001",
                role="critic",
                goal="Review implementation",
                depends_on=["code"],
            )
        )

        work_fns = {
            "research_a": make_research_fn({"findings": "topic A insights"}),
            "plan": make_research_fn({"plan": "use asyncio"}),
            "code": make_coder_fn("async def main(): ..."),
            "review": make_critic_fn(approve=True),
        }

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return work_fns[node.node_id]

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED

        # Verify parallel nodes ran before dependent ones
        ra = result.nodes["research_a"]
        plan = result.nodes["plan"]
        code = result.nodes["code"]
        assert ra.completed_at is not None
        assert plan.completed_at is not None
        assert code.started_at is not None
        assert code.started_at >= ra.completed_at or code.started_at >= plan.completed_at

    @pytest.mark.asyncio
    async def test_dag_topological_order(self):
        """DAG correctly resolves dependencies."""
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="a", agent_id="x", role="r", goal="A"))
        dag.add_node(DAGNode(node_id="b", agent_id="x", role="r", goal="B", depends_on=["a"]))
        dag.add_node(DAGNode(node_id="c", agent_id="x", role="r", goal="C", depends_on=["a"]))
        dag.add_node(DAGNode(node_id="d", agent_id="x", role="r", goal="D", depends_on=["b", "c"]))

        order = dag.topological_order()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    @pytest.mark.asyncio
    async def test_dag_cycle_detection(self):
        """DAG rejects cycles."""
        dag = WorkflowDAG()
        dag.add_node(DAGNode(node_id="a", agent_id="x", role="r", goal="A", depends_on=["c"]))
        dag.add_node(DAGNode(node_id="b", agent_id="x", role="r", goal="B", depends_on=["a"]))
        dag.add_node(DAGNode(node_id="c", agent_id="x", role="r", goal="C", depends_on=["b"]))

        with pytest.raises(ValueError, match="Cycle"):
            dag.topological_order()

    @pytest.mark.asyncio
    async def test_four_node_diamond_dag(self, spawner, bus, tmp_path):
        """Diamond: A -> (B, C) -> D. B and C run in parallel."""
        dag = WorkflowDAG(workflow_id="wf-diamond-1")
        dag.add_node(
            DAGNode(
                node_id="research",
                agent_id="research_001",
                role="research",
                goal="Research",
                depends_on=[],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="code",
                agent_id="coder_001",
                role="coder",
                goal="Code",
                depends_on=["research"],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="review",
                agent_id="critic_001",
                role="critic",
                goal="Review",
                depends_on=["research"],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="verify",
                agent_id="verifier_001",
                role="verifier",
                goal="Verify",
                depends_on=["code", "review"],
            )
        )

        work_fns = {
            "research": make_research_fn(),
            "code": make_coder_fn(),
            "review": make_critic_fn(approve=True),
            "verify": make_verifier_fn(passes=True),
        }

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return work_fns[node.node_id]

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        assert all(n.status == NodeStatus.COMPLETED for n in result.nodes.values())


# ---------------------------------------------------------------------------
# Scenario 4: Failure recovery
# ---------------------------------------------------------------------------


class TestFailureRecovery:
    @pytest.mark.asyncio
    async def test_agent_timeout(self, spawner):
        """Agent exceeds its budget timeout and fails."""
        proc = await spawner.spawn(
            "critic_001",  # 15s max_runtime
            make_slow_fn(delay=60.0),
            workflow_id="wf-timeout-1",
            budget_overrides={"max_runtime_seconds": 0.1},  # override to 100ms
        )
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert proc.status == AgentStatus.FAILED
        assert "Timeout" in (proc.error or "")

    @pytest.mark.asyncio
    async def test_agent_runtime_error(self, spawner):
        """Agent raises an unexpected error."""
        proc = await spawner.spawn(
            "coder_001",
            make_failing_fn(RuntimeError, "null pointer"),
            workflow_id="wf-error-1",
        )
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert proc.status == AgentStatus.FAILED
        assert "null pointer" in (proc.error or "")

    @pytest.mark.asyncio
    async def test_policy_violation_in_workflow(self, policy):
        """Policy violation stops the offending action."""
        policy.register_agent("coder_001", "wf-policy-v", max_actions=20)

        # Coder tries to use a denied tool
        with pytest.raises(PolicyViolation):
            policy.check_tool(
                "coder_001",
                "wf-policy-v",
                "agent.spawn",
                denied_tools=["agent.spawn", "web.search"],
            )

        # Verify violation was recorded
        report = policy.get_budget_report("wf-policy-v")
        assert report is not None
        agent_report = report["agents"]["coder_001"]
        assert len(agent_report["violations"]) == 1

    @pytest.mark.asyncio
    async def test_budget_exhaustion(self, policy):
        """Agent exceeds action budget."""
        policy.register_agent("research_001", "wf-budget-1", max_actions=3)

        # Each check_tool increments action count then checks budget.
        # After 3 calls, actions_used=3 == max_actions → exhausted on 3rd call.
        for i in range(2):
            policy.check_tool("research_001", "wf-budget-1", f"tool_{i}")

        with pytest.raises(BudgetExhausted):
            policy.check_tool("research_001", "wf-budget-1", "tool_overflow")

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens(self, timeout_mgr):
        """Circuit breaker opens after repeated failures."""
        for _ in range(5):
            timeout_mgr.record_failure("coder")

        breaker = timeout_mgr.get_breaker("coder")
        assert breaker.is_open

        # Should not allow retry when breaker is open
        assert not timeout_mgr.should_retry("coder", "TimeoutError", 0)

    @pytest.mark.asyncio
    async def test_retry_storm_detection(self, timeout_mgr):
        """Retry storm detector halts when too many retries."""
        for _ in range(12):
            timeout_mgr.record_failure("any_role")

        assert timeout_mgr.is_retry_storm
        assert not timeout_mgr.should_retry("coder", "TimeoutError", 0)

    @pytest.mark.asyncio
    async def test_dag_node_failure_marks_workflow_failed(self, spawner, bus, tmp_path):
        """If a node fails and exhausts retries, workflow is marked FAILED."""
        dag = WorkflowDAG(workflow_id="wf-node-fail-1")
        dag.add_node(
            DAGNode(
                node_id="fail_node",
                agent_id="coder_001",
                role="coder",
                goal="Will fail",
                max_retries=0,
            )
        )

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return make_failing_fn(RuntimeError, "deliberate failure")

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.FAILED
        assert result.nodes["fail_node"].status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_compensation_log(self, timeout_mgr):
        """Compensation entries are recorded for rollback."""
        entry = CompensationEntry(
            workflow_id="wf-comp-1",
            node_id="code",
            action="wrote file /tmp/test.py",
            rollback_instruction="delete /tmp/test.py",
        )
        timeout_mgr.compensation_log.record(entry)
        entries = timeout_mgr.compensation_log.get_entries("wf-comp-1")
        assert len(entries) == 1
        assert entries[0].action == "wrote file /tmp/test.py"

    @pytest.mark.asyncio
    async def test_dag_deadlock_detection(self, spawner, bus, tmp_path):
        """Deadlock: a node depends on a failed node (no retries left)."""
        dag = WorkflowDAG(workflow_id="wf-deadlock-1")
        dag.add_node(
            DAGNode(
                node_id="step1",
                agent_id="coder_001",
                role="coder",
                goal="First step",
                max_retries=0,
            )
        )
        dag.add_node(
            DAGNode(
                node_id="step2",
                agent_id="critic_001",
                role="critic",
                goal="Second step",
                depends_on=["step1"],
            )
        )

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            if node.node_id == "step1":
                return make_failing_fn(RuntimeError, "fail step1")
            return make_critic_fn(approve=True)

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        # step1 fails, step2 can never start → deadlock → workflow fails
        assert result.status == WorkflowStatus.FAILED


# ---------------------------------------------------------------------------
# Scenario 5: Memory integration
# ---------------------------------------------------------------------------


class TestMemoryIntegration:
    @pytest.mark.asyncio
    async def test_memory_agent_extracts_patterns(self, spawner):
        """Memory agent runs after workflow and extracts patterns."""
        mem_proc = await spawner.spawn(
            "memory_001",
            make_memory_fn(),
            workflow_id="wf-memory-1",
            task_description="Extract patterns from workflow wf-pipeline-1",
        )
        await spawner.wait_for(mem_proc.spawn_id, timeout=5.0)
        assert mem_proc.status == AgentStatus.COMPLETED
        assert mem_proc.result["patterns_extracted"] == 2

    @pytest.mark.asyncio
    async def test_memory_in_full_pipeline(self, spawner, bus, tmp_path):
        """Memory agent runs as final DAG node after verification."""
        dag = WorkflowDAG(workflow_id="wf-mem-pipeline-1")
        dag.add_node(
            DAGNode(
                node_id="code",
                agent_id="coder_001",
                role="coder",
                goal="Write code",
            )
        )
        dag.add_node(
            DAGNode(
                node_id="review",
                agent_id="critic_001",
                role="critic",
                goal="Review",
                depends_on=["code"],
            )
        )
        dag.add_node(
            DAGNode(
                node_id="memorize",
                agent_id="memory_001",
                role="memory",
                goal="Extract patterns",
                depends_on=["code", "review"],
            )
        )

        work_fns = {
            "code": make_coder_fn(),
            "review": make_critic_fn(approve=True),
            "memorize": make_memory_fn(),
        }

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return work_fns[node.node_id]

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED
        mem_node = result.nodes["memorize"]
        assert mem_node.status == NodeStatus.COMPLETED
        assert mem_node.result["patterns_extracted"] == 2

    @pytest.mark.asyncio
    async def test_memory_context_scoping(self, assembler):
        """Memory agent receives only workflow_summary and contracts."""
        context = assembler.assemble(
            role="memory",
            available_context={
                "workflow_summary": "completed 3-step pipeline",
                "contracts": {"output": "greet function"},
                "decisions": ["used asyncio"],
                "source_files": "should NOT be included",
                "test_results": "should NOT be included",
            },
        )
        assert "workflow_summary" in context
        assert "contracts" in context
        assert "decisions" in context
        assert "source_files" not in context
        assert "test_results" not in context


# ---------------------------------------------------------------------------
# Scenario 6: Watcher integration / feature flag routing
# ---------------------------------------------------------------------------


class TestWatcherIntegration:
    @pytest.mark.asyncio
    async def test_fallback_to_single_agent(self, spawner):
        """When multi-agent flag is off, fall back to single-agent execution."""
        feature_flags = {"multi_agent_enabled": False}

        # Simulate watcher decision: if flag off, run single coder
        if not feature_flags["multi_agent_enabled"]:
            proc = await spawner.spawn(
                "coder_001",
                make_coder_fn("solo mode"),
                workflow_id="wf-solo-1",
            )
            await spawner.wait_for(proc.spawn_id, timeout=5.0)
            assert proc.status == AgentStatus.COMPLETED
            assert proc.result["code"] == "solo mode"

    @pytest.mark.asyncio
    async def test_feature_flag_routes_to_pipeline(self, spawner, bus, tmp_path):
        """When multi-agent flag is on, route to full pipeline."""
        feature_flags = {"multi_agent_enabled": True, "stage": "code_with_review"}

        if feature_flags["multi_agent_enabled"] and feature_flags["stage"] == "code_with_review":
            dag = WorkflowDAG(workflow_id="wf-flagged-1")
            dag.add_node(
                DAGNode(
                    node_id="code",
                    agent_id="coder_001",
                    role="coder",
                    goal="Write",
                )
            )
            dag.add_node(
                DAGNode(
                    node_id="review",
                    agent_id="critic_001",
                    role="critic",
                    goal="Review",
                    depends_on=["code"],
                )
            )

            work_fns = {
                "code": make_coder_fn(),
                "review": make_critic_fn(approve=True),
            }

            def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
                return work_fns[node.node_id]

            runtime = OrchestratorRuntime(
                spawner,
                bus,
                work_fn_factory=factory,
                persist_dir=tmp_path / "workflows",
            )
            result = await runtime.execute(dag)
            assert result.status == WorkflowStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_spawn_failure(self, spawner):
        """If agent spawning fails, fall back gracefully."""
        # Fill up the coder role
        proc1 = await spawner.spawn("coder_001", make_slow_fn(10.0), workflow_id="wf-deg-1")
        # Can't spawn same agent_id again
        from agents.spawner import AgentAlreadyActive

        with pytest.raises(AgentAlreadyActive):
            await spawner.spawn("coder_001", make_coder_fn(), workflow_id="wf-deg-1")

        # Cleanup
        await spawner.terminate(proc1.spawn_id)

    @pytest.mark.asyncio
    async def test_research_only_stage(self, spawner):
        """Feature flag stage: research_only — only research agents allowed."""
        feature_flags = {"multi_agent_enabled": True, "stage": "research_only"}
        allowed_roles = {"research"} if feature_flags["stage"] == "research_only" else set()

        # Research is allowed
        assert "research" in allowed_roles
        proc = await spawner.spawn(
            "research_001",
            make_research_fn(),
            workflow_id="wf-research-only",
        )
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert proc.status == AgentStatus.COMPLETED


# ---------------------------------------------------------------------------
# Cross-cutting: Metrics, persistence, budget reports
# ---------------------------------------------------------------------------


class TestCrossCutting:
    @pytest.mark.asyncio
    async def test_spawner_metrics(self, spawner):
        """Spawner metrics reflect active agents."""
        proc = await spawner.spawn("research_001", make_research_fn(), workflow_id="wf-m-1")
        metrics = spawner.get_metrics()
        assert metrics["total_in_pool"] >= 1
        await spawner.wait_for(proc.spawn_id, timeout=5.0)

    @pytest.mark.asyncio
    async def test_bus_metrics(self, bus):
        """Bus metrics track sends and receives."""
        msg = task_assign_message(
            from_role="orchestrator",
            to_role="coder",
            workflow_id="wf-bm",
            subtask_id="s1",
            goal="test",
        )
        await bus.send(msg)
        await bus.receive("coder")
        metrics = bus.get_metrics()
        assert metrics["send_count"] == 1
        assert metrics["receive_count"] == 1

    @pytest.mark.asyncio
    async def test_workflow_persistence(self, spawner, bus, tmp_path):
        """Workflow state is persisted to disk after execution."""
        dag = WorkflowDAG(workflow_id="wf-persist-1")
        dag.add_node(
            DAGNode(
                node_id="step",
                agent_id="research_001",
                role="research",
                goal="Test",
            )
        )

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return make_research_fn()

        persist_dir = tmp_path / "workflows"
        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=persist_dir,
        )
        await runtime.execute(dag)

        # Check file exists
        wf_file = persist_dir / "wf-persist-1.json"
        assert wf_file.exists()
        data = json.loads(wf_file.read_text())
        assert data["status"] == "completed"
        assert data["nodes"]["step"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_message_persistence(self, bus, tmp_path):
        """Messages are persisted to JSONL."""
        msg = task_assign_message(
            from_role="orchestrator",
            to_role="coder",
            workflow_id="wf-mp",
            subtask_id="s1",
            goal="persist test",
        )
        await bus.send(msg)

        loaded = bus.load_messages("wf-mp")
        assert len(loaded) >= 1
        assert loaded[0].payload["goal"] == "persist test"

    @pytest.mark.asyncio
    async def test_budget_report_after_workflow(self, policy):
        """Budget report shows correct utilization after workflow."""
        policy.register_agent("coder_001", "wf-br", max_actions=10)
        policy.register_agent("critic_001", "wf-br", max_actions=5)

        for i in range(3):
            policy.check_tool("coder_001", "wf-br", f"tool_{i}")
        policy.check_tool("critic_001", "wf-br", "review")

        report = policy.get_budget_report("wf-br")
        assert report is not None
        assert report["total_actions"] == 4
        assert report["agents"]["coder_001"]["actions_used"] == 3
        assert report["agents"]["critic_001"]["actions_used"] == 1

    @pytest.mark.asyncio
    async def test_context_token_budget(self, assembler):
        """Context assembler enforces token budget."""
        large_content = "x" * 40000  # ~10K tokens, exceeds 8K budget at 90%
        context = assembler.assemble(
            role="coder",
            available_context={
                "source_files": large_content,
                "spec": "implement feature X",
            },
        )
        # source_files should be truncated or spec excluded due to budget
        total_tokens = assembler.estimate_context_size(context)
        assert total_tokens <= assembler.token_budget

    @pytest.mark.asyncio
    async def test_orchestrator_runtime_metrics(self, spawner, bus, tmp_path):
        """OrchestratorRuntime.get_metrics() aggregates spawner + bus metrics."""
        runtime = OrchestratorRuntime(
            spawner,
            bus,
            persist_dir=tmp_path / "workflows",
        )
        metrics = runtime.get_metrics()
        assert "spawner" in metrics
        assert "bus" in metrics
        assert "total_in_pool" in metrics["spawner"]
        assert "send_count" in metrics["bus"]

    @pytest.mark.asyncio
    async def test_terminate_running_workflow(self, spawner, bus, tmp_path):
        """Orchestrator can terminate agents when workflow times out."""
        dag = WorkflowDAG(workflow_id="wf-term-1")
        dag.add_node(
            DAGNode(
                node_id="slow",
                agent_id="coder_001",
                role="coder",
                goal="Slow task",
            )
        )

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return make_slow_fn(60.0)

        runtime = OrchestratorRuntime(
            spawner,
            bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
            max_workflow_runtime_s=0.2,  # 200ms timeout
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.HALTED
        assert "timeout" in (result.error or "").lower()
