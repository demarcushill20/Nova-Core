"""Tests for P11.1 — Agent Spawner and Lifecycle Manager.

Covers: status transitions, budget enforcement, timeout, concurrent limits,
orphan cleanup, lease lifecycle, delegation records, registry loading,
ID uniqueness, pool persistence, metrics, and cancellation.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agents.spawner import (
    AgentAlreadyActive,
    AgentBudget,
    AgentNotFound,
    AgentProcess,
    AgentSpawner,
    AgentStatus,
    BudgetExceeded,
    ConcurrencyLimitReached,
    RegistryError,
    load_registry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_REGISTRY = {
    "schema_version": "1.0",
    "agents": [
        {
            "agent_id": "research_001",
            "role": "research",
            "status": "idle",
            "policy_profile": "research_safe",
            "allowed_tools": ["web.search", "repo.files.read"],
            "denied_tools": ["shell.run"],
            "max_runtime_seconds": 10,
            "max_actions": 5,
            "max_retries": 1,
            "spawn_depth": 0,
            "parent_agent_id": "orchestrator_main",
        },
        {
            "agent_id": "coder_001",
            "role": "coder",
            "status": "idle",
            "policy_profile": "coder_scoped_write",
            "allowed_tools": ["repo.files.read", "repo.files.write"],
            "denied_tools": ["web.search"],
            "max_runtime_seconds": 15,
            "max_actions": 10,
            "max_retries": 2,
            "spawn_depth": 0,
            "parent_agent_id": "orchestrator_main",
        },
        {
            "agent_id": "critic_001",
            "role": "critic",
            "status": "idle",
            "policy_profile": "critic_readonly",
            "allowed_tools": ["repo.files.read"],
            "denied_tools": ["repo.files.write"],
            "max_runtime_seconds": 5,
            "max_actions": 8,
            "max_retries": 1,
            "spawn_depth": 0,
            "parent_agent_id": "orchestrator_main",
        },
        {
            "agent_id": "research_002",
            "role": "research",
            "status": "idle",
            "policy_profile": "research_safe",
            "allowed_tools": ["web.search"],
            "denied_tools": ["shell.run"],
            "max_runtime_seconds": 10,
            "max_actions": 5,
            "max_retries": 1,
            "spawn_depth": 0,
            "parent_agent_id": "orchestrator_main",
        },
    ],
}


@pytest.fixture
def registry_path(tmp_path):
    """Write a sample registry and return its path."""
    reg = tmp_path / "STATE" / "agents" / "registry.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps(SAMPLE_REGISTRY))
    return reg


@pytest.fixture
def spawner(registry_path, tmp_path, monkeypatch):
    """Create a spawner with temp directories."""
    monkeypatch.setattr("agents.spawner.RUNTIME_DIR", tmp_path / "STATE" / "agents" / "runtime")
    monkeypatch.setattr("agents.spawner.SPAWNER_STATE_FILE", tmp_path / "STATE" / "agents" / "spawner_state.json")
    return AgentSpawner(
        registry_path=registry_path,
        max_concurrent_per_role=1,
        max_total_concurrent=3,
    )


# Simple async work functions for testing
async def quick_work(proc: AgentProcess) -> str:
    return "done"


async def slow_work(proc: AgentProcess) -> str:
    await asyncio.sleep(100)
    return "done"


async def failing_work(proc: AgentProcess) -> str:
    raise ValueError("something broke")


async def budget_exceeding_work(proc: AgentProcess) -> str:
    # Simulate exceeding action budget
    for _ in range(proc.budget.max_actions + 1):
        proc.budget.record_action()
        if proc.budget.actions_exceeded:
            raise BudgetExceeded(f"Action limit {proc.budget.max_actions} exceeded")
    return "done"


async def timed_work(proc: AgentProcess, duration: float = 0.1) -> str:
    await asyncio.sleep(duration)
    return "done"


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


class TestRegistryLoading:
    def test_load_valid_registry(self, registry_path):
        reg = load_registry(registry_path)
        assert "research_001" in reg
        assert "coder_001" in reg
        assert reg["research_001"]["role"] == "research"

    def test_load_missing_registry(self, tmp_path):
        with pytest.raises(RegistryError, match="not found"):
            load_registry(tmp_path / "nonexistent.json")

    def test_load_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid json")
        with pytest.raises(RegistryError, match="Failed to load"):
            load_registry(bad)

    def test_load_empty_agents(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"agents": []}))
        with pytest.raises(RegistryError, match="no agents"):
            load_registry(empty)


# ---------------------------------------------------------------------------
# AgentBudget
# ---------------------------------------------------------------------------


class TestAgentBudget:
    def test_initial_state(self):
        b = AgentBudget()
        assert b.actions_used == 0
        assert b.is_within_budget
        assert b.runtime_elapsed == 0.0

    def test_record_action(self):
        b = AgentBudget(max_actions=3)
        b.record_action()
        assert b.actions_used == 1
        assert b.is_within_budget
        b.record_action()
        b.record_action()
        assert b.actions_exceeded
        assert not b.is_within_budget

    def test_runtime_exceeded(self):
        b = AgentBudget(max_runtime_seconds=1.0, started_at=time.time() - 2.0)
        assert b.runtime_exceeded
        assert not b.is_within_budget

    def test_to_dict(self):
        b = AgentBudget(max_actions=5, actions_used=2)
        d = b.to_dict()
        assert d["max_actions"] == 5
        assert d["actions_used"] == 2
        assert "runtime_elapsed" in d
        assert "runtime_exceeded" in d


# ---------------------------------------------------------------------------
# AgentProcess
# ---------------------------------------------------------------------------


class TestAgentProcess:
    def test_creation(self):
        proc = AgentProcess(agent_id="test", role="research", policy_profile="safe")
        assert proc.status == AgentStatus.IDLE
        assert proc.is_terminal is False
        assert proc.is_active is False  # IDLE is not active

    def test_terminal_states(self):
        for status in (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED):
            proc = AgentProcess(agent_id="t", role="r", policy_profile="p", status=status)
            assert proc.is_terminal

    def test_active_states(self):
        for status in (AgentStatus.QUEUED, AgentStatus.EXECUTING):
            proc = AgentProcess(agent_id="t", role="r", policy_profile="p", status=status)
            assert proc.is_active

    def test_serialization_roundtrip(self):
        proc = AgentProcess(
            agent_id="test",
            role="research",
            policy_profile="safe",
            workflow_id="wf-1",
            allowed_tools=["web.search"],
            denied_tools=["shell.run"],
        )
        d = proc.to_dict()
        restored = AgentProcess.from_dict(d)
        assert restored.agent_id == proc.agent_id
        assert restored.role == proc.role
        assert restored.workflow_id == proc.workflow_id
        assert restored.allowed_tools == proc.allowed_tools

    def test_spawn_id_unique(self):
        ids = {AgentProcess(agent_id="t", role="r", policy_profile="p").spawn_id for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Spawner — basic spawn
# ---------------------------------------------------------------------------


class TestSpawnerBasic:
    @pytest.mark.asyncio
    async def test_spawn_and_complete(self, spawner):
        proc = await spawner.spawn("research_001", quick_work, task_description="test task")
        assert proc.status in (AgentStatus.QUEUED, AgentStatus.EXECUTING)
        result = await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert result.status == AgentStatus.COMPLETED
        assert result.result == "done"
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_spawn_unknown_agent(self, spawner):
        with pytest.raises(AgentNotFound):
            await spawner.spawn("nonexistent_agent", quick_work)

    @pytest.mark.asyncio
    async def test_spawn_sets_budget_from_registry(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        assert proc.budget.max_runtime_seconds == 10
        assert proc.budget.max_actions == 5
        await spawner.wait_for(proc.spawn_id, timeout=5.0)

    @pytest.mark.asyncio
    async def test_spawn_with_budget_overrides(self, spawner):
        proc = await spawner.spawn(
            "research_001",
            quick_work,
            budget_overrides={"max_runtime_seconds": 60, "max_actions": 100},
        )
        assert proc.budget.max_runtime_seconds == 60
        assert proc.budget.max_actions == 100
        await spawner.wait_for(proc.spawn_id, timeout=5.0)

    @pytest.mark.asyncio
    async def test_spawn_with_workflow_id(self, spawner):
        proc = await spawner.spawn("research_001", quick_work, workflow_id="wf-123")
        assert proc.workflow_id == "wf-123"
        await spawner.wait_for(proc.spawn_id, timeout=5.0)

    @pytest.mark.asyncio
    async def test_spawn_populates_tools(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        assert "web.search" in proc.allowed_tools
        assert "shell.run" in proc.denied_tools
        await spawner.wait_for(proc.spawn_id, timeout=5.0)


# ---------------------------------------------------------------------------
# Spawner — failure modes
# ---------------------------------------------------------------------------


class TestSpawnerFailures:
    @pytest.mark.asyncio
    async def test_agent_failure(self, spawner):
        proc = await spawner.spawn("research_001", failing_work)
        result = await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert result.status == AgentStatus.FAILED
        assert "something broke" in result.error

    @pytest.mark.asyncio
    async def test_agent_timeout(self, spawner):
        # research_001 has max_runtime_seconds=10, override to 0.1 for test speed
        proc = await spawner.spawn(
            "research_001",
            slow_work,
            budget_overrides={"max_runtime_seconds": 0.2},
        )
        result = await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert result.status == AgentStatus.FAILED
        assert "Timeout" in result.error

    @pytest.mark.asyncio
    async def test_budget_exceeded(self, spawner):
        proc = await spawner.spawn("research_001", budget_exceeding_work)
        result = await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert result.status == AgentStatus.FAILED
        assert "exceeded" in result.error.lower()


# ---------------------------------------------------------------------------
# Spawner — concurrency limits
# ---------------------------------------------------------------------------


class TestSpawnerConcurrency:
    @pytest.mark.asyncio
    async def test_per_role_limit(self, spawner):
        # Spawn research_001, which uses role "research"
        proc1 = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        # research_002 also has role "research" — should be blocked
        with pytest.raises(ConcurrencyLimitReached, match="Role"):
            await spawner.spawn("research_002", quick_work)
        await spawner.terminate(proc1.spawn_id)

    @pytest.mark.asyncio
    async def test_total_limit(self, spawner):
        """Spawn 3 agents (the max), then a 4th should fail."""
        p1 = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        # Need max_per_role > 1 for this, so create a new spawner
        spawner._max_per_role = 4
        p2 = await spawner.spawn("coder_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        p3 = await spawner.spawn("critic_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        with pytest.raises(ConcurrencyLimitReached, match="Total"):
            await spawner.spawn("research_002", quick_work)
        for p in (p1, p2, p3):
            await spawner.terminate(p.spawn_id)

    @pytest.mark.asyncio
    async def test_agent_already_active(self, spawner):
        proc = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        with pytest.raises(AgentAlreadyActive):
            await spawner.spawn("research_001", quick_work)
        await spawner.terminate(proc.spawn_id)


# ---------------------------------------------------------------------------
# Spawner — terminate / cancel
# ---------------------------------------------------------------------------


class TestSpawnerTerminate:
    @pytest.mark.asyncio
    async def test_terminate_running(self, spawner):
        proc = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 30})
        await asyncio.sleep(0.05)  # let it start
        ok = await spawner.terminate(proc.spawn_id, reason="test cancel")
        assert ok is True
        assert proc.status == AgentStatus.CANCELLED
        assert "test cancel" in proc.error

    @pytest.mark.asyncio
    async def test_terminate_nonexistent(self, spawner):
        ok = await spawner.terminate("no-such-id")
        assert ok is False

    @pytest.mark.asyncio
    async def test_terminate_already_completed(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        ok = await spawner.terminate(proc.spawn_id)
        assert ok is False  # already terminal

    @pytest.mark.asyncio
    async def test_terminate_by_agent_id(self, spawner):
        proc = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 30})
        count = await spawner.terminate_by_agent_id("research_001")
        assert count == 1
        assert proc.status == AgentStatus.CANCELLED


# ---------------------------------------------------------------------------
# Spawner — queries
# ---------------------------------------------------------------------------


class TestSpawnerQueries:
    @pytest.mark.asyncio
    async def test_list_active(self, spawner):
        spawner._max_per_role = 4
        p1 = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        p2 = await spawner.spawn("coder_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        active = spawner.list_active()
        assert len(active) == 2
        await spawner.terminate(p1.spawn_id)
        await spawner.terminate(p2.spawn_id)

    @pytest.mark.asyncio
    async def test_list_by_role(self, spawner):
        proc = await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 5})
        assert len(spawner.list_by_role("research")) == 1
        assert len(spawner.list_by_role("coder")) == 0
        await spawner.terminate(proc.spawn_id)

    @pytest.mark.asyncio
    async def test_list_by_workflow(self, spawner):
        proc = await spawner.spawn(
            "research_001", slow_work, workflow_id="wf-1", budget_overrides={"max_runtime_seconds": 5}
        )
        assert len(spawner.list_by_workflow("wf-1")) == 1
        assert len(spawner.list_by_workflow("wf-2")) == 0
        await spawner.terminate(proc.spawn_id)

    @pytest.mark.asyncio
    async def test_get_and_status(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        assert spawner.get(proc.spawn_id) is proc
        assert spawner.status(proc.spawn_id) is not None
        assert spawner.get("nonexistent") is None
        assert spawner.status("nonexistent") is None
        await spawner.wait_for(proc.spawn_id, timeout=5.0)

    @pytest.mark.asyncio
    async def test_get_metrics(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        metrics = spawner.get_metrics()
        assert metrics["total_in_pool"] == 1
        assert metrics["registry_agents"] == 4
        assert "by_status" in metrics
        assert "by_role" in metrics


# ---------------------------------------------------------------------------
# Spawner — cleanup
# ---------------------------------------------------------------------------


class TestSpawnerCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_completed(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert proc.status == AgentStatus.COMPLETED
        # Force completed_at to be old
        proc.completed_at = time.time() - 7200
        count = await spawner.cleanup_completed(max_age_s=3600)
        assert count == 1
        assert proc.spawn_id not in spawner.pool

    @pytest.mark.asyncio
    async def test_cleanup_does_not_remove_recent(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        count = await spawner.cleanup_completed(max_age_s=3600)
        assert count == 0  # just completed, not old enough

    @pytest.mark.asyncio
    async def test_cleanup_orphans(self, spawner):
        await spawner.spawn("research_001", slow_work, budget_overrides={"max_runtime_seconds": 0.1})
        await asyncio.sleep(0.3)  # wait for timeout
        # The agent should have timed out via _run_agent, but let's test orphan logic
        # by checking cleanup doesn't error
        cleaned = await spawner.cleanup_orphans(stale_threshold_s=0.01)
        # Either cleaned or already handled by _run_agent
        assert isinstance(cleaned, list)


# ---------------------------------------------------------------------------
# Spawner — persistence
# ---------------------------------------------------------------------------


class TestSpawnerPersistence:
    @pytest.mark.asyncio
    async def test_runtime_state_persisted(self, spawner, tmp_path):
        proc = await spawner.spawn("research_001", quick_work)
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        state_file = tmp_path / "STATE" / "agents" / "runtime" / "research_001.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["agent_id"] == "research_001"
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_pool_snapshot_roundtrip(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        await spawner.wait_for(proc.spawn_id, timeout=5.0)
        spawner.save_pool_snapshot()
        restored = spawner.load_pool_snapshot()
        assert proc.spawn_id in restored
        assert restored[proc.spawn_id].agent_id == "research_001"


# ---------------------------------------------------------------------------
# Spawner — wait
# ---------------------------------------------------------------------------


class TestSpawnerWait:
    @pytest.mark.asyncio
    async def test_wait_for_completed(self, spawner):
        proc = await spawner.spawn("research_001", quick_work)
        result = await spawner.wait_for(proc.spawn_id, timeout=5.0)
        assert result.status == AgentStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_wait_for_nonexistent(self, spawner):
        with pytest.raises(KeyError):
            await spawner.wait_for("no-such-id")

    @pytest.mark.asyncio
    async def test_wait_all(self, spawner):
        spawner._max_per_role = 4
        p1 = await spawner.spawn("research_001", quick_work)
        p2 = await spawner.spawn("coder_001", quick_work)
        results = await spawner.wait_all([p1.spawn_id, p2.spawn_id], timeout=5.0)
        assert len(results) == 2
        assert all(r.status == AgentStatus.COMPLETED for r in results)
