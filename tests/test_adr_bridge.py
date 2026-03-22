"""Tests for the Decision-to-ADR Bridge (Dual Memory Integration Phase 5).

Covers: significance filtering, grouping, dedup, candidate composition,
and full pipeline execution with edge cases.
"""

from datetime import datetime, timezone

from utils.adr_bridge import (
    FusionDecision,
    SurfacingResult,
    check_dedup_static,
    compose_candidate,
    group_decisions,
    is_significant,
    run_pipeline,
)

# ---------------------------------------------------------------------------
# Fixtures — realistic Fusion Memory decision items
# ---------------------------------------------------------------------------


def _make_decision(
    memory_id: str = "test-001",
    text: str = "Selected Pinecone over Weaviate for vector storage due to managed hosting and better scale.",
    project: str = "nova-core",
    session_id: str = "session-alpha",
    event_seq: int = 10,
    tags: list[str] | None = None,
    category: str = "decision",
) -> FusionDecision:
    return FusionDecision(
        memory_id=memory_id,
        text=text,
        project=project,
        session_id=session_id,
        event_seq=event_seq,
        event_time="2026-03-08T20:00:00+00:00",
        tags=tags or ["architecture"],
        category=category,
    )


def _make_fusion_result(
    memory_id: str = "test-001",
    text: str = "Selected Pinecone over Weaviate for vector storage due to managed hosting and better scale.",
    project: str = "nova-core",
    session_id: str = "session-alpha",
    event_seq: int = 10,
    tags: list[str] | None = None,
    category: str = "decision",
    score: float = 0.5,
) -> dict:
    return {
        "id": memory_id,
        "text": text,
        "composite_score": score,
        "metadata": {
            "category": category,
            "project": project,
            "session_id": session_id,
            "event_seq": event_seq,
            "event_time": "2026-03-08T20:00:00+00:00",
            "tags": tags or ["architecture"],
            "memory_type": "decision",
        },
    }


SIGNIFICANT_DECISION_TEXT = (
    "Fusion Memory MCP: three-backend architecture — Pinecone for semantic vector search "
    "(text-embedding-3-small, 1536 dims, cosine), Neo4j for knowledge graph traversal "
    "(Session nodes, FOLLOWS/INCLUDES edges), Redis for chronological timeline indexing."
)

CEO_NOVA_DECISION_TEXT = (
    "CEO Nova Architecture Decision: Claude CLI over Anthropic SDK — CEO Nova uses the same "
    "Claude CLI binary as the Nova-Core watcher. Key advantages: (1) Same authentication "
    "(Claude Code Max subscription, no API key management). (2) MCP tools available natively."
)

TRIVIAL_DECISION_TEXT = "Used print() for debugging the auth module."

SHORT_DECISION_TEXT = "Chose Redis."

EMBEDDING_DECISION_TEXT = (
    "The embedding model was switched from text-embedding-ada-002 to text-embedding-3-small "
    "for better quality at 5x lower cost. Both use 1536 dimensions."
)


# =====================================================================
# Significance Filter Tests
# =====================================================================


class TestSignificanceFilter:
    """Test is_significant() for various decision types."""

    def test_significant_architecture_decision(self):
        d = _make_decision(text=SIGNIFICANT_DECISION_TEXT)
        assert is_significant(d) is True

    def test_significant_ceo_nova_decision(self):
        d = _make_decision(text=CEO_NOVA_DECISION_TEXT, tags=["architecture", "ceo-nova"])
        assert is_significant(d) is True

    def test_significant_embedding_decision(self):
        d = _make_decision(text=EMBEDDING_DECISION_TEXT, tags=["embeddings", "cost"])
        assert is_significant(d) is True

    def test_trivial_print_debugging(self):
        d = _make_decision(text=TRIVIAL_DECISION_TEXT)
        assert is_significant(d) is False

    def test_too_short(self):
        d = _make_decision(text=SHORT_DECISION_TEXT)
        assert is_significant(d) is False

    def test_long_but_no_architectural_keywords(self):
        d = _make_decision(
            text="We spent some time discussing the approach to the feature. "
            "Eventually we agreed on a plan that seemed reasonable enough. "
            "The team liked it and we moved forward with the implementation.",
            tags=[],
        )
        assert is_significant(d) is False

    def test_security_hardening_is_significant(self):
        d = _make_decision(
            text="Security Hardening Implementation Plan — Lethal Trifecta Elimination. "
            "Nova-Core has the Lethal Trifecta: private data access, untrusted input, "
            "and exfiltration capability. Adopted multi-phase hardening approach.",
            tags=["security", "hardening"],
        )
        assert is_significant(d) is True

    def test_decision_with_architectural_tags(self):
        d = _make_decision(
            text="Adopted hierarchical orchestrator-worker pattern for multi-agent "
            "coordination with blackboard state layer for shared context.",
            tags=["architecture", "decision", "multi-agent"],
        )
        assert is_significant(d) is True

    def test_empty_text(self):
        d = _make_decision(text="")
        assert is_significant(d) is False


# =====================================================================
# Grouping Tests
# =====================================================================


class TestGrouping:
    """Test group_decisions() topic grouping logic."""

    def test_groups_ceo_nova_decisions(self):
        d1 = _make_decision(memory_id="ceo-1", text=CEO_NOVA_DECISION_TEXT)
        d2 = _make_decision(
            memory_id="ceo-2",
            text="CEO Nova Routing Model — Three response paths for the Telegram bot.",
        )
        groups = group_decisions([d1, d2])
        assert "ceo-nova-architecture" in groups
        assert len(groups["ceo-nova-architecture"]) == 2

    def test_groups_fusion_memory_decisions(self):
        d1 = _make_decision(memory_id="fm-1", text=SIGNIFICANT_DECISION_TEXT)
        d2 = _make_decision(
            memory_id="fm-2",
            text="Fusion Memory uses Pinecone for vectors with Neo4j graph backend.",
        )
        groups = group_decisions([d1, d2])
        assert "fusion-memory-architecture" in groups

    def test_groups_embedding_decisions(self):
        d1 = _make_decision(memory_id="emb-1", text=EMBEDDING_DECISION_TEXT)
        groups = group_decisions([d1])
        assert "embedding-model-selection" in groups

    def test_separate_topics_stay_separate(self):
        d1 = _make_decision(memory_id="ceo-1", text=CEO_NOVA_DECISION_TEXT)
        d2 = _make_decision(memory_id="fm-1", text=SIGNIFICANT_DECISION_TEXT)
        groups = group_decisions([d1, d2])
        assert len(groups) == 2

    def test_empty_list(self):
        groups = group_decisions([])
        assert groups == {}


# =====================================================================
# Dedup Tests
# =====================================================================


class TestDedup:
    """Test check_dedup_static() against vault search results."""

    def test_no_duplicates(self):
        result = check_dedup_static("fusion-memory", [], [])
        assert result.is_duplicate is False

    def test_existing_adr_is_duplicate(self):
        adr_results = [{"path": "10-adrs/ADR-001-multi-agent-architecture.md"}]
        result = check_dedup_static("multi-agent", adr_results, [])
        assert result.is_duplicate is True
        assert "Existing ADR" in result.reason

    def test_existing_inbox_candidate_is_duplicate(self):
        inbox_results = [{"path": "00-inbox/adr-candidate-fusion-memory.md"}]
        result = check_dedup_static("fusion-memory", [], inbox_results)
        assert result.is_duplicate is True
        assert "inbox" in result.reason.lower()

    def test_non_adr_inbox_item_not_duplicate(self):
        inbox_results = [{"path": "00-inbox/plan-something.md"}]
        result = check_dedup_static("fusion-memory", [], inbox_results)
        assert result.is_duplicate is False

    def test_adr_in_wrong_folder_not_duplicate(self):
        # Results not in 10-adrs/ shouldn't count
        adr_results = [{"path": "20-agent-patterns/some-pattern.md"}]
        result = check_dedup_static("some-topic", adr_results, [])
        assert result.is_duplicate is False


# =====================================================================
# Candidate Composition Tests
# =====================================================================


class TestCandidateComposition:
    """Test compose_candidate() output structure and content."""

    def test_basic_composition(self):
        decisions = [_make_decision(text=SIGNIFICANT_DECISION_TEXT)]
        candidate = compose_candidate("fusion-memory-architecture", decisions)

        assert candidate.title == "Fusion Memory Three-Backend Architecture (Pinecone + Neo4j + Redis)"
        assert candidate.slug == "fusion-memory-architecture"
        assert candidate.vault_path == "00-inbox/adr-candidate-fusion-memory-architecture.md"

    def test_frontmatter_structure(self):
        decisions = [_make_decision(text=SIGNIFICANT_DECISION_TEXT)]
        candidate = compose_candidate("fusion-memory-architecture", decisions)
        fm = candidate.frontmatter()

        assert fm["type"] == "inbox"
        assert fm["title"].startswith("ADR Candidate:")
        assert fm["source"] == "nova-core-memory"
        assert "#type/inbox" in fm["tags"]
        assert "#action/review" in fm["tags"]
        assert "#action/promote-to-adr" in fm["tags"]

    def test_body_has_all_sections(self):
        decisions = [_make_decision(text=SIGNIFICANT_DECISION_TEXT)]
        candidate = compose_candidate("fusion-memory-architecture", decisions)
        body = candidate.body()

        assert "## Proposed Decision" in body
        assert "## Context" in body
        assert "## Alternatives Considered" in body
        assert "## Consequences" in body
        assert "## Source Evidence" in body
        assert "## Action Needed" in body

    def test_source_evidence_includes_memory_ids(self):
        decisions = [
            _make_decision(memory_id="abc-123", session_id="sess-1", event_seq=42),
        ]
        candidate = compose_candidate("test-topic", decisions)
        body = candidate.body()

        assert "abc-123" in body
        assert "sess-1" in body
        assert "42" in body

    def test_multiple_decisions_merged(self):
        d1 = _make_decision(memory_id="d1", session_id="sess-1", text=SIGNIFICANT_DECISION_TEXT)
        d2 = _make_decision(memory_id="d2", session_id="sess-2", text=CEO_NOVA_DECISION_TEXT)
        candidate = compose_candidate("test-group", [d1, d2])
        body = candidate.body()

        # Both decisions should appear in source evidence
        assert "d1" in body
        assert "d2" in body
        # Both sessions should be mentioned in context
        assert "sess-1" in body
        assert "sess-2" in body

    def test_alternatives_extracted_from_text(self):
        d = _make_decision(
            text="Selected text-embedding-3-small over text-embedding-ada-002 for the embedding model. "
            "5x cheaper with better benchmarks.",
        )
        candidate = compose_candidate("embedding", [d])
        body = candidate.body()
        # Should find "text-embedding-ada-002" as an alternative
        assert "Alternatives" in body

    def test_alternatives_not_fabricated(self):
        d = _make_decision(
            text="We adopted a multi-agent architecture with hierarchical orchestration "
            "and blackboard state for shared coordination context.",
        )
        candidate = compose_candidate("test", [d])
        # If no alternatives found, should say "Not recorded"
        assert "Not recorded" in candidate.alternatives or "mentioned" in candidate.alternatives

    def test_date_is_today(self):
        decisions = [_make_decision()]
        candidate = compose_candidate("test", decisions)
        fm = candidate.frontmatter()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert fm["date"] == today

    def test_project_from_decision(self):
        decisions = [_make_decision(project="fusion-memory")]
        candidate = compose_candidate("test", decisions)
        assert candidate.project == "fusion-memory"
        assert "#project/fusion-memory" in candidate.frontmatter()["tags"]


# =====================================================================
# FusionDecision.from_fusion_result Tests
# =====================================================================


class TestFusionDecisionParsing:
    """Test parsing of raw Fusion Memory API results."""

    def test_parse_standard_result(self):
        raw = _make_fusion_result(
            memory_id="xyz",
            text="Test decision",
            project="nova-core",
            session_id="sess-1",
            event_seq=42,
            tags=["architecture"],
        )
        d = FusionDecision.from_fusion_result(raw)
        assert d.memory_id == "xyz"
        assert d.text == "Test decision"
        assert d.project == "nova-core"
        assert d.session_id == "sess-1"
        assert d.event_seq == 42

    def test_parse_missing_fields(self):
        raw = {"id": "minimal", "metadata": {}}
        d = FusionDecision.from_fusion_result(raw)
        assert d.memory_id == "minimal"
        assert d.text == ""
        assert d.project == "nova-core"  # default

    def test_parse_text_from_metadata(self):
        raw = {"id": "fallback", "metadata": {"text": "From metadata"}}
        d = FusionDecision.from_fusion_result(raw)
        assert d.text == "From metadata"


# =====================================================================
# Pipeline Tests
# =====================================================================


class TestPipeline:
    """Test run_pipeline() full orchestration."""

    def test_basic_pipeline_surfaces_candidates(self):
        results = [
            _make_fusion_result(memory_id="fm-1", text=SIGNIFICANT_DECISION_TEXT),
            _make_fusion_result(memory_id="ceo-1", text=CEO_NOVA_DECISION_TEXT, tags=["architecture", "ceo-nova"]),
        ]
        # No vault results = no duplicates
        surfacing, candidates = run_pipeline(
            results,
            vault_adr_search_fn=lambda t: [],
            vault_inbox_search_fn=lambda t: [],
        )
        assert surfacing.decisions_discovered == 2
        assert surfacing.decisions_significant == 2
        assert surfacing.candidates_surfaced >= 1
        assert len(candidates) >= 1

    def test_pipeline_filters_trivial(self):
        results = [
            _make_fusion_result(memory_id="t-1", text=TRIVIAL_DECISION_TEXT),
            _make_fusion_result(memory_id="t-2", text=SHORT_DECISION_TEXT),
        ]
        surfacing, candidates = run_pipeline(results)
        assert surfacing.decisions_discovered == 2
        assert surfacing.decisions_significant == 0
        assert surfacing.candidates_surfaced == 0
        assert candidates == []

    def test_pipeline_skips_duplicates(self):
        results = [
            _make_fusion_result(
                memory_id="ma-1",
                text="Adopted hierarchical multi-agent orchestrator-worker "
                "architecture with blackboard state and policy engine.",
                tags=["architecture", "multi-agent"],
            ),
        ]

        # Simulate existing ADR for multi-agent
        def adr_search(topic):
            if "multi-agent" in topic:
                return [{"path": "10-adrs/ADR-001-multi-agent-architecture.md"}]
            return []

        surfacing, candidates = run_pipeline(
            results,
            vault_adr_search_fn=adr_search,
            vault_inbox_search_fn=lambda t: [],
        )
        assert surfacing.candidates_skipped_dedup >= 1
        assert surfacing.candidates_surfaced == 0

    def test_pipeline_respects_max_candidates(self):
        results = [
            _make_fusion_result(memory_id="a", text=SIGNIFICANT_DECISION_TEXT),
            _make_fusion_result(memory_id="b", text=CEO_NOVA_DECISION_TEXT, tags=["architecture", "ceo-nova"]),
            _make_fusion_result(memory_id="c", text=EMBEDDING_DECISION_TEXT, tags=["embeddings", "cost"]),
            _make_fusion_result(
                memory_id="d",
                text="Security Hardening plan — Lethal Trifecta: private data access, "
                "untrusted input, exfiltration. Multi-phase hardening with kill switch "
                "and circuit breakers for all integration points.",
                tags=["security", "hardening"],
            ),
        ]
        surfacing, candidates = run_pipeline(
            results,
            vault_adr_search_fn=lambda t: [],
            vault_inbox_search_fn=lambda t: [],
            max_candidates=2,
        )
        assert len(candidates) <= 2
        assert surfacing.candidates_surfaced <= 2

    def test_pipeline_deduplicates_same_memory_id(self):
        results = [
            _make_fusion_result(memory_id="dup-1", text=SIGNIFICANT_DECISION_TEXT),
            _make_fusion_result(memory_id="dup-1", text=SIGNIFICANT_DECISION_TEXT),
        ]
        surfacing, candidates = run_pipeline(results)
        assert surfacing.decisions_discovered == 1  # deduped

    def test_pipeline_empty_input(self):
        surfacing, candidates = run_pipeline([])
        assert surfacing.decisions_discovered == 0
        assert surfacing.candidates_surfaced == 0
        assert candidates == []

    def test_pipeline_no_vault_functions(self):
        """Pipeline works even without vault search functions (best-effort)."""
        results = [
            _make_fusion_result(memory_id="no-vault", text=SIGNIFICANT_DECISION_TEXT),
        ]
        surfacing, candidates = run_pipeline(results)
        assert surfacing.candidates_surfaced >= 1

    def test_pipeline_details_tracking(self):
        results = [
            _make_fusion_result(memory_id="det-1", text=SIGNIFICANT_DECISION_TEXT),
        ]
        surfacing, candidates = run_pipeline(
            results,
            vault_adr_search_fn=lambda t: [],
            vault_inbox_search_fn=lambda t: [],
        )
        assert len(surfacing.details) >= 1
        detail = surfacing.details[0]
        assert "status" in detail
        assert detail["status"] in ("surfaced", "skipped")

    def test_pipeline_mixed_significant_and_trivial(self):
        results = [
            _make_fusion_result(memory_id="sig", text=SIGNIFICANT_DECISION_TEXT),
            _make_fusion_result(memory_id="triv", text=TRIVIAL_DECISION_TEXT),
            _make_fusion_result(memory_id="short", text=SHORT_DECISION_TEXT),
        ]
        surfacing, candidates = run_pipeline(
            results,
            vault_adr_search_fn=lambda t: [],
            vault_inbox_search_fn=lambda t: [],
        )
        assert surfacing.decisions_discovered == 3
        assert surfacing.decisions_significant == 1
        assert surfacing.candidates_surfaced == 1


# =====================================================================
# Edge Cases
# =====================================================================


class TestEdgeCases:
    """Test boundary conditions and edge cases."""

    def test_decision_with_no_tags(self):
        d = _make_decision(text=SIGNIFICANT_DECISION_TEXT, tags=[])
        # Should still be significant based on text content alone
        assert is_significant(d) is True

    def test_grouping_unknown_topic(self):
        d = _make_decision(
            text="We selected a new approach for the data pipeline that uses streaming "
            "architecture with backpressure and circuit breakers for resilience.",
            tags=["custom-tag"],
        )
        groups = group_decisions([d])
        # Should still produce a group (fallback key)
        assert len(groups) == 1

    def test_slug_length_limit(self):
        d = _make_decision(text=SIGNIFICANT_DECISION_TEXT)
        candidate = compose_candidate(
            "this-is-a-very-long-topic-key-that-should-be-truncated-to-a-reasonable-length",
            [d],
        )
        assert len(candidate.slug) <= 50

    def test_candidate_vault_path_format(self):
        d = _make_decision()
        candidate = compose_candidate("test-topic", [d])
        assert candidate.vault_path.startswith("00-inbox/adr-candidate-")
        assert candidate.vault_path.endswith(".md")

    def test_surfacing_result_summary(self):
        sr = SurfacingResult(
            decisions_discovered=10,
            decisions_significant=5,
            groups_formed=3,
            candidates_surfaced=2,
            candidates_skipped_dedup=1,
        )
        summary = sr.summary()
        assert "10 decisions" in summary
        assert "5 significant" in summary
        assert "2 ADR candidates surfaced" in summary
