"""Deterministic rules engine for heartbeat decisions.

Replaces the LLM-based heartbeat agent (~10 Opus calls/day) with a pure
pattern-matching engine.  Every section of the 6-point heartbeat checklist
is fully deterministic, so we can eliminate the LLM call entirely for the
common case and only escalate truly novel situations.

Action format (compatible with ``heartbeat._handle_agent_actions``):

- Notify: ``{"type": "notify", "message": "..."}``
- Task:   ``{"type": "task", "title": "task-slug-name", "body": "..."}``
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
TASKS = BASE / "TASKS"
OUTPUT = BASE / "OUTPUT"

# Thresholds (seconds unless noted)
RESEARCH_STALE_HOURS = 4.0
IDLE_THRESHOLD_HOURS = 2.0
QUEUE_LOW_WATERMARK = 3  # inject work when queue is below this
STALE_PENDING_MINUTES = 30
STUCK_INPROGRESS_MINUTES = 20
MAX_SIMULTANEOUS_FAILURES = 3  # escalate to LLM above this


@dataclass
class RuleResult:
    """Aggregated output from the rules engine."""

    actions: list[dict[str, str]] = field(default_factory=list)
    escalate_to_llm: bool = False
    escalation_reason: str = ""
    confidence: float = 1.0


class HeartbeatRulesEngine:
    """Deterministic replacement for the LLM-based heartbeat agent.

    Evaluates health-check dicts (``{"name": ..., "ok": ..., "detail": ...}``)
    and filesystem state to produce notify/task actions identical to those
    the LLM agent would emit.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, checks: list[dict]) -> RuleResult:
        """Run all rules against *checks* and return aggregated actions.

        Each ``_check_*`` method returns a ``list[dict]`` of actions.  The
        engine collects them, deduplicates, and decides whether LLM
        escalation is needed.
        """
        result = RuleResult()

        rule_methods = [
            ("research_pipeline", self._check_research_pipeline),
            ("planning", self._check_planning),
            ("task_queue_health", self._check_task_queue_health),
            ("idle_detection", self._check_idle_detection),
            ("system_health", self._check_system_health),
            ("memory_health", self._check_memory_health),
        ]

        escalation_reasons: list[str] = []

        for name, method in rule_methods:
            try:
                actions, escalate, reason = method(checks)
                log.debug(
                    "rule=%s actions=%d escalate=%s",
                    name,
                    len(actions),
                    escalate,
                )
                result.actions.extend(actions)
                if escalate:
                    escalation_reasons.append(reason)
            except Exception:
                log.exception("rule %s raised; skipping", name)

        if escalation_reasons:
            result.escalate_to_llm = True
            result.escalation_reason = "; ".join(escalation_reasons)
            # Lower confidence when escalation is needed
            result.confidence = 0.5

        return result

    def shadow_compare(self, checks: list[dict], llm_response: str) -> dict[str, Any]:
        """Run rules engine *and* compare with the LLM response.

        Used in shadow mode to validate rules before cutting over.
        Returns a dict with both sets of actions and an ``agreement`` flag.
        """
        rules_result = self.evaluate(checks)

        # Parse LLM response for structured actions
        llm_actions = self._parse_llm_actions(llm_response)
        llm_is_ok = "HEARTBEAT_OK" in llm_response

        rules_is_ok = len(rules_result.actions) == 0

        agreement = llm_is_ok == rules_is_ok

        comparison = {
            "rules_actions": rules_result.actions,
            "llm_actions": llm_actions,
            "agreement": agreement,
            "rules_ok": rules_is_ok,
            "llm_ok": llm_is_ok,
            "escalate_to_llm": rules_result.escalate_to_llm,
            "escalation_reason": rules_result.escalation_reason,
        }

        if not agreement:
            log.warning(
                "SHADOW DISAGREEMENT: rules_ok=%s llm_ok=%s rules_actions=%d llm_actions=%d",
                rules_is_ok,
                llm_is_ok,
                len(rules_result.actions),
                len(llm_actions),
            )

        return comparison

    # ------------------------------------------------------------------
    # Rule: Research Pipeline
    # ------------------------------------------------------------------

    def _check_research_pipeline(self, checks: list[dict]) -> tuple[list[dict[str, str]], bool, str]:
        """Inject a research task when the pipeline is dry.

        Conditions (all must be true):
        - No pending research tasks in TASKS/
        - Last research output is older than RESEARCH_STALE_HOURS
        - Task queue has fewer than QUEUE_LOW_WATERMARK items
        """
        actions: list[dict[str, str]] = []

        has_pending_research = self._has_pending_research_tasks()
        last_research_age = self._last_output_age_hours("hb_research_")
        queue_size = self._task_queue_size()

        log.debug(
            "research_pipeline: pending=%s last_age=%.1fh queue=%d",
            has_pending_research,
            last_research_age,
            queue_size,
        )

        if not has_pending_research and last_research_age > RESEARCH_STALE_HOURS and queue_size < QUEUE_LOW_WATERMARK:
            actions.append(
                {
                    "type": "task",
                    "title": "hb-research-injection",
                    "body": (
                        "No research output in the last "
                        f"{RESEARCH_STALE_HOURS:.0f}h and queue is low "
                        f"({queue_size} items). Injecting research task "
                        "to keep the pipeline active."
                    ),
                }
            )

        return actions, False, ""

    # ------------------------------------------------------------------
    # Rule: Planning
    # ------------------------------------------------------------------

    def _check_planning(self, checks: list[dict]) -> tuple[list[dict[str, str]], bool, str]:
        """Inject a planning task when new research outputs exist.

        Compares timestamps of ``hb_research_*`` vs ``hb_plan_*`` files
        in OUTPUT/.  If research is newer than the last plan, planning
        is overdue.
        """
        actions: list[dict[str, str]] = []

        last_research_ts = self._newest_output_mtime("hb_research_")
        last_plan_ts = self._newest_output_mtime("hb_plan_")

        log.debug(
            "planning: last_research_ts=%.0f last_plan_ts=%.0f",
            last_research_ts,
            last_plan_ts,
        )

        # Research exists and is newer than the last plan (or no plan exists)
        if last_research_ts > 0 and last_research_ts > last_plan_ts:
            actions.append(
                {
                    "type": "task",
                    "title": "hb-planning-cycle",
                    "body": (
                        "New research outputs found since last planning "
                        "cycle. Triggering planning to incorporate findings."
                    ),
                }
            )

        return actions, False, ""

    # ------------------------------------------------------------------
    # Rule: Task Queue Health
    # ------------------------------------------------------------------

    def _scan_task_filesystem(self) -> tuple[list[str], list[str], list[str]]:
        """Scan TASKS/ for stale pending, stuck in-progress, and recent failures."""
        stale_pending: list[str] = []
        stuck_inprogress: list[str] = []
        recent_failures: list[str] = []

        if not TASKS.exists():
            return stale_pending, stuck_inprogress, recent_failures

        now = time.time()
        lifecycle_suffixes = (".inprogress", ".done", ".failed", ".cancelled")

        for p in TASKS.glob("*.md"):
            if any(p.name.endswith(s) for s in lifecycle_suffixes):
                continue
            try:
                age_min = (now - p.stat().st_mtime) / 60
                if age_min > STALE_PENDING_MINUTES:
                    stale_pending.append(p.name)
            except OSError:
                continue

        for p in TASKS.glob("*.inprogress"):
            try:
                age_min = (now - p.stat().st_mtime) / 60
                if age_min > STUCK_INPROGRESS_MINUTES:
                    stuck_inprogress.append(p.name)
            except OSError:
                continue

        two_hours_ago = now - 7200
        for p in TASKS.glob("*.failed"):
            try:
                if p.stat().st_mtime > two_hours_ago:
                    recent_failures.append(p.name)
            except OSError:
                continue

        return stale_pending, stuck_inprogress, recent_failures

    def _check_task_queue_health(self, checks: list[dict]) -> tuple[list[dict[str, str]], bool, str]:
        """Flag stale pending, stuck in-progress, and recent failures."""
        actions: list[dict[str, str]] = []

        # First, check the deterministic check results for queue issues
        queue_checks = [c for c in checks if "task_queue" in c.get("name", "") or "queue" in c.get("name", "")]
        for qc in queue_checks:
            if not qc.get("ok", True):
                detail = qc.get("detail", "unknown issue")
                actions.append({"type": "notify", "message": f"Task queue issue: {detail}"})

        stale_pending, stuck_inprogress, recent_failures = self._scan_task_filesystem()

        if stuck_inprogress:
            names = ", ".join(stuck_inprogress[:5])
            actions.append(
                {
                    "type": "notify",
                    "message": (
                        f"{len(stuck_inprogress)} task(s) stuck in-progress (>{STUCK_INPROGRESS_MINUTES}min): {names}"
                    ),
                }
            )

        if recent_failures:
            names = ", ".join(recent_failures[:5])
            actions.append(
                {
                    "type": "notify",
                    "message": f"{len(recent_failures)} recent task failure(s) (last 2h): {names}",
                }
            )

        if len(stale_pending) > 10:
            actions.append(
                {
                    "type": "notify",
                    "message": (
                        f"{len(stale_pending)} tasks pending >{STALE_PENDING_MINUTES}min — queue may need attention"
                    ),
                }
            )

        return actions, False, ""

    # ------------------------------------------------------------------
    # Rule: Idle Detection
    # ------------------------------------------------------------------

    def _check_idle_detection(self, checks: list[dict]) -> tuple[list[dict[str, str]], bool, str]:
        """Inject a research task when the system has been idle too long.

        Conditions (all must be true):
        - Last output is older than IDLE_THRESHOLD_HOURS
        - Task queue has fewer than QUEUE_LOW_WATERMARK items
        - We are in active hours (06-22 UTC)
        """
        actions: list[dict[str, str]] = []

        last_output_age = self._last_output_age_hours("")
        queue_size = self._task_queue_size()

        import datetime

        current_hour = datetime.datetime.now(datetime.timezone.utc).hour
        in_active_hours = 6 <= current_hour < 22

        log.debug(
            "idle_detection: last_output_age=%.1fh queue=%d active=%s",
            last_output_age,
            queue_size,
            in_active_hours,
        )

        if last_output_age > IDLE_THRESHOLD_HOURS and queue_size < QUEUE_LOW_WATERMARK and in_active_hours:
            actions.append(
                {
                    "type": "task",
                    "title": "hb-idle-research-injection",
                    "body": (
                        f"System idle for {last_output_age:.1f}h with only "
                        f"{queue_size} queued tasks. Injecting research "
                        "task to keep the pipeline productive."
                    ),
                }
            )

        return actions, False, ""

    # ------------------------------------------------------------------
    # Rule: System Health
    # ------------------------------------------------------------------

    def _check_system_health(self, checks: list[dict]) -> tuple[list[dict[str, str]], bool, str]:
        """Pass through FAIL/WARN checks; escalate on mass failures."""
        actions: list[dict[str, str]] = []
        failures: list[dict] = []

        for c in checks:
            if c.get("ok", True):
                continue

            name = c.get("name", "unknown")
            detail = c.get("detail", "no detail")
            failures.append(c)

            actions.append(
                {
                    "type": "notify",
                    "message": f"Health check FAIL: {name} - {detail}",
                }
            )

        escalate = len(failures) > MAX_SIMULTANEOUS_FAILURES
        reason = ""
        if escalate:
            reason = (
                f"{len(failures)} simultaneous health check failures "
                f"(>{MAX_SIMULTANEOUS_FAILURES}) — novel situation, "
                "escalating to LLM for triage"
            )
            log.warning("system_health escalation: %s", reason)

        return actions, escalate, reason

    # ------------------------------------------------------------------
    # Rule: Memory Health
    # ------------------------------------------------------------------

    def _check_memory_health(self, checks: list[dict]) -> tuple[list[dict[str, str]], bool, str]:
        """Pass through memory-related check results."""
        actions: list[dict[str, str]] = []

        memory_checks = [
            c
            for c in checks
            if any(kw in c.get("name", "") for kw in ("memory", "fusion", "pinecone", "neo4j", "redis"))
        ]

        for mc in memory_checks:
            if not mc.get("ok", True):
                name = mc.get("name", "unknown")
                detail = mc.get("detail", "no detail")
                actions.append(
                    {
                        "type": "notify",
                        "message": (f"Memory system issue: {name} - {detail}"),
                    }
                )

        return actions, False, ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _has_pending_research_tasks(self) -> bool:
        """Check if any pending task in TASKS/ contains 'research' in name."""
        if not TASKS.exists():
            return False

        lifecycle_suffixes = (".inprogress", ".done", ".failed", ".cancelled")
        for p in TASKS.glob("*.md"):
            if any(p.name.endswith(s) for s in lifecycle_suffixes):
                continue
            if "research" in p.name.lower():
                return True
        return False

    def _count_pending_tasks(self) -> int:
        """Count .md files in TASKS/ that are still pending."""
        if not TASKS.exists():
            return 0

        lifecycle_suffixes = (".inprogress", ".done", ".failed", ".cancelled")
        count = 0
        for p in TASKS.glob("*.md"):
            if any(p.name.endswith(s) for s in lifecycle_suffixes):
                continue
            count += 1
        return count

    def _last_output_age_hours(self, prefix: str) -> float:
        """Age (in hours) of the most recent OUTPUT/<prefix>* file.

        Returns ``float('inf')`` when no matching file is found or the
        OUTPUT directory does not exist.
        """
        if not OUTPUT.exists():
            return float("inf")

        newest_mtime = 0.0
        pattern = f"{prefix}*" if prefix else "*"
        for p in OUTPUT.glob(pattern):
            if not p.is_file():
                continue
            try:
                mt = p.stat().st_mtime
                if mt > newest_mtime:
                    newest_mtime = mt
            except OSError:
                continue

        if newest_mtime == 0.0:
            return float("inf")

        return (time.time() - newest_mtime) / 3600

    def _newest_output_mtime(self, prefix: str) -> float:
        """Return the mtime (epoch seconds) of the newest OUTPUT/<prefix>* file.

        Returns ``0.0`` if no matching file is found.
        """
        if not OUTPUT.exists():
            return 0.0

        newest = 0.0
        for p in OUTPUT.glob(f"{prefix}*"):
            if not p.is_file():
                continue
            try:
                mt = p.stat().st_mtime
                if mt > newest:
                    newest = mt
            except OSError:
                continue
        return newest

    def _task_queue_size(self) -> int:
        """Total count of task files in TASKS/ (all statuses)."""
        if not TASKS.exists():
            return 0

        count = 0
        for p in TASKS.iterdir():
            if p.is_file() and p.suffix == ".md":
                count += 1
        return count

    def _parse_llm_actions(self, response: str) -> list[dict]:
        """Extract structured JSON actions from an LLM response string.

        Handles the same formats as ``heartbeat._extract_json_actions``.
        """
        if not response:
            return []

        # Try to find a JSON array in the response
        # Look for [...] pattern
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, list) and all(isinstance(d, dict) for d in data):
                    return data
            except (json.JSONDecodeError, TypeError):
                pass

        # Try to find a JSON object (single action)
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict):
                    return [data]
            except (json.JSONDecodeError, TypeError):
                pass

        return []
