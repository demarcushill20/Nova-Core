"""Workflow graph: construct and render multi-agent workflow DAGs.

Reads blackboard state (workflows, delegations, child contracts, agent
runtime) and builds a directed graph that shows:
  - workflow → delegation edges
  - delegation → child-contract result nodes
  - agent status, timing, and artifacts at each node

Rendering targets:
  - Structured dict (JSON-serializable) for programmatic consumers
  - Markdown for human-readable reports and Telegram notifications
  - ASCII tree for terminal output
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.blackboard import Blackboard


# ---------------------------------------------------------------------------
# Graph data model
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """A single node in the workflow graph."""
    node_id: str
    node_type: str          # workflow | delegation | contract | agent
    label: str
    status: str
    metadata: dict = field(default_factory=dict)
    children: list["GraphNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "node_id": self.node_id,
            "type": self.node_type,
            "label": self.label,
            "status": self.status,
        }
        if self.metadata:
            d["metadata"] = self.metadata
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


@dataclass
class GraphEdge:
    """A directed edge in the workflow graph."""
    source: str
    target: str
    edge_type: str          # delegates | produces | verifies
    label: str = ""

    def to_dict(self) -> dict:
        d = {"source": self.source, "target": self.target, "type": self.edge_type}
        if self.label:
            d["label"] = self.label
        return d


@dataclass
class WorkflowGraph:
    """Complete graph of a workflow execution."""
    workflow_id: str
    root: GraphNode | None = None
    edges: list[GraphEdge] = field(default_factory=list)
    built_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "root": self.root.to_dict() if self.root else None,
            "edges": [e.to_dict() for e in self.edges],
            "built_at": self.built_at,
        }


# ---------------------------------------------------------------------------
# Status symbols for rendering
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "completed": "[OK]",
    "failed":    "[FAIL]",
    "executing": "[RUN]",
    "claimed":   "[RUN]",
    "pending":   "[..]",
    "queued":    "[..]",
    "idle":      "[--]",
    "halted":    "[HALT]",
    "created":   "[NEW]",
    "planning":  "[PLAN]",
    "partial":   "[PART]",
    "waiting":   "[WAIT]",
    "blocked":   "[BLK]",
    "stale":     "[STALE]",
}


def _icon(status: str) -> str:
    return _STATUS_ICON.get(status, f"[{status[:4].upper()}]")


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

class WorkflowGraphBuilder:
    """Build a WorkflowGraph from blackboard state.

    Usage:
        builder = WorkflowGraphBuilder()
        graph = builder.build("wf_001")
        print(render_markdown(graph))
    """

    def __init__(self, blackboard: Blackboard | None = None):
        self.bb = blackboard or Blackboard()

    def build(self, workflow_id: str) -> WorkflowGraph:
        """Construct the full workflow graph for a given workflow_id.

        Reads delegation records, child contracts, agent runtime state,
        AND coordination-layer node_states (persisted by CoordinationLayer
        into the workflow JSON).  This ensures child results persisted
        through the coordination layer remain visible in the graph.
        """
        wf_data = self.bb.get_workflow(workflow_id)
        if wf_data is None:
            graph = WorkflowGraph(workflow_id=workflow_id)
            graph.root = GraphNode(
                node_id=workflow_id,
                node_type="workflow",
                label=f"Workflow {workflow_id} (not found)",
                status="unknown",
            )
            return graph

        # Build root workflow node
        root = GraphNode(
            node_id=workflow_id,
            node_type="workflow",
            label=f"Workflow: {wf_data.get('task_id', workflow_id)}",
            status=wf_data.get("status", "unknown"),
            metadata=_workflow_metadata(wf_data),
        )

        graph = WorkflowGraph(workflow_id=workflow_id, root=root)

        # Coordination-layer node states (written by CoordinationLayer)
        node_states = wf_data.get("node_states", {})

        # Attach delegation nodes with child contracts
        delegations = self.bb.list_delegations(workflow_id)
        contracts_by_subtask = {
            c["subtask_id"]: c
            for c in self.bb.list_child_contracts(workflow_id)
        }

        seen_node_ids: set[str] = set()

        for deleg in delegations:
            subtask_id = deleg.get("subtask_id", "unknown")
            agent_id = deleg.get("agent_id", "unknown")
            role = deleg.get("role", "unknown")
            seen_node_ids.add(subtask_id)

            # Prefer coordination-layer status over delegation status
            ns = node_states.get(subtask_id, {})
            effective_status = ns.get("status") or deleg.get("status", "pending")

            # Delegation node
            deleg_meta = _delegation_metadata(deleg)
            # Merge coordination node_state metadata
            if ns:
                deleg_meta.update(_node_state_metadata(ns))

            deleg_node = GraphNode(
                node_id=subtask_id,
                node_type="delegation",
                label=f"{role}/{agent_id}: {deleg.get('goal', '?')[:60]}",
                status=effective_status,
                metadata=deleg_meta,
            )

            # Edge: workflow → delegation
            graph.edges.append(GraphEdge(
                source=workflow_id,
                target=subtask_id,
                edge_type="delegates",
                label=role,
            ))

            # If a child contract exists, attach it
            contract = contracts_by_subtask.get(subtask_id)
            if contract:
                contract_node = GraphNode(
                    node_id=f"contract_{subtask_id}",
                    node_type="contract",
                    label=contract.get("summary", "No summary"),
                    status=contract.get("status", "unknown"),
                    metadata=_contract_metadata(contract),
                )
                deleg_node.children.append(contract_node)
                graph.edges.append(GraphEdge(
                    source=subtask_id,
                    target=f"contract_{subtask_id}",
                    edge_type="produces",
                ))

            # Attach agent runtime state if available
            agent_state = self.bb.get_agent_state(agent_id)
            if agent_state:
                agent_node = GraphNode(
                    node_id=f"agent_{agent_id}",
                    node_type="agent",
                    label=f"Agent: {agent_id}",
                    status=agent_state.get("status", "idle"),
                    metadata={
                        "action_count": agent_state.get("action_count", 0),
                        "error": agent_state.get("error"),
                    },
                )
                deleg_node.children.append(agent_node)

            root.children.append(deleg_node)

        # Surface coordination-only nodes (node_states without a delegation)
        for nid, ns in node_states.items():
            if nid in seen_node_ids:
                continue
            coord_node = GraphNode(
                node_id=nid,
                node_type="coordination",
                label=f"node/{ns.get('assigned_agent', '?')}: {nid}",
                status=ns.get("status", "pending"),
                metadata=_node_state_metadata(ns),
            )
            graph.edges.append(GraphEdge(
                source=workflow_id,
                target=nid,
                edge_type="delegates",
            ))
            root.children.append(coord_node)

        return graph

    def build_all(self) -> list[WorkflowGraph]:
        """Build graphs for all workflows on the blackboard."""
        wf_dir = self.bb.state / "workflows"
        if not wf_dir.exists():
            return []
        graphs = []
        for f in sorted(wf_dir.glob("*.json")):
            wf_id = f.stem
            graphs.append(self.build(wf_id))
        return graphs


# ---------------------------------------------------------------------------
# Metadata extractors
# ---------------------------------------------------------------------------

def _workflow_metadata(wf: dict) -> dict:
    meta: dict[str, Any] = {}
    if wf.get("created_at"):
        elapsed = time.time() - wf["created_at"]
        meta["elapsed"] = _fmt_duration(elapsed)
    if wf.get("halt_reason"):
        meta["halt_reason"] = wf["halt_reason"]
    if wf.get("budget"):
        meta["budget"] = wf["budget"]
    n = len(wf.get("delegations", []))
    meta["delegation_count"] = n
    return meta


def _delegation_metadata(deleg: dict) -> dict:
    meta: dict[str, Any] = {
        "agent_id": deleg.get("agent_id"),
        "role": deleg.get("role"),
    }
    if deleg.get("claimed_at") and deleg.get("completed_at"):
        meta["duration"] = _fmt_duration(
            deleg["completed_at"] - deleg["claimed_at"]
        )
    elif deleg.get("claimed_at"):
        meta["running_for"] = _fmt_duration(
            time.time() - deleg["claimed_at"]
        )
    if deleg.get("error"):
        meta["error"] = deleg["error"]
    return meta


def _contract_metadata(contract: dict) -> dict:
    meta: dict[str, Any] = {}
    if contract.get("artifacts"):
        meta["artifacts"] = contract["artifacts"]
    if contract.get("verification"):
        meta["verification"] = contract["verification"]
    if contract.get("handoff"):
        meta["handoff"] = contract["handoff"]
    return meta


def _node_state_metadata(ns: dict) -> dict:
    """Extract displayable metadata from a coordination-layer node_state."""
    meta: dict[str, Any] = {}
    if ns.get("output_ref"):
        meta["output_ref"] = ns["output_ref"]
    if ns.get("assigned_agent"):
        meta["assigned_agent"] = ns["assigned_agent"]
    if ns.get("retry_count"):
        meta["retry_count"] = ns["retry_count"]
    if ns.get("depends_on"):
        meta["depends_on"] = ns["depends_on"]
    if ns.get("error"):
        meta["error"] = ns["error"]
    if ns.get("claimed_at") and ns.get("completed_at"):
        meta["coord_duration"] = _fmt_duration(
            ns["completed_at"] - ns["claimed_at"]
        )
    return meta


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_json(graph: WorkflowGraph) -> str:
    """Render graph as pretty-printed JSON."""
    return json.dumps(graph.to_dict(), indent=2, default=str)


def render_markdown(graph: WorkflowGraph) -> str:
    """Render graph as a Markdown report with child results inline."""
    lines: list[str] = []
    root = graph.root
    if root is None:
        return f"# Workflow {graph.workflow_id}\n\nNo data available.\n"

    lines.append(f"# {_icon(root.status)} {root.label}")
    lines.append("")

    # Workflow summary
    meta = root.metadata
    lines.append(f"**Status:** {root.status}")
    if meta.get("elapsed"):
        lines.append(f"**Elapsed:** {meta['elapsed']}")
    if meta.get("halt_reason"):
        lines.append(f"**Halt reason:** {meta['halt_reason']}")
    lines.append(f"**Delegations:** {meta.get('delegation_count', 0)}")
    lines.append("")

    if not root.children:
        lines.append("_No delegations yet._")
        return "\n".join(lines) + "\n"

    # Delegation table
    lines.append("## Subtasks")
    lines.append("")

    for deleg_node in root.children:
        if deleg_node.node_type not in ("delegation", "coordination"):
            continue

        icon = _icon(deleg_node.status)
        dmeta = deleg_node.metadata
        duration = (dmeta.get("duration") or dmeta.get("coord_duration")
                    or dmeta.get("running_for", "—"))
        lines.append(f"### {icon} {deleg_node.label}")
        lines.append(f"- **Agent:** {dmeta.get('agent_id') or dmeta.get('assigned_agent', '?')}")
        if dmeta.get("role"):
            lines.append(f"- **Role:** {dmeta['role']}")
        lines.append(f"- **Status:** {deleg_node.status}")
        lines.append(f"- **Duration:** {duration}")
        if dmeta.get("output_ref"):
            lines.append(f"- **Output:** {dmeta['output_ref']}")
        if dmeta.get("retry_count"):
            lines.append(f"- **Retries:** {dmeta['retry_count']}")
        if dmeta.get("depends_on"):
            lines.append(f"- **Depends on:** {', '.join(dmeta['depends_on'])}")
        if dmeta.get("error"):
            lines.append(f"- **Error:** {dmeta['error']}")
        lines.append("")

        # Child results (contracts)
        for child in deleg_node.children:
            if child.node_type == "contract":
                lines.append(f"  **Result:** {_icon(child.status)} {child.label}")
                cmeta = child.metadata
                if cmeta.get("artifacts"):
                    lines.append(f"  **Artifacts:** {', '.join(cmeta['artifacts'])}")
                if cmeta.get("verification"):
                    v = cmeta["verification"]
                    if isinstance(v, dict):
                        lines.append(f"  **Verification:** {v.get('result', '?')} "
                                     f"(confidence: {v.get('confidence', '?')})")
                    else:
                        lines.append(f"  **Verification:** {v}")
                if cmeta.get("handoff"):
                    lines.append(f"  **Handoff:** {json.dumps(cmeta['handoff'])}")
                lines.append("")

            elif child.node_type == "agent":
                agent_meta = child.metadata
                lines.append(f"  **Agent state:** {_icon(child.status)} "
                             f"actions={agent_meta.get('action_count', 0)}")
                if agent_meta.get("error"):
                    lines.append(f"  **Agent error:** {agent_meta['error']}")
                lines.append("")

    return "\n".join(lines) + "\n"


def render_ascii_tree(graph: WorkflowGraph) -> str:
    """Render graph as an ASCII tree for terminal output."""
    lines: list[str] = []
    root = graph.root
    if root is None:
        return f"(empty graph: {graph.workflow_id})\n"

    _tree_node(root, lines, prefix="", is_last=True)
    return "\n".join(lines) + "\n"


def _tree_node(node: GraphNode, lines: list[str],
               prefix: str, is_last: bool) -> None:
    connector = "└── " if is_last else "├── "
    icon = _icon(node.status)
    lines.append(f"{prefix}{connector}{icon} {node.label}")

    child_prefix = prefix + ("    " if is_last else "│   ")
    for i, child in enumerate(node.children):
        _tree_node(child, lines, child_prefix, i == len(node.children) - 1)


# ---------------------------------------------------------------------------
# Convenience: build + render in one call
# ---------------------------------------------------------------------------

def workflow_graph_markdown(workflow_id: str,
                            blackboard: Blackboard | None = None) -> str:
    """One-shot: build graph and render as Markdown."""
    builder = WorkflowGraphBuilder(blackboard)
    graph = builder.build(workflow_id)
    return render_markdown(graph)


def workflow_graph_json(workflow_id: str,
                        blackboard: Blackboard | None = None) -> str:
    """One-shot: build graph and render as JSON."""
    builder = WorkflowGraphBuilder(blackboard)
    graph = builder.build(workflow_id)
    return render_json(graph)


def workflow_graph_tree(workflow_id: str,
                        blackboard: Blackboard | None = None) -> str:
    """One-shot: build graph and render as ASCII tree."""
    builder = WorkflowGraphBuilder(blackboard)
    graph = builder.build(workflow_id)
    return render_ascii_tree(graph)


def all_workflows_summary(blackboard: Blackboard | None = None) -> str:
    """Render a summary of all workflows as Markdown."""
    builder = WorkflowGraphBuilder(blackboard)
    graphs = builder.build_all()
    if not graphs:
        return "# Workflow Summary\n\nNo workflows found.\n"

    lines = ["# Workflow Summary", ""]
    for g in graphs:
        root = g.root
        if root is None:
            continue
        icon = _icon(root.status)
        n_deleg = root.metadata.get("delegation_count", 0)
        elapsed = root.metadata.get("elapsed", "—")
        lines.append(f"- {icon} **{g.workflow_id}** — "
                     f"{root.status} | {n_deleg} delegations | {elapsed}")
    lines.append("")
    return "\n".join(lines) + "\n"
