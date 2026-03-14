"""Phase 5 — Intent-Aware Recall.

Deterministic recall intent classification, routing policy, multi-factor
ranking, and retrieval explanation metadata.

Usage:
    from agents.recall_intent import classify_intent, IntentClassification
    from agents.recall_intent import get_routing_plan, RoutingPlan
    from agents.recall_intent import rank_results, RecallExplanation

    classification = classify_intent("when did we fix the auth bug?")
    plan = get_routing_plan(classification.intent)
    ranked = rank_results(raw_hits, classification)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent enum
# ---------------------------------------------------------------------------

VALID_RECALL_INTENTS = frozenset(
    {
        "temporal_recall",  # When did X happen? Timeline queries.
        "decision_recall",  # What was decided about X? ADR/tradeoff.
        "procedural_recall",  # How do we do X? Patterns, playbooks.
        "project_state_recall",  # What is the current state of X?
        "user_preference_recall",  # What does the user prefer for X?
        "factual_recall",  # What do we know about X? Stable facts.
        "open_loop_recall",  # What is unfinished? Open threads.
        "relationship_entity_recall",  # What is related to X? Entity links.
    }
)

# Legacy intents that Phase 1–4 code uses — map to Phase 5 intents
_LEGACY_INTENT_MAP: dict[str, str] = {
    "pattern_retrieval": "procedural_recall",
    "vault_context": "factual_recall",
    "prior_decision": "decision_recall",
    "session_replay": "temporal_recall",
    "general": "factual_recall",
}


# ---------------------------------------------------------------------------
# Intent classification result
# ---------------------------------------------------------------------------


@dataclass
class IntentClassification:
    """Result of classifying a recall query's intent."""

    intent: str  # One of VALID_RECALL_INTENTS
    confidence: str  # high, medium, low
    signals: list[str]  # Why this intent was inferred
    fallback_used: bool = False  # True if no strong signal → default
    caller_override: bool = False  # True if caller explicitly set intent
    legacy_mapped: bool = False  # True if mapped from Phase 1-4 intent

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Routing plan
# ---------------------------------------------------------------------------


@dataclass
class RoutingPlan:
    """Which adapters to query and how for a given intent."""

    intent: str
    primary_adapters: list[str]  # First-priority adapters
    secondary_adapters: list[str]  # Blended retrieval sources
    blend_mode: str  # "none", "merge", "fallback"
    ranking_emphasis: list[str]  # Which ranking factors to emphasize
    max_results_per_adapter: int = 5

    def all_adapters(self) -> list[str]:
        """All adapters to query (primary + secondary)."""
        return self.primary_adapters + self.secondary_adapters

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Retrieval explanation metadata
# ---------------------------------------------------------------------------


@dataclass
class RecallExplanation:
    """Per-result explanation of why a memory was retrieved and ranked."""

    matched_intent: str
    matched_source: str
    why_matched: str
    why_ranked_high: str
    ranking_factors_used: dict[str, float]
    degradation_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Classification signals — keyword patterns per intent
# ---------------------------------------------------------------------------

# Each entry: (compiled_regex, intent, signal_description)
_SIGNAL_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # Temporal
    (re.compile(r"\bwhen\s+did\b", re.I), "temporal_recall", "temporal_keyword:when_did"),
    (re.compile(r"\bhow\s+long\s+ago\b", re.I), "temporal_recall", "temporal_keyword:how_long_ago"),
    (re.compile(r"\btimeline\b", re.I), "temporal_recall", "temporal_keyword:timeline"),
    (re.compile(r"\brecent(ly)?\b", re.I), "temporal_recall", "temporal_keyword:recent"),
    (re.compile(r"\blast\s+(session|time|week|day)\b", re.I), "temporal_recall", "temporal_keyword:last_period"),
    (re.compile(r"\bhistory\b", re.I), "temporal_recall", "temporal_keyword:history"),
    (re.compile(r"\byesterday\b", re.I), "temporal_recall", "temporal_keyword:yesterday"),
    # Decision
    (re.compile(r"\bdecid(e|ed|ing)\b", re.I), "decision_recall", "decision_keyword:decided"),
    (re.compile(r"\bdecision\b", re.I), "decision_recall", "decision_keyword:decision"),
    (re.compile(r"\bwhy\s+did\s+we\b", re.I), "decision_recall", "decision_keyword:why_did_we"),
    (re.compile(r"\btradeoff\b", re.I), "decision_recall", "decision_keyword:tradeoff"),
    (re.compile(r"\bADR\b", re.I), "decision_recall", "decision_keyword:adr"),
    (re.compile(r"\brational(e)?\b", re.I), "decision_recall", "decision_keyword:rationale"),
    (re.compile(r"\bchose\b", re.I), "decision_recall", "decision_keyword:chose"),
    # Procedural
    (re.compile(r"\bhow\s+(do|should|to|can)\b", re.I), "procedural_recall", "procedural_keyword:how_to"),
    (re.compile(r"\bpattern\b", re.I), "procedural_recall", "procedural_keyword:pattern"),
    (re.compile(r"\bplaybook\b", re.I), "procedural_recall", "procedural_keyword:playbook"),
    (re.compile(r"\bstep[- ]by[- ]step\b", re.I), "procedural_recall", "procedural_keyword:step_by_step"),
    (re.compile(r"\bprocedure\b", re.I), "procedural_recall", "procedural_keyword:procedure"),
    (re.compile(r"\bworkflow\b", re.I), "procedural_recall", "procedural_keyword:workflow"),
    (re.compile(r"\bfix(ed|ing)?\s", re.I), "procedural_recall", "procedural_keyword:fix"),
    # Project state
    (re.compile(r"\bstatus\b", re.I), "project_state_recall", "state_keyword:status"),
    (
        re.compile(r"\bcurrent(ly)?\s+(state|status|progress)\b", re.I),
        "project_state_recall",
        "state_keyword:current_state",
    ),
    (re.compile(r"\bprogress\b", re.I), "project_state_recall", "state_keyword:progress"),
    (re.compile(r"\bwhat\s+is\s+the\s+state\b", re.I), "project_state_recall", "state_keyword:what_state"),
    (re.compile(r"\bactive\s+plan\b", re.I), "project_state_recall", "state_keyword:active_plan"),
    # User preference
    (re.compile(r"\bprefer(s|ence|ences|red)?\b", re.I), "user_preference_recall", "pref_keyword:prefer"),
    (re.compile(r"\buser\s+(likes?|wants?|style)\b", re.I), "user_preference_recall", "pref_keyword:user_wants"),
    (
        re.compile(r"\bconfig(uration)?\s+(style|setting)\b", re.I),
        "user_preference_recall",
        "pref_keyword:config_style",
    ),
    # Open loop
    (re.compile(r"\bunfinished\b", re.I), "open_loop_recall", "loop_keyword:unfinished"),
    (re.compile(r"\bopen\s+(thread|loop|item|task)\b", re.I), "open_loop_recall", "loop_keyword:open_thread"),
    (re.compile(r"\btodo\b", re.I), "open_loop_recall", "loop_keyword:todo"),
    (re.compile(r"\bpending\b", re.I), "open_loop_recall", "loop_keyword:pending"),
    (re.compile(r"\bblocked\b", re.I), "open_loop_recall", "loop_keyword:blocked"),
    (re.compile(r"\bremaining\b", re.I), "open_loop_recall", "loop_keyword:remaining"),
    # Relationship / entity
    (re.compile(r"\brelated\s+to\b", re.I), "relationship_entity_recall", "rel_keyword:related_to"),
    (re.compile(r"\bconnect(ed|ion)\b", re.I), "relationship_entity_recall", "rel_keyword:connected"),
    (re.compile(r"\bdepend(s|ency|encies)\b", re.I), "relationship_entity_recall", "rel_keyword:dependency"),
    (re.compile(r"\blink(ed|s)?\s+to\b", re.I), "relationship_entity_recall", "rel_keyword:linked_to"),
    # Factual (broad — lower priority, matched if nothing else wins)
    (re.compile(r"\bwhat\s+(is|are|does|do)\b", re.I), "factual_recall", "factual_keyword:what_is"),
    (re.compile(r"\barchitecture\b", re.I), "factual_recall", "factual_keyword:architecture"),
    (re.compile(r"\bschema\b", re.I), "factual_recall", "factual_keyword:schema"),
]


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


def classify_intent(
    query: str,
    *,
    caller_intent: str | None = None,
    context: dict[str, Any] | None = None,
) -> IntentClassification:
    """Classify a recall query into one of the Phase 5 intent types.

    Deterministic, rule-based classification. Uses keyword signals from the
    query string. Caller can override with an explicit intent.

    Args:
        query: The recall query string.
        caller_intent: Optional explicit intent from the caller. If valid,
                       it overrides inference. If it's a legacy Phase 1-4
                       intent, it's mapped to the Phase 5 equivalent.
        context: Optional additional context (unused in Phase 5, reserved
                 for Phase 6+ enrichment).

    Returns:
        IntentClassification with intent, confidence, and signals.
    """
    # --- Caller override path ---
    if caller_intent:
        # Accept valid Phase 5 intents directly
        if caller_intent in VALID_RECALL_INTENTS:
            return IntentClassification(
                intent=caller_intent,
                confidence="high",
                signals=["caller_explicit_intent"],
                caller_override=True,
            )
        # Map legacy Phase 1-4 intents
        if caller_intent in _LEGACY_INTENT_MAP:
            mapped = _LEGACY_INTENT_MAP[caller_intent]
            return IntentClassification(
                intent=mapped,
                confidence="high",
                signals=[f"legacy_mapped:{caller_intent}→{mapped}"],
                caller_override=True,
                legacy_mapped=True,
            )
        # Invalid caller intent — log warning, fall through to inference
        logger.warning("Invalid caller_intent=%r, falling through to inference", caller_intent)

    # --- Inference path ---
    if not query or not query.strip():
        return IntentClassification(
            intent="factual_recall",
            confidence="low",
            signals=["empty_query"],
            fallback_used=True,
        )

    # Tally signals per intent
    intent_signals: dict[str, list[str]] = {}
    for pattern, intent, signal_desc in _SIGNAL_PATTERNS:
        if pattern.search(query):
            intent_signals.setdefault(intent, []).append(signal_desc)

    if not intent_signals:
        # No keyword matches → factual fallback
        return IntentClassification(
            intent="factual_recall",
            confidence="low",
            signals=["no_keyword_match"],
            fallback_used=True,
        )

    # Pick the intent with the most matching signals
    ranked = sorted(intent_signals.items(), key=lambda x: len(x[1]), reverse=True)
    best_intent, best_signals = ranked[0]

    # Confidence based on signal count and dominance
    if len(best_signals) >= 3:
        confidence = "high"
    elif len(best_signals) >= 2:
        confidence = "medium"
    else:
        # Single signal — check if ambiguous (tie with another intent)
        if len(ranked) > 1 and len(ranked[1][1]) == len(best_signals):
            confidence = "low"
        else:
            confidence = "medium"

    return IntentClassification(
        intent=best_intent,
        confidence=confidence,
        signals=best_signals,
        fallback_used=False,
    )


# ---------------------------------------------------------------------------
# Routing policy — intent → adapters + blend mode
# ---------------------------------------------------------------------------

# Routing table: intent → (primary, secondary, blend_mode, ranking_emphasis)
_ROUTING_TABLE: dict[str, tuple[list[str], list[str], str, list[str]]] = {
    "temporal_recall": (
        ["state_working", "memory_file"],
        [],
        "none",
        ["recency", "confidence"],
    ),
    "decision_recall": (
        ["obsidian_vault"],
        ["memory_file"],
        "fallback",
        ["source_authority", "confidence", "recency"],
    ),
    "procedural_recall": (
        ["memory_file", "obsidian_vault"],
        [],
        "merge",
        ["source_authority", "confidence", "recency"],
    ),
    "project_state_recall": (
        ["state_working", "memory_file"],
        ["obsidian_vault"],
        "fallback",
        ["recency", "confidence"],
    ),
    "user_preference_recall": (
        ["obsidian_vault"],
        ["memory_file"],
        "fallback",
        ["source_authority", "confidence"],
    ),
    "factual_recall": (
        ["memory_file", "obsidian_vault"],
        [],
        "merge",
        ["source_authority", "confidence", "recency"],
    ),
    "open_loop_recall": (
        ["state_working", "memory_file"],
        ["obsidian_vault"],
        "fallback",
        ["recency", "confidence"],
    ),
    "relationship_entity_recall": (
        ["memory_file", "obsidian_vault"],
        [],
        "merge",
        ["confidence", "source_authority"],
    ),
}


def get_routing_plan(
    intent: str,
    *,
    scope_override: str | None = None,
    max_results: int = 5,
) -> RoutingPlan:
    """Get the routing plan for a classified intent.

    Args:
        intent: A valid Phase 5 recall intent.
        scope_override: If set, constrains to a single adapter (backward compat).
        max_results: Max results per adapter.

    Returns:
        RoutingPlan describing which adapters to query and how.

    Raises:
        ValueError: If intent is not a valid Phase 5 intent.
    """
    if intent not in VALID_RECALL_INTENTS:
        raise ValueError(f"Invalid recall intent: {intent!r}")

    # Scope override (backward compatibility with Phase 1 scope param)
    if scope_override and scope_override != "all":
        scope_map = {
            "memory_files": ["memory_file"],
            "vault": ["obsidian_vault"],
            "fusion": ["fusion_memory"],
            "working": ["state_working"],
        }
        adapters = scope_map.get(scope_override, ["memory_file", "obsidian_vault"])
        return RoutingPlan(
            intent=intent,
            primary_adapters=adapters,
            secondary_adapters=[],
            blend_mode="none",
            ranking_emphasis=["confidence", "recency"],
            max_results_per_adapter=max_results,
        )

    primary, secondary, blend, emphasis = _ROUTING_TABLE[intent]
    return RoutingPlan(
        intent=intent,
        primary_adapters=list(primary),
        secondary_adapters=list(secondary),
        blend_mode=blend,
        ranking_emphasis=list(emphasis),
        max_results_per_adapter=max_results,
    )


# ---------------------------------------------------------------------------
# Multi-factor ranking
# ---------------------------------------------------------------------------

# Factor weights (normalized to sum ~1.0 for each emphasis profile)
_RANKING_WEIGHTS: dict[str, float] = {
    "recency": 0.25,
    "confidence": 0.20,
    "source_authority": 0.25,
    "relevance_score": 0.20,
    "promotion_level": 0.10,
}

# Source authority scores — how authoritative each adapter/source is
_SOURCE_AUTHORITY: dict[str, float] = {
    "obsidian_vault": 1.0,  # Curated vault notes — highest authority
    "memory_file": 0.7,  # File-based artifacts — good but uncurated
    "state_working": 0.3,  # Working memory — transient
    "fusion_memory": 0.6,  # Fusion memory — mixed quality
}

# Promotion level scores for ranking
_PROMOTION_LEVEL: dict[str, float] = {
    "procedural": 1.0,
    "semantic": 0.8,
    "episodic": 0.5,
    "working": 0.2,
}

# Confidence → numeric for ranking
_CONFIDENCE_RANK: dict[str, float] = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}


def compute_ranking_score(
    result: dict[str, Any],
    *,
    emphasis: list[str] | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute a multi-factor ranking score for a recall result.

    Returns (total_score, factor_breakdown) where factor_breakdown maps
    factor name → contribution.

    Args:
        result: A single recall result dict from an adapter.
        emphasis: Which factors to emphasize (boosted by 1.5x). From RoutingPlan.

    Returns:
        Tuple of (score, {factor: contribution}).
    """
    emphasis = emphasis or []
    factors: dict[str, float] = {}

    # --- Relevance score (from adapter, if available) ---
    raw_relevance = result.get("_relevance_score", result.get("score", 0))
    # Normalize: adapter scores vary; cap at 1.0
    if isinstance(raw_relevance, (int, float)) and raw_relevance > 0:
        # MemoryFileAdapter scores can be 0-8+; normalize to 0-1
        norm_relevance = min(1.0, raw_relevance / 5.0)
    else:
        norm_relevance = 0.1  # minimal baseline
    factors["relevance_score"] = norm_relevance

    # --- Recency ---
    recency = _compute_recency_factor(result)
    factors["recency"] = recency

    # --- Confidence ---
    conf_str = result.get("confidence", "medium")
    factors["confidence"] = _CONFIDENCE_RANK.get(conf_str, 0.5)

    # --- Source authority ---
    source = result.get("_source_adapter", result.get("source", ""))
    factors["source_authority"] = _SOURCE_AUTHORITY.get(source, 0.5)

    # --- Promotion level ---
    layer = result.get("current_layer", result.get("memory_layer_candidate", "episodic"))
    factors["promotion_level"] = _PROMOTION_LEVEL.get(layer, 0.5)

    # --- Weighted sum with emphasis boost ---
    total = 0.0
    for factor_name, factor_value in factors.items():
        weight = _RANKING_WEIGHTS.get(factor_name, 0.1)
        if factor_name in emphasis:
            weight *= 1.5  # Emphasis boost
        total += factor_value * weight

    return round(total, 4), {k: round(v, 4) for k, v in factors.items()}


def _compute_recency_factor(result: dict[str, Any]) -> float:
    """Compute recency factor (0.0–1.0). More recent = higher."""
    timestamp = result.get("timestamp") or result.get("created_at") or result.get("created") or ""
    if not timestamp:
        return 0.3  # Unknown age → middling

    try:
        # Try ISO format
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                ts = time.mktime(time.strptime(timestamp, fmt))
                age_days = max(0, (time.time() - ts) / 86400)
                # Decay: 1.0 at 0 days, 0.5 at 7 days, 0.1 at 30+ days
                if age_days <= 1:
                    return 1.0
                elif age_days <= 7:
                    return round(1.0 - (age_days / 14), 2)
                elif age_days <= 30:
                    return round(0.5 - (age_days - 7) / 60, 2)
                else:
                    return 0.1
            except ValueError:
                continue
    except (OverflowError, OSError):
        pass

    return 0.3


def rank_results(
    results: list[dict[str, Any]],
    classification: IntentClassification,
    plan: RoutingPlan,
) -> list[dict[str, Any]]:
    """Rank recall results using multi-factor scoring.

    Adds _ranking_score, _ranking_factors, and _recall_explanation to each
    result dict. Returns results sorted by ranking score (descending).

    Args:
        results: Raw results from adapter queries.
        classification: The intent classification that drove retrieval.
        plan: The routing plan used.

    Returns:
        Results sorted by ranking score, with explanation metadata attached.
    """
    for result in results:
        score, factors = compute_ranking_score(
            result,
            emphasis=plan.ranking_emphasis,
        )
        result["_ranking_score"] = score
        result["_ranking_factors"] = factors

        # Build explanation
        source = result.get("_source_adapter", result.get("source", "unknown"))
        top_factor = max(factors, key=lambda f: factors[f]) if factors else "none"

        # Why matched
        if result.get("_relevance_score", 0) > 0:
            why_matched = f"keyword/content match (relevance={result.get('_relevance_score', 0):.2f})"
        elif result.get("score", 0) > 0:
            why_matched = f"search score={result.get('score', 0)}"
        else:
            why_matched = "chronological inclusion"

        # Why ranked high
        why_high_parts = []
        if factors.get("source_authority", 0) >= 0.7:
            why_high_parts.append(f"authoritative source ({source})")
        if factors.get("recency", 0) >= 0.7:
            why_high_parts.append("recent")
        if factors.get("confidence", 0) >= 0.7:
            why_high_parts.append("high confidence")
        if factors.get("relevance_score", 0) >= 0.5:
            why_high_parts.append("strong content match")
        why_high = "; ".join(why_high_parts) if why_high_parts else f"top factor: {top_factor}"

        # Degradation notes
        degradation = []
        if classification.fallback_used:
            degradation.append("intent_fallback: no strong signal, used factual_recall default")
        if classification.legacy_mapped:
            degradation.append("legacy_intent_mapped: original intent preserved via mapping")

        result["_recall_explanation"] = RecallExplanation(
            matched_intent=classification.intent,
            matched_source=source,
            why_matched=why_matched,
            why_ranked_high=why_high,
            ranking_factors_used=factors,
            degradation_notes=degradation,
        ).to_dict()

    # Sort by ranking score descending
    results.sort(key=lambda r: r.get("_ranking_score", 0), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Deduplication for blended retrieval
# ---------------------------------------------------------------------------


def dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate results from blended multi-adapter retrieval.

    Deduplication keys:
    1. memory_id (if present)
    2. path (for vault results)
    3. title + source composite key

    When duplicates are found, keep the one with the higher ranking score.
    """
    seen: dict[str, dict[str, Any]] = {}

    for r in results:
        # Compute dedup key
        key = r.get("memory_id") or r.get("path") or ""
        if not key:
            title = r.get("title", "")
            source = r.get("_source_adapter", r.get("source", ""))
            key = f"{title}:{source}"

        if not key or key == ":":
            # Cannot dedup without a key — keep all
            seen[str(id(r))] = r  # use object id as unique key
            continue

        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        else:
            # Keep higher-ranked result
            if r.get("_ranking_score", 0) > existing.get("_ranking_score", 0):
                seen[key] = r

    return list(seen.values())
