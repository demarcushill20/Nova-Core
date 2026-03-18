"""P11.1 — Agent Spawner and Lifecycle Manager.

Spawns, tracks, and terminates agent processes as asyncio tasks. Each agent
runs with its own policy profile, budget, and tool allowlist loaded from
the agent registry (STATE/agents/registry.json).

Lifecycle: idle → queued → executing → completed | failed | cancelled
Integrates with: CoordinationLayer (leases), Blackboard (runtime state),
  observability (metrics).

State paths:
  STATE/agents/runtime/<agent_id>.json   — agent runtime state
  STATE/agents/spawner_state.json        — spawner pool snapshot
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agents.validation import atomic_write, validate_id

logger = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
REGISTRY_FILE = STATE / "agents" / "registry.json"
# Use distinct path from Blackboard's STATE/agents/runtime/ to avoid schema conflicts
RUNTIME_DIR = STATE / "agents" / "spawner_runtime"
SPAWNER_STATE_FILE = STATE / "agents" / "spawner_state.json"


# ---------------------------------------------------------------------------
# Agent status
# ---------------------------------------------------------------------------


class AgentStatus(str, Enum):
    """Lifecycle states for a spawned agent."""

    IDLE = "idle"
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Terminal states — agents in these states cannot transition further.
_TERMINAL_STATES = {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED}


# ---------------------------------------------------------------------------
# Agent budget (runtime)
# ---------------------------------------------------------------------------


@dataclass
class AgentBudget:
    """Runtime budget constraints for a single agent execution."""

    max_runtime_seconds: float = 300.0
    max_actions: int = 20
    max_retries: int = 1
    actions_used: int = 0
    retries_used: int = 0
    started_at: float | None = None

    @property
    def runtime_elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    @property
    def runtime_exceeded(self) -> bool:
        return self.runtime_elapsed > self.max_runtime_seconds

    @property
    def actions_exceeded(self) -> bool:
        return self.actions_used >= self.max_actions

    @property
    def is_within_budget(self) -> bool:
        return not self.runtime_exceeded and not self.actions_exceeded

    def record_action(self) -> None:
        self.actions_used += 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["runtime_elapsed"] = self.runtime_elapsed
        d["runtime_exceeded"] = self.runtime_exceeded
        d["actions_exceeded"] = self.actions_exceeded
        return d


# ---------------------------------------------------------------------------
# Agent process
# ---------------------------------------------------------------------------


@dataclass
class AgentProcess:
    """Runtime representation of a spawned agent."""

    agent_id: str
    role: str
    policy_profile: str
    status: AgentStatus = AgentStatus.IDLE
    workflow_id: str | None = None
    task_description: str = ""
    budget: AgentBudget = field(default_factory=AgentBudget)
    spawn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_agent_id: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    result: Any = None
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)

    # Internal — not serialised
    _task: asyncio.Task | None = field(default=None, repr=False, compare=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.status in (AgentStatus.QUEUED, AgentStatus.EXECUTING)

    def to_dict(self) -> dict:
        d = {
            "agent_id": self.agent_id,
            "role": self.role,
            "policy_profile": self.policy_profile,
            "status": self.status.value,
            "workflow_id": self.workflow_id,
            "task_description": self.task_description,
            "budget": self.budget.to_dict(),
            "spawn_id": self.spawn_id,
            "parent_agent_id": self.parent_agent_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "allowed_tools": self.allowed_tools,
            "denied_tools": self.denied_tools,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> AgentProcess:
        d = dict(d)  # shallow copy — don't mutate caller's dict
        budget_data = dict(d.pop("budget", {}))
        budget_data.pop("runtime_elapsed", None)
        budget_data.pop("runtime_exceeded", None)
        budget_data.pop("actions_exceeded", None)
        status = d.pop("status", "idle")
        d.pop("result", None)
        d.pop("_task", None)
        proc = cls(
            **{
                k: v
                for k, v in d.items()
                if k in {f.name for f in cls.__dataclass_fields__.values()} and k not in ("status", "budget")
            },
            status=AgentStatus(status),
            budget=AgentBudget(
                **{
                    k: v
                    for k, v in budget_data.items()
                    if k in {f.name for f in AgentBudget.__dataclass_fields__.values()}
                }
            ),
        )
        return proc


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Raised when the agent registry is invalid or missing."""


def load_registry(path: Path | None = None) -> dict[str, dict]:
    """Load agent definitions from registry.json.

    Returns:
        Dict of agent_id → agent config dict.
    """
    path = path or REGISTRY_FILE
    if not path.exists():
        raise RegistryError(f"Agent registry not found: {path}")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RegistryError(f"Failed to load registry: {e}") from e

    agents = data.get("agents", [])
    if not agents:
        raise RegistryError("Registry contains no agents")

    return {a["agent_id"]: a for a in agents}


# ---------------------------------------------------------------------------
# Spawner errors
# ---------------------------------------------------------------------------


class SpawnError(Exception):
    """Base error for spawner operations."""


class AgentNotFound(SpawnError):
    """Agent ID not in registry."""


class ConcurrencyLimitReached(SpawnError):
    """Too many agents of this role are already running."""


class BudgetExceeded(SpawnError):
    """Agent exceeded its runtime budget."""


class AgentAlreadyActive(SpawnError):
    """Agent is already executing — cannot spawn again."""


# ---------------------------------------------------------------------------
# Agent Spawner
# ---------------------------------------------------------------------------

# Type alias for the async work function an agent executes
AgentWorkFn = Callable[[AgentProcess], Coroutine[Any, Any, Any]]


class AgentSpawner:
    """Manages the lifecycle of agent processes.

    Each agent runs as an asyncio task. The spawner enforces:
      - Max concurrent agents per role (default 1)
      - Budget limits (runtime, actions)
      - Unique spawn IDs
      - Lease integration (optional, via CoordinationLayer)
      - Orphan cleanup
    """

    def __init__(
        self,
        registry_path: Path | None = None,
        max_concurrent_per_role: int = 1,
        max_total_concurrent: int = 4,
    ):
        self._registry = load_registry(registry_path)
        self._max_per_role = max_concurrent_per_role
        self._max_total = max_total_concurrent
        self._pool: dict[str, AgentProcess] = {}  # spawn_id → AgentProcess
        self._lock = asyncio.Lock()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def registry(self) -> dict[str, dict]:
        return dict(self._registry)

    @property
    def pool(self) -> dict[str, AgentProcess]:
        return dict(self._pool)

    # -------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------

    def list_active(self) -> list[AgentProcess]:
        """Return all non-terminal agents."""
        return [p for p in self._pool.values() if p.is_active]

    def list_by_role(self, role: str) -> list[AgentProcess]:
        """Return active agents for a given role."""
        return [p for p in self._pool.values() if p.role == role and p.is_active]

    def list_by_workflow(self, workflow_id: str) -> list[AgentProcess]:
        """Return active agents for a given workflow."""
        return [p for p in self._pool.values() if p.workflow_id == workflow_id and p.is_active]

    def get(self, spawn_id: str) -> AgentProcess | None:
        """Get an agent process by spawn_id."""
        return self._pool.get(spawn_id)

    def status(self, spawn_id: str) -> AgentStatus | None:
        """Get the status of an agent by spawn_id."""
        proc = self._pool.get(spawn_id)
        return proc.status if proc else None

    # -------------------------------------------------------------------
    # Spawn
    # -------------------------------------------------------------------

    async def spawn(
        self,
        agent_id: str,
        work_fn: AgentWorkFn,
        *,
        workflow_id: str | None = None,
        task_description: str = "",
        budget_overrides: dict | None = None,
    ) -> AgentProcess:
        """Spawn an agent as an asyncio task.

        Args:
            agent_id: Must match an entry in registry.json.
            work_fn: Async callable(AgentProcess) → result.
            workflow_id: Optional workflow context.
            task_description: Human-readable description of the work.
            budget_overrides: Override max_runtime_seconds, max_actions, etc.

        Returns:
            The AgentProcess (status will be QUEUED then EXECUTING).

        Raises:
            AgentNotFound: agent_id not in registry.
            ConcurrencyLimitReached: too many agents of this role running.
            AgentAlreadyActive: this agent_id is already executing.
        """
        async with self._lock:
            # Validate ID safety (prevents path traversal in persistence)
            validate_id(agent_id, "agent_id")
            if workflow_id is not None:
                validate_id(workflow_id, "workflow_id")

            # Validate agent exists in registry
            config = self._registry.get(agent_id)
            if config is None:
                raise AgentNotFound(f"Agent '{agent_id}' not in registry")

            role = config["role"]

            # Check if this agent_id is already active
            for proc in self._pool.values():
                if proc.agent_id == agent_id and proc.is_active:
                    raise AgentAlreadyActive(f"Agent '{agent_id}' is already active (spawn_id={proc.spawn_id})")

            # Enforce per-role concurrency
            active_role = self.list_by_role(role)
            if len(active_role) >= self._max_per_role:
                raise ConcurrencyLimitReached(
                    f"Role '{role}' has {len(active_role)}/{self._max_per_role} active agents"
                )

            # Enforce total concurrency
            total_active = self.list_active()
            if len(total_active) >= self._max_total:
                raise ConcurrencyLimitReached(f"Total active agents: {len(total_active)}/{self._max_total}")

            # Build budget from registry + overrides
            budget = AgentBudget(
                max_runtime_seconds=config.get("max_runtime_seconds", 300),
                max_actions=config.get("max_actions", 20),
                max_retries=config.get("max_retries", 1),
            )
            _BUDGET_OVERRIDABLE = {"max_runtime_seconds", "max_actions", "max_retries"}
            if budget_overrides:
                for k, v in budget_overrides.items():
                    if k in _BUDGET_OVERRIDABLE:
                        setattr(budget, k, v)

            # Create agent process
            proc = AgentProcess(
                agent_id=agent_id,
                role=role,
                policy_profile=config.get("policy_profile", "default"),
                status=AgentStatus.QUEUED,
                workflow_id=workflow_id,
                task_description=task_description,
                budget=budget,
                parent_agent_id=config.get("parent_agent_id"),
                allowed_tools=list(config.get("allowed_tools", [])),
                denied_tools=list(config.get("denied_tools", [])),
            )

            self._pool[proc.spawn_id] = proc
            self._persist_runtime_state(proc)

            # Launch the async task
            proc._task = asyncio.create_task(
                self._run_agent(proc, work_fn),
                name=f"agent-{proc.agent_id}-{proc.spawn_id}",
            )

            logger.info(
                "Spawned agent %s (role=%s, spawn_id=%s, workflow=%s)",
                agent_id,
                role,
                proc.spawn_id,
                workflow_id,
            )
            return proc

    async def _run_agent(self, proc: AgentProcess, work_fn: AgentWorkFn) -> None:
        """Internal wrapper that manages the agent lifecycle."""
        try:
            # Transition to EXECUTING
            proc.status = AgentStatus.EXECUTING
            proc.started_at = time.time()
            proc.budget.started_at = proc.started_at
            self._persist_runtime_state(proc)

            # Run with timeout
            result = await asyncio.wait_for(
                work_fn(proc),
                timeout=proc.budget.max_runtime_seconds,
            )

            # Success
            proc.status = AgentStatus.COMPLETED
            proc.result = result
            proc.completed_at = time.time()
            logger.info(
                "Agent %s completed (spawn_id=%s, elapsed=%.1fs, actions=%d)",
                proc.agent_id,
                proc.spawn_id,
                proc.completed_at - (proc.started_at or proc.completed_at),
                proc.budget.actions_used,
            )

        except asyncio.TimeoutError:
            proc.status = AgentStatus.FAILED
            proc.error = f"Timeout after {proc.budget.max_runtime_seconds}s"
            proc.completed_at = time.time()
            logger.warning("Agent %s timed out (spawn_id=%s)", proc.agent_id, proc.spawn_id)

        except asyncio.CancelledError:
            proc.status = AgentStatus.CANCELLED
            proc.error = "Cancelled"
            proc.completed_at = time.time()
            logger.info("Agent %s cancelled (spawn_id=%s)", proc.agent_id, proc.spawn_id)

        except BudgetExceeded as e:
            proc.status = AgentStatus.FAILED
            proc.error = str(e)
            proc.completed_at = time.time()
            logger.warning("Agent %s budget exceeded: %s", proc.agent_id, e)

        except Exception as e:
            proc.status = AgentStatus.FAILED
            proc.error = f"{type(e).__name__}: {e}"
            proc.completed_at = time.time()
            logger.error("Agent %s failed: %s", proc.agent_id, e, exc_info=True)

        finally:
            self._persist_runtime_state(proc)

    # -------------------------------------------------------------------
    # Terminate / Cancel
    # -------------------------------------------------------------------

    async def terminate(self, spawn_id: str, reason: str = "operator request") -> bool:
        """Cancel a running agent task.

        Returns True if the agent was cancelled, False if not found or already terminal.
        """
        proc = self._pool.get(spawn_id)
        if proc is None or proc.is_terminal:
            return False

        if proc._task is not None and not proc._task.done():
            proc._task.cancel()
            # Wait briefly for cancellation to propagate (asyncio.wait does
            # not cancel the task on timeout, unlike wait_for).
            try:
                await asyncio.wait({proc._task}, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Always overwrite with the terminate reason (the CancelledError handler
        # in _run_agent may have set a generic "Cancelled" message first).
        proc.status = AgentStatus.CANCELLED
        proc.error = f"Terminated: {reason}"
        if proc.completed_at is None:
            proc.completed_at = time.time()
        self._persist_runtime_state(proc)

        logger.info("Terminated agent %s (spawn_id=%s): %s", proc.agent_id, spawn_id, reason)
        return True

    async def terminate_by_agent_id(self, agent_id: str, reason: str = "operator request") -> int:
        """Cancel all active instances of an agent. Returns count terminated."""
        count = 0
        for proc in list(self._pool.values()):
            if proc.agent_id == agent_id and proc.is_active and await self.terminate(proc.spawn_id, reason):
                count += 1
        return count

    # -------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------

    async def cleanup_completed(self, max_age_s: float = 3600.0) -> int:
        """Remove terminal agents older than max_age_s from the pool.

        Returns count of agents cleaned up.
        """
        now = time.time()
        to_remove = []
        for spawn_id, proc in self._pool.items():
            if proc.is_terminal and proc.completed_at is not None and now - proc.completed_at > max_age_s:
                to_remove.append(spawn_id)

        for spawn_id in to_remove:
            del self._pool[spawn_id]

        if to_remove:
            logger.info("Cleaned up %d completed agents", len(to_remove))
        return len(to_remove)

    async def cleanup_orphans(self, stale_threshold_s: float = 600.0) -> list[str]:
        """Find and terminate agents whose tasks are done but status isn't terminal.

        Also catches agents stuck in EXECUTING beyond their budget + stale threshold.
        Returns list of spawn_ids that were cleaned up.
        """
        cleaned = []
        now = time.time()

        for spawn_id, proc in list(self._pool.items()):
            # Task finished but status not updated (shouldn't happen, but defensive)
            if proc._task is not None and proc._task.done() and not proc.is_terminal:
                proc.status = AgentStatus.FAILED
                proc.error = "Orphaned: task completed without status update"
                proc.completed_at = now
                self._persist_runtime_state(proc)
                cleaned.append(spawn_id)
                continue

            # Stuck in executing well beyond budget
            if proc.status == AgentStatus.EXECUTING and proc.started_at is not None:
                max_time = proc.budget.max_runtime_seconds + stale_threshold_s
                if now - proc.started_at > max_time:
                    await self.terminate(spawn_id, reason="orphan: exceeded budget + stale threshold")
                    cleaned.append(spawn_id)

        if cleaned:
            logger.warning("Cleaned up %d orphan agents: %s", len(cleaned), cleaned)
        return cleaned

    # -------------------------------------------------------------------
    # Await
    # -------------------------------------------------------------------

    async def wait_for(self, spawn_id: str, timeout: float | None = None) -> AgentProcess:
        """Wait for an agent to reach a terminal state.

        Args:
            spawn_id: The spawn_id of the agent.
            timeout: Optional timeout in seconds.

        Returns:
            The AgentProcess in its terminal state.

        Raises:
            KeyError: If spawn_id not found.
            asyncio.TimeoutError: If timeout exceeded.
        """
        proc = self._pool.get(spawn_id)
        if proc is None:
            raise KeyError(f"No agent with spawn_id={spawn_id}")

        if proc.is_terminal:
            return proc

        if proc._task is None:
            return proc

        await asyncio.wait_for(proc._task, timeout=timeout)
        return proc

    async def wait_all(self, spawn_ids: list[str], timeout: float | None = None) -> list[AgentProcess]:
        """Wait for multiple agents to complete."""
        tasks = []
        for sid in spawn_ids:
            proc = self._pool.get(sid)
            if proc is not None and proc._task is not None and not proc._task.done():
                tasks.append(proc._task)

        if tasks:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )

        return [self._pool[sid] for sid in spawn_ids if sid in self._pool]

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def _persist_runtime_state(self, proc: AgentProcess) -> None:
        """Write agent runtime state to disk (atomic)."""
        try:
            path = RUNTIME_DIR / f"{proc.agent_id}.json"
            atomic_write(path, json.dumps(proc.to_dict(), indent=2, default=str))
        except OSError as e:
            logger.error("Failed to persist agent state for %s: %s", proc.agent_id, e)

    def save_pool_snapshot(self) -> None:
        """Save current pool state to disk for crash recovery."""
        try:
            snapshot = {
                "timestamp": time.time(),
                "agents": {sid: proc.to_dict() for sid, proc in self._pool.items()},
            }
            atomic_write(SPAWNER_STATE_FILE, json.dumps(snapshot, indent=2, default=str))
        except OSError as e:
            logger.error("Failed to save pool snapshot: %s", e)

    def load_pool_snapshot(self) -> dict[str, AgentProcess]:
        """Load pool snapshot from disk (for crash recovery).

        Returns dict of spawn_id → AgentProcess. Does NOT restore asyncio tasks.
        Caller must decide whether to re-spawn or mark as failed.
        """
        if not SPAWNER_STATE_FILE.exists():
            return {}
        try:
            data = json.loads(SPAWNER_STATE_FILE.read_text())
            agents = data.get("agents", {})
            return {sid: AgentProcess.from_dict(d) for sid, d in agents.items()}
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.error("Failed to load pool snapshot: %s", e)
            return {}

    # -------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """Return spawner metrics for observability."""
        active = self.list_active()
        by_role: dict[str, int] = {}
        for proc in active:
            by_role[proc.role] = by_role.get(proc.role, 0) + 1

        by_status: dict[str, int] = {}
        for proc in self._pool.values():
            s = proc.status.value
            by_status[s] = by_status.get(s, 0) + 1

        return {
            "total_in_pool": len(self._pool),
            "active_count": len(active),
            "max_total_concurrent": self._max_total,
            "max_per_role": self._max_per_role,
            "by_role": by_role,
            "by_status": by_status,
            "registry_agents": len(self._registry),
        }
