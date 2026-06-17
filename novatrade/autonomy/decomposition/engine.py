"""Goal Decomposition Engine — manages sub-goal DAGs and progress tracking."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.decomposition.models import (
    GoalDecomposition,
    SubGoal,
    SubGoalStatus,
)
from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.decomposition.engine")


# =====================================================================
# Per-SubGoal Evaluators
# =====================================================================
# Each evaluator returns (progress: float, evidence: list[str], failure_reason: str | None)

EvalResult = tuple[float, list[str], str | None]


def _eval_services_running(base_path: Path) -> EvalResult:
    """Check if core systemd services are running."""
    services = [
        "novacore-watcher",
        "novacore-telegram",
        "novacore-telegram-notifier",
        "novacore-novatrade",
    ]
    running = 0
    evidence: list[str] = []
    for svc in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True,
                text=True,
                timeout=5,
            )
            status = result.stdout.strip()
            if status == "active":
                running += 1
                evidence.append(f"{svc}: active")
            elif _service_intentionally_disabled(svc):
                # Operator turned it off on purpose — intent satisfied, not a fault.
                running += 1
                evidence.append(f"{svc}: {status} but DISABLED (operator intent)")
            else:
                evidence.append(f"{svc}: {status}")
        except (subprocess.TimeoutExpired, OSError):
            evidence.append(f"{svc}: check failed")
    progress = running / len(services) if services else 0.0
    failure = None if running == len(services) else f"{len(services) - running} services not active"
    return progress, evidence, failure


def _eval_no_orphan_tasks(base_path: Path) -> EvalResult:
    """Check for stale in-progress tasks."""
    tasks_dir = base_path / "TASKS"
    if not tasks_dir.exists():
        return 1.0, ["No TASKS directory"], None
    stale: list[str] = []
    now = time.time()
    for f in tasks_dir.iterdir():
        if f.suffix == ".inprogress":
            try:
                age_h = (now - f.stat().st_mtime) / 3600
                if age_h > 4:
                    stale.append(f"{f.name} ({age_h:.1f}h old)")
            except OSError:
                continue
    if not stale:
        return 1.0, ["No stale in-progress tasks"], None
    progress = max(0.0, 1.0 - len(stale) * 0.25)
    return progress, [f"Stale tasks: {', '.join(stale[:5])}"], f"{len(stale)} stale tasks"


def _eval_state_file(
    base_path: Path,
    rel_path: str,
    *,
    max_fresh_age: float = 300,
    max_aging_age: float = 3600,
) -> EvalResult:
    """Generic evaluator: check if a state file exists and is recent."""
    p = base_path / rel_path
    if not p.exists():
        return 0.0, [f"{rel_path} does not exist"], f"{rel_path} missing"
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return 0.0, [f"{rel_path} stat failed"], f"{rel_path} unreadable"
    if age < max_fresh_age:
        return 1.0, [f"{rel_path} exists and fresh ({age:.0f}s old)"], None
    elif age < max_aging_age:
        return 0.7, [f"{rel_path} exists but aging ({age / 60:.0f}min old)"], None
    else:
        return 0.3, [f"{rel_path} stale ({age / 3600:.1f}h old)"], f"{rel_path} is stale"


def _service_intentionally_disabled(service: str) -> bool:
    """True if systemd reports the unit as deliberately not-to-run.

    `disabled`/`masked`/`linked` (or a non-zero is-enabled exit) means the
    operator turned the unit off on purpose. That is intent, not a fault, and
    must not score a dimension to 0 — otherwise the autonomy engine spins a
    REPAIR->ESCALATE loop it can neither win (NoNewPrivileges blocks restart)
    nor should win (the daemon is off by design). See OUTPUT/1008.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode != 0 or result.stdout.strip() in {"disabled", "masked", "linked"}
    except (subprocess.TimeoutExpired, OSError):
        return False


def _eval_webhook_live(base_path: Path) -> EvalResult:
    """Check if novacore-novatrade service is active (proxy for webhook)."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "novacore-novatrade"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip() == "active":
            return 1.0, ["novacore-novatrade: active (webhook proxy)"], None
        if _service_intentionally_disabled("novacore-novatrade"):
            return 1.0, ["novacore-novatrade: inactive but DISABLED (operator intent)"], None
        return 0.0, [f"novacore-novatrade: {result.stdout.strip()}"], "novacore-novatrade not active"
    except (subprocess.TimeoutExpired, OSError):
        return 0.0, ["novacore-novatrade: check failed"], "service check failed"


def _eval_adapter_connected(base_path: Path) -> EvalResult:
    """Check STATE/novatrade/connection_status.json."""
    return _eval_state_file(base_path, "STATE/novatrade/connection_status.json")


def _eval_feed_healthy(base_path: Path) -> EvalResult:
    """Check STATE/novatrade/feed_health.json."""
    return _eval_state_file(base_path, "STATE/novatrade/feed_health.json")


def _eval_strategy_loaded(base_path: Path) -> EvalResult:
    """Check STATE/novatrade/strategy_config.json exists."""
    return _eval_state_file(
        base_path,
        "STATE/novatrade/strategy_config.json",
        max_fresh_age=86400,  # strategy config is not frequently updated
        max_aging_age=604800,
    )


def _eval_signals_generating(base_path: Path) -> EvalResult:
    """Check STATE/novatrade/signal_log.json for recent entries."""
    p = base_path / "STATE" / "novatrade" / "signal_log.json"
    if not p.exists():
        return 0.0, ["signal_log.json does not exist"], "No signal log"
    try:
        data = json.loads(p.read_text())
        # Handle both list and dict forms (live_loop writes {"signals": [...], ...})
        if isinstance(data, dict):
            data = data.get("signals", [])
        if not isinstance(data, list) or len(data) == 0:
            return 0.0, ["signal_log.json is empty"], "No signals recorded"
        # Check recency of last entry
        age = time.time() - p.stat().st_mtime
        if age < 3600:  # 1 hour
            return 1.0, [f"signal_log.json has {len(data)} entries, last update {age:.0f}s ago"], None
        elif age < 14400:  # 4 hours
            return 0.5, [f"signal_log.json aging ({age / 3600:.1f}h since last update)"], "Signals may be stale"
        else:
            return 0.2, [f"signal_log.json stale ({age / 3600:.1f}h since last update)"], "Signals are stale"
    except (json.JSONDecodeError, OSError) as exc:
        return 0.0, [f"signal_log.json unreadable: {exc}"], "Signal log unreadable"


def _eval_risk_not_halted(base_path: Path) -> EvalResult:
    """Check STATE/novatrade_risk_state.json (the authoritative halt source)."""
    p = base_path / "STATE" / "novatrade_risk_state.json"
    if not p.exists():
        return 0.5, ["No novatrade_risk_state.json"], "Halt state unknown"
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict) and data.get("halted"):
            reason = data.get("halt_reason") or data.get("reason") or "unknown"
            return 0.0, [f"Risk engine HALTED: {reason}"], f"Risk halted: {reason}"
        return 1.0, ["novatrade_risk_state.json exists, not halted"], None
    except (json.JSONDecodeError, OSError):
        return 0.5, ["novatrade_risk_state.json unreadable"], "Halt state unknown"


def _eval_drawdown_safe(base_path: Path) -> EvalResult:
    """Check STATE/novatrade/daily_loss_tracker.json."""
    p = base_path / "STATE" / "novatrade" / "daily_loss_tracker.json"
    if not p.exists():
        return 0.5, ["No daily_loss_tracker.json"], "Drawdown data unavailable"
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            return 0.5, ["daily_loss_tracker.json not a dict"], "Drawdown data malformed"
        # Accept both field naming conventions (drawdown may be negative)
        pct = abs(float(data.get("current_drawdown_pct", data.get("current_loss_pct", 0.0)) or 0.0))
        limit = abs(float(data.get("daily_limit_pct", data.get("daily_loss_limit_pct", 5.0)) or 5.0))
        ratio = pct / limit if limit > 0 else 0.0
        if ratio < 0.5:
            return 1.0, [f"Drawdown {pct:.2f}% of {limit:.1f}% limit ({ratio:.0%})"], None
        elif ratio < 0.8:
            return 0.5, [f"Drawdown {pct:.2f}% approaching limit ({ratio:.0%})"], f"Drawdown at {ratio:.0%} of limit"
        else:
            return 0.1, [f"Drawdown {pct:.2f}% near/at limit ({ratio:.0%})"], f"Drawdown critical: {ratio:.0%} of limit"
    except (json.JSONDecodeError, OSError):
        return 0.5, ["daily_loss_tracker.json unreadable"], "Drawdown data unreadable"


# Registry mapping sub-goal IDs to their evaluator functions
SUBGOAL_EVALUATORS: dict[str, Callable[[Path], EvalResult]] = {
    "sg_services_running": _eval_services_running,
    "sg_no_orphan_tasks": _eval_no_orphan_tasks,
    "sg_webhook_live": _eval_webhook_live,
    "sg_adapter_connected": _eval_adapter_connected,
    "sg_feed_healthy": _eval_feed_healthy,
    "sg_strategy_loaded": _eval_strategy_loaded,
    "sg_signals_generating": _eval_signals_generating,
    "sg_risk_not_halted": _eval_risk_not_halted,
    "sg_drawdown_safe": _eval_drawdown_safe,
}


class GoalDecomposer:
    """Manages goal decomposition trees: progress updates, actionable sub-goals, topological sort."""

    def __init__(
        self,
        base_path: str = "/home/nova/nova-core",
        evaluators: dict[str, Callable[[Path], EvalResult]] | None = None,
    ) -> None:
        self.base_path = Path(base_path)
        self._state_dir = self.base_path / "STATE" / "goal_trees"
        self._evaluators: dict[str, Callable[[Path], EvalResult]] = (
            evaluators if evaluators is not None else SUBGOAL_EVALUATORS
        )

    def update_progress(
        self,
        decomposition: GoalDecomposition,
        report: ProgressReport,
    ) -> GoalDecomposition:
        """Update sub-goal statuses based on per-subgoal evaluators and dimension scores.

        WARNING: Mutates the decomposition object in-place and returns it.
        If you need the original state preserved, pass a deep copy.

        Strategy:
        1. Try per-subgoal evaluator first (specific, evidence-based)
        2. Fall back to dimension-level scoring (generic):
           - Score >= 70 (GREEN) → progress 1.0 (completed)
           - Score 40-70 (YELLOW) → progress proportional
           - Score < 40 (RED) → progress 0.0
        """
        dim_scores: dict[str, float] = {}
        for name, dim in report.dimensions.items():
            dim_scores[name] = dim.score

        for sg in decomposition.sub_goals:
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED):
                continue

            # Try per-subgoal evaluator first
            evaluator = self._evaluators.get(sg.sub_goal_id)
            used_evaluator = False
            if evaluator:
                try:
                    progress, evidence, failure = evaluator(self.base_path)
                    sg.progress = round(max(0.0, min(1.0, progress)), 2)
                    sg.evidence = evidence[-5:]  # keep last 5
                    sg.failure_reason = failure
                    sg.last_checked = datetime.now(timezone.utc)
                    used_evaluator = True
                except Exception as exc:
                    log.warning("Evaluator for %s failed: %s", sg.sub_goal_id, exc)
                    # Fall through to dimension-level scoring

            if not used_evaluator:
                # Fallback: dimension-level scoring (existing logic)
                dim_score = dim_scores.get(sg.dimension)
                if dim_score is None:
                    continue

                if dim_score >= 70:
                    sg.progress = 1.0
                elif dim_score >= 40:
                    sg.progress = round((dim_score - 40) / 30, 2)
                else:
                    sg.progress = 0.0

            # Update status based on progress
            if sg.progress >= 1.0:
                sg.status = SubGoalStatus.COMPLETED
            elif sg.progress > 0:
                sg.status = SubGoalStatus.IN_PROGRESS
                sg.blockers = []  # Clear stale blockers
            # Check if blocked by unmet dependencies
            elif self._has_unmet_deps(sg, decomposition):
                sg.status = SubGoalStatus.BLOCKED
                sg.blockers = self._get_unmet_dep_names(sg, decomposition)

        return decomposition

    @staticmethod
    def _completed_ids(decomposition: GoalDecomposition) -> set[str]:
        """Return the set of sub-goal IDs that are completed or skipped."""
        return {
            sg.sub_goal_id
            for sg in decomposition.sub_goals
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED)
        }

    def get_actionable_subgoals(self, decomposition: GoalDecomposition) -> list[SubGoal]:
        """Return sub-goals that are actionable: NOT_STARTED or IN_PROGRESS with all deps met."""
        actionable: list[SubGoal] = []
        completed_ids = self._completed_ids(decomposition)

        for sg in decomposition.sub_goals:
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED):
                continue
            # Check all dependencies are met
            deps_met = all(dep in completed_ids for dep in sg.dependencies)
            if deps_met:
                actionable.append(sg)

        # Sort by priority (1=highest)
        actionable.sort(key=lambda s: s.priority)
        return actionable

    def get_blocked_subgoals(
        self,
        decomposition: GoalDecomposition,
    ) -> list[tuple[SubGoal, list[str]]]:
        """Return blocked sub-goals with their unmet dependency IDs."""
        completed_ids = self._completed_ids(decomposition)
        blocked: list[tuple[SubGoal, list[str]]] = []
        for sg in decomposition.sub_goals:
            if sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED):
                continue
            unmet = [dep for dep in sg.dependencies if dep not in completed_ids]
            if unmet:
                blocked.append((sg, unmet))
        return blocked

    def topological_sort(self, decomposition: GoalDecomposition) -> list[SubGoal]:
        """Return sub-goals in dependency-respecting execution order (Kahn's algorithm)."""
        id_to_sg = {sg.sub_goal_id: sg for sg in decomposition.sub_goals}
        in_degree: dict[str, int] = defaultdict(int)
        adjacency: dict[str, list[str]] = defaultdict(list)

        for sg in decomposition.sub_goals:
            if sg.sub_goal_id not in in_degree:
                in_degree[sg.sub_goal_id] = 0

        for src, dst in decomposition.dag_edges:
            adjacency[src].append(dst)
            in_degree[dst] += 1

        # BFS
        queue: deque[str] = deque()
        for sid, deg in in_degree.items():
            if deg == 0:
                queue.append(sid)

        result: list[SubGoal] = []
        while queue:
            node = queue.popleft()
            if node in id_to_sg:
                result.append(id_to_sg[node])
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return result

    def has_cycle(self, decomposition: GoalDecomposition) -> bool:
        """Check if the DAG contains a cycle."""
        sorted_goals = self.topological_sort(decomposition)
        return len(sorted_goals) != len(decomposition.sub_goals)

    @staticmethod
    def _validate_goal_id(goal_id: str) -> str:
        """Ensure goal_id is filesystem-safe (no path traversal)."""
        if not re.match(r"^[a-zA-Z0-9_-]+$", goal_id):
            raise ValueError(f"Invalid goal_id: {goal_id!r} — must be alphanumeric with underscores/hyphens")
        return goal_id

    def persist(self, decomposition: GoalDecomposition) -> None:
        """Save decomposition to STATE/goal_trees/<goal_id>.json."""
        self._validate_goal_id(decomposition.goal_id)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / f"{decomposition.goal_id}.json"

        fd, tmp_path = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(decomposition.model_dump_json(indent=2))
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load(self, goal_id: str) -> GoalDecomposition | None:
        """Load a decomposition from STATE/goal_trees/<goal_id>.json."""
        self._validate_goal_id(goal_id)
        path = self._state_dir / f"{goal_id}.json"
        if not path.exists():
            return None
        try:
            return GoalDecomposition.model_validate_json(path.read_text())
        except Exception as exc:
            log.warning("Failed to load decomposition %s: %s", goal_id, exc)
            return None

    def _has_unmet_deps(self, sg: SubGoal, decomposition: GoalDecomposition) -> bool:
        """Check if a sub-goal has unmet dependencies."""
        completed_ids = self._completed_ids(decomposition)
        return any(dep not in completed_ids for dep in sg.dependencies)

    def _get_unmet_dep_names(self, sg: SubGoal, decomposition: GoalDecomposition) -> list[str]:
        """Get names of unmet dependencies."""
        completed_ids = self._completed_ids(decomposition)
        return [dep for dep in sg.dependencies if dep not in completed_ids]
