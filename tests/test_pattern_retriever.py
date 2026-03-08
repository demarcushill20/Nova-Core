"""Tests for planner/pattern_retriever.py — Phase 7 agent-pattern retrieval."""

from unittest.mock import patch
import pytest

from planner.pattern_retriever import (
    is_eligible_for_pattern_retrieval,
    extract_pattern_keywords,
    search_agent_patterns,
    read_pattern_guidance,
    format_pattern_guidance,
    retrieve_pattern_guidance,
    _extract_title,
    _extract_sections,
    MAX_PATTERNS,
    MAX_PATTERN_CONTEXT_SIZE,
    _PATTERN_FOLDER,
    _GUIDANCE_SECTIONS,
)

# All vault tool patches target the source module
_VS = "tools.mcp_vault_server"

# Sample pattern note content for testing
_SAMPLE_PATTERN_NOTE = """---
type: agent-pattern
pattern_id: "ap-research-multi-query"
title: "Multi-Query Research Strategy"
agent_role: "research"
confidence: "high"
task_classes:
  - "research"
date_created: "2026-03-01"
source: "nova-core-memory"
tags:
  - "#type/pattern"
  - "#agent/research"
  - "#confidence/high"
  - "#status/active"
---

## Summary

Use multiple targeted queries instead of a single broad query when researching
complex topics. Each query should focus on a specific aspect.

## When to Apply

- Research tasks involving multi-faceted topics
- When initial broad queries return noisy results
- When the task requires cross-referencing multiple sources

## Pattern Steps

1. **Decompose the topic** — break into 2-4 specific sub-questions
2. **Query per sub-question** — one focused search per facet
3. **Cross-reference** — compare findings across sub-queries
4. **Synthesize** — merge into a coherent answer

## Success Indicators

- Reduced noise in search results
- Higher relevance scores

## Failure Modes

- **Over-decomposition** — too many sub-queries wastes time
- **Redundant queries** — overlapping sub-questions return duplicates

## Guidance

Start with 2-3 sub-queries. Only add more if initial results are insufficient.
Prefer specific noun phrases over generic verbs in query construction.
"""


# ===================================================================
# Eligibility
# ===================================================================

class TestEligibility:
    def test_research_eligible(self):
        ok, reason = is_eligible_for_pattern_retrieval("research", "find information")
        assert ok
        assert "eligible" in reason

    def test_code_impl_eligible(self):
        ok, reason = is_eligible_for_pattern_retrieval("code_impl", "implement feature")
        assert ok

    def test_code_review_eligible(self):
        ok, reason = is_eligible_for_pattern_retrieval("code_review", "review code")
        assert ok

    def test_simple_ineligible(self):
        ok, reason = is_eligible_for_pattern_retrieval("simple", "hello")
        assert not ok
        assert "ineligible" in reason

    def test_unknown_ineligible(self):
        ok, reason = is_eligible_for_pattern_retrieval("unknown", "test")
        assert not ok

    def test_system_ineligible(self):
        ok, reason = is_eligible_for_pattern_retrieval("system", "configure")
        assert not ok

    def test_runtime_state_always_skipped(self):
        ok, reason = is_eligible_for_pattern_retrieval(
            "research", "check systemctl status"
        )
        assert not ok
        assert "runtime_state" in reason

    def test_runtime_watcher_skipped(self):
        ok, reason = is_eligible_for_pattern_retrieval(
            "code_impl", "restart the watcher daemon"
        )
        assert not ok


# ===================================================================
# Keyword extraction
# ===================================================================

class TestKeywordExtraction:
    def test_basic_extraction(self):
        kws = extract_pattern_keywords("implement vault search tokenized")
        assert "tokenized" in kws
        assert "implement" in kws

    def test_stopwords_filtered(self):
        kws = extract_pattern_keywords("the and but is are")
        assert len(kws) == 0

    def test_sorted_by_length(self):
        kws = extract_pattern_keywords("cat verification implementation")
        assert kws == sorted(kws, key=len, reverse=True)

    def test_max_capped(self):
        text = " ".join(f"word{i}longterm" for i in range(20))
        kws = extract_pattern_keywords(text, max_keywords=3)
        assert len(kws) <= 3

    def test_empty_text(self):
        assert extract_pattern_keywords("") == []


# ===================================================================
# Title extraction
# ===================================================================

class TestTitleExtraction:
    def test_from_frontmatter(self):
        title = _extract_title(_SAMPLE_PATTERN_NOTE)
        assert title == "Multi-Query Research Strategy"

    def test_from_heading(self):
        content = "# My Pattern\n\nSome content"
        assert _extract_title(content) == "My Pattern"

    def test_fallback(self):
        assert _extract_title("no title here") == "Untitled pattern"


# ===================================================================
# Section extraction
# ===================================================================

class TestSectionExtraction:
    def test_extracts_summary(self):
        sections = _extract_sections(_SAMPLE_PATTERN_NOTE)
        assert "Summary" in sections
        assert "multiple targeted queries" in sections["Summary"]

    def test_extracts_when_to_apply(self):
        sections = _extract_sections(_SAMPLE_PATTERN_NOTE)
        assert "When to Apply" in sections

    def test_extracts_pattern_steps(self):
        sections = _extract_sections(_SAMPLE_PATTERN_NOTE)
        assert "Pattern Steps" in sections
        assert "Decompose" in sections["Pattern Steps"]

    def test_extracts_failure_modes(self):
        sections = _extract_sections(_SAMPLE_PATTERN_NOTE)
        assert "Failure Modes" in sections

    def test_extracts_guidance(self):
        sections = _extract_sections(_SAMPLE_PATTERN_NOTE)
        assert "Guidance" in sections
        assert "sub-queries" in sections["Guidance"]

    def test_empty_content(self):
        sections = _extract_sections("")
        assert sections == {}

    def test_no_matching_sections(self):
        sections = _extract_sections("Just some random text\nwithout headers")
        assert sections == {}


# ===================================================================
# Pattern search
# ===================================================================

class TestSearchAgentPatterns:
    def test_search_scoped_to_pattern_folder(self):
        """vault_search is called with the pattern folder."""
        calls = []

        def mock_vault_search(query, folder=""):
            calls.append({"query": query, "folder": folder})
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            search_agent_patterns(["research", "strategy"], "research")

        assert len(calls) == 1
        assert calls[0]["folder"] == _PATTERN_FOLDER

    def test_returns_empty_on_no_hits(self):
        def mock_vault_search(query, folder=""):
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = search_agent_patterns(["test"], "research")
        assert result == []

    def test_caps_results(self):
        def mock_vault_search(query, folder=""):
            return {
                "results_count": 5,
                "results": [
                    {"path": f"20-agent-patterns/p{i}.md", "snippet": f"pat {i}"}
                    for i in range(5)
                ],
            }

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = search_agent_patterns(["test"], "research", max_results=2)
        assert len(result) == 2

    def test_deduplicates(self):
        def mock_vault_search(query, folder=""):
            return {
                "results_count": 3,
                "results": [
                    {"path": "20-agent-patterns/a.md", "snippet": "a"},
                    {"path": "20-agent-patterns/a.md", "snippet": "a dup"},
                    {"path": "20-agent-patterns/b.md", "snippet": "b"},
                ],
            }

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = search_agent_patterns(["test"], "research", max_results=5)
        paths = [r["path"] for r in result]
        assert len(paths) == len(set(paths))

    def test_empty_keywords(self):
        result = search_agent_patterns([], "research")
        assert result == []


# ===================================================================
# Read pattern guidance
# ===================================================================

class TestReadPatternGuidance:
    def test_reads_and_extracts(self):
        def mock_vault_read(path):
            return {"path": path, "size": 100, "content": _SAMPLE_PATTERN_NOTE}

        with patch(f"{_VS}.vault_read", mock_vault_read):
            result = read_pattern_guidance("20-agent-patterns/test.md")

        assert result is not None
        assert result["title"] == "Multi-Query Research Strategy"
        assert "Summary" in result["sections"]
        assert "Guidance" in result["sections"]

    def test_returns_none_on_error(self):
        def mock_vault_read(path):
            return {"error": "not found"}

        with patch(f"{_VS}.vault_read", mock_vault_read):
            result = read_pattern_guidance("20-agent-patterns/missing.md")
        assert result is None

    def test_returns_none_on_empty_content(self):
        def mock_vault_read(path):
            return {"path": path, "size": 0, "content": ""}

        with patch(f"{_VS}.vault_read", mock_vault_read):
            result = read_pattern_guidance("20-agent-patterns/empty.md")
        assert result is None

    def test_returns_none_on_no_sections(self):
        def mock_vault_read(path):
            return {"path": path, "size": 10, "content": "No sections here"}

        with patch(f"{_VS}.vault_read", mock_vault_read):
            result = read_pattern_guidance("20-agent-patterns/nosections.md")
        assert result is None


# ===================================================================
# Context formatting
# ===================================================================

class TestFormatPatternGuidance:
    def test_formats_single_pattern(self):
        patterns = [{
            "title": "Test Pattern",
            "path": "20-agent-patterns/test.md",
            "sections": {"Summary": "A useful method", "Guidance": "Do X then Y"},
        }]
        result = format_pattern_guidance(patterns)
        assert "Agent Pattern Guidance" in result
        assert "advisory" in result
        assert "Test Pattern" in result
        assert "A useful method" in result
        assert "Do X then Y" in result

    def test_empty_patterns(self):
        assert format_pattern_guidance([]) == ""

    def test_advisory_label(self):
        patterns = [{
            "title": "Test",
            "path": "test.md",
            "sections": {"Summary": "test"},
        }]
        result = format_pattern_guidance(patterns)
        assert "not runtime facts" in result
        assert "advisory" in result

    def test_truncation(self):
        # Generate a very large pattern to trigger truncation
        big_sections = {s: "x" * 500 for s in _GUIDANCE_SECTIONS}
        patterns = [
            {"title": f"Pat {i}", "path": f"p{i}.md", "sections": big_sections}
            for i in range(3)
        ]
        result = format_pattern_guidance(patterns)
        assert len(result) <= MAX_PATTERN_CONTEXT_SIZE


# ===================================================================
# Main integration function
# ===================================================================

class TestRetrievePatternGuidance:
    def test_ineligible_class_skips(self):
        result = retrieve_pattern_guidance("simple", "hello world")
        assert not result["pattern_retrieval_activated"]
        assert "ineligible" in result["retrieval_reason"]

    def test_no_keywords_skips(self):
        result = retrieve_pattern_guidance("research", "go")
        assert not result["pattern_retrieval_activated"]
        assert "no_keywords" in result["retrieval_reason"]

    def test_no_patterns_found(self):
        def mock_vault_search(query, folder=""):
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = retrieve_pattern_guidance(
                "research", "multi-query research strategy"
            )
        assert not result["pattern_retrieval_activated"]
        assert "no_patterns_found" in result["retrieval_reason"]

    def test_successful_retrieval(self):
        def mock_vault_search(query, folder=""):
            return {
                "results_count": 1,
                "results": [{
                    "path": "20-agent-patterns/test.md",
                    "snippet": "research strategy",
                }],
            }

        def mock_vault_read(path):
            return {"path": path, "size": 100, "content": _SAMPLE_PATTERN_NOTE}

        with patch(f"{_VS}.vault_search", mock_vault_search), \
             patch(f"{_VS}.vault_read", mock_vault_read):
            result = retrieve_pattern_guidance(
                "research", "multi-query research strategy"
            )

        assert result["pattern_retrieval_activated"]
        assert len(result["selected_patterns"]) == 1
        assert result["selected_patterns"][0]["title"] == "Multi-Query Research Strategy"
        assert "Agent Pattern Guidance" in result["pattern_guidance_context"]
        assert result["matched_pattern_paths"] == ["20-agent-patterns/test.md"]

    def test_no_extractable_guidance(self):
        """Pattern found but has no extractable sections."""
        def mock_vault_search(query, folder=""):
            return {
                "results_count": 1,
                "results": [{
                    "path": "20-agent-patterns/bad.md",
                    "snippet": "no sections",
                }],
            }

        def mock_vault_read(path):
            return {"path": path, "size": 10, "content": "No useful sections here"}

        with patch(f"{_VS}.vault_search", mock_vault_search), \
             patch(f"{_VS}.vault_read", mock_vault_read):
            result = retrieve_pattern_guidance(
                "research", "multi-query research strategy"
            )

        assert not result["pattern_retrieval_activated"]
        assert "no_extractable_guidance" in result["retrieval_reason"]

    def test_result_structure(self):
        result = retrieve_pattern_guidance("simple", "test")
        assert "pattern_retrieval_activated" in result
        assert "retrieval_reason" in result
        assert "search_queries" in result
        assert "matched_pattern_paths" in result
        assert "selected_patterns" in result
        assert "pattern_guidance_context" in result
        assert "errors" in result

    def test_runtime_state_skips(self):
        result = retrieve_pattern_guidance(
            "research", "check watcher pid and systemctl status"
        )
        assert not result["pattern_retrieval_activated"]
        assert "runtime_state" in result["retrieval_reason"]


# ===================================================================
# Orchestrator integration
# ===================================================================

class TestOrchestratorIntegration:
    def test_pattern_retriever_imported(self):
        """orchestrator_adapter imports retrieve_pattern_guidance."""
        import tools.orchestrator_adapter as oa
        from planner.pattern_retriever import retrieve_pattern_guidance
        assert callable(retrieve_pattern_guidance)

    def test_pattern_guidance_in_build_plan(self):
        """build_plan_from_task includes pattern retrieval call."""
        import tools.orchestrator_adapter as oa
        import inspect
        source = inspect.getsource(oa.build_plan_from_task)
        assert "retrieve_pattern_guidance" in source
        assert "pattern_guidance" in source

    def test_step_executor_includes_pattern_section(self):
        """Step executor prompt includes pattern guidance."""
        import tools.orchestrator_adapter as oa
        import inspect
        source = inspect.getsource(oa._claude_step_executor)
        assert "pattern_guidance" in source
        assert "pattern_section" in source
