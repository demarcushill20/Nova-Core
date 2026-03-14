"""Tests for agents/recall_intent.py — Phase 5 Intent-Aware Recall.

Covers:
- Intent classification (keyword signals, caller override, legacy mapping)
- Ambiguous/fallback behavior
- Invalid intent rejection
- Routing plan generation
- Blended retrieval policy
- Multi-factor ranking
- Retrieval explanation metadata
- Deduplication
- Router integration (end-to-end intent-aware recall)
- Backward compatibility with Phase 1–4 intents
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from agents.memory_router import (
    MemoryFileAdapter,
    MemoryRouter,
    RecallResult,
    WorkingMemoryAdapter,
)
from agents.recall_intent import (
    _LEGACY_INTENT_MAP,
    VALID_RECALL_INTENTS,
    RecallExplanation,
    classify_intent,
    compute_ranking_score,
    dedupe_results,
    get_routing_plan,
    rank_results,
)
from utils.trace_context import TraceContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router(tmp_path: Path) -> MemoryRouter:
    """Router with memory_file + state_working adapters."""
    (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
    return MemoryRouter(
        adapters=[
            MemoryFileAdapter(base=tmp_path),
            WorkingMemoryAdapter(base=tmp_path),
        ],
        memory_file_base=tmp_path,
    )


def _sample_result(**overrides) -> dict:
    """Build a sample recall result dict."""
    base = {
        "title": "Test result",
        "summary": "A test memory result.",
        "confidence": "medium",
        "_source_adapter": "memory_file",
        "_relevance_score": 2.5,
        "current_layer": "episodic",
        "timestamp": "2026-03-13T12:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests: Intent classification
# ---------------------------------------------------------------------------


class TestIntentClassification:
    """Classify recall queries into Phase 5 intents."""

    def test_temporal_query(self):
        c = classify_intent("when did we recently change the auth timeline?")
        assert c.intent == "temporal_recall"
        assert c.confidence in ("high", "medium")
        assert any("temporal" in s for s in c.signals)

    def test_decision_query(self):
        c = classify_intent("why did we decide to use the router pattern?")
        assert c.intent == "decision_recall"
        assert any("decision" in s for s in c.signals)

    def test_procedural_query(self):
        c = classify_intent("how should we validate vault schemas?")
        assert c.intent == "procedural_recall"
        assert any("procedural" in s for s in c.signals)

    def test_project_state_query(self):
        c = classify_intent("what is the current status of Phase 5?")
        assert c.intent == "project_state_recall"

    def test_user_preference_query(self):
        c = classify_intent("what does the user prefer for code style?")
        assert c.intent == "user_preference_recall"

    def test_factual_query(self):
        c = classify_intent("what is the memory router architecture?")
        assert c.intent == "factual_recall"

    def test_open_loop_query(self):
        c = classify_intent("what tasks are still pending and unfinished?")
        assert c.intent == "open_loop_recall"

    def test_relationship_query(self):
        c = classify_intent("what is related to the memory router?")
        assert c.intent == "relationship_entity_recall"

    def test_multiple_signals_increases_confidence(self):
        # "recently" + "history" + "last session" → high confidence temporal
        c = classify_intent("show me the recent history from the last session")
        assert c.intent == "temporal_recall"
        assert c.confidence == "high"

    def test_all_valid_intents_reachable(self):
        """Each intent has at least one trigger pattern."""
        reachable = set()
        queries = [
            "when did this happen?",
            "what was the decision?",
            "how do we fix this?",
            "current progress status",
            "user preferences",
            "what is the architecture?",
            "pending unfinished tasks",
            "what is related to the router?",
        ]
        for q in queries:
            c = classify_intent(q)
            reachable.add(c.intent)
        assert reachable == VALID_RECALL_INTENTS


class TestCallerOverride:
    """Caller can explicitly set intent."""

    def test_valid_phase5_intent_accepted(self):
        c = classify_intent("anything", caller_intent="temporal_recall")
        assert c.intent == "temporal_recall"
        assert c.caller_override is True
        assert c.confidence == "high"

    def test_legacy_intent_mapped(self):
        c = classify_intent("anything", caller_intent="pattern_retrieval")
        assert c.intent == "procedural_recall"
        assert c.legacy_mapped is True
        assert c.caller_override is True

    def test_all_legacy_intents_mapped(self):
        for legacy, expected in _LEGACY_INTENT_MAP.items():
            c = classify_intent("test", caller_intent=legacy)
            assert c.intent == expected, f"{legacy} should map to {expected}"
            assert c.legacy_mapped is True

    def test_invalid_intent_falls_through(self):
        c = classify_intent("how do we fix this?", caller_intent="invalid_intent")
        # Should fall through to inference
        assert c.caller_override is False
        assert c.intent in VALID_RECALL_INTENTS


class TestFallbackBehavior:
    """Ambiguous or empty queries fall back conservatively."""

    def test_empty_query_falls_back(self):
        c = classify_intent("")
        assert c.intent == "factual_recall"
        assert c.fallback_used is True
        assert c.confidence == "low"

    def test_whitespace_query_falls_back(self):
        c = classify_intent("   ")
        assert c.intent == "factual_recall"
        assert c.fallback_used is True

    def test_no_matching_keywords_falls_back(self):
        c = classify_intent("xyzzy plugh")
        assert c.intent == "factual_recall"
        assert c.fallback_used is True
        assert c.confidence == "low"

    def test_ambiguous_query_low_confidence(self):
        # "recent fix" matches both temporal (recent) and procedural (fix)
        c = classify_intent("recent fix")
        assert c.intent in VALID_RECALL_INTENTS
        # With only 1 signal per intent (tie), confidence should be low
        assert c.confidence in ("low", "medium")


# ---------------------------------------------------------------------------
# Tests: Routing plan
# ---------------------------------------------------------------------------


class TestRoutingPlan:
    """Routing plan maps intent to adapters and blend mode."""

    def test_temporal_routes_to_working_and_memory(self):
        plan = get_routing_plan("temporal_recall")
        assert "state_working" in plan.primary_adapters
        assert "memory_file" in plan.primary_adapters
        assert "recency" in plan.ranking_emphasis

    def test_decision_routes_to_vault(self):
        plan = get_routing_plan("decision_recall")
        assert "obsidian_vault" in plan.primary_adapters
        assert plan.blend_mode == "fallback"

    def test_procedural_routes_to_memory_and_vault(self):
        plan = get_routing_plan("procedural_recall")
        assert "memory_file" in plan.primary_adapters
        assert "obsidian_vault" in plan.primary_adapters
        assert plan.blend_mode == "merge"

    def test_factual_merges_memory_and_vault(self):
        plan = get_routing_plan("factual_recall")
        assert plan.blend_mode == "merge"
        assert "memory_file" in plan.primary_adapters
        assert "obsidian_vault" in plan.primary_adapters

    def test_open_loop_with_fallback(self):
        plan = get_routing_plan("open_loop_recall")
        assert "state_working" in plan.primary_adapters
        assert plan.blend_mode == "fallback"
        assert len(plan.secondary_adapters) > 0

    def test_every_intent_has_routing(self):
        for intent in VALID_RECALL_INTENTS:
            plan = get_routing_plan(intent)
            assert plan.intent == intent
            assert len(plan.primary_adapters) > 0

    def test_invalid_intent_raises(self):
        with pytest.raises(ValueError, match="Invalid recall intent"):
            get_routing_plan("not_a_real_intent")

    def test_scope_override_constrains_adapters(self):
        plan = get_routing_plan("factual_recall", scope_override="vault")
        assert plan.primary_adapters == ["obsidian_vault"]
        assert plan.secondary_adapters == []
        assert plan.blend_mode == "none"

    def test_all_adapters_returns_combined(self):
        plan = get_routing_plan("decision_recall")
        all_a = plan.all_adapters()
        assert "obsidian_vault" in all_a
        assert "memory_file" in all_a


# ---------------------------------------------------------------------------
# Tests: Multi-factor ranking
# ---------------------------------------------------------------------------


class TestMultiFactorRanking:
    """Ranking uses recency, confidence, source authority, and more."""

    def test_high_confidence_ranks_higher(self):
        high = _sample_result(confidence="high")
        low = _sample_result(confidence="low")
        score_h, _ = compute_ranking_score(high)
        score_l, _ = compute_ranking_score(low)
        assert score_h > score_l

    def test_recent_ranks_higher(self):
        recent = _sample_result(timestamp="2026-03-13T12:00:00Z")
        old = _sample_result(timestamp="2025-01-01T00:00:00Z")
        score_r, _ = compute_ranking_score(recent)
        score_o, _ = compute_ranking_score(old)
        assert score_r > score_o

    def test_vault_source_has_higher_authority(self):
        vault = _sample_result(_source_adapter="obsidian_vault")
        working = _sample_result(_source_adapter="state_working")
        _, factors_v = compute_ranking_score(vault)
        _, factors_w = compute_ranking_score(working)
        assert factors_v["source_authority"] > factors_w["source_authority"]

    def test_emphasis_boosts_factor(self):
        r = _sample_result()
        score_base, _ = compute_ranking_score(r, emphasis=[])
        score_boosted, _ = compute_ranking_score(r, emphasis=["recency", "confidence"])
        assert score_boosted > score_base

    def test_factor_breakdown_returned(self):
        score, factors = compute_ranking_score(_sample_result())
        assert "recency" in factors
        assert "confidence" in factors
        assert "source_authority" in factors
        assert "relevance_score" in factors
        assert "promotion_level" in factors
        assert score > 0

    def test_missing_timestamp_gets_middling_recency(self):
        r = _sample_result()
        del r["timestamp"]
        _, factors = compute_ranking_score(r)
        assert factors["recency"] == 0.3

    def test_scores_bounded(self):
        for conf in ("high", "medium", "low"):
            for source in ("obsidian_vault", "memory_file", "state_working"):
                r = _sample_result(confidence=conf, _source_adapter=source)
                score, factors = compute_ranking_score(r)
                assert score >= 0
                for v in factors.values():
                    assert 0.0 <= v <= 1.0


class TestRankResults:
    """rank_results sorts, annotates, and explains."""

    def test_results_sorted_by_score(self):
        results = [
            _sample_result(confidence="low", _relevance_score=0.5),
            _sample_result(confidence="high", _relevance_score=5.0),
        ]
        c = classify_intent("how to fix", caller_intent="procedural_recall")
        plan = get_routing_plan("procedural_recall")
        ranked = rank_results(results, c, plan)
        assert ranked[0]["_ranking_score"] >= ranked[1]["_ranking_score"]

    def test_explanation_metadata_attached(self):
        results = [_sample_result()]
        c = classify_intent("test", caller_intent="factual_recall")
        plan = get_routing_plan("factual_recall")
        ranked = rank_results(results, c, plan)
        expl = ranked[0]["_recall_explanation"]
        assert expl["matched_intent"] == "factual_recall"
        assert "matched_source" in expl
        assert "why_matched" in expl
        assert "why_ranked_high" in expl
        assert "ranking_factors_used" in expl

    def test_fallback_noted_in_explanation(self):
        results = [_sample_result()]
        c = classify_intent("")  # fallback
        plan = get_routing_plan("factual_recall")
        ranked = rank_results(results, c, plan)
        expl = ranked[0]["_recall_explanation"]
        assert any("fallback" in n for n in expl.get("degradation_notes", []))


# ---------------------------------------------------------------------------
# Tests: Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Blended retrieval deduplicates across adapters."""

    def test_same_memory_id_deduped(self):
        results = [
            _sample_result(memory_id="cm-abc-123", _ranking_score=0.5),
            _sample_result(memory_id="cm-abc-123", _ranking_score=0.8),
        ]
        deduped = dedupe_results(results)
        assert len(deduped) == 1
        assert deduped[0]["_ranking_score"] == 0.8  # higher score kept

    def test_same_path_deduped(self):
        results = [
            _sample_result(path="30-workflow-learnings/note.md", _ranking_score=0.3),
            _sample_result(path="30-workflow-learnings/note.md", _ranking_score=0.7),
        ]
        deduped = dedupe_results(results)
        assert len(deduped) == 1

    def test_different_results_preserved(self):
        results = [
            _sample_result(memory_id="cm-abc-123"),
            _sample_result(memory_id="cm-def-456"),
        ]
        deduped = dedupe_results(results)
        assert len(deduped) == 2

    def test_no_key_results_with_different_sources_preserved(self):
        r1 = _sample_result(_source_adapter="memory_file")
        r2 = _sample_result(_source_adapter="obsidian_vault")
        # Remove primary dedup keys, different source → different composite key
        for r in (r1, r2):
            r.pop("memory_id", None)
            r.pop("path", None)
            r["title"] = ""
        deduped = dedupe_results([r1, r2])
        assert len(deduped) == 2  # different composite keys → both kept

    def test_identical_no_key_results_deduped(self):
        r1 = _sample_result(_ranking_score=0.5)
        r2 = _sample_result(_ranking_score=0.8)
        for r in (r1, r2):
            r.pop("memory_id", None)
            r.pop("path", None)
            r["title"] = ""
        deduped = dedupe_results([r1, r2])
        assert len(deduped) == 1  # same composite key → deduped


# ---------------------------------------------------------------------------
# Tests: Router integration (end-to-end intent-aware recall)
# ---------------------------------------------------------------------------


class TestRouterIntentAwareRecall:
    """Router recall() is now Phase 5 intent-aware."""

    def test_legacy_intent_works(self, tmp_path):
        """Backward compat: legacy 'pattern_retrieval' still works."""
        router = _make_router(tmp_path)
        result = router.recall(
            "test query",
            intent="pattern_retrieval",
            task_class="code_impl",
            keywords=["test"],
        )
        assert isinstance(result, RecallResult)
        # Intent is mapped to Phase 5
        assert result.intent == "procedural_recall"

    def test_phase5_intent_works(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.recall(
            "how to validate schemas?",
            intent="procedural_recall",
            task_class="code_impl",
            keywords=["schema"],
        )
        assert isinstance(result, RecallResult)
        assert result.intent == "procedural_recall"

    def test_temporal_recall_routes_to_working(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.recall(
            "what happened in the last session?",
            intent="temporal_recall",
        )
        assert "state_working" in result.sources_queried

    def test_scope_override_respected(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.recall(
            "test",
            intent="factual_recall",
            scope="memory_files",
        )
        assert result.sources_queried == ["memory_file"]

    def test_results_have_ranking_score(self, tmp_path):
        """Results from intent-aware recall include ranking metadata."""
        import json

        # Create an artifact so there are results
        wl = tmp_path / "workflow_learnings"
        wl.mkdir(parents=True, exist_ok=True)
        artifact = {
            "task_class": "code_impl",
            "task_summary": "Implemented vault schema validation",
            "reusable_guidance": "Use schema-first approach",
            "confidence": "high",
            "key_decisions": ["schema validation"],
            "created_at": "2026-03-13T10:00:00Z",
        }
        (wl / "mem_test_1234.json").write_text(json.dumps(artifact))

        router = _make_router(tmp_path)
        result = router.recall(
            "vault schema",
            intent="procedural_recall",
            task_class="code_impl",
            keywords=["vault", "schema"],
        )
        assert result.total_found >= 1
        r = result.results[0]
        assert "_ranking_score" in r
        assert "_ranking_factors" in r
        assert "_recall_explanation" in r

    def test_results_have_explanation(self, tmp_path):
        """Each result has explanation metadata."""
        import json

        wl = tmp_path / "workflow_learnings"
        wl.mkdir(parents=True, exist_ok=True)
        artifact = {
            "task_class": "code_impl",
            "task_summary": "Test task",
            "reusable_guidance": "Test guidance",
            "confidence": "high",
            "created_at": "2026-03-13T10:00:00Z",
        }
        (wl / "mem_test_5678.json").write_text(json.dumps(artifact))

        router = _make_router(tmp_path)
        result = router.recall(
            "test task",
            intent="factual_recall",
            keywords=["test"],
        )
        if result.total_found >= 1:
            expl = result.results[0]["_recall_explanation"]
            assert expl["matched_intent"] == "factual_recall"
            assert "matched_source" in expl
            assert "ranking_factors_used" in expl

    def test_trace_includes_phase5_fields(self, tmp_path):
        router = _make_router(tmp_path)
        ctx = TraceContext.new("test")

        with patch("agents.memory_router.slog") as mock_slog:
            router.recall(
                "test query",
                intent="procedural_recall",
                caller="test",
                ctx=ctx,
            )
            mock_slog.event.assert_called()
            kw = mock_slog.event.call_args[1]
            assert kw["intent"] == "procedural_recall"
            assert "intent_confidence" in kw
            assert "intent_signals" in kw
            assert "blend_mode" in kw
            assert "ranking_emphasis" in kw

    def test_blended_recall_decision_with_fallback(self, tmp_path):
        """Decision recall falls back to secondary when primary returns nothing."""
        router = _make_router(tmp_path)
        # With no vault, decision_recall primary (obsidian_vault) returns nothing.
        # Fallback should try memory_file.
        result = router.recall(
            "what was decided about schema?",
            intent="decision_recall",
            keywords=["schema", "decision"],
        )
        # memory_file should be in sources_queried as a fallback
        assert "memory_file" in result.sources_queried

    def test_backward_compat_general_intent(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.recall("test", intent="general")
        assert result.intent == "factual_recall"

    def test_backward_compat_session_replay(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.recall("session", intent="session_replay")
        assert result.intent == "temporal_recall"
        assert "state_working" in result.sources_queried


# ---------------------------------------------------------------------------
# Tests: RecallExplanation structure
# ---------------------------------------------------------------------------


class TestRecallExplanation:
    """RecallExplanation has all required fields."""

    def test_to_dict(self):
        expl = RecallExplanation(
            matched_intent="procedural_recall",
            matched_source="memory_file",
            why_matched="keyword match",
            why_ranked_high="high confidence",
            ranking_factors_used={"confidence": 0.8, "recency": 0.5},
        )
        d = expl.to_dict()
        assert d["matched_intent"] == "procedural_recall"
        assert "ranking_factors_used" in d
        assert d["degradation_notes"] == []


# ---------------------------------------------------------------------------
# Tests: IntentClassification structure
# ---------------------------------------------------------------------------


class TestIntentClassificationStructure:
    """IntentClassification has all required fields and valid values."""

    def test_to_dict(self):
        c = classify_intent("how to fix?")
        d = c.to_dict()
        assert "intent" in d
        assert "confidence" in d
        assert "signals" in d
        assert "fallback_used" in d
        assert "caller_override" in d
        assert "legacy_mapped" in d

    def test_intent_always_valid(self):
        queries = [
            "when did we fix it?",
            "how to deploy?",
            "",
            "xyzzy",
            "what is the status?",
            "user preferences",
            "unfinished tasks",
            "related to the router",
        ]
        for q in queries:
            c = classify_intent(q)
            assert c.intent in VALID_RECALL_INTENTS, f"Query {q!r} → {c.intent}"
