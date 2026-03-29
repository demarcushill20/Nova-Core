"""Background evolution processor — dequeues and executes skill evolutions.

Designed to be called periodically (e.g., every 5 minutes from heartbeat)
rather than running as its own event loop.  All operations are wrapped in
try/except so the processor never crashes the caller.

Phase 2 of the evolution pipeline.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from skills.evolution_queue import EvolutionQueue, EvolutionRequest
from skills.skill_evolver import EvolutionError, SkillEvolver

if TYPE_CHECKING:
    from skills.skill_dashboard import SkillDashboard
    from skills.version_store import SkillVersionStore

logger = logging.getLogger(__name__)


class EvolutionProcessor:
    """Background evolution processor — dequeues and executes skill evolutions.

    Designed to be called periodically (e.g., every 5 minutes from heartbeat)
    rather than running as its own event loop.
    """

    def __init__(
        self,
        version_store: SkillVersionStore,
        evolution_queue: EvolutionQueue,
        skill_evolver: SkillEvolver,
        dashboard: SkillDashboard | None = None,
        telegram_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.version_store = version_store
        self.evolution_queue = evolution_queue
        self.skill_evolver = skill_evolver
        self.dashboard = dashboard
        self.telegram_fn = telegram_fn

        # Lifetime counters for observability
        self._processed_count: int = 0
        self._success_count: int = 0
        self._failure_count: int = 0

    # -----------------------------------------------------------------
    # Core processing
    # -----------------------------------------------------------------

    def process_one(self) -> dict | None:
        """Dequeue and process the next evolution request.

        Returns a result dict with evolution outcome, or None if queue empty.
        """
        try:
            request = self.evolution_queue.dequeue()
        except Exception as exc:
            logger.error("Failed to dequeue evolution request: %s", exc)
            return None

        if request is None:
            return None

        logger.info(
            "Processing evolution: type=%s skill=%s (id=%s) task=%s",
            request.evolution_type,
            request.skill_name,
            request.skill_id,
            request.task_id or "(none)",
        )

        result = self._execute_evolution(request)

        self._processed_count += 1
        if result.get("success"):
            self._success_count += 1
        else:
            self._failure_count += 1

        return result

    def process_batch(self, max_items: int = 3) -> list[dict]:
        """Process up to max_items from the queue.

        Called by heartbeat or periodic scan.
        """
        results: list[dict] = []
        for _ in range(max_items):
            result = self.process_one()
            if result is None:
                break
            results.append(result)
        return results

    # -----------------------------------------------------------------
    # Health scan
    # -----------------------------------------------------------------

    def run_health_scan(self) -> dict:
        """Periodic health scan — detect improvement/capture candidates.

        Calls:
        - skill_evolver.detect_improvement_candidates() -> enqueues DERIVED
        - skill_evolver.detect_novel_patterns() -> enqueues CAPTURED
        - dashboard.get_health_summary() -> alerts on unhealthy skills

        Returns scan results dict.
        """
        candidates_found = 0
        patterns_found = 0
        enqueued = 0
        health: dict = {}

        # 1. Detect improvement candidates -> enqueue DERIVED
        try:
            candidates = self.skill_evolver.detect_improvement_candidates()
            candidates_found = len(candidates)

            for candidate in candidates:
                req = EvolutionRequest(
                    skill_id=candidate["skill_id"],
                    skill_name=candidate["skill_name"],
                    evolution_type="DERIVED",
                    direction=candidate.get("suggestion", "auto-detected improvement candidate"),
                    priority=4,  # lower priority than manual/triggered evolutions
                )
                if self.evolution_queue.enqueue(req):
                    enqueued += 1
                    logger.info(
                        "Health scan: enqueued DERIVED for %s",
                        candidate["skill_name"],
                    )
        except Exception as exc:
            logger.error("Health scan: detect_improvement_candidates failed: %s", exc)

        # 2. Detect novel patterns -> enqueue CAPTURED
        try:
            patterns = self.skill_evolver.detect_novel_patterns()
            patterns_found = len(patterns)

            for pattern in patterns:
                task_ids = pattern.get("task_ids", [])
                description = pattern.get("description_pattern", "novel pattern detected")

                req = EvolutionRequest(
                    skill_id="",  # no parent for CAPTURED
                    skill_name="",
                    evolution_type="CAPTURED",
                    direction=description,
                    priority=5,  # lowest priority — speculative
                    task_id=task_ids[0] if task_ids else "",
                    context=f"task_ids={','.join(task_ids[:10])}",
                )
                if self.evolution_queue.enqueue(req):
                    enqueued += 1
                    logger.info(
                        "Health scan: enqueued CAPTURED for pattern: %s",
                        description[:80],
                    )
        except Exception as exc:
            logger.error("Health scan: detect_novel_patterns failed: %s", exc)

        # 3. Dashboard health summary
        if self.dashboard:
            try:
                health = self.dashboard.get_health_summary()

                frozen = health.get("frozen_skills", [])
                unhealthy = health.get("unhealthy_skills", 0)
                if frozen or unhealthy:
                    logger.warning(
                        "Health scan: %d unhealthy skills, %d frozen",
                        unhealthy,
                        len(frozen),
                    )
            except Exception as exc:
                logger.error("Health scan: get_health_summary failed: %s", exc)

        scan_result = {
            "candidates_found": candidates_found,
            "patterns_found": patterns_found,
            "enqueued": enqueued,
            "health": health,
        }

        logger.info(
            "Health scan complete: candidates=%d patterns=%d enqueued=%d",
            candidates_found,
            patterns_found,
            enqueued,
        )
        return scan_result

    # -----------------------------------------------------------------
    # Observability
    # -----------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return lifetime processing stats."""
        return {
            "processed": self._processed_count,
            "successes": self._success_count,
            "failures": self._failure_count,
            "queue_size": self.evolution_queue.queue_size(),
        }

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _execute_evolution(self, request: EvolutionRequest) -> dict:
        """Execute a single evolution request and return result dict."""
        result: dict = {
            "type": request.evolution_type,
            "skill_id": request.skill_id,
            "skill_name": request.skill_name,
            "success": False,
            "new_skill_id": None,
            "error": None,
        }

        start = time.monotonic()

        try:
            new_version = self._dispatch_evolution(request)

            if new_version is not None:
                result["success"] = True
                result["new_skill_id"] = new_version.skill_id
                result["skill_name"] = new_version.name
                logger.info(
                    "Evolution succeeded: %s -> %s (id=%s) in %.1fs",
                    request.evolution_type,
                    new_version.name,
                    new_version.skill_id,
                    time.monotonic() - start,
                )
            else:
                result["error"] = "evolution returned None"
                logger.warning(
                    "Evolution returned None: type=%s skill=%s",
                    request.evolution_type,
                    request.skill_name,
                )

        except EvolutionError as exc:
            result["error"] = str(exc)
            logger.error(
                "EvolutionError for %s/%s: %s",
                request.evolution_type,
                request.skill_name,
                exc,
            )
        except Exception as exc:
            result["error"] = f"unexpected: {exc}"
            logger.error(
                "Unexpected error during %s/%s evolution: %s",
                request.evolution_type,
                request.skill_name,
                exc,
                exc_info=True,
            )

        # Mark complete in the queue (success or failure)
        try:
            self.evolution_queue.complete_evolution(request, success=result["success"])
        except Exception as exc:
            logger.error("Failed to complete evolution in queue: %s", exc)

        # Send telegram notification on success (opt-in, concise)
        if result["success"] and self.telegram_fn:
            self._notify_telegram(request, result)

        return result

    def _dispatch_evolution(self, request: EvolutionRequest):
        """Route to the correct SkillEvolver method based on evolution_type.

        Returns a SkillVersion or None.
        """
        etype = request.evolution_type.upper()

        if etype == "FIX":
            return self.skill_evolver.evolve_fix(
                skill_id=request.skill_id,
                direction=request.direction,
                task_id=request.task_id,
                failure_context=request.context,
            )

        if etype == "DERIVED":
            return self.skill_evolver.evolve_derived(
                parent_id=request.skill_id,
                direction=request.direction,
                task_id=request.task_id,
                success_context=request.context,
            )

        if etype == "CAPTURED":
            # Build task_examples from context (comma-separated task_ids)
            task_examples: list[str] = []
            ctx = request.context or ""
            if ctx.startswith("task_ids="):
                task_examples = [tid.strip() for tid in ctx[len("task_ids=") :].split(",") if tid.strip()]
            # Ensure minimum examples for the evolver
            if not task_examples:
                task_examples = [request.task_id] if request.task_id else ["unknown"]
            # Pad to minimum 2 examples if needed (evolver requires MIN_TASK_EXAMPLES=2)
            if len(task_examples) < 2:
                task_examples.append(task_examples[0])

            return self.skill_evolver.evolve_captured(
                pattern_description=request.direction,
                task_examples=task_examples,
                task_id=request.task_id,
            )

        logger.error("Unknown evolution type: %s", etype)
        return None

    def _notify_telegram(self, request: EvolutionRequest, result: dict) -> None:
        """Send a concise telegram notification for a successful evolution."""
        try:
            etype = request.evolution_type
            skill = result.get("skill_name", request.skill_name)
            new_id = result.get("new_skill_id", "?")

            msg = f"Skill evolved ({etype}): {skill}\nNew version: {new_id}"
            if self.telegram_fn is not None:
                self.telegram_fn(msg)
        except Exception as exc:
            logger.warning("Failed to send evolution telegram notification: %s", exc)
