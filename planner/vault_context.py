"""Phase 5 — Read-only Obsidian vault context injection for planner flows.

Retrieves bounded, advisory context from the synced Obsidian vault
before task execution. Injected context is clearly marked as advisory
and never overrides runtime truth (STATE/, TASKS/, LOGS/, OUTPUT/).

Design constraints:
  - Read-only: no vault writes.
  - Bounded: max 3 notes retrieved, max 2KB formatted output.
  - Selective: only eligible task classes trigger retrieval.
  - Advisory: context is labeled and downstream systems may ignore it.
  - Deterministic: no LLM calls, keyword-based retrieval only.
  - Fail-open: vault unavailability does not block execution.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Eligibility rules
# ---------------------------------------------------------------------------

# Task classes that benefit from prior vault context
_ELIGIBLE_TASK_CLASSES = frozenset({
    "research",
    "code_impl",
    "code_review",
})

# Task classes that should NOT get vault context
# (trivial, runtime-focused, or unsupported)
_INELIGIBLE_TASK_CLASSES = frozenset({
    "simple",
    "unknown",
})

# Patterns indicating runtime-state queries (never inject vault context)
_RUNTIME_STATE_PATTERNS = re.compile(
    r"\b(service\s+status|systemctl|restart|pid|daemon|watcher|"
    r"running\s+tasks?|uptime|health\s*check|kill|sigterm)\b",
    re.IGNORECASE,
)

# Maximum notes to retrieve from vault
MAX_VAULT_NOTES = 3

# Maximum formatted context size (bytes)
MAX_CONTEXT_SIZE = 2048

# Stopwords to exclude from keyword extraction
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "can", "it", "its", "this", "that", "these", "those", "i", "we", "you",
    "they", "he", "she", "not", "no", "from", "as", "if", "then", "than",
    "so", "just", "about", "up", "out", "all", "also", "how", "what",
    "when", "where", "which", "who", "make", "use", "new", "get", "set",
})


# ---------------------------------------------------------------------------
# Eligibility check
# ---------------------------------------------------------------------------

def is_eligible_for_vault_context(
    task_class: str,
    task_text: str,
) -> tuple[bool, str]:
    """Determine whether a task should receive vault context injection.

    Returns (eligible, reason).
    Fails closed: unknown cases return ineligible.
    """
    # Runtime-state tasks never get vault context
    if _RUNTIME_STATE_PATTERNS.search(task_text):
        return False, "runtime_state_query"

    if task_class in _ELIGIBLE_TASK_CLASSES:
        return True, f"eligible_class:{task_class}"

    if task_class in _INELIGIBLE_TASK_CLASSES:
        return False, f"ineligible_class:{task_class}"

    # system tasks: eligible only if not runtime-state focused
    if task_class == "system":
        return False, "system_class_default_skip"

    # Unknown/unrecognized: fail closed
    return False, f"unknown_class:{task_class}"


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def extract_keywords(task_text: str, max_keywords: int = 5) -> list[str]:
    """Extract search keywords from task text.

    Returns a bounded list of non-stopword tokens, longest first
    (longer words tend to be more specific).
    """
    # Tokenize: split on non-alphanumeric, lowercase
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", task_text.lower())

    # Deduplicate preserving order, filter stopwords
    seen: set[str] = set()
    unique: list[str] = []
    for t in tokens:
        if t not in seen and t not in _STOPWORDS:
            seen.add(t)
            unique.append(t)

    # Sort by length descending (more specific words first)
    unique.sort(key=len, reverse=True)

    return unique[:max_keywords]


# ---------------------------------------------------------------------------
# Vault retrieval (uses MCP tools via import)
# ---------------------------------------------------------------------------

def retrieve_vault_context(
    task_class: str,
    keywords: list[str],
    max_notes: int = MAX_VAULT_NOTES,
) -> list[dict[str, Any]]:
    """Retrieve relevant notes from the Obsidian vault.

    Uses the vault MCP server's search and read functions directly.
    Returns a bounded list of note summaries.
    Fails open: returns empty list on any error.
    """
    if not keywords:
        return []

    max_notes = min(max_notes, MAX_VAULT_NOTES)

    try:
        from tools.mcp_vault_server import vault_search, vault_read
    except ImportError:
        logger.warning("vault MCP server not available — skipping context injection")
        return []

    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    # Search with up to 2 queries: combined keywords, then task_class-specific
    queries = [
        " ".join(keywords[:3]),
    ]
    if task_class in ("code_impl", "code_review"):
        queries.append(f"{task_class.replace('_', ' ')} pattern")
    elif task_class == "research":
        queries.append(f"research {keywords[0]}" if keywords else "research pattern")

    for query in queries[:2]:
        try:
            search_results = vault_search(query=query, max_results=max_notes)
        except Exception as exc:
            logger.debug("vault_search failed for %r: %s", query, exc)
            continue

        if not isinstance(search_results, list):
            continue

        for hit in search_results:
            path = hit.get("path", "")
            if path in seen_paths:
                continue
            seen_paths.add(path)

            # Extract summary from search result (avoid extra vault_read)
            results.append({
                "path": path,
                "title": hit.get("title", path),
                "snippet": hit.get("snippet", "")[:300],
                "score": hit.get("score", 0),
                "source": "obsidian_vault",
            })

            if len(results) >= max_notes:
                break

        if len(results) >= max_notes:
            break

    return results


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def format_vault_context(
    notes: list[dict[str, Any]],
    task_class: str,
) -> str:
    """Format retrieved vault notes into a compact, advisory context block.

    The output is clearly labeled as advisory and bounded in size.
    """
    if not notes:
        return ""

    lines = [
        "## Prior Knowledge (advisory — from Obsidian vault)",
        "",
        f"*{len(notes)} related note(s) found. "
        "This context is advisory only — runtime state in STATE/TASKS/LOGS/ "
        "is authoritative. Do not treat vault notes as current execution truth.*",
        "",
    ]

    for i, note in enumerate(notes, 1):
        title = note.get("title", "untitled")
        path = note.get("path", "?")
        snippet = note.get("snippet", "").strip()

        lines.append(f"### {i}. {title}")
        lines.append(f"- **Path**: `{path}`")
        if snippet:
            # Truncate long snippets
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            lines.append(f"- **Excerpt**: {snippet}")
        lines.append("")

    result = "\n".join(lines)

    # Hard cap
    if len(result) > MAX_CONTEXT_SIZE:
        result = result[:MAX_CONTEXT_SIZE - 20] + "\n\n...(truncated)\n"

    return result


# ---------------------------------------------------------------------------
# Main integration function
# ---------------------------------------------------------------------------

def inject_vault_context(
    task_class: str,
    task_text: str,
) -> dict[str, Any]:
    """Top-level function: decide, retrieve, and format vault context.

    Returns a dict suitable for merging into TaskIntent.context:
    {
        "vault_context_injected": bool,
        "vault_eligibility_reason": str,
        "vault_notes_found": int,
        "vault_queries": list[str],
        "vault_advisory_context": str,  # formatted markdown
        "vault_note_paths": list[str],  # for audit trail
    }

    Fails open: on any error, returns injected=False with reason.
    """
    eligible, reason = is_eligible_for_vault_context(task_class, task_text)

    if not eligible:
        return {
            "vault_context_injected": False,
            "vault_eligibility_reason": reason,
            "vault_notes_found": 0,
            "vault_queries": [],
            "vault_advisory_context": "",
            "vault_note_paths": [],
        }

    keywords = extract_keywords(task_text)
    if not keywords:
        return {
            "vault_context_injected": False,
            "vault_eligibility_reason": "no_keywords_extracted",
            "vault_notes_found": 0,
            "vault_queries": [],
            "vault_advisory_context": "",
            "vault_note_paths": [],
        }

    try:
        notes = retrieve_vault_context(task_class, keywords)
    except Exception as exc:
        logger.warning("Vault context retrieval failed: %s", exc)
        return {
            "vault_context_injected": False,
            "vault_eligibility_reason": f"retrieval_error:{exc}",
            "vault_notes_found": 0,
            "vault_queries": [" ".join(keywords[:3])],
            "vault_advisory_context": "",
            "vault_note_paths": [],
        }

    if not notes:
        return {
            "vault_context_injected": False,
            "vault_eligibility_reason": "no_relevant_notes",
            "vault_notes_found": 0,
            "vault_queries": [" ".join(keywords[:3])],
            "vault_advisory_context": "",
            "vault_note_paths": [],
        }

    formatted = format_vault_context(notes, task_class)
    note_paths = [n.get("path", "") for n in notes]

    logger.info(
        "Vault context injected: %d notes for task_class=%s, keywords=%s",
        len(notes), task_class, keywords[:3],
    )

    return {
        "vault_context_injected": True,
        "vault_eligibility_reason": reason,
        "vault_notes_found": len(notes),
        "vault_queries": [" ".join(keywords[:3])],
        "vault_advisory_context": formatted,
        "vault_note_paths": note_paths,
    }
