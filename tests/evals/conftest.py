"""Shared fixtures and configuration for DeepEval-based evaluation tests.

Local-only mode: no DeepEval SaaS telemetry is sent.
LLM-dependent tests are gated behind the ``eval_llm`` marker and skip
automatically when OPENAI_API_KEY is absent.
"""

from __future__ import annotations

import json
import os
import textwrap

import pytest

# ---------------------------------------------------------------------------
# DeepEval local-only configuration
# ---------------------------------------------------------------------------
# Setting DEEPEVAL_TELEMETRY_OPT_OUT prevents any SaaS calls.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")


# ---------------------------------------------------------------------------
# Pytest markers
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "eval: All evaluation tests")
    config.addinivalue_line("markers", "eval_llm: Evaluation tests that require LLM API access")


# ---------------------------------------------------------------------------
# Skip LLM-dependent tests when no API key is available
# ---------------------------------------------------------------------------
HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))

skip_no_llm = pytest.mark.skipif(
    not HAS_OPENAI_KEY,
    reason="OPENAI_API_KEY not set — skipping LLM-dependent eval",
)


# ---------------------------------------------------------------------------
# Sample fixtures: representative NovaCore agent outputs
# ---------------------------------------------------------------------------


@pytest.fixture
def heartbeat_output() -> str:
    """A typical heartbeat report produced by the NovaCore agent."""
    return textwrap.dedent("""\
        ## Heartbeat Report — 2026-03-18T14:32:00Z

        ### System Metrics
        - CPU usage: 23%
        - Memory usage: 1.2 GB / 4.0 GB (30%)
        - Disk usage: 18.4 GB / 50.0 GB (37%)
        - Uptime: 14d 6h 22m

        ### Service Status
        | Service                  | Status  |
        |--------------------------|---------|
        | novacore-watcher         | running |
        | novacore-telegram        | running |
        | novacore-telegram-notifier | running |

        ### Open Threads
        1. Enhancement Plan v32 — Phase 7 in progress
        2. NovaTrade — monitoring first live trades

        ### Anomalies
        - None detected

        ### Next Actions
        - Continue Phase 7 eval framework integration
        - Review NovaTrade first-trade failure logs
    """)


@pytest.fixture
def heartbeat_json_output() -> str:
    """Heartbeat data as structured JSON."""
    data = {
        "timestamp": "2026-03-18T14:32:00Z",
        "system_metrics": {
            "cpu_percent": 23.0,
            "memory_used_gb": 1.2,
            "memory_total_gb": 4.0,
            "disk_used_gb": 18.4,
            "disk_total_gb": 50.0,
            "uptime_seconds": 1231320,
        },
        "services": [
            {"name": "novacore-watcher", "status": "running"},
            {"name": "novacore-telegram", "status": "running"},
            {"name": "novacore-telegram-notifier", "status": "running"},
        ],
        "open_threads": [
            "Enhancement Plan v32 — Phase 7 in progress",
            "NovaTrade — monitoring first live trades",
        ],
        "anomalies": [],
        "next_actions": [
            "Continue Phase 7 eval framework integration",
            "Review NovaTrade first-trade failure logs",
        ],
    }
    return json.dumps(data, indent=2)


@pytest.fixture
def codegen_output() -> str:
    """A typical code-generation output from the agent."""
    return textwrap.dedent('''\
        ## Code Generation Result

        ### Task
        Implement a retry decorator with exponential backoff.

        ### Implementation

        ```python
        import functools
        import time
        from typing import Callable, TypeVar

        F = TypeVar("F", bound=Callable)


        def retry(max_attempts: int = 3, base_delay: float = 1.0, backoff_factor: float = 2.0):
            """Retry decorator with exponential backoff.

            Args:
                max_attempts: Maximum number of attempts before giving up.
                base_delay: Initial delay in seconds between retries.
                backoff_factor: Multiplier applied to delay after each retry.
            """

            def decorator(func: F) -> F:
                @functools.wraps(func)
                def wrapper(*args, **kwargs):
                    delay = base_delay
                    for attempt in range(1, max_attempts + 1):
                        try:
                            return func(*args, **kwargs)
                        except Exception:
                            if attempt == max_attempts:
                                raise
                            time.sleep(delay)
                            delay *= backoff_factor
                    return None  # unreachable
                return wrapper
            return decorator
        ```

        ### Tests

        ```python
        import pytest
        from retry import retry


        def test_retry_succeeds_first_try():
            call_count = 0

            @retry(max_attempts=3)
            def succeed():
                nonlocal call_count
                call_count += 1
                return "ok"

            assert succeed() == "ok"
            assert call_count == 1


        def test_retry_eventual_success():
            call_count = 0

            @retry(max_attempts=3, base_delay=0.01)
            def flaky():
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("not yet")
                return "ok"

            assert flaky() == "ok"
            assert call_count == 3
        ```

        ### Verification
        - Type hints included
        - Docstring with Args section
        - Two unit tests covering happy-path and retry-path
    ''')


@pytest.fixture
def research_output() -> str:
    """A typical research summary produced by the agent."""
    return textwrap.dedent("""\
        ## Research Summary: Circuit Breaker Patterns for Distributed Systems

        ### Key Findings

        1. **Three-state model** (Closed, Open, Half-Open) is the industry standard,
           originally described by Michael Nygard in *Release It!* (2007).

        2. **Rolling-window counting** outperforms simple counters for failure
           detection because it avoids false positives from old failures.
           [Source: Netflix Tech Blog, 2012]

        3. **Exponential backoff** on the Half-Open → Closed transition reduces
           thundering-herd effects when the downstream recovers.
           [Source: AWS Architecture Blog, 2019]

        4. **Health-check probes** during the Open state allow faster recovery
           detection compared to timer-based transitions.
           [Source: Microsoft Azure Architecture Center, 2021]

        ### Recommendations
        - Adopt rolling-window counters (already implemented in NovaCore Phase 6).
        - Add active health-check probes during Open state.
        - Instrument circuit state transitions with Langfuse traces.

        ### Citations
        - Nygard, M. (2007). *Release It!*. Pragmatic Bookshelf.
        - Netflix Technology Blog (2012). "Fault Tolerance in a High Volume,
          Distributed System."
        - AWS Architecture Blog (2019). "Implementing Reliable Distributed Systems."
        - Microsoft Azure Architecture Center (2021). "Circuit Breaker Pattern."

        ### Confidence
        High — findings cross-validated across 4 independent sources.
    """)


@pytest.fixture
def empty_output() -> str:
    """An empty string representing a failed or missing output."""
    return ""


@pytest.fixture
def malformed_json_output() -> str:
    """Malformed JSON that should fail structural validation."""
    return '{"timestamp": "2026-03-18", "services": [{"name": "watcher" status: "running"}]}'
