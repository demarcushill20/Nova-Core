"""
Langfuse LLM Observability Integration for NovaCore.

Provides tracing, cost tracking, and prompt versioning for all Claude CLI calls.
Uses Langfuse Cloud free tier (50k observations/month) until self-hosting is justified.

Setup:
  1. Sign up at https://us.cloud.langfuse.com (free tier)
  2. Create a project, get public + secret keys
  3. Add to /etc/novacore/langfuse.env:
       LANGFUSE_PUBLIC_KEY=pk-lf-...
       LANGFUSE_SECRET_KEY=sk-lf-...
       LANGFUSE_HOST=https://us.cloud.langfuse.com

Usage:
  from utils.langfuse_tracing import get_langfuse, trace_event, flush
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
            host=os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com"),
        )
    except Exception:
        return None


def trace_llm_call(
    name: str,
    input_text: str,
    output_text: str,
    model: str = "claude-sonnet-4-20250514",
    metadata: dict | None = None,
):
    """Record an LLM call trace in Langfuse.

    Gracefully no-ops if Langfuse is not configured.
    Uses Langfuse v4 SDK (start_observation as_type="generation").
    """
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        obs = lf.start_observation(
            name=name,
            as_type="generation",
            input=input_text,
            output=output_text,
            model=model,
            metadata=metadata or {},
        )
        return obs
    except Exception:
        return None


def trace_event(
    event_type: str,
    trace_id: str = "",
    component: str = "",
    level: str = "info",
    **data,
):
    """Send a structured event to Langfuse.

    Called automatically by StructuredLogger when Langfuse is configured.
    Gracefully no-ops otherwise.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        meta = {"component": component, "level": level, **data}
        if trace_id:
            meta["trace_id"] = trace_id
        event = lf.create_event(
            name=event_type,
            metadata=meta,
            level=level.upper() if level in ("warn", "error") else None,
        )
        return event
    except Exception:
        return None


def trace_task(task_name: str, metadata: dict | None = None):
    """Create a trace span for a NovaCore task execution.

    Returns an observation object for adding child spans, or None.
    """
    lf = get_langfuse()
    if lf is None:
        return None

    try:
        return lf.start_observation(
            name=f"task:{task_name}",
            metadata=metadata or {},
        )
    except Exception:
        return None


def flush():
    """Flush any pending Langfuse events."""
    lf = get_langfuse()
    if lf is not None:
        lf.flush()


def traced_cached_llm_call(
    call_fn,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    ttl: int | None = None,
    cache_bypass: bool = False,
    extract_response=None,
    extract_tokens=None,
    trace_name: str = "llm_call",
    metadata: dict | None = None,
):
    """Convenience wrapper combining LLM caching with Langfuse tracing.

    Delegates to cached_llm_call() for cache logic, then traces via Langfuse
    on cache miss. On cache hit, traces with cache metadata.

    Usage:
        from utils.langfuse_tracing import traced_cached_llm_call
        result = traced_cached_llm_call(
            call_fn=lambda: client.chat(...),
            model="claude-sonnet-4-20250514",
            system_prompt="You are helpful.",
            user_prompt="Explain caching.",
            trace_name="qa_review",
        )
    """
    from utils.llm_cache import cached_llm_call

    result = cached_llm_call(
        call_fn=call_fn,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        ttl=ttl,
        cache_bypass=cache_bypass,
        extract_response=extract_response,
        extract_tokens=extract_tokens,
    )

    # Trace cache hits too (they won't be traced by cached_llm_call itself)
    if result.get("cached"):
        trace_llm_call(
            name=trace_name,
            input_text=user_prompt,
            output_text=result.get("response", ""),
            model=model,
            metadata={
                **(metadata or {}),
                "cache": result.get("source", "exact"),
                "tokens_used": result.get("tokens_used", 0),
            },
        )

    return result
