"""NovaCore Error Catalog — Phase 6B Step 6.9.

Structured registry of known error types with classification and recommended
recovery actions.  Builds on top of ``utils/self_healing.py``'s error
classifier by providing *specific* error entries (with regex patterns, error
codes, and fine-grained retry/backoff configuration) while the original
classifier continues to serve as the fast-path fallback.

Integration:
    ``catalog_classify_error()`` is the primary entry point.  It first checks
    the catalog; if no catalog entry matches it delegates to
    ``self_healing.classify_error()`` for the existing pattern-based fallback.

Stdlib only — no pip installs required.  Thread-safe.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from utils.self_healing import ClassifiedError, ErrorCategory
from utils.self_healing import classify_error as _sh_classify

# ---------------------------------------------------------------------------
# ErrorEntry dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorEntry:
    """A single entry in the error catalog.

    Attributes:
        code: Unique error code, e.g. ``"E-MCP-001"``.
        category: One of ``transient``, ``permanent``, ``rate_limit``,
            ``auth``, ``resource``.
        pattern: Regex pattern matched against ``str(exception)``
            (case-insensitive).
        recommended_action: One of ``retry``, ``skip``, ``degrade``,
            ``alert``.
        max_retries: Maximum retry attempts (0 for permanent errors).
        backoff_base: Base backoff in seconds for exponential delay.
    """

    code: str
    category: str
    pattern: str
    recommended_action: str
    max_retries: int
    backoff_base: float

    # Compiled regex is cached but excluded from __eq__ / __hash__
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", re.compile(self.pattern, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Catalog entries — known error patterns
# ---------------------------------------------------------------------------

_CATALOG: list[ErrorEntry] = [
    # MCP connection failures
    ErrorEntry(
        code="E-MCP-001",
        category="transient",
        pattern=r"mcp.*(connection refused|connection reset|broken pipe|econnrefused|econnreset)",
        recommended_action="retry",
        max_retries=3,
        backoff_base=5.0,
    ),
    ErrorEntry(
        code="E-MCP-002",
        category="transient",
        pattern=r"mcp.*(timeout|timed out|deadline exceeded)",
        recommended_action="retry",
        max_retries=3,
        backoff_base=10.0,
    ),
    # Claude API rate limits
    ErrorEntry(
        code="E-CLAUDE-001",
        category="rate_limit",
        pattern=r"(claude|anthropic).*(rate.?limit|429|too many requests)",
        recommended_action="retry",
        max_retries=3,
        backoff_base=60.0,
    ),
    # Claude API auth errors
    ErrorEntry(
        code="E-CLAUDE-002",
        category="auth",
        pattern=r"(claude|anthropic).*(auth|401|403|invalid.?api.?key|permission denied)",
        recommended_action="alert",
        max_retries=0,
        backoff_base=0.0,
    ),
    # Redis connection errors
    ErrorEntry(
        code="E-REDIS-001",
        category="transient",
        pattern=r"redis.*(connection refused|connection reset|econnrefused|econnreset|timeout|timed out)",
        recommended_action="retry",
        max_retries=3,
        backoff_base=5.0,
    ),
    # Pinecone timeout
    ErrorEntry(
        code="E-PINECONE-001",
        category="transient",
        pattern=r"pinecone.*(timeout|timed out|deadline exceeded|service unavailable|503)",
        recommended_action="retry",
        max_retries=2,
        backoff_base=10.0,
    ),
    # Neo4j connection errors
    ErrorEntry(
        code="E-NEO4J-001",
        category="transient",
        pattern=r"neo4j.*(connection refused|connection reset|service unavailable|timeout|timed out)",
        recommended_action="retry",
        max_retries=3,
        backoff_base=5.0,
    ),
    # MetaApi errors
    ErrorEntry(
        code="E-METAAPI-001",
        category="transient",
        pattern=r"meta.?api.*(connection|timeout|timed out|service unavailable|502|503|504)",
        recommended_action="retry",
        max_retries=3,
        backoff_base=30.0,
    ),
    # FileNotFoundError on TASKS/ directory
    # M4 fix: anchor to the actual path pattern to avoid matching unrelated
    # paths that contain "tasks" in a case-insensitive match
    ErrorEntry(
        code="E-TASK-001",
        category="permanent",
        pattern=r"FileNotFoundError.*(?:TASKS/|/nova-core/TASKS)",
        recommended_action="skip",
        max_retries=0,
        backoff_base=0.0,
    ),
    # MemoryError / ResourceWarning
    ErrorEntry(
        code="E-RESOURCE-001",
        category="resource",
        pattern=r"(MemoryError|ResourceWarning|cannot allocate|out of memory|oom)",
        recommended_action="degrade",
        max_retries=1,
        backoff_base=30.0,
    ),
    # JSON decode on LLM output (M3 fix: pattern is specific — only matches
    # JSONDecodeError with LLM context words, so E-LLM-002 won't shadow it)
    ErrorEntry(
        code="E-LLM-001",
        category="transient",
        pattern=r"(json\.?decode|JSONDecodeError).*(llm|claude|response|output|parse)",
        recommended_action="retry",
        max_retries=2,
        backoff_base=5.0,
    ),
    # Broader JSONDecodeError — catches non-LLM JSON failures.
    # M3 fix: uses negative lookahead to exclude strings already matched
    # by E-LLM-001, ensuring no pattern overlap.
    ErrorEntry(
        code="E-LLM-002",
        category="transient",
        pattern=r"JSONDecodeError(?!.*(llm|claude|response|output|parse))",
        recommended_action="retry",
        max_retries=2,
        backoff_base=5.0,
    ),
]

# ---------------------------------------------------------------------------
# Thread-safe catalog access
# ---------------------------------------------------------------------------

_catalog_lock = threading.Lock()
_catalog_entries: list[ErrorEntry] = list(_CATALOG)

# M5 fix: cap catalog size to prevent unbounded growth from runaway callers
MAX_CATALOG_SIZE = 200


def register_entry(entry: ErrorEntry) -> None:
    """Register a new error entry at the front of the catalog.

    Later entries are matched first when they are inserted via this function,
    allowing runtime overrides of built-in patterns.

    M5 fix: raises ValueError if the catalog exceeds MAX_CATALOG_SIZE to
    prevent unbounded memory growth from runaway registration loops.
    """
    with _catalog_lock:
        if len(_catalog_entries) >= MAX_CATALOG_SIZE:
            raise ValueError(
                f"Error catalog full ({MAX_CATALOG_SIZE} entries). Call reset_catalog() or remove stale entries."
            )
        _catalog_entries.insert(0, entry)


def get_catalog() -> list[ErrorEntry]:
    """Return a snapshot of the current catalog (thread-safe copy)."""
    with _catalog_lock:
        return list(_catalog_entries)


def reset_catalog() -> None:
    """Reset the catalog to built-in entries (useful for testing)."""
    with _catalog_lock:
        _catalog_entries.clear()
        _catalog_entries.extend(_CATALOG)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Category -> ErrorCategory mapping for integration with self_healing
_CATEGORY_MAP: dict[str, ErrorCategory] = {
    "transient": ErrorCategory.TRANSIENT,
    "rate_limit": ErrorCategory.RESOURCE,
    "auth": ErrorCategory.PERMANENT,
    "permanent": ErrorCategory.PERMANENT,
    "resource": ErrorCategory.RESOURCE,
}


def classify_from_catalog(error: str | Exception) -> ErrorEntry | None:
    """Match *error* against catalog entries and return the first match.

    Returns ``None`` when no catalog pattern matches.
    """
    text = str(error)
    with _catalog_lock:
        entries = list(_catalog_entries)
    for entry in entries:
        if entry._compiled.search(text):
            return entry
    return None


def catalog_classify_error(error: str | Exception) -> ClassifiedError:
    """Classify an error, checking the catalog first, then falling back.

    This is the recommended entry point for callers that want the richest
    classification.  The returned ``ClassifiedError`` is compatible with the
    existing ``self_healing`` interface so existing code needs no changes.
    """
    entry = classify_from_catalog(error)
    if entry is not None:
        cat = _CATEGORY_MAP.get(entry.category, ErrorCategory.UNKNOWN)
        return ClassifiedError(
            category=cat,
            original_error=str(error),
            should_retry=entry.max_retries > 0,
            max_retries=entry.max_retries,
            backoff_seconds=entry.backoff_base,
            reason=f"catalog {entry.code}: {entry.category} ({entry.recommended_action})",
        )
    # Fallback to self_healing pattern matcher
    return _sh_classify(error)


# ---------------------------------------------------------------------------
# Retry config helper
# ---------------------------------------------------------------------------

_BACKOFF_MAX_DEFAULT = 300.0  # 5 minutes cap


def get_retry_config(entry: ErrorEntry | None) -> dict:
    """Return a retry configuration dict for the given catalog entry.

    If *entry* is ``None`` (unknown error), returns a conservative default.

    Returns:
        ``{"max_retries": int, "backoff_base": float, "backoff_max": float}``
    """
    if entry is None:
        return {
            "max_retries": 1,
            "backoff_base": 10.0,
            "backoff_max": _BACKOFF_MAX_DEFAULT,
        }
    return {
        "max_retries": entry.max_retries,
        "backoff_base": entry.backoff_base,
        "backoff_max": max(entry.backoff_base * (2**entry.max_retries), _BACKOFF_MAX_DEFAULT),
    }
