"""Phase 6.5 — Controlled agent-pattern promotion from repeated workflow learnings.

After a workflow learning is promoted (Phase 5.5), checks whether the vault
now contains enough converging learnings to warrant promoting a stable
reusable method into an agent-pattern note (Phase 6 skill doctrine).

Design constraints:
  - Runs only after successful workflow-learning promotion.
  - Requires 2+ independent workflow learnings sharing a common method.
  - Searches only 30-workflow-learnings/ (scoped, bounded).
  - Dedup checks 20-agent-patterns/ before writing (no duplicates).
  - Routes through Memory Router → VaultAdapter (Phase 2).
  - Produces one compact agent-pattern note per promotion.
  - Fail-open: pattern promotion failure does not block execution.
  - All attempts are logged for auditability.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern-promotion eligibility (stricter than workflow-learning promotion)
# ---------------------------------------------------------------------------

# Minimum independent workflow learnings needed to propose a pattern
_MIN_CONVERGING_LEARNINGS = 2

# Minimum keyword overlap ratio between a learning snippet and the current task
_MIN_KEYWORD_OVERLAP = 0.4

# Maximum vault_search queries per promotion attempt (bounded)
_MAX_SEARCH_QUERIES = 2

# Maximum agent-pattern notes we check for dedup
_MAX_DEDUP_RESULTS = 5

# Task classes that can produce patterns
_PATTERN_ELIGIBLE_CLASSES = frozenset({"research", "code_impl", "code_review"})

# Stopwords for keyword extraction (same approach as vault_context.py)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "no",
        "so",
        "if",
        "then",
        "than",
        "that",
        "this",
        "it",
        "its",
        "my",
        "your",
        "our",
        "their",
        "he",
        "she",
        "we",
        "they",
        "i",
        "me",
        "him",
        "her",
        "us",
        "them",
        "what",
        "which",
        "who",
        "task",
        "step",
        "execute",
        "output",
        "input",
        "file",
        "run",
    }
)

# Valid agent roles (from vault schema)
_VALID_AGENT_ROLES = frozenset(
    {
        "research",
        "coder",
        "critic",
        "verifier",
        "planner",
        "memory",
    }
)

# Map task_class to default agent_role for the pattern
_TASK_CLASS_TO_ROLE: dict[str, str] = {
    "research": "research",
    "code_impl": "coder",
    "code_review": "critic",
}


def _extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    """Extract keywords from text, filtering stopwords, sorted by length desc."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        if w not in _STOPWORDS and w not in seen:
            seen.add(w)
            keywords.append(w)
    # Longer words tend to be more specific
    keywords.sort(key=len, reverse=True)
    return keywords[:max_keywords]


def _keyword_overlap(keywords_a: list[str], text_b: str) -> float:
    """Fraction of keywords_a that appear in text_b."""
    if not keywords_a:
        return 0.0
    text_lower = text_b.lower()
    hits = sum(1 for kw in keywords_a if kw in text_lower)
    return hits / len(keywords_a)


# ---------------------------------------------------------------------------
# Candidate assessment
# ---------------------------------------------------------------------------


def assess_pattern_candidate(
    task_class: str,
    task_text: str,
    learning_id: str,
    learning_path: str,
) -> dict[str, Any]:
    """Assess whether a newly promoted learning triggers pattern promotion.

    Searches the vault for related workflow learnings. If 2+ independent
    learnings converge on the same method, assembles a pattern candidate.

    Returns:
    {
        "candidate": bool,
        "reason": str,
        "evidence_count": int,
        "evidence_paths": list[str],
        "pattern_payload": dict | None,  # ready for vault_validate + vault_write
        "search_queries": list[str],
    }
    """
    result: dict[str, Any] = {
        "candidate": False,
        "reason": "",
        "evidence_count": 0,
        "evidence_paths": [],
        "pattern_payload": None,
        "search_queries": [],
    }

    # 1. Task class must be eligible
    if task_class not in _PATTERN_ELIGIBLE_CLASSES:
        result["reason"] = f"ineligible_class:{task_class}"
        return result

    # 2. Extract keywords from current task for similarity search
    keywords = _extract_keywords(task_text)
    if len(keywords) < 2:
        result["reason"] = "insufficient_keywords"
        return result

    # 3. Search vault for related workflow learnings
    try:
        from tools.mcp_vault_server import vault_search
    except ImportError:
        result["reason"] = "vault_unavailable"
        return result

    # Build a search query from top keywords
    search_query = " ".join(keywords[:4])
    result["search_queries"].append(search_query)

    try:
        search_result = vault_search(
            query=search_query,
            folder="30-workflow-learnings",
        )
    except Exception as exc:
        logger.warning("PATTERN CANDIDATE SEARCH FAILED: %s", exc)
        result["reason"] = f"search_error:{exc}"
        return result

    if "error" in search_result:
        result["reason"] = f"search_error:{search_result['error']}"
        return result

    hits = search_result.get("results", [])

    # 4. Filter for learnings with sufficient keyword overlap
    converging: list[dict[str, Any]] = []
    for hit in hits:
        hit_path = hit.get("path", "")
        snippet = hit.get("snippet", "")

        # Skip the learning we just promoted (no self-reference)
        if hit_path == learning_path:
            continue

        # Check keyword overlap
        overlap = _keyword_overlap(keywords, snippet + " " + hit_path)
        if overlap >= _MIN_KEYWORD_OVERLAP:
            converging.append(
                {
                    "path": hit_path,
                    "snippet": snippet,
                    "overlap": overlap,
                }
            )

    result["evidence_count"] = len(converging) + 1  # +1 for current learning
    result["evidence_paths"] = [learning_path] + [c["path"] for c in converging]

    # 5. Need at least _MIN_CONVERGING_LEARNINGS total (current + related)
    if len(converging) < _MIN_CONVERGING_LEARNINGS - 1:
        result["reason"] = f"insufficient_evidence:{result['evidence_count']}_need:{_MIN_CONVERGING_LEARNINGS}"
        return result

    # 6. Dedup: check for existing agent patterns covering this topic
    dedup_query = " ".join(keywords[:3])
    result["search_queries"].append(dedup_query)

    try:
        dedup_result = vault_search(
            query=dedup_query,
            folder="20-agent-patterns",
        )
    except Exception as exc:
        logger.warning("PATTERN DEDUP SEARCH FAILED: %s", exc)
        result["reason"] = f"dedup_error:{exc}"
        return result

    dedup_hits = dedup_result.get("results", [])
    if dedup_hits:
        # Check if any existing pattern has high keyword overlap
        for dhit in dedup_hits[:_MAX_DEDUP_RESULTS]:
            d_snippet = dhit.get("snippet", "") + " " + dhit.get("path", "")
            if _keyword_overlap(keywords[:4], d_snippet) >= 0.5:
                result["reason"] = f"duplicate_pattern:{dhit.get('path', '?')}"
                return result

    # 7. Assemble pattern payload
    evidence_snippets = [c["snippet"] for c in converging]
    payload = _build_pattern_payload(
        task_class=task_class,
        task_text=task_text,
        keywords=keywords,
        evidence_paths=result["evidence_paths"],
        evidence_snippets=evidence_snippets,
    )

    result["candidate"] = True
    result["reason"] = "converging_evidence"
    result["pattern_payload"] = payload
    return result


# ---------------------------------------------------------------------------
# Pattern payload assembly
# ---------------------------------------------------------------------------


def _build_pattern_payload(
    task_class: str,
    task_text: str,
    keywords: list[str],
    evidence_paths: list[str],
    evidence_snippets: list[str],
) -> dict[str, Any]:
    """Build a structured agent-pattern note payload for vault_validate + vault_write.

    Returns {"frontmatter": dict, "body": str, "path": str}.
    """
    date_str = time.strftime("%Y-%m-%d", time.gmtime())
    slug = "-".join(keywords[:4])
    slug = re.sub(r"[^a-z0-9-]", "", slug)[:60].strip("-")
    pattern_id = f"ap-{slug}"

    agent_role = _TASK_CLASS_TO_ROLE.get(task_class, "coder")

    # Title: derive from top keywords
    title_words = [kw.replace("-", " ").title() for kw in keywords[:4]]
    title = f"Auto-promoted: {' '.join(title_words)}"
    if len(title) > 80:
        title = title[:77] + "..."

    frontmatter = {
        "type": "agent-pattern",
        "pattern_id": pattern_id,
        "title": title,
        "agent_role": agent_role,
        "confidence": "medium",  # auto-promoted always starts at medium
        "task_classes": [task_class],
        "date_created": date_str,
        "source": "nova-core-memory",
        "tags": [
            "#type/pattern",
            f"#agent/{agent_role}",
            "#confidence/medium",
            "#status/active",
            "#source/auto-promoted",
        ],
    }

    # Build body from evidence
    body_parts = []

    body_parts.append("## Summary\n")
    body_parts.append(
        f"Automatically promoted pattern based on {len(evidence_paths)} "
        f"converging workflow learnings. Review and refine manually.\n"
    )

    body_parts.append("\n## When to Apply\n")
    body_parts.append(f"- Task class: `{task_class}`\n")
    body_parts.append(f"- Keywords: {', '.join(keywords[:5])}\n")

    body_parts.append("\n## Method Outline\n")
    # Extract actionable content from task text
    first_line = task_text.strip().split("\n")[0].strip()
    first_line = re.sub(r"^#+\s*", "", first_line)
    body_parts.append(f"- {_truncate(first_line, 200)}\n")
    body_parts.append("- *(Review source learnings for detailed steps)*\n")

    if evidence_snippets:
        body_parts.append("\n## Converging Evidence\n")
        for i, snippet in enumerate(evidence_snippets[:3], 1):
            body_parts.append(f"\n### Evidence {i}\n")
            body_parts.append(f"{_truncate(snippet, 300)}\n")

    body_parts.append("\n## Source Learnings\n")
    for ep in evidence_paths:
        body_parts.append(f"- `{ep}`\n")

    body_parts.append("\n## Guidance\n")
    body_parts.append(
        "This pattern was auto-promoted by Phase 6.5. "
        "Confidence is medium until manually reviewed and confirmed. "
        "Promote to high confidence after independent verification.\n"
    )

    body_parts.append("\n## Trace\n")
    body_parts.append("- **Promotion**: auto-promoted by Phase 6.5\n")
    body_parts.append(f"- **Date**: {date_str}\n")
    body_parts.append(f"- **Evidence count**: {len(evidence_paths)} workflow learnings\n")

    body = "\n".join(body_parts)
    path = f"20-agent-patterns/{date_str}-{slug}.md"

    return {
        "frontmatter": frontmatter,
        "body": body,
        "path": path,
    }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Pattern promotion execution
# ---------------------------------------------------------------------------


def attempt_pattern_promotion(
    task_class: str,
    task_text: str,
    learning_id: str,
    learning_path: str,
) -> dict[str, Any]:
    """Attempt to promote converging workflow learnings into an agent pattern.

    Called after a workflow learning is successfully promoted.
    Searches for related learnings, assesses convergence, and writes
    a pattern note if eligible.

    Returns:
    {
        "promoted": bool,
        "reason": str,
        "pattern_id": str | None,
        "note_path": str | None,
        "evidence_count": int,
        "evidence_paths": list[str],
        "search_queries": list[str],
        "errors": list[str],
    }
    """
    base_result: dict[str, Any] = {
        "promoted": False,
        "reason": "",
        "pattern_id": None,
        "note_path": None,
        "evidence_count": 0,
        "evidence_paths": [],
        "search_queries": [],
        "errors": [],
    }

    # 1. Assess candidate
    try:
        assessment = assess_pattern_candidate(
            task_class=task_class,
            task_text=task_text,
            learning_id=learning_id,
            learning_path=learning_path,
        )
    except Exception as exc:
        logger.warning("PATTERN ASSESSMENT FAILED: %s", exc)
        base_result["reason"] = f"assessment_error:{exc}"
        base_result["errors"].append(str(exc))
        return base_result

    base_result["evidence_count"] = assessment.get("evidence_count", 0)
    base_result["evidence_paths"] = assessment.get("evidence_paths", [])
    base_result["search_queries"] = assessment.get("search_queries", [])

    if not assessment.get("candidate", False):
        base_result["reason"] = assessment.get("reason", "not_candidate")
        logger.debug(
            "PATTERN PROMOTION DEFERRED: %s — %s",
            learning_id,
            base_result["reason"],
        )
        return base_result

    payload = assessment.get("pattern_payload")
    if not payload:
        base_result["reason"] = "no_payload"
        return base_result

    # 2. Route through unified Memory Router (validate + write)
    try:
        from agents.memory_router import router
    except ImportError:
        base_result["reason"] = "router_unavailable"
        base_result["errors"].append("memory router not importable")
        return base_result

    pattern_id = payload["frontmatter"].get("pattern_id", "")
    try:
        obj = router.ingest_event(
            {
                "source": "promoter",
                "event_type": "agent_pattern_promoted",
                "title": payload["frontmatter"].get("title", f"Pattern from {learning_id}")[:100],
                "summary": f"Agent pattern promoted from learning {learning_id}",
                "content": payload["body"],
                "current_layer": "procedural",
                "target_store": "obsidian_vault",
                "confidence": payload["frontmatter"].get("confidence", "medium"),
                "tags": payload["frontmatter"].get("tags", []),
                "related_task_ids": [learning_id],
                "provenance": "automatic",
                "storage_intent": "pattern_promotion",
                "promotion_status": "promoted",
                "adapter_metadata": {
                    "vault_frontmatter": payload["frontmatter"],
                    "vault_body": payload["body"],
                    "vault_path": payload["path"],
                },
            },
            caller="pattern_promoter",
        )
        store_result = router.store(obj, caller="pattern_promoter")
    except Exception as exc:
        logger.warning("PATTERN ROUTER FAILED: %s — %s", learning_id, exc)
        base_result["reason"] = f"router_error:{exc}"
        base_result["note_path"] = payload["path"]
        base_result["errors"].append(str(exc))
        return base_result

    if not store_result.stored:
        reason = store_result.rejection_reason or "unknown"
        errors = store_result.validation_errors or [reason]
        logger.warning("PATTERN REJECTED: %s — %s", learning_id, reason)
        base_result["reason"] = reason
        base_result["note_path"] = payload["path"]
        base_result["errors"] = errors
        return base_result

    # Success
    logger.info(
        "PATTERN PROMOTION SUCCESS: %s → %s (%s, %d evidence, via router)",
        learning_id,
        store_result.path_or_id or payload["path"],
        pattern_id,
        assessment.get("evidence_count", 0),
    )

    base_result["promoted"] = True
    base_result["reason"] = "converging_evidence"
    base_result["pattern_id"] = pattern_id
    base_result["note_path"] = store_result.path_or_id or payload["path"]
    return base_result
