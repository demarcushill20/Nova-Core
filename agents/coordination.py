"""Phase 7.3 — Shared Coordination Layer.

Provides:
  1. NodeLease: file-based lock/lease for workflow node claiming
  2. WorkflowCheckpoint: enhanced workflow persistence with node-level state
  3. Resume helpers: reconstruct workflow state after restart
  4. DelegationRecord: enriched delegation tracking with retry metadata

All state persisted under STATE/ using atomic file operations from Blackboard.

State paths added:
  STATE/leases/<workflow_id>_<node_id>.json     — active leases
  STATE/workflows/<workflow_id>.json            — enhanced with node_states + checkpoints
  STATE/delegations/<wf>_<subtask>.json         — extended with retry/reassignment metadata
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.blackboard import Blackboard, WorkflowState, Delegation

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
STATE = BASE / "STATE"

# Default lease TTL — 10 minutes; nodes not renewed within TTL are stale.
DEFAULT_LEASE_TTL_S = 600


# ---------------------------------------------------------------------------
# Node state tracking
# ---------------------------------------------------------------------------

@dataclass
class NodeState:
    """State of a single node within a workflow DAG."""
    node_id: str
    workflow_id: str
    status: str = "pending"  # pending|blocked|claimed|executing|completed|failed|stale
    assigned_agent: str | None = None
    depends_on: list[str] = field(default_factory=list)
    claimed_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 1
    output_ref: str | None = None  # path to output artifact

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "NodeState":
        """Reconstruct from persisted dict, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Lease model
# ---------------------------------------------------------------------------

@dataclass
class Lease:
    """File-based lease for a workflow node.

    A lease grants exclusive claim to process a workflow node.
    Leases expire after ttl_s seconds and can be recovered by
    other claimants.
    """
    workflow_id: str
    node_id: str
    holder: str           # agent_id or process identifier
    acquired_at: float = field(default_factory=time.time)
    ttl_s: float = DEFAULT_LEASE_TTL_S
    renewed_at: float | None = None

    @property
    def expires_at(self) -> float:
        base = self.renewed_at or self.acquired_at
        return base + self.ttl_s

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expires_at"] = self.expires_at
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Coordination layer
# ---------------------------------------------------------------------------

class CoordinationLayer:
    """Shared coordination substrate for workflow and delegation persistence.

    Extends the existing Blackboard with:
      - Lease-based node claiming (prevents double-execution)
      - Node-level state tracking within workflows
      - Resume-safe checkpoint/restore helpers
      - Enriched delegation metadata

    Does NOT replace Blackboard or WorkflowEngine — it provides the
    persistence primitives they need for safe concurrent coordination.
    """

    def __init__(self, blackboard: Blackboard | None = None):
        self.bb = blackboard or Blackboard()
        self.leases_dir = self.bb.state / "leases"
        self.leases_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Lease management
    # -----------------------------------------------------------------------

    def _lease_path(self, workflow_id: str, node_id: str) -> Path:
        return self.leases_dir / f"{workflow_id}_{node_id}.json"

    def acquire_lease(self, workflow_id: str, node_id: str, holder: str,
                      ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        """Attempt to acquire an exclusive lease on a workflow node.

        Raises:
            LeaseConflict: if the node is already held by an active lease.
        """
        path = self._lease_path(workflow_id, node_id)

        # Check for existing lease
        existing = self.bb._read_json(path)
        if existing is not None:
            lease = Lease.from_dict(existing)
            if not lease.is_expired:
                raise LeaseConflict(
                    f"Node {workflow_id}/{node_id} already held by "
                    f"{lease.holder} until {lease.expires_at:.0f}"
                )
            # Stale lease — allow takeover, but record it
            self._record_stale_recovery(workflow_id, node_id, lease.holder, holder)

        # Acquire
        new_lease = Lease(
            workflow_id=workflow_id,
            node_id=node_id,
            holder=holder,
            acquired_at=time.time(),
            ttl_s=ttl_s,
        )
        self._write_lease(new_lease)
        return new_lease

    def renew_lease(self, workflow_id: str, node_id: str,
                    holder: str) -> Lease:
        """Renew an existing lease. Only the holder can renew.

        Raises:
            LeaseNotFound: if no lease exists.
            LeaseConflict: if caller is not the holder.
        """
        path = self._lease_path(workflow_id, node_id)
        existing = self.bb._read_json(path)
        if existing is None:
            raise LeaseNotFound(f"No lease for {workflow_id}/{node_id}")

        lease = Lease.from_dict(existing)
        if lease.holder != holder:
            raise LeaseConflict(
                f"Lease held by {lease.holder}, not {holder}"
            )

        lease.renewed_at = time.time()
        self._write_lease(lease)
        return lease

    def release_lease(self, workflow_id: str, node_id: str,
                      holder: str) -> None:
        """Release a lease. Only the holder can release.

        Silently succeeds if the lease doesn't exist (idempotent).
        """
        path = self._lease_path(workflow_id, node_id)
        existing = self.bb._read_json(path)
        if existing is None:
            return

        lease = Lease.from_dict(existing)
        if lease.holder != holder and not lease.is_expired:
            raise LeaseConflict(
                f"Cannot release: lease held by {lease.holder}, not {holder}"
            )

        # Remove lease file
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def get_lease(self, workflow_id: str, node_id: str) -> Lease | None:
        """Read the current lease for a node, if any."""
        path = self._lease_path(workflow_id, node_id)
        data = self.bb._read_json(path)
        if data is None:
            return None
        return Lease.from_dict(data)

    def list_leases(self, workflow_id: str | None = None) -> list[Lease]:
        """List all active leases, optionally filtered by workflow."""
        results = []
        for f in sorted(self.leases_dir.glob("*.json")):
            data = self.bb._read_json(f)
            if data is None:
                continue
            lease = Lease.from_dict(data)
            if workflow_id is None or lease.workflow_id == workflow_id:
                results.append(lease)
        return results

    def recover_stale_leases(self, workflow_id: str | None = None) -> list[Lease]:
        """Find and remove all expired leases. Returns the recovered leases."""
        recovered = []
        for lease in self.list_leases(workflow_id):
            if lease.is_expired:
                path = self._lease_path(lease.workflow_id, lease.node_id)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                recovered.append(lease)
        return recovered

    def _write_lease(self, lease: Lease) -> None:
        """Persist lease to disk atomically."""
        path = self._lease_path(lease.workflow_id, lease.node_id)
        self.bb._write_json(path, lease.to_dict())

    def _record_stale_recovery(self, workflow_id: str, node_id: str,
                               old_holder: str, new_holder: str) -> None:
        """Log stale lease recovery for auditability."""
        log_path = self.bb.state / "leases" / "recovery.jsonl"
        record = {
            "ts": time.time(),
            "workflow_id": workflow_id,
            "node_id": node_id,
            "old_holder": old_holder,
            "new_holder": new_holder,
            "event": "stale_lease_recovered",
        }
        self.bb._append_jsonl(log_path, record)

    # -----------------------------------------------------------------------
    # Enhanced workflow persistence (node-level state + checkpoints)
    # -----------------------------------------------------------------------

    def save_node_states(self, workflow_id: str,
                         nodes: list[NodeState]) -> None:
        """Persist node-level state into the workflow state file.

        Merges node_states into the existing workflow JSON so that
        orchestrators can reconstruct the full DAG state after restart.
        """
        node_dicts = {n.node_id: n.to_dict() for n in nodes}
        self.bb.update_workflow(workflow_id, {
            "node_states": node_dicts,
        })

    def get_node_states(self, workflow_id: str) -> dict[str, NodeState]:
        """Read node-level state from the workflow file."""
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            return {}
        raw = wf.get("node_states", {})
        return {nid: NodeState.from_dict(ns) for nid, ns in raw.items()}

    def update_node_state(self, workflow_id: str, node_id: str,
                          updates: dict) -> NodeState:
        """Update a single node's state within the workflow.

        Returns the updated NodeState.
        """
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            raise FileNotFoundError(f"Workflow not found: {workflow_id}")

        node_states = wf.get("node_states", {})
        current = node_states.get(node_id, {"node_id": node_id,
                                             "workflow_id": workflow_id})
        current.update(updates)
        current["node_id"] = node_id
        current["workflow_id"] = workflow_id
        node_states[node_id] = current

        self.bb.update_workflow(workflow_id, {"node_states": node_states})
        return NodeState.from_dict(current)

    def save_checkpoint(self, workflow_id: str, checkpoint: dict) -> None:
        """Save a resume checkpoint into the workflow state.

        Checkpoints capture orchestrator progress so that execution
        can resume after a restart from the last known-good point.
        """
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            raise FileNotFoundError(f"Workflow not found: {workflow_id}")

        checkpoints = wf.get("checkpoints", [])
        checkpoint["saved_at"] = time.time()
        checkpoints.append(checkpoint)

        self.bb.update_workflow(workflow_id, {"checkpoints": checkpoints})

    def get_latest_checkpoint(self, workflow_id: str) -> dict | None:
        """Get the most recent checkpoint for a workflow."""
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            return None
        checkpoints = wf.get("checkpoints", [])
        return checkpoints[-1] if checkpoints else None

    # -----------------------------------------------------------------------
    # Resume support
    # -----------------------------------------------------------------------

    def resume_workflow(self, workflow_id: str) -> dict:
        """Reconstruct the full resumable state of a workflow.

        Returns a dict with:
          - workflow: the persisted WorkflowState
          - node_states: per-node NodeState objects
          - pending_nodes: nodes ready to execute (deps satisfied)
          - executing_nodes: nodes currently claimed/executing
          - blocked_nodes: nodes waiting on dependencies
          - completed_nodes: finished nodes
          - stale_nodes: nodes with expired leases
          - latest_checkpoint: last saved checkpoint
          - delegations: all delegation records
          - active_leases: current leases for this workflow
        """
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            raise FileNotFoundError(f"Workflow not found: {workflow_id}")

        node_states = self.get_node_states(workflow_id)
        leases = self.list_leases(workflow_id)
        lease_map = {l.node_id: l for l in leases}

        # Classify nodes
        pending = []
        executing = []
        blocked = []
        completed = []
        stale = []
        failed = []

        for nid, ns in node_states.items():
            # Check for stale leases
            if nid in lease_map and lease_map[nid].is_expired:
                stale.append(nid)
                continue

            if ns.status in ("completed",):
                completed.append(nid)
            elif ns.status in ("failed",):
                failed.append(nid)
            elif ns.status in ("claimed", "executing"):
                # Verify lease still valid
                if nid in lease_map and not lease_map[nid].is_expired:
                    executing.append(nid)
                else:
                    stale.append(nid)
            elif ns.status == "blocked":
                blocked.append(nid)
            elif ns.status == "pending":
                # Check if dependencies are satisfied
                deps_satisfied = all(
                    node_states.get(dep, NodeState(node_id=dep,
                                                   workflow_id=workflow_id)
                                    ).status == "completed"
                    for dep in ns.depends_on
                )
                if deps_satisfied:
                    pending.append(nid)
                else:
                    blocked.append(nid)
            else:
                # stale or unknown status
                stale.append(nid)

        return {
            "workflow": wf,
            "node_states": {nid: ns.to_dict() for nid, ns in node_states.items()},
            "pending_nodes": pending,
            "executing_nodes": executing,
            "blocked_nodes": blocked,
            "completed_nodes": completed,
            "failed_nodes": failed,
            "stale_nodes": stale,
            "latest_checkpoint": self.get_latest_checkpoint(workflow_id),
            "delegations": self.bb.list_delegations(workflow_id),
            "active_leases": [l.to_dict() for l in leases if not l.is_expired],
        }

    def recover_workflow(self, workflow_id: str) -> dict:
        """Recover a workflow after restart or crash.

        1. Recover stale leases
        2. Reset stale nodes to pending (if retries remain)
        3. Return the resume state

        Returns the same structure as resume_workflow() plus a
        recovery_actions list.
        """
        recovery_actions = []

        # 1. Recover stale leases
        stale_leases = self.recover_stale_leases(workflow_id)
        for sl in stale_leases:
            recovery_actions.append({
                "action": "stale_lease_removed",
                "node_id": sl.node_id,
                "old_holder": sl.holder,
            })

        # 2. Reset stale nodes
        node_states = self.get_node_states(workflow_id)
        for nid, ns in node_states.items():
            if ns.status in ("claimed", "executing"):
                # Check if lease exists — if not, the node is stale
                lease = self.get_lease(workflow_id, nid)
                if lease is None or lease.is_expired:
                    if ns.retry_count < ns.max_retries:
                        self.update_node_state(workflow_id, nid, {
                            "status": "pending",
                            "assigned_agent": None,
                            "claimed_at": None,
                            "started_at": None,
                            "retry_count": ns.retry_count + 1,
                        })
                        recovery_actions.append({
                            "action": "node_reset_for_retry",
                            "node_id": nid,
                            "retry_count": ns.retry_count + 1,
                        })
                    else:
                        self.update_node_state(workflow_id, nid, {
                            "status": "failed",
                            "error": "max retries exceeded after stale lease",
                            "completed_at": time.time(),
                        })
                        recovery_actions.append({
                            "action": "node_failed_max_retries",
                            "node_id": nid,
                        })

        # 3. Return resume state with recovery info
        state = self.resume_workflow(workflow_id)
        state["recovery_actions"] = recovery_actions
        return state

    # -----------------------------------------------------------------------
    # Coordinated node claiming (combines lease + node state + delegation)
    # -----------------------------------------------------------------------

    def claim_node(self, workflow_id: str, node_id: str,
                   agent_id: str, ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        """Atomically claim a workflow node: acquire lease + update state.

        This is the primary coordination entry point. It ensures:
          1. No duplicate execution (lease prevents double-claim)
          2. Node state is updated to 'claimed'
          3. Delegation record is updated if one exists

        Raises:
            LeaseConflict: if the node is already held by an active lease.
        """
        # Acquire lease first (this is the guard)
        lease = self.acquire_lease(workflow_id, node_id, agent_id, ttl_s)

        # Update node state
        self.update_node_state(workflow_id, node_id, {
            "status": "claimed",
            "assigned_agent": agent_id,
            "claimed_at": time.time(),
        })

        # Update delegation if one exists
        try:
            self.bb.update_delegation(workflow_id, node_id, {
                "status": "claimed",
                "claimed_at": time.time(),
            })
        except FileNotFoundError:
            pass  # No delegation record — that's fine for direct node claims

        return lease

    def complete_node(self, workflow_id: str, node_id: str,
                      agent_id: str, output_ref: str | None = None) -> None:
        """Mark a node as completed and release its lease.

        Updates node state, releases the lease, and updates the
        delegation record if one exists.
        """
        # Update node state
        self.update_node_state(workflow_id, node_id, {
            "status": "completed",
            "completed_at": time.time(),
            "output_ref": output_ref,
        })

        # Release lease
        self.release_lease(workflow_id, node_id, agent_id)

        # Update delegation if one exists
        try:
            self.bb.update_delegation(workflow_id, node_id, {
                "status": "completed",
                "completed_at": time.time(),
            })
        except FileNotFoundError:
            pass

    def fail_node(self, workflow_id: str, node_id: str,
                  agent_id: str, error: str) -> None:
        """Mark a node as failed and release its lease."""
        self.update_node_state(workflow_id, node_id, {
            "status": "failed",
            "completed_at": time.time(),
            "error": error,
        })

        self.release_lease(workflow_id, node_id, agent_id)

        try:
            self.bb.update_delegation(workflow_id, node_id, {
                "status": "failed",
                "completed_at": time.time(),
                "error": error,
            })
        except FileNotFoundError:
            pass

    # -----------------------------------------------------------------------
    # Dependency resolution
    # -----------------------------------------------------------------------

    def get_ready_nodes(self, workflow_id: str) -> list[str]:
        """Return node IDs whose dependencies are all satisfied and
        that are in pending status (ready to be claimed)."""
        node_states = self.get_node_states(workflow_id)
        ready = []
        for nid, ns in node_states.items():
            if ns.status != "pending":
                continue
            deps_satisfied = all(
                node_states.get(dep, NodeState(node_id=dep,
                                               workflow_id=workflow_id)
                                ).status == "completed"
                for dep in ns.depends_on
            )
            if deps_satisfied:
                ready.append(nid)
        return ready


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LeaseConflict(Exception):
    """Raised when a lease cannot be acquired due to an active holder."""
    pass


class LeaseNotFound(Exception):
    """Raised when a lease operation targets a non-existent lease."""
    pass
