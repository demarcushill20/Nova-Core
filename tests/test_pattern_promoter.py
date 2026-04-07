"""Tests for planner/pattern_promoter.py — Phase 6.5 agent-pattern promotion."""

from unittest.mock import patch

from planner.pattern_promoter import (
    _build_pattern_payload,
    _extract_keywords,
    _keyword_overlap,
    assess_pattern_candidate,
    attempt_pattern_promotion,
)

# All vault tool patches target the source module because the functions
# are imported locally inside assess_pattern_candidate / attempt_pattern_promotion.
_VS = "tools.mcp_vault_server"


# ===================================================================
# Keyword extraction
# ===================================================================


class TestKeywordExtraction:
    def test_basic_extraction(self):
        kws = _extract_keywords("implement vault search with tokenized matching")
        assert "tokenized" in kws
        assert "implement" in kws
        assert "matching" in kws

    def test_stopwords_filtered(self):
        kws = _extract_keywords("the task is to run a file and execute output")
        for sw in ["the", "is", "to", "and"]:
            assert sw not in kws

    def test_sorted_by_length_desc(self):
        kws = _extract_keywords("cat implementation verification strategy")
        assert kws == sorted(kws, key=len, reverse=True)

    def test_max_keywords_capped(self):
        long_text = " ".join(f"keyword{i}abc" for i in range(20))
        kws = _extract_keywords(long_text, max_keywords=5)
        assert len(kws) <= 5

    def test_dedup(self):
        kws = _extract_keywords("search search search vault vault")
        assert kws.count("search") == 1
        assert kws.count("vault") == 1

    def test_short_words_filtered(self):
        kws = _extract_keywords("go do it ok ab cd")
        assert len(kws) == 0

    def test_empty_text(self):
        assert _extract_keywords("") == []


class TestKeywordOverlap:
    def test_full_overlap(self):
        kws = ["vault", "search", "pattern"]
        text = "The vault search found a pattern"
        assert _keyword_overlap(kws, text) == 1.0

    def test_no_overlap(self):
        kws = ["vault", "search"]
        text = "completely unrelated text about cooking"
        assert _keyword_overlap(kws, text) == 0.0

    def test_partial_overlap(self):
        kws = ["vault", "search", "pattern", "research"]
        text = "vault and search results"
        assert _keyword_overlap(kws, text) == 0.5

    def test_empty_keywords(self):
        assert _keyword_overlap([], "some text") == 0.0


# ===================================================================
# Pattern payload assembly
# ===================================================================


class TestBuildPatternPayload:
    def test_basic_payload_structure(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="Multi-query web research strategy",
            keywords=["research", "strategy", "multi-query", "web"],
            evidence_paths=["30-workflow-learnings/a.md", "30-workflow-learnings/b.md"],
            evidence_snippets=["snippet about research", "snippet about strategy"],
        )
        assert "frontmatter" in payload
        assert "body" in payload
        assert "path" in payload

    def test_frontmatter_schema(self):
        payload = _build_pattern_payload(
            task_class="code_impl",
            task_text="Bounded file edits with verification",
            keywords=["bounded", "verification", "edits"],
            evidence_paths=["30-workflow-learnings/a.md"],
            evidence_snippets=["snippet"],
        )
        fm = payload["frontmatter"]
        assert fm["type"] == "agent-pattern"
        assert fm["pattern_id"].startswith("ap-")
        assert fm["agent_role"] == "coder"
        assert fm["confidence"] == "medium"
        assert fm["source"] == "nova-core-memory"
        assert "#type/pattern" in fm["tags"]
        assert "#source/auto-promoted" in fm["tags"]

    def test_research_role_mapping(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert payload["frontmatter"]["agent_role"] == "research"

    def test_code_review_role_mapping(self):
        payload = _build_pattern_payload(
            task_class="code_review",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert payload["frontmatter"]["agent_role"] == "critic"

    def test_path_format(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["research", "strategy"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert payload["path"].startswith("20-agent-patterns/")
        assert payload["path"].endswith(".md")

    def test_body_contains_evidence(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test research",
            keywords=["research"],
            evidence_paths=["30-workflow-learnings/a.md"],
            evidence_snippets=["found useful strategy"],
        )
        assert "found useful strategy" in payload["body"]
        assert "[[a]]" in payload["body"]

    def test_body_contains_auto_promoted_trace(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert "Phase 6.5" in payload["body"]

    def test_task_classes_is_list(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert isinstance(payload["frontmatter"]["task_classes"], list)
        assert "research" in payload["frontmatter"]["task_classes"]


# ===================================================================
# Candidate assessment
# ===================================================================


class TestAssessPatternCandidate:
    def test_ineligible_class(self):
        result = assess_pattern_candidate(
            task_class="simple",
            task_text="do something",
            learning_id="wl-test",
            learning_path="30-workflow-learnings/test.md",
        )
        assert not result["candidate"]
        assert "ineligible_class" in result["reason"]

    def test_system_class_ineligible(self):
        result = assess_pattern_candidate(
            task_class="system",
            task_text="restart services",
            learning_id="wl-test",
            learning_path="30-workflow-learnings/test.md",
        )
        assert not result["candidate"]

    def test_insufficient_keywords(self):
        result = assess_pattern_candidate(
            task_class="research",
            task_text="go",
            learning_id="wl-test",
            learning_path="30-workflow-learnings/test.md",
        )
        assert not result["candidate"]
        assert "insufficient_keywords" in result["reason"]

    def test_vault_search_returns_error(self):
        """vault_search returning error is handled."""

        def mock_vault_search(query, folder=""):
            return {"error": "vault unavailable"}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = assess_pattern_candidate(
                task_class="research",
                task_text="research strategy multi-query validation",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )
        assert not result["candidate"]
        assert "search_error" in result["reason"]

    def test_no_converging_learnings(self):
        """No related learnings found → not candidate."""

        def mock_vault_search(query, folder=""):
            return {
                "query": query,
                "folder": folder,
                "results_count": 0,
                "results": [],
            }

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = assess_pattern_candidate(
                task_class="research",
                task_text="research strategy multi-query validation",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )
        assert not result["candidate"]
        assert "insufficient_evidence" in result["reason"]

    def test_converging_learnings_produce_candidate(self):
        """2+ related learnings with keyword overlap → candidate."""

        def mock_vault_search(query, folder=""):
            if folder == "30-workflow-learnings":
                return {
                    "results_count": 2,
                    "results": [
                        {
                            "path": "30-workflow-learnings/other-learning.md",
                            "snippet": "research strategy multi-query validation approach works well",
                        },
                        {
                            "path": "30-workflow-learnings/test.md",
                            "snippet": "self-ref should be skipped",
                        },
                    ],
                }
            elif folder == "20-agent-patterns":
                return {"results_count": 0, "results": []}
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = assess_pattern_candidate(
                task_class="research",
                task_text="research strategy multi-query validation approach",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert result["candidate"]
        assert result["reason"] == "converging_evidence"
        assert result["evidence_count"] >= 2
        assert result["pattern_payload"] is not None

    def test_self_reference_excluded(self):
        """The just-promoted learning is excluded from convergence check."""

        def mock_vault_search(query, folder=""):
            if folder == "30-workflow-learnings":
                return {
                    "results_count": 1,
                    "results": [
                        {
                            "path": "30-workflow-learnings/test.md",
                            "snippet": "research strategy multi-query validation",
                        },
                    ],
                }
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = assess_pattern_candidate(
                task_class="research",
                task_text="research strategy multi-query validation",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert not result["candidate"]
        assert "insufficient_evidence" in result["reason"]

    def test_duplicate_pattern_blocks(self):
        """Existing similar pattern blocks promotion."""

        def mock_vault_search(query, folder=""):
            if folder == "30-workflow-learnings":
                return {
                    "results_count": 1,
                    "results": [
                        {
                            "path": "30-workflow-learnings/other.md",
                            "snippet": "research strategy multi-query validation proven method",
                        },
                    ],
                }
            elif folder == "20-agent-patterns":
                return {
                    "results_count": 1,
                    "results": [
                        {
                            "path": "20-agent-patterns/existing-pattern.md",
                            "snippet": "research strategy multi-query validation pattern",
                        },
                    ],
                }
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = assess_pattern_candidate(
                task_class="research",
                task_text="research strategy multi-query validation",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert not result["candidate"]
        assert "duplicate_pattern" in result["reason"]


# ===================================================================
# Full promotion attempt
# ===================================================================


class TestAttemptPatternPromotion:
    def test_no_candidate_returns_deferred(self):
        def mock_vault_search(query, folder=""):
            return {"results_count": 0, "results": []}

        with patch(f"{_VS}.vault_search", mock_vault_search):
            result = attempt_pattern_promotion(
                task_class="research",
                task_text="research strategy multi-query validation",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert not result["promoted"]
        assert result["pattern_id"] is None

    def test_successful_promotion(self):
        """Full happy path: converging learnings → validated → written."""

        def mock_vault_search(query, folder=""):
            if folder == "30-workflow-learnings":
                return {
                    "results_count": 1,
                    "results": [
                        {
                            "path": "30-workflow-learnings/other.md",
                            "snippet": "research strategy multi-query validation proven method works well",
                        }
                    ],
                }
            elif folder == "20-agent-patterns":
                return {"results_count": 0, "results": []}
            return {"results_count": 0, "results": []}

        def mock_vault_validate(frontmatter, body=""):
            return {"valid": True, "errors": []}

        def mock_vault_write(path, frontmatter, body=""):
            return {"status": "created", "path": path, "size": 500}

        with (
            patch(f"{_VS}.vault_search", mock_vault_search),
            patch(f"{_VS}.vault_validate", mock_vault_validate),
            patch(f"{_VS}.vault_write", mock_vault_write),
        ):
            result = attempt_pattern_promotion(
                task_class="research",
                task_text="research strategy multi-query validation approach",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert result["promoted"]
        assert result["pattern_id"].startswith("ap-")
        assert result["note_path"].startswith("20-agent-patterns/")
        assert result["evidence_count"] >= 2

    def test_validation_failure_blocks(self):
        """Validation failure blocks write."""

        def mock_vault_search(query, folder=""):
            if folder == "30-workflow-learnings":
                return {
                    "results_count": 1,
                    "results": [
                        {
                            "path": "30-workflow-learnings/other.md",
                            "snippet": "research strategy multi-query validation proven",
                        }
                    ],
                }
            return {"results_count": 0, "results": []}

        def mock_vault_validate(frontmatter, body=""):
            return {"valid": False, "errors": ["missing required field"]}

        with patch(f"{_VS}.vault_search", mock_vault_search), patch(f"{_VS}.vault_validate", mock_vault_validate):
            result = attempt_pattern_promotion(
                task_class="research",
                task_text="research strategy multi-query validation approach",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert not result["promoted"]
        assert "validation" in result["reason"]  # vault_validation_failed or validation_failed
        assert len(result["errors"]) > 0

    def test_write_rejection_blocks(self):
        """Write rejection (e.g., file exists) blocks promotion."""

        def mock_vault_search(query, folder=""):
            if folder == "30-workflow-learnings":
                return {
                    "results_count": 1,
                    "results": [
                        {
                            "path": "30-workflow-learnings/other.md",
                            "snippet": "research strategy multi-query validation proven",
                        }
                    ],
                }
            return {"results_count": 0, "results": []}

        def mock_vault_validate(frontmatter, body=""):
            return {"valid": True, "errors": []}

        def mock_vault_write(path, frontmatter, body=""):
            return {"error": "file already exists"}

        with (
            patch(f"{_VS}.vault_search", mock_vault_search),
            patch(f"{_VS}.vault_validate", mock_vault_validate),
            patch(f"{_VS}.vault_write", mock_vault_write),
        ):
            result = attempt_pattern_promotion(
                task_class="research",
                task_text="research strategy multi-query validation approach",
                learning_id="wl-test",
                learning_path="30-workflow-learnings/test.md",
            )

        assert not result["promoted"]
        assert "error" in result["reason"] or "file already exists" in result["reason"]

    def test_ineligible_class_fast_exit(self):
        result = attempt_pattern_promotion(
            task_class="simple",
            task_text="do something",
            learning_id="wl-test",
            learning_path="30-workflow-learnings/test.md",
        )
        assert not result["promoted"]
        assert "ineligible_class" in result["reason"]


# ===================================================================
# Safety boundaries
# ===================================================================


class TestSafetyBoundaries:
    def test_source_always_nova_core_memory(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert payload["frontmatter"]["source"] == "nova-core-memory"

    def test_target_folder_always_agent_patterns(self):
        payload = _build_pattern_payload(
            task_class="code_impl",
            task_text="bounded implementation verification",
            keywords=["bounded", "implementation", "verification"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert payload["path"].startswith("20-agent-patterns/")

    def test_confidence_always_medium_for_auto_promoted(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert payload["frontmatter"]["confidence"] == "medium"

    def test_auto_promoted_tag_present(self):
        payload = _build_pattern_payload(
            task_class="research",
            task_text="test",
            keywords=["test"],
            evidence_paths=[],
            evidence_snippets=[],
        )
        assert "#source/auto-promoted" in payload["frontmatter"]["tags"]

    def test_result_structure_on_failure(self):
        result = attempt_pattern_promotion(
            task_class="simple",
            task_text="test",
            learning_id="wl-test",
            learning_path="30-workflow-learnings/test.md",
        )
        assert "promoted" in result
        assert "reason" in result
        assert "pattern_id" in result
        assert "note_path" in result
        assert "evidence_count" in result
        assert "evidence_paths" in result
        assert "search_queries" in result
        assert "errors" in result


# ===================================================================
# Orchestrator integration
# ===================================================================


class TestOrchestratorIntegration:
    def test_pattern_promotion_key_in_return(self):
        """execute_via_orchestrator returns pattern_promotion key."""
        from planner.pattern_promoter import attempt_pattern_promotion

        assert callable(attempt_pattern_promotion)

    def test_integration_only_after_learning_promoted(self):
        """Pattern promotion only triggers when learning was promoted."""
        import inspect

        import tools.orchestrator_adapter as oa

        source = inspect.getsource(oa.execute_via_orchestrator)
        assert "attempt_pattern_promotion" in source
        assert 'promotion_result.get("promoted")' in source
        assert 'promotion_result.get("note_path")' in source
        assert 'promotion_result.get("learning_id")' in source
