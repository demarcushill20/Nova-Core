"""Structured JSONL logging for NovaCore observability.

Writes machine-parseable log events to LOGS/structured.jsonl with
trace context, timestamps, and typed event data. Complements (does NOT
replace) existing plain-text logs and the hash-chained audit log.

Usage:
    from utils.structured_log import slog
    from utils.trace_context import TraceContext

    ctx = TraceContext.new("watcher", task="0065")
    slog.event("task.picked_up", ctx, stem="0065", priority="high")
    slog.event("task.completed", ctx, duration_ms=ctx.elapsed_ms())
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.trace_context import TraceContext

_LOGS_DIR = Path(os.environ.get("NOVA_LOGS_DIR", "LOGS"))
_LOG_FILE = _LOGS_DIR / "structured.jsonl"
_MAX_SIZE = 10 * 1024 * 1024  # 10 MB before rotation


class StructuredLogger:
    """Append-only JSONL logger with trace context."""

    def __init__(self, path: Path = _LOG_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def event(
        self,
        event_type: str,
        ctx: TraceContext | None = None,
        level: str = "info",
        **data: Any,
    ) -> dict[str, Any]:
        """Write a structured log event.

        Args:
            event_type: Dotted event name (e.g. "task.completed", "heartbeat.check")
            ctx: Optional trace context for correlation
            level: Log level (debug, info, warn, error)
            **data: Arbitrary key-value pairs for the event
        """
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "level": level,
        }
        if ctx:
            entry.update(ctx.as_dict())
        if data:
            entry["data"] = data

        line = json.dumps(entry, default=str, separators=(",", ":"))

        with self._lock:
            self._maybe_rotate()
            with open(self._path, "a") as f:
                f.write(line + "\n")

        # Forward to Langfuse if configured (best-effort)
        try:
            from utils.langfuse_tracing import trace_event

            trace_event(
                event_type,
                trace_id=ctx.trace_id if ctx else "",
                component=ctx.component if ctx else "",
                level=level,
                **data,
            )
        except Exception:
            pass  # Langfuse is optional

        return entry

    def _maybe_rotate(self) -> None:
        """Rotate log file if it exceeds max size."""
        try:
            if self._path.exists() and self._path.stat().st_size > _MAX_SIZE:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                rotated = self._path.with_suffix(f".{ts}.jsonl")
                self._path.rename(rotated)
        except OSError:
            pass  # rotation is best-effort


# Singleton instance
slog = StructuredLogger()
