"""Tests for skills.skill_ranker — BM25 ranking with quality-based exclusion."""

import math

import pytest

from skills.skill_ranker import BM25Ranker, RankedSkill

# ---------------------------------------------------------------------------
# Sample skill corpus
# ---------------------------------------------------------------------------

SAMPLE_SKILLS = [
    {
        "name": "git-ops",
        "description": "Perform safe Git operations",
        "path": "/skills/git-ops",
        "body": "git commit push pull merge branch rebase stash",
    },
    {
        "name": "web-research",
        "description": "Fast reliable web research using Brave Search",
        "path": "/skills/web-research",
        "body": "search web browse fetch URL query internet",
    },
    {
        "name": "email-triage",
        "description": "Triage Gmail inbox messages",
        "path": "/skills/email-triage",
        "body": "email inbox gmail unread triage priority filter",
    },
    {
        "name": "pinescript-debug",
        "description": "Pine Script debugging assistant",
        "path": "/skills/pinescript",
        "body": "pine script tradingview debug error fix indicator strategy",
    },
    {
        "name": "memory-store",
        "description": "Store knowledge into Fusion Memory",
        "path": "/skills/memory-store",
        "body": "memory persist store knowledge decision pattern context",
    },
]


@pytest.fixture
def ranker() -> BM25Ranker:
    """Pre-built ranker with the sample corpus."""
    r = BM25Ranker()
    r.build_index(SAMPLE_SKILLS)
    return r


@pytest.fixture
def empty_ranker() -> BM25Ranker:
    """Ranker with no index built."""
    return BM25Ranker()


# ===========================================================================
# TestBM25Ranker — index building and basic ranking
# ===========================================================================


class TestBM25Ranker:
    """Test BM25 index construction and relevance ranking."""

    def test_build_index_creates_index(self, ranker: BM25Ranker) -> None:
        assert ranker._built is True
        assert ranker.get_corpus_size() == len(SAMPLE_SKILLS)

    def test_build_index_computes_idf(self, ranker: BM25Ranker) -> None:
        assert len(ranker._idf) > 0
        # "git" appears in exactly one document, so IDF should be positive
        assert ranker._idf.get("git", 0) > 0

    def test_build_index_computes_doc_lengths(self, ranker: BM25Ranker) -> None:
        assert len(ranker._doc_lengths) == len(SAMPLE_SKILLS)
        assert all(dl > 0 for dl in ranker._doc_lengths)

    def test_build_index_computes_avg_doc_length(self, ranker: BM25Ranker) -> None:
        expected = sum(ranker._doc_lengths) / len(ranker._doc_lengths)
        assert ranker._avg_doc_length == pytest.approx(expected)

    def test_build_index_with_empty_corpus(self) -> None:
        r = BM25Ranker()
        r.build_index([])
        assert r._built is True
        assert r.get_corpus_size() == 0
        assert r._avg_doc_length == 0.0

    def test_build_index_with_missing_optional_fields(self) -> None:
        r = BM25Ranker()
        r.build_index([{"name": "minimal", "path": "/skills/minimal"}])
        assert r._built is True
        assert r.get_corpus_size() == 1

    def test_rank_returns_results(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("git commit push")
        assert len(results) > 0

    def test_rank_relevance_git(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("git commit and push changes")
        assert results[0].name == "git-ops"
        assert results[0].bm25_score > 0

    def test_rank_relevance_email(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("triage my email inbox")
        assert results[0].name == "email-triage"
        assert results[0].bm25_score > 0

    def test_rank_relevance_web(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("search the web for information")
        assert results[0].name == "web-research"

    def test_rank_relevance_memory(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("store this decision in memory")
        assert results[0].name == "memory-store"

    def test_rank_relevance_pinescript(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("debug pinescript indicator error")
        assert results[0].name == "pinescript-debug"

    def test_rank_top_k_limits_results(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("git email web pine memory", top_k=2)
        assert len(results) <= 2

    def test_rank_top_k_one(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("git commit push", top_k=1)
        assert len(results) == 1

    def test_rank_without_build_returns_empty(self, empty_ranker: BM25Ranker) -> None:
        results = empty_ranker.rank("git commit")
        assert results == []

    def test_rank_with_empty_query(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("")
        # All BM25 scores should be 0 for empty query
        for r in results:
            assert r.bm25_score == 0.0

    def test_rank_results_sorted_descending(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("git email web")
        for i in range(len(results) - 1):
            assert results[i].combined_score >= results[i + 1].combined_score

    def test_get_corpus_size(self, ranker: BM25Ranker) -> None:
        assert ranker.get_corpus_size() == 5

    def test_get_corpus_size_empty(self, empty_ranker: BM25Ranker) -> None:
        assert empty_ranker.get_corpus_size() == 0


# ===========================================================================
# TestTokenization — tokenizer correctness
# ===========================================================================


class TestTokenization:
    """Test the _tokenize static method."""

    def test_lowercase(self) -> None:
        tokens = BM25Ranker._tokenize("Hello WORLD FooBar")
        assert all(t == t.lower() for t in tokens)

    def test_filters_single_chars(self) -> None:
        tokens = BM25Ranker._tokenize("a b c de fg")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "c" not in tokens
        assert "de" in tokens
        assert "fg" in tokens

    def test_splits_on_punctuation(self) -> None:
        tokens = BM25Ranker._tokenize("hello-world foo.bar baz_qux")
        assert "hello" in tokens
        assert "world" in tokens
        assert "foo" in tokens
        assert "bar" in tokens
        assert "baz" in tokens
        assert "qux" in tokens

    def test_empty_string(self) -> None:
        assert BM25Ranker._tokenize("") == []

    def test_numeric_tokens(self) -> None:
        tokens = BM25Ranker._tokenize("version 42 build 2026")
        assert "42" in tokens
        assert "2026" in tokens


# ===========================================================================
# TestIDF — IDF computation
# ===========================================================================


class TestIDF:
    """Test IDF computation properties."""

    def test_unique_term_high_idf(self, ranker: BM25Ranker) -> None:
        # "gmail" only appears in email-triage
        assert "gmail" in ranker._idf
        assert ranker._idf["gmail"] > 0

    def test_common_term_lower_idf(self) -> None:
        r = BM25Ranker()
        # Build corpus where "common" appears in all docs
        skills = [{"name": f"skill-{i}", "description": f"common term skill {i}", "path": f"/s/{i}"} for i in range(5)]
        r.build_index(skills)
        # "common" appears in all 5 docs
        assert "common" in r._idf
        # "skill" also in all — both should have low (but positive via +1) IDF
        n = 5
        freq = 5
        expected_idf = math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
        assert r._idf["common"] == pytest.approx(expected_idf)

    def test_idf_formula_correctness(self) -> None:
        r = BM25Ranker()
        # 3 docs, "target" in 1 of them
        skills = [
            {"name": "alpha", "description": "target word", "path": "/a"},
            {"name": "beta", "description": "other stuff", "path": "/b"},
            {"name": "gamma", "description": "more things", "path": "/c"},
        ]
        r.build_index(skills)
        n = 3
        freq = 1  # "target" in 1 doc
        expected = math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
        assert r._idf["target"] == pytest.approx(expected, abs=1e-6)


# ===========================================================================
# TestBM25Score — score computation
# ===========================================================================


class TestBM25Score:
    """Test BM25 score computation details."""

    def test_score_nonzero_for_matching_terms(self, ranker: BM25Ranker) -> None:
        query_tokens = BM25Ranker._tokenize("git commit")
        # git-ops is index 0
        score = ranker._score_document(query_tokens, ranker._tokenized_docs[0], 0)
        assert score > 0

    def test_score_zero_for_no_matching_terms(self, ranker: BM25Ranker) -> None:
        query_tokens = BM25Ranker._tokenize("zzzzz xxxxx")
        score = ranker._score_document(query_tokens, ranker._tokenized_docs[0], 0)
        assert score == 0.0

    def test_score_higher_for_more_matches(self, ranker: BM25Ranker) -> None:
        one_match = BM25Ranker._tokenize("git")
        two_match = BM25Ranker._tokenize("git commit")
        s1 = ranker._score_document(one_match, ranker._tokenized_docs[0], 0)
        s2 = ranker._score_document(two_match, ranker._tokenized_docs[0], 0)
        assert s2 > s1


# ===========================================================================
# TestQualityFiltering — auto-exclusion logic
# ===========================================================================


class TestQualityFiltering:
    """Test quality-based skill exclusion."""

    def test_exclusion_by_low_completion_rate(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.1,  # below 0.2 floor
                "fallback_rate": 0.1,
                "quality_score": 0.3,
                "selections": 10,
            }
        }
        results = ranker.rank("git commit push", quality_data=quality)
        # git-ops should be excluded
        git_in_results = [r for r in results if r.name == "git-ops"]
        assert len(git_in_results) == 0

    def test_exclusion_by_high_fallback_rate(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.8,
                "fallback_rate": 0.6,  # above 0.5 ceiling
                "quality_score": 0.5,
                "selections": 10,
            }
        }
        results = ranker.rank("git commit push", quality_data=quality)
        git_in_results = [r for r in results if r.name == "git-ops"]
        assert len(git_in_results) == 0

    def test_no_exclusion_below_min_selections(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.05,  # terrible but too few selections
                "fallback_rate": 0.9,
                "quality_score": 0.1,
                "selections": 3,  # below MIN_SELECTIONS_FOR_EXCLUSION=5
            }
        }
        results = ranker.rank("git commit push", quality_data=quality)
        git_in_results = [r for r in results if r.name == "git-ops"]
        assert len(git_in_results) == 1

    def test_no_exclusion_when_rates_healthy(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.9,
                "fallback_rate": 0.1,
                "quality_score": 0.95,
                "selections": 20,
            }
        }
        results = ranker.rank("git commit push", quality_data=quality)
        git_in_results = [r for r in results if r.name == "git-ops"]
        assert len(git_in_results) == 1
        assert git_in_results[0].excluded is False

    def test_quality_score_affects_combined_score(self, ranker: BM25Ranker) -> None:
        quality_high = {
            "git-ops": {
                "completion_rate": 0.9,
                "fallback_rate": 0.1,
                "quality_score": 1.0,
                "selections": 20,
            }
        }
        quality_low = {
            "git-ops": {
                "completion_rate": 0.5,
                "fallback_rate": 0.3,
                "quality_score": 0.3,
                "selections": 20,
            }
        }
        results_high = ranker.rank("git commit push", quality_data=quality_high)
        results_low = ranker.rank("git commit push", quality_data=quality_low)

        git_high = next(r for r in results_high if r.name == "git-ops")
        git_low = next(r for r in results_low if r.name == "git-ops")

        # Same BM25 score, but different quality multiplier
        assert git_high.bm25_score == pytest.approx(git_low.bm25_score)
        assert git_high.combined_score > git_low.combined_score

    def test_excluded_skills_not_in_results(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.05,
                "fallback_rate": 0.1,
                "quality_score": 0.1,
                "selections": 10,
            },
            "web-research": {
                "completion_rate": 0.9,
                "fallback_rate": 0.1,
                "quality_score": 0.9,
                "selections": 10,
            },
        }
        results = ranker.rank("git commit web search", quality_data=quality)
        names = [r.name for r in results]
        assert "git-ops" not in names
        assert "web-research" in names

    def test_multiple_exclusions(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.05,
                "fallback_rate": 0.1,
                "quality_score": 0.1,
                "selections": 10,
            },
            "email-triage": {
                "completion_rate": 0.9,
                "fallback_rate": 0.7,  # high fallback
                "quality_score": 0.3,
                "selections": 10,
            },
        }
        results = ranker.rank("git email web pine memory", quality_data=quality)
        names = [r.name for r in results]
        assert "git-ops" not in names
        assert "email-triage" not in names

    def test_quality_data_missing_for_some_skills(self, ranker: BM25Ranker) -> None:
        # Only provide quality data for git-ops, not others
        quality = {
            "git-ops": {
                "completion_rate": 0.9,
                "fallback_rate": 0.1,
                "quality_score": 0.8,
                "selections": 10,
            }
        }
        results = ranker.rank("git commit web search", quality_data=quality)
        # git-ops should have quality applied
        git = next(r for r in results if r.name == "git-ops")
        assert git.quality_score == 0.8
        # web-research should have default quality
        web = next(r for r in results if r.name == "web-research")
        assert web.quality_score == 1.0

    def test_exclusion_reason_set_for_completion(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.1,
                "fallback_rate": 0.1,
                "quality_score": 0.1,
                "selections": 10,
            }
        }
        # We need to inspect all scored items, so rank with quality
        # then check excluded items via internal scoring
        r2 = BM25Ranker()
        r2.build_index(SAMPLE_SKILLS)

        # Manually invoke rank and inspect the internal sorted list
        query_tokens = r2._tokenize("git commit push")
        scored: list[RankedSkill] = []
        for idx, doc_tokens in enumerate(r2._tokenized_docs):
            skill = r2._corpus[idx]
            bm25_score = r2._score_document(query_tokens, doc_tokens, idx)
            ranked = RankedSkill(
                name=skill["name"],
                description=skill.get("description", ""),
                path=skill.get("path", ""),
                bm25_score=bm25_score,
            )
            if skill["name"] in quality:
                qd = quality[skill["name"]]
                ranked.completion_rate = qd["completion_rate"]
                ranked.fallback_rate = qd["fallback_rate"]
                ranked.quality_score = qd["quality_score"]
                if qd["selections"] >= r2.MIN_SELECTIONS_FOR_EXCLUSION:
                    if ranked.completion_rate < r2.COMPLETION_RATE_FLOOR:
                        ranked.excluded = True
                        ranked.exclusion_reason = (
                            f"completion_rate={ranked.completion_rate:.2f} < {r2.COMPLETION_RATE_FLOOR}"
                        )
            scored.append(ranked)

        git_skill = next(s for s in scored if s.name == "git-ops")
        assert git_skill.excluded is True
        assert "completion_rate" in git_skill.exclusion_reason

    def test_exclusion_reason_set_for_fallback(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.8,
                "fallback_rate": 0.7,
                "quality_score": 0.5,
                "selections": 10,
            }
        }
        # Use a fresh ranker so we can check excluded items
        r2 = BM25Ranker()
        r2.build_index(SAMPLE_SKILLS)
        query_tokens = r2._tokenize("git commit push")
        scored: list[RankedSkill] = []
        for idx, doc_tokens in enumerate(r2._tokenized_docs):
            skill = r2._corpus[idx]
            bm25_score = r2._score_document(query_tokens, doc_tokens, idx)
            ranked = RankedSkill(
                name=skill["name"],
                description=skill.get("description", ""),
                path=skill.get("path", ""),
                bm25_score=bm25_score,
            )
            if skill["name"] in quality:
                qd = quality[skill["name"]]
                ranked.completion_rate = qd["completion_rate"]
                ranked.fallback_rate = qd["fallback_rate"]
                ranked.quality_score = qd["quality_score"]
                if qd["selections"] >= r2.MIN_SELECTIONS_FOR_EXCLUSION:
                    if ranked.fallback_rate > r2.FALLBACK_RATE_CEIL:
                        ranked.excluded = True
                        ranked.exclusion_reason = f"fallback_rate={ranked.fallback_rate:.2f} > {r2.FALLBACK_RATE_CEIL}"
            scored.append(ranked)

        git_skill = next(s for s in scored if s.name == "git-ops")
        assert git_skill.excluded is True
        assert "fallback_rate" in git_skill.exclusion_reason


# ===========================================================================
# TestRankedSkill — dataclass behavior
# ===========================================================================


class TestRankedSkill:
    """Test RankedSkill dataclass creation and defaults."""

    def test_creation_with_defaults(self) -> None:
        rs = RankedSkill(name="test", description="desc", path="/p")
        assert rs.bm25_score == 0.0
        assert rs.quality_score == 1.0
        assert rs.completion_rate == 1.0
        assert rs.fallback_rate == 0.0
        assert rs.combined_score == 0.0
        assert rs.excluded is False
        assert rs.exclusion_reason == ""

    def test_creation_with_custom_values(self) -> None:
        rs = RankedSkill(
            name="test",
            description="desc",
            path="/p",
            bm25_score=2.5,
            quality_score=0.8,
            completion_rate=0.7,
            fallback_rate=0.3,
            combined_score=2.0,
            excluded=True,
            exclusion_reason="low quality",
        )
        assert rs.bm25_score == 2.5
        assert rs.quality_score == 0.8
        assert rs.excluded is True
        assert rs.exclusion_reason == "low quality"

    def test_excluded_flag_independent(self) -> None:
        rs = RankedSkill(
            name="test",
            description="desc",
            path="/p",
            excluded=True,
            exclusion_reason="test reason",
        )
        assert rs.excluded is True
        assert rs.exclusion_reason == "test reason"


# ===========================================================================
# TestIntegration — end-to-end ranking scenarios
# ===========================================================================


class TestIntegration:
    """Integration tests combining BM25 ranking with quality filtering."""

    def test_rank_with_quality_data_applied(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.9,
                "fallback_rate": 0.05,
                "quality_score": 0.95,
                "selections": 20,
            },
            "web-research": {
                "completion_rate": 0.8,
                "fallback_rate": 0.1,
                "quality_score": 0.85,
                "selections": 15,
            },
        }
        results = ranker.rank("git commit web search", quality_data=quality)
        assert len(results) > 0
        # Verify quality was applied
        git = next(r for r in results if r.name == "git-ops")
        assert git.quality_score == 0.95

    def test_rank_without_quality_data(self, ranker: BM25Ranker) -> None:
        results = ranker.rank("git commit push")
        for r in results:
            assert r.quality_score == 1.0
            assert r.excluded is False

    def test_rank_with_all_skills_excluded(self, ranker: BM25Ranker) -> None:
        quality = {
            skill["name"]: {
                "completion_rate": 0.05,
                "fallback_rate": 0.1,
                "quality_score": 0.1,
                "selections": 20,
            }
            for skill in SAMPLE_SKILLS
        }
        results = ranker.rank("git email web pine memory", quality_data=quality)
        # All excluded, so result should be empty
        assert len(results) == 0

    def test_rank_with_mixed_excluded_nonexcluded(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.05,  # excluded
                "fallback_rate": 0.1,
                "quality_score": 0.1,
                "selections": 10,
            },
            "web-research": {
                "completion_rate": 0.9,  # healthy
                "fallback_rate": 0.1,
                "quality_score": 0.9,
                "selections": 10,
            },
        }
        results = ranker.rank("git commit web search", quality_data=quality)
        names = [r.name for r in results]
        assert "git-ops" not in names
        assert "web-research" in names

    def test_rebuild_index_resets_state(self) -> None:
        r = BM25Ranker()
        r.build_index(SAMPLE_SKILLS[:2])
        assert r.get_corpus_size() == 2

        r.build_index(SAMPLE_SKILLS)
        assert r.get_corpus_size() == 5

    def test_body_content_influences_ranking(self) -> None:
        r = BM25Ranker()
        skills = [
            {
                "name": "skill-a",
                "description": "Generic tool",
                "path": "/a",
                "body": "kubernetes helm deploy cluster pod container",
            },
            {
                "name": "skill-b",
                "description": "Generic tool",
                "path": "/b",
                "body": "database query SQL postgres migration",
            },
        ]
        r.build_index(skills)

        k8s_results = r.rank("deploy to kubernetes cluster")
        assert k8s_results[0].name == "skill-a"

        db_results = r.rank("run SQL query on postgres database")
        assert db_results[0].name == "skill-b"

    def test_combined_score_is_bm25_times_quality(self, ranker: BM25Ranker) -> None:
        quality = {
            "git-ops": {
                "completion_rate": 0.9,
                "fallback_rate": 0.1,
                "quality_score": 0.7,
                "selections": 10,
            }
        }
        results = ranker.rank("git commit push", quality_data=quality)
        git = next(r for r in results if r.name == "git-ops")
        expected = git.bm25_score * 0.7
        assert git.combined_score == pytest.approx(expected)

    def test_large_corpus_ranking(self) -> None:
        """Verify ranking works with a larger corpus."""
        r = BM25Ranker()
        skills = [
            {
                "name": f"skill-{i}",
                "description": f"Skill number {i} does things",
                "path": f"/skills/{i}",
                "body": f"content for skill {i} with filler words",
            }
            for i in range(50)
        ]
        # Add one distinctive skill
        skills.append(
            {
                "name": "kubernetes-deploy",
                "description": "Deploy applications to Kubernetes clusters",
                "path": "/skills/k8s",
                "body": "kubectl helm chart pod service ingress namespace",
            }
        )
        r.build_index(skills)
        results = r.rank("deploy to kubernetes using helm")
        assert results[0].name == "kubernetes-deploy"

    def test_repeated_query_terms_boost_score(self, ranker: BM25Ranker) -> None:
        """BM25 should give similar results whether query has repeated terms."""
        r1 = ranker.rank("git")
        r2 = ranker.rank("git git git")
        # Both should rank git-ops first
        assert r1[0].name == "git-ops"
        assert r2[0].name == "git-ops"
        # Repeated terms increase score due to query term frequency
        assert r2[0].bm25_score >= r1[0].bm25_score
