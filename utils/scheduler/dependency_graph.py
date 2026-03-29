"""Dependency Graph & Unlock Chains for the Intelligent Dynamic Block Scheduler.

Phase 7: Builds a sophisticated dependency graph with cycle detection,
critical path identification, fail-fast probe generation, and epic
decomposition suggestions.

All heuristic-based — no LLM calls.
"""

from __future__ import annotations

import logging
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.work_unit import TaskClass, WorkMode, WorkUnit

log = logging.getLogger("nova.scheduler.dependency_graph")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ProbeTask(BaseModel):
    """A lightweight fail-fast probe to validate assumptions before committing."""

    model_config = ConfigDict(extra="forbid")

    original_task_id: str
    probe_id: str
    title: str
    estimated_duration_min: float = Field(default=15.0, ge=1.0)
    probe_type: str  # inspect / validate / test_access / reproduce
    done_condition: str
    work_mode: WorkMode = WorkMode.ANALYSIS


class DecompositionSuggestion(BaseModel):
    """Heuristic decomposition of an EPIC task into bounded subtasks."""

    model_config = ConfigDict(extra="forbid")

    original_task_id: str
    original_title: str
    suggested_subtasks: list[dict]  # each: title, estimated_min, work_mode
    decomposition_strategy: str  # split_by_component / split_by_phase / split_evenly


# ---------------------------------------------------------------------------
# Cycle detection helper
# ---------------------------------------------------------------------------


def _detect_cycle(
    graph_edges: dict[str, set[str]],
    start: str,
    target: str,
) -> bool:
    """DFS from *start* — return True if *target* is reachable.

    Used to check whether adding an edge target -> start would create a cycle
    (i.e. if start can already reach target via existing edges).
    """
    visited: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in visited:
            continue
        visited.add(node)
        for child in graph_edges.get(node, set()):
            if child not in visited:
                stack.append(child)
    return False


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------


class DependencyGraph:
    """Mutable directed acyclic graph of task dependencies.

    Edges go from *parent* (dependency) to *child* (dependent):
      parent --unblocks--> child
    i.e. child depends on parent.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, WorkUnit] = {}
        self._edges: dict[str, set[str]] = {}  # parent -> children (forward)
        self._reverse: dict[str, set[str]] = {}  # child -> parents (reverse)
        self._completed: set[str] = set()

    # -- Mutation -----------------------------------------------------------

    def add_task(self, work_unit: WorkUnit) -> None:
        """Add a work unit and auto-wire its dependency_ids as edges."""
        tid = work_unit.id
        if not tid:
            raise ValueError("WorkUnit must have a non-empty id")

        self._nodes[tid] = work_unit
        self._edges.setdefault(tid, set())
        self._reverse.setdefault(tid, set())

        # Auto-wire: for each dep in dependency_ids, dep -> tid
        for dep_id in work_unit.dependency_ids:
            if dep_id in self._nodes:
                self.add_dependency(dep_id, tid)

        # Also wire any existing tasks that list *this* task as a dependency
        for other_id, other_wu in self._nodes.items():
            if other_id == tid:
                continue
            if tid in other_wu.dependency_ids and other_id not in self._edges.get(tid, set()):
                # tid -> other_id (tid unblocks other_id)
                self.add_dependency(tid, other_id)

    def add_dependency(self, parent_id: str, child_id: str) -> None:
        """Add an edge parent -> child. Raises ValueError on cycle or self-loop."""
        if parent_id == child_id:
            raise ValueError(f"Self-loop detected: {parent_id}")

        # Would adding parent -> child create a cycle?
        # That happens if child can already reach parent via existing edges.
        if _detect_cycle(self._edges, child_id, parent_id):
            raise ValueError(f"Cycle detected: adding {parent_id} -> {child_id} would create a cycle")

        self._edges.setdefault(parent_id, set()).add(child_id)
        self._reverse.setdefault(child_id, set()).add(parent_id)

    def remove_task(self, task_id: str) -> None:
        """Remove a node and all its edges."""
        if task_id not in self._nodes:
            return

        # Remove forward edges from this node
        for child in list(self._edges.get(task_id, set())):
            self._reverse.get(child, set()).discard(task_id)
        self._edges.pop(task_id, None)

        # Remove reverse edges to this node
        for parent in list(self._reverse.get(task_id, set())):
            self._edges.get(parent, set()).discard(task_id)
        self._reverse.pop(task_id, None)

        del self._nodes[task_id]
        self._completed.discard(task_id)

    def prune_completed(self, completed_ids: set[str]) -> None:
        """Remove completed tasks and their edges."""
        for cid in completed_ids:
            self._completed.add(cid)
            self.remove_task(cid)

    # -- Query --------------------------------------------------------------

    def get_task(self, task_id: str) -> WorkUnit | None:
        """Return the WorkUnit for a task, or None if not found."""
        return self._nodes.get(task_id)

    def get_dependents(self, task_id: str) -> list[str]:
        """Direct children (tasks that depend on task_id)."""
        return sorted(self._edges.get(task_id, set()))

    def get_dependencies(self, task_id: str) -> list[str]:
        """Direct parents (tasks that task_id depends on)."""
        return sorted(self._reverse.get(task_id, set()))

    def get_ready_tasks(self) -> list[WorkUnit]:
        """Tasks with all dependencies satisfied (no pending parents)."""
        ready = []
        for tid, wu in self._nodes.items():
            parents = self._reverse.get(tid, set())
            if not parents:
                ready.append(wu)
        return ready

    def size(self) -> int:
        """Number of nodes in the graph."""
        return len(self._nodes)

    # -- Topological sort ---------------------------------------------------

    def topological_sort(self) -> list[str]:
        """Kahn's algorithm. Raises ValueError if cycle exists."""
        in_degree: dict[str, int] = {tid: 0 for tid in self._nodes}
        for tid in self._nodes:
            for child in self._edges.get(tid, set()):
                if child in in_degree:
                    in_degree[child] += 1

        queue: deque[str] = deque()
        for tid, deg in in_degree.items():
            if deg == 0:
                queue.append(tid)

        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for child in sorted(self._edges.get(node, set())):
                if child in in_degree:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

        if len(result) != len(self._nodes):
            raise ValueError("Cycle detected in graph — topological sort impossible")

        return result

    # -- Critical path ------------------------------------------------------

    def get_critical_path(self) -> list[str]:
        """Longest path by estimated_expected_min sum using topo sort + DP."""
        if not self._nodes:
            return []

        topo = self.topological_sort()

        # dist[node] = longest path weight ending at node
        dist: dict[str, float] = {}
        pred: dict[str, str | None] = {}

        for tid in topo:
            wu = self._nodes[tid]
            my_cost = wu.estimated_expected_min

            parents = self._reverse.get(tid, set())
            if not parents:
                dist[tid] = my_cost
                pred[tid] = None
            else:
                best_parent: str | None = None
                best_dist = -1.0
                for p in parents:
                    if p in dist and dist[p] > best_dist:
                        best_dist = dist[p]
                        best_parent = p
                if best_parent is not None:
                    dist[tid] = best_dist + my_cost
                else:
                    dist[tid] = my_cost
                pred[tid] = best_parent

        if not dist:
            return []

        # Trace back from the node with the largest distance
        end = max(dist, key=lambda k: dist[k])
        path: list[str] = []
        current: str | None = end
        while current is not None:
            path.append(current)
            current = pred.get(current)
        path.reverse()
        return path

    # -- Unlock value -------------------------------------------------------

    def get_unlock_value(self, task_id: str) -> float:
        """Sum of downstream base values (recursive BFS, memoized per call)."""
        if task_id not in self._nodes:
            return 0.0

        memo: dict[str, float] = {}
        return self._compute_unlock_recursive(task_id, memo)

    def _compute_unlock_recursive(self, task_id: str, memo: dict[str, float]) -> float:
        if task_id in memo:
            return memo[task_id]

        total = 0.0
        for child in self._edges.get(task_id, set()):
            wu = self._nodes.get(child)
            if wu is not None:
                base = (
                    (wu.urgency + wu.strategic_value + wu.risk_reduction + wu.unblock_value)
                    / 4.0
                    * wu.success_probability
                )
                total += base + self._compute_unlock_recursive(child, memo)

        memo[task_id] = total
        return total

    # -- Cycle check (public) -----------------------------------------------

    def has_cycle(self, parent_id: str, child_id: str) -> bool:
        """Check if adding edge parent_id -> child_id would create a cycle."""
        if parent_id == child_id:
            return True
        return _detect_cycle(self._edges, child_id, parent_id)


# ---------------------------------------------------------------------------
# Builder convenience
# ---------------------------------------------------------------------------


def build_graph(work_units: list[WorkUnit]) -> DependencyGraph:
    """Create a graph from a list of WorkUnits.

    Skips invalid dependency_ids (logs a warning, doesn't crash).
    """
    graph = DependencyGraph()

    # First pass: add all nodes (no edge wiring yet so ordering doesn't matter)
    for wu in work_units:
        if not wu.id:
            log.warning("Skipping WorkUnit with empty id: %s", wu.title)
            continue
        if wu.id in graph._nodes:
            log.warning("Duplicate WorkUnit id %r — skipping", wu.id)
            continue
        # Temporarily clear dependency_ids to add node without wiring
        graph._nodes[wu.id] = wu
        graph._edges.setdefault(wu.id, set())
        graph._reverse.setdefault(wu.id, set())

    # Second pass: wire edges
    for wu in work_units:
        if not wu.id or wu.id not in graph._nodes:
            continue
        for dep_id in wu.dependency_ids:
            if dep_id not in graph._nodes:
                log.warning(
                    "Task %r depends on %r which is not in the graph — skipping edge",
                    wu.id,
                    dep_id,
                )
                continue
            try:
                graph.add_dependency(dep_id, wu.id)
            except ValueError as exc:
                log.warning("Could not add dependency %r -> %r: %s", dep_id, wu.id, exc)

    return graph


# ---------------------------------------------------------------------------
# Unlock values (standalone function)
# ---------------------------------------------------------------------------


def get_unlock_values(graph: DependencyGraph) -> dict[str, float]:
    """Compute normalized (0-1) unlock values for every task in the graph."""
    if graph.size() == 0:
        return {}

    raw: dict[str, float] = {}
    for tid in graph._nodes:
        raw[tid] = graph.get_unlock_value(tid)

    max_val = max(raw.values()) if raw else 0.0
    if max_val <= 0.0:
        return {tid: 0.0 for tid in raw}

    return {tid: round(val / max_val, 4) for tid, val in raw.items()}


# ---------------------------------------------------------------------------
# Critical path (standalone functions)
# ---------------------------------------------------------------------------


def get_critical_path(graph: DependencyGraph) -> list[str]:
    """Find the critical path (longest by estimated_expected_min)."""
    return graph.get_critical_path()


def is_on_critical_path(graph: DependencyGraph, task_id: str) -> bool:
    """Check if task_id is on the critical path."""
    return task_id in graph.get_critical_path()


# ---------------------------------------------------------------------------
# Fail-fast probe generation
# ---------------------------------------------------------------------------


def generate_probe(work_unit: WorkUnit) -> ProbeTask | None:
    """Generate a fail-fast probe for high-uncertainty tasks.

    Returns None if the task doesn't need a probe (high confidence,
    reasonable pessimistic/expected ratio).
    """
    needs_probe = False

    # Low confidence
    if work_unit.confidence < 0.5:
        needs_probe = True

    # High pessimistic/expected ratio (2x or more)
    if (
        work_unit.estimated_expected_min > 0
        and work_unit.estimated_pessimistic_min >= 2.0 * work_unit.estimated_expected_min
    ):
        needs_probe = True

    if not needs_probe:
        return None

    # Determine probe type based on work_mode
    probe_type_map = {
        WorkMode.DEBUGGING: "reproduce",
        WorkMode.DEPLOYMENT: "test_access",
        WorkMode.RESEARCH: "validate",
    }
    probe_type = probe_type_map.get(work_unit.work_mode, "inspect")

    # Done condition based on probe type
    done_conditions = {
        "reproduce": f"Reproduced the issue described in '{work_unit.title}' or confirmed it cannot be reproduced",
        "test_access": f"Verified access and credentials required for '{work_unit.title}'",
        "validate": f"Validated key assumptions for '{work_unit.title}'",
        "inspect": f"Completed quick investigation of '{work_unit.title}' and documented findings",
    }

    # Duration: scale with original complexity, bounded 10-20
    base_duration = min(max(work_unit.estimated_expected_min * 0.3, 10.0), 20.0)

    return ProbeTask(
        original_task_id=work_unit.id,
        probe_id=f"probe_{work_unit.id}",
        title=f"[Probe] {work_unit.title}",
        estimated_duration_min=round(base_duration, 1),
        probe_type=probe_type,
        done_condition=done_conditions[probe_type],
        work_mode=WorkMode.ANALYSIS,
    )


# ---------------------------------------------------------------------------
# Epic decomposition
# ---------------------------------------------------------------------------


def suggest_decomposition(work_unit: WorkUnit) -> DecompositionSuggestion | None:
    """Suggest decomposition of an EPIC task into bounded subtasks.

    Pure heuristic — no LLM. Returns None if not an EPIC.
    """
    if work_unit.task_class != TaskClass.EPIC:
        return None

    mode = work_unit.work_mode
    duration = work_unit.estimated_expected_min

    # Strategy and subtask templates based on work_mode
    if mode == WorkMode.DEEP_IMPLEMENTATION:
        strategy = "split_by_component"
        templates = [
            ("Setup & scaffolding", WorkMode.ADMIN),
            ("Core implementation", WorkMode.DEEP_IMPLEMENTATION),
            ("Tests & validation", WorkMode.REVIEW),
            ("Integration wiring", WorkMode.DEEP_IMPLEMENTATION),
            ("Cleanup & documentation", WorkMode.ADMIN),
        ]
    elif mode == WorkMode.RESEARCH:
        strategy = "split_by_phase"
        templates = [
            ("Scope & define research questions", WorkMode.ANALYSIS),
            ("Investigate & gather data", WorkMode.RESEARCH),
            ("Analyze findings", WorkMode.ANALYSIS),
            ("Synthesize & document conclusions", WorkMode.COMMUNICATION),
        ]
    elif mode == WorkMode.DEBUGGING:
        strategy = "split_by_phase"
        templates = [
            ("Reproduce the issue", WorkMode.DEBUGGING),
            ("Isolate root cause", WorkMode.DEBUGGING),
            ("Implement fix", WorkMode.DEEP_IMPLEMENTATION),
            ("Verify fix & regression test", WorkMode.REVIEW),
        ]
    else:
        strategy = "split_evenly"
        num_parts = max(3, min(5, int(duration / 30)))
        templates = [(f"Part {i + 1}", mode) for i in range(num_parts)]

    num_subtasks = len(templates)
    # 20% overhead distributed across subtasks
    per_task_duration = round((duration * 1.2) / num_subtasks, 1)

    subtasks = [
        {
            "title": f"{work_unit.title} — {label}",
            "estimated_min": per_task_duration,
            "work_mode": sub_mode.value,
        }
        for label, sub_mode in templates
    ]

    return DecompositionSuggestion(
        original_task_id=work_unit.id,
        original_title=work_unit.title,
        suggested_subtasks=subtasks,
        decomposition_strategy=strategy,
    )
