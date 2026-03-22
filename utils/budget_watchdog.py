"""Budget Watchdog — enforces hard budgets on 6 dimensions for task execution.

Prevents runaway tasks by tracking step count, wall-clock time, token consumption,
conversation turns, cost, and progress-stall detection (hash-based loop detection).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BudgetWatchdog:
    """Enforces hard budgets on 6 dimensions simultaneously.

    Usage::

        budget = BudgetWatchdog(max_steps=50, max_wall_seconds=300)
        with budget:
            for step_output in run_task():
                budget.tick(step_output)
                allowed, reason = budget.check()
                if not allowed:
                    log_and_checkpoint(reason)
                    break
    """

    # --- configurable limits (None = unlimited) ---
    max_steps: int | None = 50
    max_wall_seconds: float | None = 300.0
    max_tokens: int | None = None
    max_turns: int | None = None
    max_cost_usd: float | None = None
    max_stall_count: int = 3  # consecutive identical outputs before stall

    # --- internal state ---
    steps: int = field(default=0, init=False, repr=False)
    tokens_used: int = field(default=0, init=False, repr=False)
    turns: int = field(default=0, init=False, repr=False)
    cost_usd: float = field(default=0.0, init=False, repr=False)
    _start_time: float | None = field(default=None, init=False, repr=False)
    _last_hash: str | None = field(default=None, init=False, repr=False)
    _stall_count: int = field(default=0, init=False, repr=False)
    _exhausted_reason: str | None = field(default=None, init=False, repr=False)

    # --- context manager ---
    def __enter__(self) -> BudgetWatchdog:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Log final state on exit
        elapsed = self.elapsed_seconds
        logger.info(
            "BudgetWatchdog exiting: steps=%d tokens=%d turns=%d cost=$%.4f elapsed=%.1fs reason=%s",
            self.steps,
            self.tokens_used,
            self.turns,
            self.cost_usd,
            elapsed,
            self._exhausted_reason or "completed",
        )
        return None  # do not suppress exceptions

    def start(self) -> None:
        """Start the wall-clock timer."""
        self._start_time = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        """Seconds elapsed since start (0.0 if not started)."""
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    def tick(
        self,
        step_output: str = "",
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        is_turn: bool = False,
    ) -> None:
        """Record one step of progress.

        Args:
            step_output: Text output of this step (used for stall detection).
            tokens: Tokens consumed in this step.
            cost_usd: Dollar cost incurred in this step.
            is_turn: Whether this step counts as a conversation turn.
        """
        self.steps += 1
        self.tokens_used += tokens
        self.cost_usd += cost_usd
        if is_turn:
            self.turns += 1

        # Progress-stall detection via MD5 hash
        output_hash = hashlib.md5(step_output.encode("utf-8"), usedforsecurity=False).hexdigest()
        if output_hash == self._last_hash:
            self._stall_count += 1
        else:
            self._stall_count = 0
        self._last_hash = output_hash

    def check(self) -> tuple[bool, str]:
        """Check whether the task is still within budget.

        Returns:
            (allowed, reason) — allowed is True if budget permits continuation.
            If allowed is False, reason describes which limit was exceeded.
        """
        # If already exhausted, keep returning the same reason
        if self._exhausted_reason is not None:
            return False, self._exhausted_reason

        # 1. Step count
        if self.max_steps is not None and self.steps >= self.max_steps:
            reason = f"step limit exceeded: {self.steps}/{self.max_steps}"
            self._exhausted_reason = reason
            return False, reason

        # 2. Wall-clock time
        if self.max_wall_seconds is not None and self._start_time is not None:
            elapsed = self.elapsed_seconds
            if elapsed >= self.max_wall_seconds:
                reason = f"wall-clock limit exceeded: {elapsed:.1f}s/{self.max_wall_seconds:.1f}s"
                self._exhausted_reason = reason
                return False, reason

        # 3. Token consumption
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            reason = f"token limit exceeded: {self.tokens_used}/{self.max_tokens}"
            self._exhausted_reason = reason
            return False, reason

        # 4. Conversation turns
        if self.max_turns is not None and self.turns >= self.max_turns:
            reason = f"turn limit exceeded: {self.turns}/{self.max_turns}"
            self._exhausted_reason = reason
            return False, reason

        # 5. Cost
        if self.max_cost_usd is not None and self.cost_usd >= self.max_cost_usd:
            reason = f"cost limit exceeded: ${self.cost_usd:.4f}/${self.max_cost_usd:.4f}"
            self._exhausted_reason = reason
            return False, reason

        # 6. Progress-stall detection
        if self._stall_count >= self.max_stall_count:
            reason = f"progress stall detected: {self._stall_count} consecutive identical outputs"
            self._exhausted_reason = reason
            return False, reason

        return True, "within budget"

    def reset(self) -> None:
        """Reset all counters and state."""
        self.steps = 0
        self.tokens_used = 0
        self.turns = 0
        self.cost_usd = 0.0
        self._start_time = None
        self._last_hash = None
        self._stall_count = 0
        self._exhausted_reason = None

    def summary(self) -> dict:
        """Return a dict summarizing current budget state."""
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "max_wall_seconds": self.max_wall_seconds,
            "tokens_used": self.tokens_used,
            "max_tokens": self.max_tokens,
            "turns": self.turns,
            "max_turns": self.max_turns,
            "cost_usd": round(self.cost_usd, 6),
            "max_cost_usd": self.max_cost_usd,
            "stall_count": self._stall_count,
            "max_stall_count": self.max_stall_count,
            "exhausted_reason": self._exhausted_reason,
        }
