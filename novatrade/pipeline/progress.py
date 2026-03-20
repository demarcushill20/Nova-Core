"""Pipeline progress callbacks — observability and notifications.

Provides a callback protocol that the pipeline orchestrator and campaign engine
can invoke at key milestones.  Two concrete implementations ship:

- ``LoggingProgressCallback`` — structured logger output.
- ``TelegramProgressCallback`` — milestone notifications via Telegram.

Usage::

    from novatrade.pipeline.progress import LoggingProgressCallback

    cb = LoggingProgressCallback()
    result = run_pipeline(config, callbacks=[cb])
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("novatrade.pipeline.progress")


# ---------------------------------------------------------------------------
# Callback protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProgressCallback(Protocol):
    """Protocol that all progress callbacks must satisfy."""

    def on_experiment_start(self, experiment_index: int, total: int) -> None:
        """Called before a single experiment runs."""
        ...

    def on_experiment_complete(
        self,
        experiment_index: int,
        total: int,
        status: str,
        score: float,
    ) -> None:
        """Called after a single experiment completes."""
        ...

    def on_stage_start(self, stage_name: str) -> None:
        """Called when a pipeline stage begins."""
        ...

    def on_stage_complete(self, stage_name: str, result: dict[str, Any]) -> None:
        """Called when a pipeline stage finishes."""
        ...

    def on_campaign_progress(
        self,
        completed: int,
        total: int,
        best_score: float,
        gate_pass_rate: float,
    ) -> None:
        """Called periodically to report overall campaign progress."""
        ...


# ---------------------------------------------------------------------------
# Logging callback
# ---------------------------------------------------------------------------


class LoggingProgressCallback:
    """Logs pipeline progress to the standard logger."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or log

    def on_experiment_start(self, experiment_index: int, total: int) -> None:
        self._log.info(
            "Experiment %d/%d starting",
            experiment_index + 1,
            total,
        )

    def on_experiment_complete(
        self,
        experiment_index: int,
        total: int,
        status: str,
        score: float,
    ) -> None:
        self._log.info(
            "Experiment %d/%d complete: status=%s score=%.4f",
            experiment_index + 1,
            total,
            status,
            score,
        )

    def on_stage_start(self, stage_name: str) -> None:
        self._log.info("Stage '%s' starting", stage_name)

    def on_stage_complete(self, stage_name: str, result: dict[str, Any]) -> None:
        status = result.get("status", "unknown")
        duration = result.get("duration_ms", 0)
        self._log.info(
            "Stage '%s' complete: status=%s duration=%dms",
            stage_name,
            status,
            duration,
        )

    def on_campaign_progress(
        self,
        completed: int,
        total: int,
        best_score: float,
        gate_pass_rate: float,
    ) -> None:
        pct = (completed / total * 100) if total > 0 else 0.0
        self._log.info(
            "Campaign progress: %d/%d (%.1f%%) best=%.4f gate_pass=%.1f%%",
            completed,
            total,
            pct,
            best_score,
            gate_pass_rate * 100,
        )


# ---------------------------------------------------------------------------
# Telegram callback
# ---------------------------------------------------------------------------


class TelegramProgressCallback:
    """Sends milestone Telegram notifications.

    Sends a message every *notify_every_n* experiments, whenever a new best
    score is found, and when the campaign completes.

    The Telegram transport is lazy-imported so the module works even when
    the notifier is not installed.

    Parameters:
        notify_every_n: Send a progress update every N experiments.
        send_fn: Optional custom send function (for testing).  If *None*,
            uses ``telegram_notifier.send_text`` or a no-op fallback.
    """

    def __init__(
        self,
        notify_every_n: int = 25,
        send_fn: Any | None = None,
    ) -> None:
        self.notify_every_n = max(1, notify_every_n)
        self._send = send_fn or self._default_send
        self._last_best: float = float("-inf")

    # -- protocol methods ---------------------------------------------------

    def on_experiment_start(self, experiment_index: int, total: int) -> None:
        pass  # no notification on start

    def on_experiment_complete(
        self,
        experiment_index: int,
        total: int,
        status: str,
        score: float,
    ) -> None:
        # New best found
        if score > self._last_best and self._last_best > float("-inf"):
            self._send(f"[NovaTrade] New best score: {score:.4f} (experiment {experiment_index + 1}/{total})")
        if score > self._last_best:
            self._last_best = score

        # Periodic milestone
        if (experiment_index + 1) % self.notify_every_n == 0:
            self._send(
                f"[NovaTrade] Progress: {experiment_index + 1}/{total} experiments complete, best={self._last_best:.4f}"
            )

    def on_stage_start(self, stage_name: str) -> None:
        pass

    def on_stage_complete(self, stage_name: str, result: dict[str, Any]) -> None:
        pass

    def on_campaign_progress(
        self,
        completed: int,
        total: int,
        best_score: float,
        gate_pass_rate: float,
    ) -> None:
        if completed >= total:
            self._send(
                f"[NovaTrade] Campaign complete: {total} experiments, "
                f"best={best_score:.4f}, gate_pass={gate_pass_rate * 100:.1f}%"
            )

    # -- transport ----------------------------------------------------------

    @staticmethod
    def _default_send(text: str) -> None:
        """Attempt to send via telegram_notifier; fall back to logging."""
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
        if not bot_token or not chat_id:
            log.debug("Telegram not configured — skipping notification")
            return
        try:
            from telegram_notifier import send_text  # type: ignore[import-untyped]

            send_text(text)
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# Helper: fan-out to multiple callbacks
# ---------------------------------------------------------------------------


def notify_experiment_start(
    callbacks: list[ProgressCallback],
    experiment_index: int,
    total: int,
) -> None:
    """Fan-out on_experiment_start to all callbacks, swallowing errors."""
    for cb in callbacks:
        try:
            cb.on_experiment_start(experiment_index, total)
        except Exception as exc:
            log.warning("Callback error in on_experiment_start: %s", exc)


def notify_experiment_complete(
    callbacks: list[ProgressCallback],
    experiment_index: int,
    total: int,
    status: str,
    score: float,
) -> None:
    """Fan-out on_experiment_complete to all callbacks, swallowing errors."""
    for cb in callbacks:
        try:
            cb.on_experiment_complete(experiment_index, total, status, score)
        except Exception as exc:
            log.warning("Callback error in on_experiment_complete: %s", exc)


def notify_stage_start(
    callbacks: list[ProgressCallback],
    stage_name: str,
) -> None:
    """Fan-out on_stage_start to all callbacks, swallowing errors."""
    for cb in callbacks:
        try:
            cb.on_stage_start(stage_name)
        except Exception as exc:
            log.warning("Callback error in on_stage_start: %s", exc)


def notify_stage_complete(
    callbacks: list[ProgressCallback],
    stage_name: str,
    result: dict[str, Any],
) -> None:
    """Fan-out on_stage_complete to all callbacks, swallowing errors."""
    for cb in callbacks:
        try:
            cb.on_stage_complete(stage_name, result)
        except Exception as exc:
            log.warning("Callback error in on_stage_complete: %s", exc)


def notify_campaign_progress(
    callbacks: list[ProgressCallback],
    completed: int,
    total: int,
    best_score: float,
    gate_pass_rate: float,
) -> None:
    """Fan-out on_campaign_progress to all callbacks, swallowing errors."""
    for cb in callbacks:
        try:
            cb.on_campaign_progress(completed, total, best_score, gate_pass_rate)
        except Exception as exc:
            log.warning("Callback error in on_campaign_progress: %s", exc)
