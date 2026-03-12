"""Trace context for cross-process correlation in NovaCore.

Generates trace IDs, propagates them via environment variables to
child processes (Claude CLI workers), and provides context managers
for span tracking.

Usage:
    from utils.trace_context import TraceContext

    # New trace for a task
    ctx = TraceContext.new("watcher", task="0065_fix_bug")
    env = ctx.to_env()  # pass to subprocess

    # In child process
    ctx = TraceContext.from_env()  # reads NOVA_TRACE_ID etc.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TraceContext:
    trace_id: str
    span_id: str
    component: str
    parent_span_id: str = ""
    task: str = ""
    started_at: float = field(default_factory=time.time)

    @classmethod
    def new(cls, component: str, task: str = "") -> TraceContext:
        """Create a new root trace."""
        return cls(
            trace_id=uuid.uuid4().hex[:16],
            span_id=uuid.uuid4().hex[:8],
            component=component,
            task=task,
        )

    @classmethod
    def child(cls, parent: TraceContext, component: str) -> TraceContext:
        """Create a child span under an existing trace."""
        return cls(
            trace_id=parent.trace_id,
            span_id=uuid.uuid4().hex[:8],
            component=component,
            parent_span_id=parent.span_id,
            task=parent.task,
        )

    @classmethod
    def from_env(cls, component: str = "worker") -> TraceContext:
        """Reconstruct trace context from environment variables."""
        trace_id = os.environ.get("NOVA_TRACE_ID", uuid.uuid4().hex[:16])
        parent_span = os.environ.get("NOVA_SPAN_ID", "")
        task = os.environ.get("NOVA_TASK", "")
        return cls(
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:8],
            component=component,
            parent_span_id=parent_span,
            task=task,
        )

    def to_env(self) -> dict[str, str]:
        """Export trace context as env vars for child processes."""
        return {
            "NOVA_TRACE_ID": self.trace_id,
            "NOVA_SPAN_ID": self.span_id,
            "NOVA_TASK": self.task,
        }

    def as_dict(self) -> dict[str, str]:
        """Return trace fields for inclusion in log entries."""
        d: dict[str, str] = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "component": self.component,
        }
        if self.parent_span_id:
            d["parent_span_id"] = self.parent_span_id
        if self.task:
            d["task"] = self.task
        return d

    def elapsed_ms(self) -> int:
        """Milliseconds since this span started."""
        return int((time.time() - self.started_at) * 1000)
