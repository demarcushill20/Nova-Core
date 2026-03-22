"""Decision-to-ADR Bridge — Dual Memory Integration Phase 5.

Queries Fusion Memory for architectural decisions, filters for significance,
deduplicates against existing Obsidian ADRs, and composes ADR candidate notes
for operator review in the Obsidian 00-inbox/ folder.

This module is designed to be called by the memory-surface-adr-candidates skill
or run standalone for E2E validation.

Usage (standalone):
    python utils/adr_bridge.py --dry-run   # preview without writing
    python utils/adr_bridge.py             # surface candidates to vault
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class FusionDecision:
    """A decision item retrieved from Fusion Memory."""

    memory_id: str
    text: str
    project: str = "nova-core"
    session_id: str = ""
    event_seq: int = 0
    event_time: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "decision"
    score: float = 0.0

    @classmethod
    def from_fusion_result(cls, item: dict[str, Any]) -> FusionDecision:
        meta = item.get("metadata", {})
        return cls(
            memory_id=item.get("id", ""),
            text=item.get("text", meta.get("text", "")),
            project=meta.get("project", "nova-core"),
            session_id=meta.get("session_id", ""),
            event_seq=meta.get("event_seq", 0),
            event_time=meta.get("event_time", ""),
            tags=meta.get("tags", []),
            category=meta.get("category", "decision"),
            score=item.get("composite_score", item.get("score", 0.0)),
        )


@dataclass
class ADRCandidate:
    """A candidate ADR note ready to be written to Obsidian 00-inbox/."""

    title: str
    slug: str
    proposed_decision: str
    context: str
    alternatives: str
    consequences: str
    source_evidence: str
    project: str = "nova-core"
    source_decisions: list[FusionDecision] = field(default_factory=list)

    @property
    def vault_path(self) -> str:
        return f"00-inbox/adr-candidate-{self.slug}.md"

    def frontmatter(self) -> dict[str, Any]:
        return {
            "type": "inbox",
            "title": f"ADR Candidate: {self.title}",
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": "nova-core-memory",
            "tags": [
                "#type/inbox",
                "#action/review",
                "#action/promote-to-adr",
                f"#project/{self.project}",
            ],
        }

    def body(self) -> str:
        return f"""## Proposed Decision

{self.proposed_decision}

## Context

{self.context}

## Alternatives Considered

{self.alternatives}

## Consequences

{self.consequences}

## Source Evidence

{self.source_evidence}

## Action Needed

Operator: Review this candidate. If it represents a durable architectural
decision, promote to `10-adrs/` with a proper ADR number. If it's too
tactical or already covered, dismiss by removing from inbox."""


# ---------------------------------------------------------------------------
# Significance filter
# ---------------------------------------------------------------------------

# Keywords that indicate architectural scope
ARCHITECTURAL_KEYWORDS = [
    "architecture",
    "chose",
    "selected",
    "adopted",
    "decided",
    "switched",
    "replaced",
    "migration",
    "backend",
    "frontend",
    "model",
    "framework",
    "database",
    "protocol",
    "transport",
    "integration",
    "pipeline",
    "schema",
    "api",
    "interface",
    "orchestrat",
    "hierarchical",
    "blackboard",
    "multi-agent",
    "pinecone",
    "neo4j",
    "redis",
    "vector",
    "graph",
    "embedding",
    "mcp",
    "sdk",
    "cli",
    "systemd",
    "docker",
    "sandbox",
    "security",
    "hardening",
    "threat model",
    "kill switch",
    "circuit breaker",
    "policy",
    "routing",
]

# Keywords that indicate trivial/tactical decisions
TRIVIAL_KEYWORDS = [
    "variable name",
    "print()",
    "debugging",
    "logging level",
    "indent",
    "formatting",
    "typo",
    "comment",
    "docstring",
    "test flag",
    "-v flag",
    "temp file",
    "scratch",
]

# Minimum text length for a decision to be considered significant
MIN_DECISION_LENGTH = 80


def is_significant(decision: FusionDecision) -> bool:
    """Determine if a decision is architecturally significant enough for an ADR.

    Returns True if the decision affects system structure, tool selection,
    data model, or integration approach.
    """
    text_lower = decision.text.lower()

    # Too short to be a real architectural decision
    if len(decision.text) < MIN_DECISION_LENGTH:
        return False

    # Check for trivial indicators
    for keyword in TRIVIAL_KEYWORDS:
        if keyword in text_lower:
            return False

    # Check for architectural indicators
    arch_hits = sum(1 for kw in ARCHITECTURAL_KEYWORDS if kw in text_lower)
    if arch_hits >= 2:
        return True

    # Check tags for architectural signals
    arch_tags = {"architecture", "design", "decision", "infrastructure", "security", "hardening", "integration"}
    tag_overlap = arch_tags.intersection(set(decision.tags))
    if tag_overlap and arch_hits >= 1:
        return True

    # If category is explicitly "decision" and it's reasonably long,
    # give it the benefit of the doubt
    return decision.category == "decision" and len(decision.text) > 200 and arch_hits >= 1


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _topic_key(decision: FusionDecision) -> str:
    """Extract a rough topic key for grouping related decisions."""
    text_lower = decision.text.lower()

    # Project-specific groupings
    if "ceo nova" in text_lower or "telegram bot" in text_lower:
        return "ceo-nova-architecture"
    if "fusion memory" in text_lower or ("pinecone" in text_lower and "neo4j" in text_lower):
        return "fusion-memory-architecture"
    if "embedding" in text_lower and ("model" in text_lower or "ada" in text_lower or "3-small" in text_lower):
        return "embedding-model-selection"
    if "security" in text_lower and ("hardening" in text_lower or "lethal" in text_lower or "threat" in text_lower):
        return "security-architecture"
    if "multi-agent" in text_lower or "orchestrat" in text_lower:
        return "multi-agent-architecture"
    if "novatrade" in text_lower or "metatrader" in text_lower or "mt5" in text_lower:
        return "novatrade-architecture"

    # Fall back to project + first significant tag
    if decision.tags:
        return f"{decision.project}-{decision.tags[0]}"
    return f"{decision.project}-{decision.memory_id[:8]}"


def group_decisions(decisions: list[FusionDecision]) -> dict[str, list[FusionDecision]]:
    """Group related decisions by topic so they can be merged into single ADR candidates."""
    groups: dict[str, list[FusionDecision]] = {}
    for d in decisions:
        key = _topic_key(d)
        groups.setdefault(key, []).append(d)
    return groups


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


@dataclass
class DedupResult:
    """Result of dedup check against the vault."""

    is_duplicate: bool
    reason: str = ""
    existing_path: str = ""


def check_dedup_static(
    topic: str,
    adr_search_results: list[dict[str, Any]],
    inbox_search_results: list[dict[str, Any]],
) -> DedupResult:
    """Check if a topic is already covered by an existing ADR or inbox candidate.

    This is a pure function that takes pre-fetched search results.
    The caller is responsible for querying the vault.

    Args:
        topic: The decision topic to check.
        adr_search_results: Results from vault_search in 10-adrs/.
        inbox_search_results: Results from vault_search in 00-inbox/.

    Returns:
        DedupResult indicating whether the topic is a duplicate.
    """
    # Check existing ADRs
    for result in adr_search_results:
        path = result.get("path", "")
        if path.startswith("10-adrs/"):
            return DedupResult(
                is_duplicate=True,
                reason="Existing ADR covers this topic",
                existing_path=path,
            )

    # Check pending inbox candidates
    for result in inbox_search_results:
        path = result.get("path", "")
        name = result.get("name", path)
        if "adr-candidate" in path.lower() or "adr-candidate" in name.lower():
            return DedupResult(
                is_duplicate=True,
                reason="Pending ADR candidate already in inbox",
                existing_path=path,
            )

    return DedupResult(is_duplicate=False)


# ---------------------------------------------------------------------------
# Candidate composition
# ---------------------------------------------------------------------------


def _slugify(text: str, max_len: int = 50) -> str:
    """Create a URL-safe slug from text."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug).strip("-")
    return slug[:max_len].rstrip("-")


def _extract_title(topic_key: str, decisions: list[FusionDecision]) -> str:
    """Extract a human-readable title from a topic group."""
    title_map = {
        "ceo-nova-architecture": "CEO Nova Architecture (Claude CLI + Routing + Memory)",
        "fusion-memory-architecture": "Fusion Memory Three-Backend Architecture (Pinecone + Neo4j + Redis)",
        "embedding-model-selection": "Embedding Model Selection (text-embedding-3-small)",
        "security-architecture": "Security Hardening Architecture (Lethal Trifecta Elimination)",
        "multi-agent-architecture": "Hierarchical Multi-Agent Architecture",
        "novatrade-architecture": "NovaTrade Trading Architecture",
    }
    if topic_key in title_map:
        return title_map[topic_key]

    # Derive from the first decision's text (first sentence or first 80 chars)
    first_text = decisions[0].text
    first_sentence = first_text.split(".")[0]
    if len(first_sentence) > 80:
        first_sentence = first_sentence[:77] + "..."
    return first_sentence


def _build_source_evidence(decisions: list[FusionDecision]) -> str:
    """Build the Source Evidence section from a list of decisions."""
    lines = []
    for d in decisions:
        date_str = d.event_time[:10] if d.event_time else "unknown"
        lines.append(
            f"- Fusion Memory ID: `{d.memory_id}` "
            f"(event_seq: {d.event_seq}, session: `{d.session_id}`, "
            f"date: {date_str})"
        )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines.append(f"- Surfaced from Fusion Memory on {today}")
    return "\n".join(lines)


def _build_alternatives(decisions: list[FusionDecision]) -> str:
    """Extract alternatives from decision text if mentioned."""
    combined_text = " ".join(d.text for d in decisions).lower()

    # Look for "over", "instead of", "rejected", "not chosen" patterns
    alt_patterns = [
        r"(?:over|instead of|rather than|rejected|not chosen|vs\.?)\s+(\w[\w\s-]{2,30})",
    ]
    alternatives = []
    for pattern in alt_patterns:
        matches = re.findall(pattern, combined_text)
        for m in matches:
            clean = m.strip().rstrip(".,;:")
            if clean and len(clean) > 3:
                alternatives.append(clean)

    if not alternatives:
        return "Not recorded in the original decision context."

    seen = set()
    lines = []
    for alt in alternatives[:5]:
        alt_lower = alt.lower()
        if alt_lower not in seen:
            seen.add(alt_lower)
            lines.append(f"- **{alt.title()}** — mentioned as alternative in decision context")
    return "\n".join(lines)


def _build_consequences(decisions: list[FusionDecision]) -> str:
    """Infer consequences from decision text."""
    combined = " ".join(d.text for d in decisions)

    positives = []
    negatives = []

    # Look for explicit positive/negative signals
    if "cheaper" in combined.lower() or "lower cost" in combined.lower() or "5x" in combined.lower():
        positives.append("Cost reduction compared to alternatives")
    if "better" in combined.lower() or "improved" in combined.lower():
        positives.append("Improved quality or performance")
    if "native" in combined.lower() or "natively" in combined.lower():
        positives.append("Native integration reduces complexity")
    if "consistent" in combined.lower() or "same" in combined.lower():
        positives.append("Consistency with existing system components")

    if "overhead" in combined.lower() or "startup" in combined.lower():
        negatives.append("Additional overhead or startup cost")
    if "complexity" in combined.lower():
        negatives.append("Increased system complexity")
    if "trade-off" in combined.lower() or "tradeoff" in combined.lower():
        negatives.append("Trade-offs acknowledged in original decision")

    lines = []
    for p in positives[:3]:
        lines.append(f"- (+) {p}")
    for n in negatives[:3]:
        lines.append(f"- (-) {n}")

    if not lines:
        lines.append("- Consequences not explicitly recorded; operator should assess")

    return "\n".join(lines)


def compose_candidate(
    topic_key: str,
    decisions: list[FusionDecision],
) -> ADRCandidate:
    """Compose an ADR candidate note from a group of related decisions."""
    title = _extract_title(topic_key, decisions)
    slug = _slugify(topic_key)
    project = decisions[0].project if decisions else "nova-core"

    # Build proposed decision from all grouped decisions
    proposed_lines = []
    for d in decisions:
        # Take the first 2-3 sentences as the core decision
        sentences = d.text.split(". ")
        summary = ". ".join(sentences[:3])
        if not summary.endswith("."):
            summary += "."
        proposed_lines.append(summary)

    proposed_decision = "\n\n".join(proposed_lines)

    # Build context
    sessions = {d.session_id for d in decisions if d.session_id}
    context = (
        f"This decision was recorded across {len(decisions)} Fusion Memory item(s) "
        f"in {len(sessions)} session(s): {', '.join(f'`{s}`' for s in sorted(sessions))}. "
        f"It has not yet been formalized as an ADR in the Obsidian vault."
    )

    return ADRCandidate(
        title=title,
        slug=slug,
        proposed_decision=proposed_decision,
        context=context,
        alternatives=_build_alternatives(decisions),
        consequences=_build_consequences(decisions),
        source_evidence=_build_source_evidence(decisions),
        project=project,
        source_decisions=decisions,
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration (pure logic — MCP calls happen in the caller)
# ---------------------------------------------------------------------------


@dataclass
class SurfacingResult:
    """Result of the full ADR surfacing pipeline."""

    decisions_discovered: int = 0
    decisions_significant: int = 0
    groups_formed: int = 0
    candidates_surfaced: int = 0
    candidates_skipped_dedup: int = 0
    candidates_skipped_trivial: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Discovered {self.decisions_discovered} decisions, "
            f"{self.decisions_significant} significant, "
            f"{self.groups_formed} topic groups, "
            f"{self.candidates_surfaced} ADR candidates surfaced, "
            f"{self.candidates_skipped_dedup} skipped (duplicate), "
            f"{self.candidates_skipped_trivial} skipped (trivial)"
        )


def run_pipeline(
    fusion_results: list[dict[str, Any]],
    vault_adr_search_fn=None,
    vault_inbox_search_fn=None,
    max_candidates: int = 3,
) -> tuple[SurfacingResult, list[ADRCandidate]]:
    """Run the Decision-to-ADR bridge pipeline.

    This is a mostly-pure function. The vault search functions are injected
    as callables to allow testing without MCP.

    Args:
        fusion_results: Raw results from Fusion Memory query/get_recent_events.
        vault_adr_search_fn: Callable(topic) -> list[dict] for 10-adrs/ search.
            Returns empty list if None.
        vault_inbox_search_fn: Callable(topic) -> list[dict] for 00-inbox/ search.
            Returns empty list if None.
        max_candidates: Maximum number of candidates to surface (default 3).

    Returns:
        Tuple of (SurfacingResult, list of ADRCandidate objects).
    """
    result = SurfacingResult()

    # Step 1: Parse decisions
    decisions = []
    seen_ids = set()
    for item in fusion_results:
        d = FusionDecision.from_fusion_result(item)
        if d.memory_id and d.memory_id not in seen_ids:
            seen_ids.add(d.memory_id)
            decisions.append(d)
    result.decisions_discovered = len(decisions)

    # Step 2: Filter for significance
    significant = [d for d in decisions if is_significant(d)]
    result.decisions_significant = len(significant)
    result.candidates_skipped_trivial = len(decisions) - len(significant)

    if not significant:
        return result, []

    # Step 3: Group related decisions
    groups = group_decisions(significant)
    result.groups_formed = len(groups)

    # Step 4: Compose candidates and dedup
    candidates: list[ADRCandidate] = []
    for topic_key, group_decisions_list in groups.items():
        if len(candidates) >= max_candidates:
            break

        # Dedup check
        adr_results = vault_adr_search_fn(topic_key) if vault_adr_search_fn else []
        inbox_results = vault_inbox_search_fn(topic_key) if vault_inbox_search_fn else []
        dedup = check_dedup_static(topic_key, adr_results, inbox_results)

        if dedup.is_duplicate:
            result.candidates_skipped_dedup += 1
            result.details.append(
                {
                    "topic": topic_key,
                    "status": "skipped",
                    "reason": dedup.reason,
                    "existing_path": dedup.existing_path,
                }
            )
            continue

        candidate = compose_candidate(topic_key, group_decisions_list)
        candidates.append(candidate)
        result.details.append(
            {
                "topic": topic_key,
                "status": "surfaced",
                "vault_path": candidate.vault_path,
                "title": candidate.title,
                "decision_count": len(group_decisions_list),
            }
        )

    result.candidates_surfaced = len(candidates)
    return result, candidates
