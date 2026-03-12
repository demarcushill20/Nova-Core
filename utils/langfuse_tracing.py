"""
Langfuse LLM Observability Integration for NovaCore.

Provides tracing, cost tracking, and prompt versioning for all Claude CLI calls.
Uses Langfuse Cloud free tier (50k observations/month) until self-hosting is justified.

Setup:
  1. Sign up at https://cloud.langfuse.com (free tier)
  2. Create a project, get public + secret keys
  3. Add to /etc/novacore/langfuse.env:
       LANGFUSE_PUBLIC_KEY=pk-lf-...
       LANGFUSE_SECRET_KEY=sk-lf-...
       LANGFUSE_HOST=https://cloud.langfuse.com

Usage:
  from utils.langfuse_tracing import get_langfuse, trace_llm_call
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# Load langfuse env from file if not already in environment
_ENV_FILE = Path("/etc/novacore/langfuse.env")
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


@lru_cache(maxsize=1)
def get_langfuse():
    """Get or create the singleton Langfuse client.

    Returns None if credentials are not configured (graceful degradation).
    """
    try:
        from langfuse import Langfuse

        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

        if not public_key or not secret_key:
            return None

        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
    except Exception:
        return None


def trace_llm_call(
    name: str,
    input_text: str,
    output_text: str,
    model: str = "claude-sonnet-4-20250514",
    metadata: dict | None = None,
    tags: list[str] | None = None,
):
    """Record an LLM call trace in Langfuse.

    Gracefully no-ops if Langfuse is not configured.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    trace = lf.trace(
        name=name,
        metadata=metadata or {},
        tags=tags or ["nova-core"],
    )

    trace.generation(
        name=f"{name}_generation",
        model=model,
        input=input_text,
        output=output_text,
        metadata=metadata or {},
    )

    return trace


def trace_task(task_name: str, metadata: dict | None = None):
    """Create a trace span for a NovaCore task execution.

    Returns a trace object for adding child spans, or None if not configured.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    return lf.trace(
        name=f"task:{task_name}",
        metadata=metadata or {},
        tags=["nova-core", "task"],
    )


def trace_event(
    event_type: str,
    trace_id: str = "",
    component: str = "",
    level: str = "info",
    **data,
):
    """Send a structured event to Langfuse as a trace span.

    Called automatically by StructuredLogger when Langfuse is configured.
    Gracefully no-ops otherwise.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    trace = lf.trace(
        name=event_type,
        id=trace_id or None,
        metadata={"component": component, "level": level, **data},
        tags=["nova-core", component] if component else ["nova-core"],
    )
    return trace


def flush():
    """Flush any pending Langfuse events."""
    lf = get_langfuse()
    if lf is not None:
        lf.flush()
