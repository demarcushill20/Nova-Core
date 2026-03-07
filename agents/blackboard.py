"""Blackboard state manager for Phase 7 multi-agent orchestration.

All agent coordination goes through the blackboard (STATE/ directory).
Workers never communicate directly — they read/write structured state paths
and the orchestrator makes routing decisions based on that state.

State paths:
  STATE/delegations/<workflow_id>_<subtask_id>.json  — delegation entries
  STATE/agents/runtime/<agent_id>.json               — agent runtime state
  STATE/workflows/<workflow_id>.json                  — workflow state
  STATE/budgets/<agent_id>.json                       — budget tracking
  WORK/agents/messages/<workflow_id>/<agent_id>.jsonl — append-only message log
  WORK/agents/contracts/<subtask_id>.json             — child contracts
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
STATE = BASE / "STATE"
WORK = BASE / "WORK"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AgentRuntimeState:
    agent_id: str
    workflow_id: str | None = None
    status: str = "idle"          # idle|executing|waiting|completed|failed
    current_subtask_id: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    action_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Delegation:
    workflow_id: str
    subtask_id: str
    agent_id: str
    role: str
    goal: str
    status: str = "pending"       # pending|claimed|executing|completed|failed
    created_at: float = field(default_factory=time.time)
    claimed_at: float | None = None
    completed_at: float | None = None
    result_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class WorkflowState:
    workflow_id: str
    task_id: str
    status: str = "created"       # created|planning|executing|completed|failed|halted
    created_at: float = field(default_factory=time.time)
    updated_at: float | None = None
    delegations: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    halt_reason: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class ChildContract:
    agent_id: str
    workflow_id: str
    subtask_id: str
    role: str
    status: str                   # completed|failed
    summary: str
    artifacts: list[str] = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    handoff: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Blackboard read/write operations
# ---------------------------------------------------------------------------

class Blackboard:
    """File-based blackboard for multi-agent state coordination.

    All reads and writes are atomic (write-to-tmp + rename).
    All state is JSON on disk — no in-memory shared state.
    """

    def __init__(self, base: Path | None = None):
        self.base = base or BASE
        self.state = self.base / "STATE"
        self.work = self.base / "WORK"

    # --- Atomic JSON I/O ---

    def _write_json(self, path: Path, data: dict) -> None:
        """Atomic write: tmp file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
        tmp.rename(path)

    def _read_json(self, path: Path) -> dict | None:
        """Read JSON file, return None if missing."""
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _append_jsonl(self, path: Path, record: dict) -> None:
        """Append a single JSON-lines record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    # --- Agent runtime state ---

    def set_agent_state(self, state: AgentRuntimeState) -> None:
        state.updated_at = time.time()
        path = self.state / "agents" / "runtime" / f"{state.agent_id}.json"
        self._write_json(path, state.to_dict())

    def get_agent_state(self, agent_id: str) -> dict | None:
        path = self.state / "agents" / "runtime" / f"{agent_id}.json"
        return self._read_json(path)

    def list_agent_states(self) -> list[dict]:
        d = self.state / "agents" / "runtime"
        if not d.exists():
            return []
        results = []
        for f in sorted(d.glob("*.json")):
            data = self._read_json(f)
            if data:
                results.append(data)
        return results

    # --- Delegations ---

    def create_delegation(self, delegation: Delegation) -> Path:
        key = f"{delegation.workflow_id}_{delegation.subtask_id}"
        path = self.state / "delegations" / f"{key}.json"
        self._write_json(path, delegation.to_dict())
        return path

    def get_delegation(self, workflow_id: str, subtask_id: str) -> dict | None:
        path = self.state / "delegations" / f"{workflow_id}_{subtask_id}.json"
        return self._read_json(path)

    def update_delegation(self, workflow_id: str, subtask_id: str,
                          updates: dict) -> None:
        path = self.state / "delegations" / f"{workflow_id}_{subtask_id}.json"
        data = self._read_json(path)
        if data is None:
            raise FileNotFoundError(f"Delegation not found: {path}")
        data.update(updates)
        data["updated_at"] = time.time()
        self._write_json(path, data)

    def list_delegations(self, workflow_id: str | None = None) -> list[dict]:
        d = self.state / "delegations"
        if not d.exists():
            return []
        results = []
        for f in sorted(d.glob("*.json")):
            data = self._read_json(f)
            if data and (workflow_id is None
                         or data.get("workflow_id") == workflow_id):
                results.append(data)
        return results

    # --- Workflows ---

    def create_workflow(self, wf: WorkflowState) -> Path:
        path = self.state / "workflows" / f"{wf.workflow_id}.json"
        self._write_json(path, wf.to_dict())
        return path

    def get_workflow(self, workflow_id: str) -> dict | None:
        path = self.state / "workflows" / f"{workflow_id}.json"
        return self._read_json(path)

    def update_workflow(self, workflow_id: str, updates: dict) -> None:
        path = self.state / "workflows" / f"{workflow_id}.json"
        data = self._read_json(path)
        if data is None:
            raise FileNotFoundError(f"Workflow not found: {path}")
        data.update(updates)
        data["updated_at"] = time.time()
        self._write_json(path, data)

    # --- Messages (append-only log per agent per workflow) ---

    def post_message(self, workflow_id: str, agent_id: str,
                     msg_type: str, content: Any) -> None:
        """Post a message to the blackboard message log.

        Messages are append-only JSONL — agents read the full log to see
        prior context. No direct peer-to-peer communication.
        """
        path = (self.work / "agents" / "messages"
                / workflow_id / f"{agent_id}.jsonl")
        record = {
            "ts": time.time(),
            "agent_id": agent_id,
            "type": msg_type,      # progress|output|error|handoff
            "content": content,
        }
        self._append_jsonl(path, record)

    def read_messages(self, workflow_id: str,
                      agent_id: str | None = None) -> list[dict]:
        """Read messages for a workflow. If agent_id given, filter to that agent."""
        d = self.work / "agents" / "messages" / workflow_id
        if not d.exists():
            return []
        files = ([d / f"{agent_id}.jsonl"] if agent_id
                 else sorted(d.glob("*.jsonl")))
        messages = []
        for f in files:
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        messages.sort(key=lambda m: m.get("ts", 0))
        return messages

    # --- Child contracts ---

    def write_child_contract(self, contract: ChildContract) -> Path:
        path = (self.work / "agents" / "contracts"
                / f"{contract.subtask_id}.json")
        self._write_json(path, contract.to_dict())
        return path

    def get_child_contract(self, subtask_id: str) -> dict | None:
        path = self.work / "agents" / "contracts" / f"{subtask_id}.json"
        return self._read_json(path)

    def list_child_contracts(self, workflow_id: str | None = None) -> list[dict]:
        d = self.work / "agents" / "contracts"
        if not d.exists():
            return []
        results = []
        for f in sorted(d.glob("*.json")):
            data = self._read_json(f)
            if data and (workflow_id is None
                         or data.get("workflow_id") == workflow_id):
                results.append(data)
        return results

    # --- Budget tracking ---

    def get_budget(self, agent_id: str) -> dict | None:
        path = self.state / "budgets" / f"{agent_id}.json"
        return self._read_json(path)

    def update_budget(self, agent_id: str, budget: dict) -> None:
        path = self.state / "budgets" / f"{agent_id}.json"
        self._write_json(path, budget)

    # --- Convenience: workflow metrics ---

    def workflow_metrics(self, workflow_id: str) -> dict:
        """Compute workflow-level metrics from blackboard state."""
        delegations = self.list_delegations(workflow_id)
        contracts = self.list_child_contracts(workflow_id)

        completed = [d for d in delegations if d.get("status") == "completed"]
        failed = [d for d in delegations if d.get("status") == "failed"]

        latencies = []
        for d in completed:
            if d.get("claimed_at") and d.get("completed_at"):
                latencies.append(d["completed_at"] - d["claimed_at"])

        return {
            "workflow_id": workflow_id,
            "total_delegations": len(delegations),
            "completed": len(completed),
            "failed": len(failed),
            "pending": len(delegations) - len(completed) - len(failed),
            "contracts_received": len(contracts),
            "mean_subtask_latency_s": (
                round(sum(latencies) / len(latencies), 2)
                if latencies else None
            ),
        }
