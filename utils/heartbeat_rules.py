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

# Feature gate: proactive research injection (set True to re-enable)
# Gated 2026-04-06: hb_research_injection tasks failing at ~50% rate,
# wasting tokens on non-adaptive retries. Core heartbeat unaffected.
# Permanent fix applied (task 0721) but kept OFF until validation complete.
RESEARCH_INJECTION_ENABLED = False

# Thresholds (seconds unless noted)
RESEARCH_STALE_HOURS = 4.0
IDLE_THRESHOLD_HOURS = 2.0
QUEUE_LOW_WATERMARK = 3  # inject work when queue is below this
STALE_PENDING_MINUTES = 30
STUCK_INPROGRESS_MINUTES = 20
MAX_SIMULTANEOUS_FAILURES = 3  # escalate to LLM above this

# Proactive task retry policy (overrides global MAX_TASK_RETRIES for hb_proactive_ tasks)
PROACTIVE_MAX_RETRIES = 1

# Circuit breaker: disable research injection after N consecutive failures
RESEARCH_INJECTION_CB_THRESHOLD = 2
_RESEARCH_CB_STATE_FILE = STATE / "research_injection_cb.json"

# Rolling backoff: if failure rate exceeds this in recent window, delay injections
RESEARCH_INJECTION_ROLLING_WINDOW = 5  # track last N results
RESEARCH_INJECTION_BACKOFF_RATE = 0.5  # back off if >50% failures in window
RESEARCH_INJECTION_BACKOFF_COOLDOWN_S = 3600  # 1hr cooldown when backed off


def _build_research_injection_body(queue_size: int) -> str:
    """Build a research injection task body with explicit CONTRACT guidance.

    The body must be self-contained: it includes the full output contract
    so that downstream workers produce compliant output regardless of
    which prompt layer they execute through.
    """
    return (
        f"Queue is low ({queue_size} items). Conduct ONE focused research task.\n\n"
        "INSTRUCTIONS:\n"
        "1. Pick ONE unresearched topic aligned with active goals.\n"
        "2. Use web search (tavily_search/tavily_research) for 3-5 queries.\n"
        "3. Write findings to OUTPUT/hb_research_<stamp>.md\n"
        "4. Save a summary to Fusion Memory via upsert_memory.\n\n"
        "OUTPUT FORMAT (file must contain all sections):\n"
        "# Title\n"
        "## Executive Summary\n"
        "## Key Findings\n"
        "## Recommendations\n"
        "## Sources\n\n"
        "## CONTRACT\n"
        "summary: <one-line description of research conducted>\n"
        "files_changed: OUTPUT/hb_research_<stamp>.md\n"
        "verification: OUTPUT file exists, memory saved\n"
        "confidence: high\n\n"
        "IMPORTANT: You MUST end your output with a ## CONTRACT block "
        "containing the exact fields above. Failure to include the "
        "CONTRACT block will cause task rejection."
    )


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
        - RESEARCH_INJECTION_ENABLED is True
        - Circuit breaker is not tripped
        - No pending research tasks in TASKS/
        - Last research output is older than RESEARCH_STALE_HOURS
        - Task queue has fewer than QUEUE_LOW_WATERMARK items
        """
        actions: list[dict[str, str]] = []

        if not RESEARCH_INJECTION_ENABLED:
            log.debug("research_pipeline: SKIPPED (RESEARCH_INJECTION_ENABLED=False)")
            return actions, False, ""

        if self.is_research_injection_cb_tripped():
            log.warning("research_pipeline: SKIPPED (circuit breaker tripped)")
            return actions, False, ""

        if self.is_research_injection_backed_off():
            log.info("research_pipeline: SKIPPED (rolling backoff active)")
            return actions, False, ""

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
                    "body": _build_research_injection_body(queue_size),
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

        if not RESEARCH_INJECTION_ENABLED:
            log.debug("idle_detection: SKIPPED (RESEARCH_INJECTION_ENABLED=False)")
            return actions, False, ""

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
            if self.is_research_injection_cb_tripped():
                log.warning("idle_detection: SKIPPED (circuit breaker tripped)")
            elif self.is_research_injection_backed_off():
                log.info("idle_detection: SKIPPED (rolling backoff active)")
            else:
                actions.append(
                    {
                        "type": "task",
                        "title": "hb-idle-research-injection",
                        "body": _build_research_injection_body(queue_size),
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

    # ------------------------------------------------------------------
    # Circuit breaker for research injection
    # ------------------------------------------------------------------

    @staticmethod
    def _default_cb_state() -> dict:
        return {
            "consecutive_failures": 0,
            "tripped": False,
            "last_failure_ts": 0.0,
            "last_failure_reason": "",
            "recent_results": [],  # list of bools, newest last, max ROLLING_WINDOW
        }

    @staticmethod
    def _read_research_cb() -> dict:
        """Read circuit breaker state from disk."""
        default = HeartbeatRulesEngine._default_cb_state()
        if not _RESEARCH_CB_STATE_FILE.exists():
            return default
        try:
            state = json.loads(_RESEARCH_CB_STATE_FILE.read_text())
            # Backfill missing keys from default for forward compat
            for k, v in default.items():
                state.setdefault(k, v)
            return state
        except (json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def _write_research_cb(state: dict) -> None:
        """Persist circuit breaker state."""
        STATE.mkdir(parents=True, exist_ok=True)
        _RESEARCH_CB_STATE_FILE.write_text(json.dumps(state, indent=2))

    @classmethod
    def record_research_injection_result(cls, success: bool, failure_reason: str = "") -> None:
        """Update circuit breaker after a research injection task completes.

        Call with success=True on task completion, success=False on failure.
        After RESEARCH_INJECTION_CB_THRESHOLD consecutive failures the breaker
        trips and blocks further injections until manually reset.

        Also maintains a rolling window of recent results for backoff decisions.
        """
        state = cls._read_research_cb()

        # Update rolling window
        recent = state.get("recent_results", [])
        recent.append(success)
        if len(recent) > RESEARCH_INJECTION_ROLLING_WINDOW:
            recent = recent[-RESEARCH_INJECTION_ROLLING_WINDOW:]
        state["recent_results"] = recent

        if success:
            state["consecutive_failures"] = 0
            state["tripped"] = False
        else:
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            state["last_failure_ts"] = time.time()
            if failure_reason:
                state["last_failure_reason"] = failure_reason
            if state["consecutive_failures"] >= RESEARCH_INJECTION_CB_THRESHOLD:
                state["tripped"] = True
                log.warning(
                    "research_injection circuit breaker TRIPPED after %d consecutive failures",
                    state["consecutive_failures"],
                )
        cls._write_research_cb(state)

    @classmethod
    def is_research_injection_cb_tripped(cls) -> bool:
        """Return True if the circuit breaker has tripped."""
        return cls._read_research_cb().get("tripped", False)

    @classmethod
    def is_research_injection_backed_off(cls) -> bool:
        """Return True if rolling failure rate warrants a cooldown.

        Backs off when failure rate in the recent window exceeds
        RESEARCH_INJECTION_BACKOFF_RATE and the last failure was within
        RESEARCH_INJECTION_BACKOFF_COOLDOWN_S seconds.
        """
        state = cls._read_research_cb()
        recent = state.get("recent_results", [])
        if len(recent) < 2:
            return False  # not enough data to judge
        failure_count = sum(1 for r in recent if not r)
        failure_rate = failure_count / len(recent)
        if failure_rate <= RESEARCH_INJECTION_BACKOFF_RATE:
            return False
        last_fail_ts = state.get("last_failure_ts", 0.0)
        if time.time() - last_fail_ts > RESEARCH_INJECTION_BACKOFF_COOLDOWN_S:
            return False  # cooldown expired, allow retry
        log.info(
            "research_injection BACKED OFF: failure_rate=%.0f%% (%d/%d) cooldown_remaining=%ds",
            failure_rate * 100,
            failure_count,
            len(recent),
            int(RESEARCH_INJECTION_BACKOFF_COOLDOWN_S - (time.time() - last_fail_ts)),
        )
        return True

    @classmethod
    def get_last_failure_reason(cls) -> str:
        """Return the last recorded failure reason, or empty string."""
        return cls._read_research_cb().get("last_failure_reason", "")

    @classmethod
    def reset_research_injection_cb(cls) -> None:
        """Manually reset the circuit breaker."""
        cls._write_research_cb(cls._default_cb_state())

    # ------------------------------------------------------------------
    # Helpers (continued)
    # ------------------------------------------------------------------

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
