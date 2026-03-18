"""P11.3 — Orchestrator Runtime Engine.

Executes workflow DAGs by spawning agents, routing messages, and collecting
results. This is the core loop that turns static DAG definitions into
running multi-agent workflows.

Lifecycle:
  1. Load DAG (node definitions with dependencies)
  2. Topological sort → execution order
  3. Spawn ready nodes (dependencies met) as agents
  4. Collect results via message bus
  5. Maker-checker flow: Coder → Critic → Verifier
  6. Synthesize final result
  7. Persist workflow state (resume-on-crash)

Integrates with:
  - AgentSpawner (P11.1): spawns/terminates agents
  - MessageBus (P11.2): routes messages between agents
  - CoordinationLayer: leases, checkpoints
  - WorkflowEngine: halt conditions, limits
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agents.message_bus import MessageBus
from agents.messages import (
    task_assign_message,
)
from agents.spawner import (
    AgentProcess,
    AgentSpawner,
    AgentStatus,
    AgentWorkFn,
    ConcurrencyLimitReached,
    SpawnError,
)
from agents.validation import atomic_write, validate_id

logger = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
# Use distinct path from Blackboard's STATE/workflows/ to avoid schema conflicts
WORKFLOWS_DIR = STATE / "orchestrator_workflows"


# ---------------------------------------------------------------------------
# DAG node / workflow status
# ---------------------------------------------------------------------------


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    HALTED = "halted"


# ---------------------------------------------------------------------------
# DAG definitions
# ---------------------------------------------------------------------------


@dataclass
class DAGNode:
    """A node in the workflow DAG.

    Each node maps to a single agent execution.
    """

    node_id: str
    agent_id: str  # which agent from registry to spawn
    role: str
    goal: str
    depends_on: list[str] = field(default_factory=list)
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    spawn_id: str | None = None
    max_retries: int = 1
    retries: int = 0

    def to_dict(self) -> dict:
        d = {
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "goal": self.goal,
            "depends_on": self.depends_on,
            "status": self.status.value,
            "max_retries": self.max_retries,
            "retries": self.retries,
        }
        if self.result is not None:
            d["result"] = self.result
        if self.error:
            d["error"] = self.error
        if self.started_at:
            d["started_at"] = self.started_at
        if self.completed_at:
            d["completed_at"] = self.completed_at
        if self.spawn_id:
            d["spawn_id"] = self.spawn_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DAGNode:
        return cls(
            node_id=d["node_id"],
            agent_id=d["agent_id"],
            role=d["role"],
            goal=d["goal"],
            depends_on=d.get("depends_on", []),
            status=NodeStatus(d.get("status", "pending")),
            result=d.get("result"),
            error=d.get("error"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            spawn_id=d.get("spawn_id"),
            max_retries=d.get("max_retries", 1),
            retries=d.get("retries", 0),
        )


@dataclass
class WorkflowDAG:
    """A workflow represented as a DAG of agent tasks."""

    workflow_id: str = field(default_factory=lambda: f"wf-{uuid.uuid4().hex[:8]}")
    nodes: dict[str, DAGNode] = field(default_factory=dict)
    status: WorkflowStatus = WorkflowStatus.CREATED
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_node(self, node: DAGNode) -> None:
        self.nodes[node.node_id] = node

    def get_ready_nodes(self) -> list[DAGNode]:
        """Return nodes whose dependencies are all completed."""
        ready = []
        for node in self.nodes.values():
            if node.status != NodeStatus.PENDING:
                continue
            deps_met = all(
                self.nodes[dep].status == NodeStatus.COMPLETED for dep in node.depends_on if dep in self.nodes
            )
            if deps_met:
                ready.append(node)
        return ready

    def is_complete(self) -> bool:
        """All nodes are in a terminal state."""
        return all(
            n.status in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.SKIPPED) for n in self.nodes.values()
        )

    def has_failed_nodes(self) -> bool:
        return any(n.status == NodeStatus.FAILED for n in self.nodes.values())

    def detect_deadlock(self) -> bool:
        """Check if no nodes can progress and workflow isn't complete."""
        if self.is_complete():
            return False
        executing = [n for n in self.nodes.values() if n.status == NodeStatus.EXECUTING]
        if executing:
            return False  # still working
        ready = self.get_ready_nodes()
        return len(ready) == 0

    def topological_order(self) -> list[str]:
        """Return node IDs in topological order. Raises on cycles."""
        visited: set[str] = set()
        temp: set[str] = set()
        order: list[str] = []

        def visit(nid: str) -> None:
            if nid in visited:
                return
            if nid in temp:
                raise ValueError(f"Cycle detected involving node {nid}")
            temp.add(nid)
            node = self.nodes[nid]
            for dep in node.depends_on:
                if dep in self.nodes:
                    visit(dep)
            temp.remove(nid)
            visited.add(nid)
            order.append(nid)

        for nid in self.nodes:
            visit(nid)
        return order

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": self.metadata,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> WorkflowDAG:
        dag = cls(
            workflow_id=d["workflow_id"],
            status=WorkflowStatus(d.get("status", "created")),
            created_at=d.get("created_at", time.time()),
            completed_at=d.get("completed_at"),
            error=d.get("error"),
            metadata=d.get("metadata", {}),
        )
        for nid, nd in d.get("nodes", {}).items():
            dag.nodes[nid] = DAGNode.from_dict(nd)
        return dag


# ---------------------------------------------------------------------------
# Orchestrator Runtime
# ---------------------------------------------------------------------------


class OrchestratorRuntime:
    """Executes a WorkflowDAG by spawning agents and routing messages.

    The runtime loop:
      1. Find ready nodes (dependencies met)
      2. Spawn agents for ready nodes
      3. Wait for any agent to complete
      4. Process results, update node status
      5. Repeat until all nodes are terminal or halted
    """

    def __init__(
        self,
        spawner: AgentSpawner,
        bus: MessageBus,
        *,
        work_fn_factory: Callable[[DAGNode, WorkflowDAG], AgentWorkFn] | None = None,
        persist_dir: Path | None = None,
        max_workflow_runtime_s: float = 1800.0,
        bridge: Any | None = None,
    ):
        self._spawner = spawner
        self._bus = bus
        self._work_fn_factory = work_fn_factory or self._default_work_fn_factory
        self._persist_dir = persist_dir or WORKFLOWS_DIR
        self._max_runtime_s = max_workflow_runtime_s
        self._bridge = bridge  # Optional RuntimeBridge for integration
        self._persist_dir.mkdir(parents=True, exist_ok=True)

    def _default_work_fn_factory(self, node: DAGNode, dag: WorkflowDAG) -> AgentWorkFn:
        """Create a default work function that sends/receives via the bus."""

        async def work(proc: AgentProcess) -> Any:
            # Send task assignment
            msg = task_assign_message(
                from_role="orchestrator",
                to_role=proc.role,
                workflow_id=dag.workflow_id,
                subtask_id=node.node_id,
                goal=node.goal,
            )
            await self._bus.send(msg)

            # The actual work would be done by the agent reading the bus
            # For now, return the goal as a placeholder result
            return {"node_id": node.node_id, "goal": node.goal, "status": "completed"}

        return work

    async def _spawn_ready_nodes(
        self,
        dag: WorkflowDAG,
        active_spawns: dict[str, str],
    ) -> None:
        """Find and spawn agents for all ready DAG nodes."""
        for node in dag.get_ready_nodes():
            try:
                # Claim via CoordinationLayer if bridge is present
                if self._bridge is not None and not self._bridge.claim_node(
                    dag.workflow_id, node.node_id, node.agent_id
                ):
                    logger.debug("Could not claim node %s, skipping", node.node_id)
                    continue

                work_fn = self._work_fn_factory(node, dag)
                proc = await self._spawner.spawn(
                    node.agent_id,
                    work_fn,
                    workflow_id=dag.workflow_id,
                    task_description=node.goal,
                )
                node.status = NodeStatus.EXECUTING
                node.started_at = time.time()
                node.spawn_id = proc.spawn_id
                active_spawns[proc.spawn_id] = node.node_id

                # Sync agent state to blackboard
                if self._bridge is not None:
                    self._bridge.sync_agent_state(
                        node.agent_id,
                        dag.workflow_id,
                        "executing",
                    )
            except ConcurrencyLimitReached:
                logger.debug("Concurrency limit, deferring node %s", node.node_id)
            except SpawnError as e:
                node.status = NodeStatus.FAILED
                node.error = str(e)
                if self._bridge is not None:
                    self._bridge.fail_node(dag.workflow_id, node.node_id, node.agent_id, str(e))

    def _process_completed_agents(
        self,
        dag: WorkflowDAG,
        active_spawns: dict[str, str],
    ) -> bool:
        """Process agents that have reached a terminal state. Returns True if any were processed."""
        done = [
            (sid, nid, p)
            for sid, nid in list(active_spawns.items())
            if (p := self._spawner.get(sid)) is not None and p.is_terminal
        ]
        for spawn_id, node_id, proc in done:
            node = dag.nodes[node_id]
            del active_spawns[spawn_id]
            if proc.status == AgentStatus.COMPLETED:
                node.status = NodeStatus.COMPLETED
                node.result = proc.result
                node.completed_at = time.time()
                if self._bridge is not None:
                    self._bridge.complete_node(
                        dag.workflow_id,
                        node_id,
                        node.agent_id,
                    )
                    self._bridge.sync_agent_state(
                        node.agent_id,
                        dag.workflow_id,
                        "completed",
                    )
            elif node.retries < node.max_retries:
                node.retries += 1
                node.status = NodeStatus.PENDING
                node.spawn_id = None
            else:
                node.status = NodeStatus.FAILED
                node.error = proc.error or "Unknown failure"
                node.completed_at = time.time()
                if self._bridge is not None:
                    self._bridge.fail_node(
                        dag.workflow_id,
                        node_id,
                        node.agent_id,
                        node.error,
                    )
                    self._bridge.sync_agent_state(
                        node.agent_id,
                        dag.workflow_id,
                        "failed",
                        error=node.error,
                    )
        return len(done) > 0

    async def execute(self, dag: WorkflowDAG) -> WorkflowDAG:
        """Execute a workflow DAG to completion.

        Returns the DAG with updated node statuses and results.
        """
        dag.status = WorkflowStatus.RUNNING
        start_time = time.time()
        self._persist_workflow(dag)

        try:
            dag.topological_order()
        except ValueError as e:
            dag.status = WorkflowStatus.FAILED
            dag.error = str(e)
            self._persist_workflow(dag)
            return dag

        active_spawns: dict[str, str] = {}

        try:
            while not dag.is_complete():
                elapsed = time.time() - start_time
                if elapsed > self._max_runtime_s:
                    dag.status = WorkflowStatus.HALTED
                    dag.error = f"Workflow timeout after {elapsed:.0f}s"
                    for sid in list(active_spawns):
                        await self._spawner.terminate(sid, reason="workflow timeout")
                    break

                if dag.detect_deadlock():
                    dag.status = WorkflowStatus.FAILED
                    dag.error = "Deadlock: no nodes can progress"
                    break

                await self._spawn_ready_nodes(dag, active_spawns)
                self._persist_workflow(dag)

                if not active_spawns:
                    if dag.is_complete():
                        break
                    await asyncio.sleep(0.05)
                    continue

                if not self._process_completed_agents(dag, active_spawns):
                    await asyncio.sleep(0.05)
                    continue

                self._persist_workflow(dag)

        except Exception as e:
            dag.status = WorkflowStatus.FAILED
            dag.error = f"Runtime error: {type(e).__name__}: {e}"
            logger.error("Workflow %s failed: %s", dag.workflow_id, e, exc_info=True)
            for sid in list(active_spawns):
                await self._spawner.terminate(sid, reason="workflow error")

        if dag.status == WorkflowStatus.RUNNING:
            if dag.has_failed_nodes():
                dag.status = WorkflowStatus.FAILED
                failed = [n.node_id for n in dag.nodes.values() if n.status == NodeStatus.FAILED]
                dag.error = f"Nodes failed: {', '.join(failed)}"
            else:
                dag.status = WorkflowStatus.COMPLETED
        dag.completed_at = time.time()
        self._persist_workflow(dag)
        return dag

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def _persist_workflow(self, dag: WorkflowDAG) -> None:
        """Save workflow state to disk."""
        try:
            validate_id(dag.workflow_id, "workflow_id")
            path = self._persist_dir / f"{dag.workflow_id}.json"
            atomic_write(path, json.dumps(dag.to_dict(), indent=2, default=str))
        except OSError as e:
            logger.error("Failed to persist workflow %s: %s", dag.workflow_id, e)

    def load_workflow(self, workflow_id: str) -> WorkflowDAG | None:
        """Load a persisted workflow."""
        path = self._persist_dir / f"{workflow_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return WorkflowDAG.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.error("Failed to load workflow %s: %s", workflow_id, e)
            return None

    # -------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """Aggregate metrics from spawner and bus."""
        return {
            "spawner": self._spawner.get_metrics(),
            "bus": self._bus.get_metrics(),
        }
