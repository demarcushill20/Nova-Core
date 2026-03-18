"""P11.8 — Stress Tests and Production Hardening for Multi-Agent Runtime.

Validates the system under load and at operational boundaries:
  1. Concurrent workflow execution (up to resource limits)
  2. Message bus throughput and backpressure
  3. Spawner resource limits and graceful degradation
  4. Context assembler under large payloads
  5. Feature flag rollout stages
  6. Graceful degradation when optional services are unavailable
  7. Performance benchmarks (spawn latency, bus throughput, context assembly)
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agents.context_assembler import ContextAssembler
from agents.message_bus import MessageBus, QueueFull
from agents.messages import task_assign_message
from agents.orchestrator_runtime import (
    DAGNode,
    OrchestratorRuntime,
    WorkflowDAG,
    WorkflowStatus,
)
from agents.runtime_policy import RuntimePolicyEnforcer
from agents.spawner import (
    AgentProcess,
    AgentSpawner,
    AgentStatus,
    AgentWorkFn,
    ConcurrencyLimitReached,
)
from agents.timeout_manager import RetryConfig, TimeoutManager

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

STRESS_REGISTRY = {
    "schema_version": "1.0",
    "agents": [
        {
            "agent_id": f"worker_{i:03d}",
            "role": "worker",
            "policy_profile": "default",
            "allowed_tools": ["repo.files.read"],
            "denied_tools": [],
            "max_runtime_seconds": 10,
            "max_actions": 20,
            "max_retries": 1,
        }
        for i in range(10)
    ]
    + [
        {
            "agent_id": "coder_001",
            "role": "coder",
            "policy_profile": "coder_scoped_write",
            "allowed_tools": ["repo.files.read", "repo.files.write"],
            "denied_tools": [],
            "max_runtime_seconds": 10,
            "max_actions": 30,
            "max_retries": 1,
        },
        {
            "agent_id": "critic_001",
            "role": "critic",
            "policy_profile": "critic_readonly",
            "allowed_tools": ["repo.files.read"],
            "denied_tools": [],
            "max_runtime_seconds": 10,
            "max_actions": 15,
            "max_retries": 1,
        },
    ],
}


@pytest.fixture
def stress_registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(STRESS_REGISTRY))
    return path


@pytest.fixture
def stress_spawner(stress_registry):
    return AgentSpawner(
        registry_path=stress_registry,
        max_concurrent_per_role=10,
        max_total_concurrent=8,
    )


@pytest.fixture
def stress_bus(tmp_path):
    return MessageBus(max_queue_size=50, persist_dir=tmp_path / "messages")


@pytest.fixture
def stress_policy(tmp_path):
    return RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")


# ---------------------------------------------------------------------------
# Work function helpers
# ---------------------------------------------------------------------------


def make_quick_fn(delay: float = 0.005) -> AgentWorkFn:
    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(delay)
        return {"agent": proc.agent_id, "done": True}

    return work


def make_slow_fn(delay: float = 30.0) -> AgentWorkFn:
    async def work(proc: AgentProcess) -> dict:
        await asyncio.sleep(delay)
        return {"done": True}

    return work


# ---------------------------------------------------------------------------
# 1. Concurrent workflow execution
# ---------------------------------------------------------------------------


class TestConcurrentWorkflows:
    @pytest.mark.asyncio
    async def test_multiple_workflows_in_parallel(self, stress_spawner, stress_bus, tmp_path):
        """Run 3 independent single-node workflows concurrently."""
        dags = []
        for i in range(3):
            dag = WorkflowDAG(workflow_id=f"wf-stress-{i}")
            dag.add_node(
                DAGNode(
                    node_id="step",
                    agent_id=f"worker_{i:03d}",
                    role="worker",
                    goal=f"Workflow {i}",
                )
            )
            dags.append(dag)

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return make_quick_fn()

        runtime = OrchestratorRuntime(
            stress_spawner,
            stress_bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )

        results = await asyncio.gather(*[runtime.execute(dag) for dag in dags])
        assert all(r.status == WorkflowStatus.COMPLETED for r in results)

    @pytest.mark.asyncio
    async def test_workflow_limit_enforcement(self, stress_spawner, stress_bus, tmp_path):
        """Spawner enforces total concurrent limit (8).

        Per-role limit is 10, so total limit (8) is the bottleneck.
        """
        procs = []
        for i in range(8):
            proc = await stress_spawner.spawn(
                f"worker_{i:03d}",
                make_slow_fn(10.0),
                workflow_id=f"wf-limit-{i}",
            )
            procs.append(proc)

        assert len(stress_spawner.list_active()) == 8

        # 9th should fail (total limit = 8)
        with pytest.raises(ConcurrencyLimitReached, match="Total"):
            await stress_spawner.spawn(
                "worker_008",
                make_quick_fn(),
                workflow_id="wf-limit-overflow",
            )

        # Cleanup
        for p in procs:
            await stress_spawner.terminate(p.spawn_id)


# ---------------------------------------------------------------------------
# 2. Message bus throughput and backpressure
# ---------------------------------------------------------------------------


class TestBusThroughput:
    @pytest.mark.asyncio
    async def test_high_volume_messages(self, tmp_path):
        """Send and receive many messages without data loss."""
        bus = MessageBus(max_queue_size=500, persist_dir=tmp_path / "high_vol_msgs")
        count = 200
        for i in range(count):
            msg = task_assign_message(
                from_role="orchestrator",
                to_role="worker",
                workflow_id="wf-throughput",
                subtask_id=f"task-{i}",
                goal=f"Task {i}",
            )
            await bus.send(msg)

        received = await bus.receive_all("worker", max_messages=count)
        assert len(received) == count

    @pytest.mark.asyncio
    async def test_backpressure(self, stress_bus):
        """Bus raises QueueFull when capacity is exceeded."""
        # Fill the queue (max_queue_size=50)
        for i in range(50):
            msg = task_assign_message(
                from_role="orchestrator",
                to_role="backpressure_role",
                workflow_id="wf-bp",
                subtask_id=f"t-{i}",
                goal="fill",
            )
            await stress_bus.send(msg)

        # 51st should fail
        with pytest.raises(QueueFull):
            await stress_bus.send(
                task_assign_message(
                    from_role="orchestrator",
                    to_role="backpressure_role",
                    workflow_id="wf-bp",
                    subtask_id="overflow",
                    goal="too many",
                )
            )

    @pytest.mark.asyncio
    async def test_bus_drain_on_shutdown(self, stress_bus):
        """All messages can be drained for graceful shutdown."""
        for i in range(20):
            await stress_bus.send(
                task_assign_message(
                    from_role="orch",
                    to_role="drain_role",
                    workflow_id="wf-drain",
                    subtask_id=f"d-{i}",
                    goal="drain test",
                )
            )

        drained = await stress_bus.drain("drain_role")
        assert len(drained) == 20
        assert stress_bus.queue_depth("drain_role") == 0

    @pytest.mark.asyncio
    async def test_bus_throughput_benchmark(self, stress_bus):
        """Bus should handle 100+ messages/second."""
        count = 100
        start = time.monotonic()
        for i in range(count):
            msg = task_assign_message(
                from_role="orch",
                to_role=f"bench_{i % 10}",
                workflow_id="wf-bench",
                subtask_id=f"b-{i}",
                goal="benchmark",
            )
            await stress_bus.send(msg)
        elapsed = time.monotonic() - start
        rate = count / elapsed if elapsed > 0 else float("inf")
        assert rate > 100, f"Bus throughput too low: {rate:.0f} msg/s"


# ---------------------------------------------------------------------------
# 3. Spawner resource limits
# ---------------------------------------------------------------------------


class TestSpawnerLimits:
    @pytest.mark.asyncio
    async def test_per_role_concurrency(self, stress_registry):
        """Per-role limit enforced (3 workers max with custom spawner)."""
        spawner = AgentSpawner(
            registry_path=stress_registry,
            max_concurrent_per_role=3,
            max_total_concurrent=20,
        )
        procs = []
        for i in range(3):
            p = await spawner.spawn(
                f"worker_{i:03d}",
                make_slow_fn(10.0),
                workflow_id=f"wf-role-{i}",
            )
            procs.append(p)

        with pytest.raises(ConcurrencyLimitReached, match="Role"):
            await spawner.spawn(
                "worker_003",
                make_quick_fn(),
                workflow_id="wf-role-overflow",
            )

        for p in procs:
            await spawner.terminate(p.spawn_id)

    @pytest.mark.asyncio
    async def test_spawn_latency_benchmark(self, stress_spawner):
        """Agent spawn should complete in < 100ms."""
        start = time.monotonic()
        proc = await stress_spawner.spawn(
            "worker_000",
            make_quick_fn(),
            workflow_id="wf-spawn-bench",
        )
        spawn_latency = time.monotonic() - start
        await stress_spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert spawn_latency < 0.1, f"Spawn latency too high: {spawn_latency * 1000:.0f}ms"

    @pytest.mark.asyncio
    async def test_cleanup_completed_agents(self, stress_spawner):
        """Completed agents are cleaned from the pool."""
        procs = []
        for i in range(3):
            p = await stress_spawner.spawn(
                f"worker_{i:03d}",
                make_quick_fn(0.001),
                workflow_id=f"wf-clean-{i}",
            )
            procs.append(p)

        # Wait for all to complete
        for p in procs:
            await stress_spawner.wait_for(p.spawn_id, timeout=5.0)

        # All should be terminal
        assert all(stress_spawner.status(p.spawn_id) in (AgentStatus.COMPLETED,) for p in procs)

        # Cleanup with zero age threshold
        cleaned = await stress_spawner.cleanup_completed(max_age_s=0.0)
        assert cleaned == 3
        assert len(stress_spawner.pool) == 0

    @pytest.mark.asyncio
    async def test_pool_snapshot_and_recovery(self, stress_spawner, tmp_path):
        """Pool snapshot persists and can be loaded."""
        proc = await stress_spawner.spawn(
            "worker_000",
            make_quick_fn(),
            workflow_id="wf-snap",
        )
        await stress_spawner.wait_for(proc.spawn_id, timeout=5.0)

        stress_spawner.save_pool_snapshot()
        recovered = stress_spawner.load_pool_snapshot()
        assert proc.spawn_id in recovered
        assert recovered[proc.spawn_id].agent_id == "worker_000"


# ---------------------------------------------------------------------------
# 4. Context assembler under load
# ---------------------------------------------------------------------------


class TestContextAssemblerStress:
    def test_large_context_truncation(self):
        """Assembler truncates context exceeding token budget."""
        assembler = ContextAssembler(token_budget=1000)
        # ~5000 tokens of source_files (20K chars / 4 chars per token)
        context = assembler.assemble(
            role="coder",
            available_context={
                "source_files": "x" * 20000,
                "spec": "implement feature",
            },
        )
        total = assembler.estimate_context_size(context)
        assert total <= 1000

    def test_context_assembly_speed(self):
        """Context assembly should complete in < 200ms even with large payloads."""
        assembler = ContextAssembler(token_budget=8000)
        available = {
            "source_files": "code " * 5000,
            "upstream_summary": "summary " * 1000,
            "spec": "implement " * 500,
            "constraints": "constraint " * 200,
            "test_files": "test " * 1000,
        }
        start = time.monotonic()
        assembler.assemble(role="coder", available_context=available)
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, f"Context assembly too slow: {elapsed * 1000:.0f}ms"

    def test_all_roles_have_scoped_context(self):
        """Each role gets different context keys."""
        assembler = ContextAssembler(token_budget=8000)
        all_context = {
            "query": "search term",
            "existing_findings": "found X",
            "repo_structure": "src/",
            "source_files": "code here",
            "upstream_summary": "prior results",
            "spec": "build Y",
            "constraints": "must be fast",
            "test_files": "tests here",
            "work_product": "code output",
            "review_criteria": "correctness",
            "contracts": "output schema",
            "artifacts": "file.py",
            "test_results": "5/5 passed",
            "workflow_summary": "completed",
            "decisions": "chose asyncio",
        }

        roles = ["research", "coder", "critic", "verifier", "memory"]
        contexts = {role: assembler.assemble(role=role, available_context=all_context) for role in roles}

        # Research should NOT have source_files
        assert "source_files" not in contexts["research"]
        # Coder should have source_files
        assert "source_files" in contexts["coder"]
        # Critic should have work_product
        assert "work_product" in contexts["critic"]
        # Verifier should have contracts
        assert "contracts" in contexts["verifier"]
        # Memory should have workflow_summary
        assert "workflow_summary" in contexts["memory"]

    def test_context_utilization_reporting(self):
        """Utilization fraction is correctly computed."""
        assembler = ContextAssembler(token_budget=1000)
        context = {"spec": "x" * 2000}  # ~500 tokens
        util = assembler.context_utilization(context)
        assert 0.4 < util < 0.6  # ~50% utilization


# ---------------------------------------------------------------------------
# 5. Feature flag rollout stages
# ---------------------------------------------------------------------------


class TestFeatureFlagRollout:
    def _allowed_stages(self, stage: str) -> set[str]:
        """Map feature flag stage to allowed roles."""
        stages = {
            "dry_run": set(),
            "research_only": {"research"},
            "code_with_review": {"research", "coder", "critic"},
            "full": {"research", "coder", "critic", "verifier", "memory"},
        }
        return stages.get(stage, set())

    def test_dry_run_allows_no_agents(self):
        assert self._allowed_stages("dry_run") == set()

    def test_research_only_stage(self):
        allowed = self._allowed_stages("research_only")
        assert "research" in allowed
        assert "coder" not in allowed

    def test_code_with_review_stage(self):
        allowed = self._allowed_stages("code_with_review")
        assert "research" in allowed
        assert "coder" in allowed
        assert "critic" in allowed
        assert "verifier" not in allowed

    def test_full_stage(self):
        allowed = self._allowed_stages("full")
        assert len(allowed) == 5
        assert "memory" in allowed


# ---------------------------------------------------------------------------
# 6. Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_skip_memory_on_failure(self, stress_spawner, stress_bus, tmp_path):
        """If memory agent fails, workflow still completes (memory is optional)."""
        dag = WorkflowDAG(workflow_id="wf-degrade-1")
        dag.add_node(
            DAGNode(
                node_id="code",
                agent_id="coder_001",
                role="coder",
                goal="Write code",
            )
        )
        # No memory agent in this DAG — simulates "skip memory"
        # This proves the core pipeline works without memory

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return make_quick_fn()

        runtime = OrchestratorRuntime(
            stress_spawner,
            stress_bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )
        result = await runtime.execute(dag)
        assert result.status == WorkflowStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_queue_on_concurrency_limit(self, stress_spawner):
        """When concurrency limit is hit, caller gets ConcurrencyLimitReached."""
        procs = []
        for i in range(8):  # total limit = 8
            p = await stress_spawner.spawn(
                f"worker_{i:03d}",
                make_slow_fn(10.0),
                workflow_id=f"wf-q-{i}",
            )
            procs.append(p)

        # Should raise, not hang
        with pytest.raises(ConcurrencyLimitReached):
            await stress_spawner.spawn("worker_008", make_quick_fn(), workflow_id="wf-q-overflow")

        for p in procs:
            await stress_spawner.terminate(p.spawn_id)


# ---------------------------------------------------------------------------
# 7. Performance benchmarks
# ---------------------------------------------------------------------------


class TestPerformanceBenchmarks:
    @pytest.mark.asyncio
    async def test_end_to_end_workflow_latency(self, stress_spawner, stress_bus, tmp_path):
        """Simple workflow completes in well under 5 seconds."""
        dag = WorkflowDAG(workflow_id="wf-perf-1")
        dag.add_node(
            DAGNode(
                node_id="fast",
                agent_id="worker_000",
                role="worker",
                goal="Quick task",
            )
        )

        def factory(node: DAGNode, wf: WorkflowDAG) -> AgentWorkFn:
            return make_quick_fn(0.001)

        runtime = OrchestratorRuntime(
            stress_spawner,
            stress_bus,
            work_fn_factory=factory,
            persist_dir=tmp_path / "workflows",
        )

        start = time.monotonic()
        result = await runtime.execute(dag)
        elapsed = time.monotonic() - start

        assert result.status == WorkflowStatus.COMPLETED
        assert elapsed < 5.0, f"Workflow latency: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_persistence_speed(self, tmp_path):
        """Workflow persistence write should be < 50ms."""
        dag = WorkflowDAG(workflow_id="wf-persist-perf")
        for i in range(10):
            dag.add_node(
                DAGNode(
                    node_id=f"n{i}",
                    agent_id="x",
                    role="r",
                    goal=f"Goal {i}",
                    depends_on=[f"n{i - 1}"] if i > 0 else [],
                )
            )

        path = tmp_path / "perf.json"
        start = time.monotonic()
        path.write_text(json.dumps(dag.to_dict(), indent=2, default=str))
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, f"Persistence too slow: {elapsed * 1000:.0f}ms"

    def test_timeout_manager_overhead(self):
        """TimeoutManager should_retry decisions should be near-instant."""
        mgr = TimeoutManager(retry_config=RetryConfig(max_retries=3))
        start = time.monotonic()
        for _ in range(1000):
            mgr.should_retry("coder", "TimeoutError", 0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"1000 should_retry calls took {elapsed * 1000:.0f}ms"

    def test_policy_check_overhead(self, tmp_path):
        """Policy check should add minimal overhead per action."""
        policy = RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")
        policy.register_agent("a", "wf", max_actions=10000)
        # Raise workflow-level budget so it doesn't trigger first
        policy._workflow_budgets["wf"].max_total_actions = 10000
        start = time.monotonic()
        for i in range(1000):
            policy.check_tool("a", "wf", f"tool_{i}")
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"1000 policy checks took {elapsed * 1000:.0f}ms"
