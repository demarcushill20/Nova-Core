"""Phase 7 — Bounded agent-pattern retrieval for planner/orchestrator flows.

Searches the 20-agent-patterns/ folder for relevant reusable methods,
reads matched patterns, extracts structured guidance sections, and produces
a compact planner-friendly summary for injection into planning context.

Design constraints:
  - Read-only: no vault writes.
  - Scoped: searches only 20-agent-patterns/ (not whole vault).
  - Bounded: max 2 patterns retrieved, max 1.5KB formatted output.
  - Advisory: context is labeled and downstream systems may ignore it.
  - Deterministic: no LLM calls, keyword-based retrieval only.
  - Fail-open: vault unavailability does not block planning.
  - Complements Phase 5's general vault context injection.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Task classes eligible for agent-pattern retrieval
_ELIGIBLE_TASK_CLASSES = frozenset({"research", "code_impl", "code_review"})

# Task classes explicitly ineligible
_INELIGIBLE_TASK_CLASSES = frozenset({"simple", "unknown", "system"})

# Runtime-state patterns (never retrieve patterns for these)
_RUNTIME_STATE_PATTERNS = re.compile(
    r"\b(service\s+status|systemctl|restart|pid|daemon|watcher|"
    r"running\s+tasks?|uptime|health\s*check|kill|sigterm)\b",
    re.IGNORECASE,
)

# Target folder for pattern search (scoped)
_PATTERN_FOLDER = "20-agent-patterns"

# Maximum patterns to retrieve and inject
MAX_PATTERNS = 2

# Maximum formatted context size (bytes)
MAX_PATTERN_CONTEXT_SIZE = 1536

# Maximum content to read from a single pattern note (bytes)
_MAX_NOTE_READ = 4096

# Stopwords for keyword extraction
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "can", "it", "its", "this", "that", "these", "those", "i", "we", "you",
    "they", "he", "she", "not", "no", "from", "as", "if", "then", "than",
    "so", "just", "about", "up", "out", "all", "also", "how", "what",
    "when", "where", "which", "who", "make", "use", "new", "get", "set",
    "task", "step", "execute", "output", "input", "file", "run",
})

# Guidance sections to extract from pattern body (in priority order)
_GUIDANCE_SECTIONS = [
    "Summary",
    "When to Apply",
    "Pattern Steps",
    "Guidance",
    "Failure Modes",
]


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def is_eligible_for_pattern_retrieval(
    task_class: str,
    task_text: str,
) -> tuple[bool, str]:
    """Determine whether a task should receive agent-pattern guidance.

    Returns (eligible, reason). Fails closed on uncertainty.
    """
    if _RUNTIME_STATE_PATTERNS.search(task_text):
        return False, "runtime_state_query"

    if task_class in _ELIGIBLE_TASK_CLASSES:
        return True, f"eligible_class:{task_class}"

    if task_class in _INELIGIBLE_TASK_CLASSES:
        return False, f"ineligible_class:{task_class}"

    return False, f"unknown_class:{task_class}"


# ---------------------------------------------------------------------------
# Keyword extraction (reuses same logic as vault_context.py)
# ---------------------------------------------------------------------------

def extract_pattern_keywords(task_text: str, max_keywords: int = 5) -> list[str]:
    """Extract search keywords from task text for pattern matching."""
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", task_text.lower())
    seen: set[str] = set()
    unique: list[str] = []
    for t in tokens:
        if t not in seen and t not in _STOPWORDS:
            seen.add(t)
            unique.append(t)
    unique.sort(key=len, reverse=True)
    return unique[:max_keywords]


# ---------------------------------------------------------------------------
# Pattern search and read
# ---------------------------------------------------------------------------

def search_agent_patterns(
    keywords: list[str],
    task_class: str,
    max_results: int = MAX_PATTERNS,
) -> list[dict[str, Any]]:
    """Search 20-agent-patterns/ for relevant patterns.

    Returns a bounded list of search hits with path and snippet.
    Fails open: returns empty list on any error.
    """
    if not keywords:
        return []

    try:
        from tools.mcp_vault_server import vault_search
    except ImportError:
        logger.warning("vault MCP server not available — skipping pattern retrieval")
        return []

    # Build search query from top keywords
    query = " ".join(keywords[:4])

    try:
        result = vault_search(query=query, folder=_PATTERN_FOLDER)
    except Exception as exc:
        logger.debug("vault_search failed for patterns: %s", exc)
        return []

    if "error" in result:
        logger.debug("vault_search error: %s", result["error"])
        return []

    hits = result.get("results", [])
    if not hits:
        return []

    # Deduplicate and cap
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for hit in hits:
        path = hit.get("path", "")
        if path in seen or not path:
            continue
        seen.add(path)
        selected.append({
            "path": path,
            "snippet": hit.get("snippet", ""),
        })
        if len(selected) >= max_results:
            break

    return selected


def read_pattern_guidance(path: str) -> dict[str, Any] | None:
    """Read a pattern note and extract structured guidance sections.

    Returns a dict with title and extracted sections, or None on failure.
    """
    try:
        from tools.mcp_vault_server import vault_read
    except ImportError:
        return None

    try:
        result = vault_read(path=path)
    except Exception as exc:
        logger.debug("vault_read failed for %s: %s", path, exc)
        return None

    if "error" in result:
        return None

    content = result.get("content", "")
    if not content:
        return None

    # Truncate overly large notes
    if len(content) > _MAX_NOTE_READ:
        content = content[:_MAX_NOTE_READ]

    # Extract title from frontmatter or first heading
    title = _extract_title(content)

    # Extract guidance sections
    sections = _extract_sections(content)

    if not sections:
        return None

    return {
        "path": path,
        "title": title,
        "sections": sections,
    }


def _extract_title(content: str) -> str:
    """Extract pattern title from frontmatter or first heading."""
    # Try frontmatter title
    m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    if m:
        return m.group(1).strip()

    # Try first heading
    m = re.search(r'^#+\s+(.+)$', content, re.MULTILINE)
    if m:
        return m.group(1).strip()

    return "Untitled pattern"


def _extract_sections(content: str) -> dict[str, str]:
    """Extract key guidance sections from a pattern note body.

    Returns a dict mapping section name to compact content.
    Only extracts sections listed in _GUIDANCE_SECTIONS.
    """
    extracted: dict[str, str] = {}

    for section_name in _GUIDANCE_SECTIONS:
        # Match ## Section Name (case-insensitive)
        pattern = re.compile(
            rf"^##\s+{re.escape(section_name)}\b.*$",
            re.MULTILINE | re.IGNORECASE,
        )
        match = pattern.search(content)
        if not match:
            continue

        # Extract content until next ## heading or end
        start = match.end()
        next_heading = re.search(r"^##\s+", content[start:], re.MULTILINE)
        if next_heading:
            section_content = content[start:start + next_heading.start()]
        else:
            section_content = content[start:]

        # Clean and truncate
        section_content = section_content.strip()
        if section_content:
            extracted[section_name] = _truncate(section_content, 300)

    return extracted


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def format_pattern_guidance(
    patterns: list[dict[str, Any]],
) -> str:
    """Format retrieved pattern guidance into a compact, advisory block.

    Each pattern includes title, key sections, and advisory labeling.
    """
    if not patterns:
        return ""

    lines = [
        "## Agent Pattern Guidance (advisory — from Obsidian vault)",
        "",
        f"*{len(patterns)} relevant agent pattern(s) found. "
        "These are reusable methods — not runtime facts. "
        "Use as guidance for planning approach. "
        "Runtime state in STATE/TASKS/LOGS/ is authoritative.*",
        "",
    ]

    for i, pat in enumerate(patterns, 1):
        title = pat.get("title", "Untitled")
        path = pat.get("path", "?")
        sections = pat.get("sections", {})

        lines.append(f"### Pattern {i}: {title}")
        lines.append(f"*Source: `{path}`*")
        lines.append("")

        for section_name in _GUIDANCE_SECTIONS:
            if section_name in sections:
                lines.append(f"**{section_name}:**")
                lines.append(sections[section_name])
                lines.append("")

        lines.append("---")
        lines.append("")

    result = "\n".join(lines)

    # Hard cap
    if len(result) > MAX_PATTERN_CONTEXT_SIZE:
        result = result[:MAX_PATTERN_CONTEXT_SIZE - 20] + "\n\n...(truncated)\n"

    return result


# ---------------------------------------------------------------------------
# Main integration function
# ---------------------------------------------------------------------------

def retrieve_pattern_guidance(
    task_class: str,
    task_text: str,
) -> dict[str, Any]:
    """Top-level function: decide, search, read, extract, and format pattern guidance.

    Returns a structured result dict:
    {
        "pattern_retrieval_activated": bool,
        "retrieval_reason": str,
        "search_queries": list[str],
        "matched_pattern_paths": list[str],
        "selected_patterns": list[dict],  # title + path for audit
        "pattern_guidance_context": str,   # formatted markdown
        "errors": list[str],
    }

    Fails open: on any error, returns activated=False.
    """
    result: dict[str, Any] = {
        "pattern_retrieval_activated": False,
        "retrieval_reason": "",
        "search_queries": [],
        "matched_pattern_paths": [],
        "selected_patterns": [],
        "pattern_guidance_context": "",
        "errors": [],
    }

    # 1. Check eligibility
    eligible, reason = is_eligible_for_pattern_retrieval(task_class, task_text)
    if not eligible:
        result["retrieval_reason"] = reason
        return result

    # 2. Extract keywords
    keywords = extract_pattern_keywords(task_text)
    if not keywords:
        result["retrieval_reason"] = "no_keywords_extracted"
        return result

    search_query = " ".join(keywords[:4])
    result["search_queries"].append(search_query)

    # 3. Search for patterns
    try:
        hits = search_agent_patterns(keywords, task_class)
    except Exception as exc:
        result["retrieval_reason"] = f"search_error:{exc}"
        result["errors"].append(str(exc))
        return result

    if not hits:
        result["retrieval_reason"] = "no_patterns_found"
        return result

    result["matched_pattern_paths"] = [h["path"] for h in hits]

    # 4. Read and extract guidance from matched patterns
    patterns_with_guidance: list[dict[str, Any]] = []
    for hit in hits:
        guidance = read_pattern_guidance(hit["path"])
        if guidance and guidance.get("sections"):
            patterns_with_guidance.append(guidance)
            result["selected_patterns"].append({
                "title": guidance["title"],
                "path": guidance["path"],
            })

    if not patterns_with_guidance:
        result["retrieval_reason"] = "no_extractable_guidance"
        return result

    # 5. Format guidance
    formatted = format_pattern_guidance(patterns_with_guidance)

    result["pattern_retrieval_activated"] = True
    result["retrieval_reason"] = reason
    result["pattern_guidance_context"] = formatted

    logger.info(
        "PATTERN GUIDANCE INJECTED: %d pattern(s) for task_class=%s, keywords=%s",
        len(patterns_with_guidance), task_class, keywords[:3],
    )

    return result
