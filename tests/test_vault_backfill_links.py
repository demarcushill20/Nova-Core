"""Tests for scripts/vault_backfill_links.py — vault wikilink backfill.

Tests keyword extraction, dedup filtering, idempotency, source safety,
rate limiting, and dry-run mode.
"""

import time

from scripts.vault_backfill_links import (
    BackfillResult,
    VaultBackfiller,
    extract_keywords,
    format_related_section,
    pick_top_related,
)

# ---------------------------------------------------------------------------
# Keyword extraction tests
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    """Test keyword extraction from title and content."""

    def test_extracts_title_words(self):
        keywords = extract_keywords("Circuit Breaker Pattern", "")
        assert "circuit" in keywords
        assert "breaker" in keywords
        assert "pattern" in keywords

    def test_filters_stopwords(self):
        keywords = extract_keywords("The Art of Testing", "")
        assert "the" not in keywords
        assert "art" in keywords
        assert "testing" in keywords

    def test_filters_short_words(self):
        keywords = extract_keywords("A to B", "some content here")
        assert "a" not in keywords  # too short
        assert "to" not in keywords  # stopword

    def test_title_prioritized_over_content(self):
        keywords = extract_keywords("Alpha Beta", "gamma delta epsilon zeta eta")
        assert keywords[0] == "alpha"
        assert keywords[1] == "beta"

    def test_max_keywords_respected(self):
        keywords = extract_keywords(
            "one two three four five six seven",
            "eight nine ten",
            max_keywords=3,
        )
        assert len(keywords) <= 3

    def test_deduplicates(self):
        keywords = extract_keywords("test pattern", "test pattern repeated test")
        assert keywords.count("test") == 1
        assert keywords.count("pattern") == 1

    def test_empty_input(self):
        keywords = extract_keywords("", "")
        assert keywords == []

    def test_content_supplements_title(self):
        keywords = extract_keywords("Short", "longer content with keywords here")
        assert len(keywords) > 1  # gets content words too


# ---------------------------------------------------------------------------
# Related note selection tests
# ---------------------------------------------------------------------------


class TestPickTopRelated:
    """Test related note selection from search results."""

    def test_excludes_self(self):
        results = [
            {"path": "30-workflow-learnings/note-a.md", "title": "Note A"},
            {"path": "30-workflow-learnings/note-b.md", "title": "Note B"},
        ]
        related = pick_top_related(results, "30-workflow-learnings/note-a.md")
        assert len(related) == 1
        assert related[0]["path"] == "30-workflow-learnings/note-b.md"

    def test_excludes_diary(self):
        results = [
            {"path": "90-diary/2026-03-01.md", "title": "Diary"},
            {"path": "30-workflow-learnings/note-a.md", "title": "Note A"},
        ]
        related = pick_top_related(results, "some-other-note.md")
        assert len(related) == 1
        assert related[0]["path"] == "30-workflow-learnings/note-a.md"

    def test_excludes_meta(self):
        results = [
            {"path": "_meta/index.md", "title": "Index"},
            {"path": "20-agent-patterns/pattern-a.md", "title": "Pattern A"},
        ]
        related = pick_top_related(results, "some-note.md")
        assert len(related) == 1
        assert related[0]["path"] == "20-agent-patterns/pattern-a.md"

    def test_max_related_respected(self):
        results = [{"path": f"40-research/note-{i}.md", "title": f"Note {i}"} for i in range(10)]
        related = pick_top_related(results, "other.md", max_related=3)
        assert len(related) == 3

    def test_empty_results(self):
        related = pick_top_related([], "note.md")
        assert related == []


# ---------------------------------------------------------------------------
# Format related section tests
# ---------------------------------------------------------------------------


class TestFormatRelatedSection:
    """Test formatting of the Related Notes section."""

    def test_basic_format(self):
        related = [
            {"path": "20-agent-patterns/research-patterns.md", "title": "Research Patterns"},
        ]
        section = format_related_section(related)
        assert "## Related Notes" in section
        assert "[[research-patterns]]" in section
        assert "Research Patterns" in section

    def test_with_domain(self):
        related = [{"path": "30-workflow-learnings/note.md", "title": "Note"}]
        section = format_related_section(related, domain="novatrade")
        assert "up:: [[moc-novatrade]]" in section
        assert "## Related Notes" in section

    def test_no_related(self):
        section = format_related_section([])
        assert "## Related Notes" in section
        assert "no related notes found" in section

    def test_without_domain(self):
        related = [{"path": "40-research/note.md", "title": "Note"}]
        section = format_related_section(related, domain=None)
        assert "up::" not in section


# ---------------------------------------------------------------------------
# Backfiller unit tests
# ---------------------------------------------------------------------------


class _MockVaultCall:
    """Mock vault_call function with call log."""

    def __init__(self, responses: dict):
        self.call_log: list = []
        self._responses = responses

    def __call__(self, tool: str, args: dict):
        self.call_log.append((tool, args))
        key = f"{tool}:{args.get('path', args.get('query', ''))}"
        if key in self._responses:
            return self._responses[key]
        if tool in self._responses:
            return self._responses[tool]
        return {"error": f"unexpected call: {tool}({args})"}


def _mock_vault_call(responses: dict):
    """Create a mock vault_call function."""
    return _MockVaultCall(responses)


class TestBackfiller:
    """Test VaultBackfiller logic."""

    def test_skips_already_backfilled(self):
        """Idempotent: skip notes with existing ## Related Notes."""
        mock = _mock_vault_call(
            {
                "vault_read:30-workflow-learnings/note.md": {
                    "content": "---\ntitle: Test\nsource: nova-core-memory\n---\n## Related Notes\n- [[other]]\n"
                },
            }
        )
        bf = VaultBackfiller(vault_call_fn=mock, dry_run=True)
        result = bf.process_note("30-workflow-learnings/note.md")
        assert result.action == "skipped"
        assert "already has" in result.reason

    def test_skips_operator_source(self):
        """Source safety: skip operator-authored notes."""
        mock = _mock_vault_call(
            {
                "vault_read:30-workflow-learnings/note.md": {
                    "content": "---\ntitle: Test\nsource: operator\n---\nBody"
                },
                "vault_frontmatter:30-workflow-learnings/note.md": {
                    "frontmatter": {"title": "Test", "source": "operator"}
                },
            }
        )
        bf = VaultBackfiller(vault_call_fn=mock, dry_run=True)
        result = bf.process_note("30-workflow-learnings/note.md")
        assert result.action == "skipped"
        assert "operator" in result.reason

    def test_skips_no_source(self):
        """Source safety: skip notes with no source field."""
        mock = _mock_vault_call(
            {
                "vault_read:30-workflow-learnings/note.md": {"content": "---\ntitle: Test\n---\nBody"},
                "vault_frontmatter:30-workflow-learnings/note.md": {"frontmatter": {"title": "Test"}},
            }
        )
        bf = VaultBackfiller(vault_call_fn=mock, dry_run=True)
        result = bf.process_note("30-workflow-learnings/note.md")
        assert result.action == "skipped"
        assert "no source" in result.reason

    def test_dry_run_does_not_write(self):
        """Dry-run mode logs but does not call vault_update."""
        mock = _mock_vault_call(
            {
                "vault_read:30-workflow-learnings/note.md": {
                    "content": "---\ntitle: Circuit Breaker\nsource: nova-core-memory\n---\nBody about circuit breakers"
                },
                "vault_frontmatter:30-workflow-learnings/note.md": {
                    "frontmatter": {"title": "Circuit Breaker", "source": "nova-core-memory"}
                },
                "vault_search": {
                    "results": [
                        {"path": "20-agent-patterns/breaker-pattern.md", "title": "Breaker Pattern"},
                        {"path": "30-workflow-learnings/note.md", "title": "Circuit Breaker"},
                    ],
                    "results_count": 2,
                },
            }
        )
        bf = VaultBackfiller(vault_call_fn=mock, dry_run=True)
        result = bf.process_note("30-workflow-learnings/note.md")
        assert result.action == "dry-run"
        assert result.related_count == 1  # excluded self
        # Verify no vault_update was called
        assert not any(tool == "vault_update" for tool, _ in mock.call_log)

    def test_successful_update(self):
        """Happy path: note gets related notes appended."""
        mock = _mock_vault_call(
            {
                "vault_read:30-workflow-learnings/note.md": {
                    "content": "---\ntitle: Risk Engine\nsource: nova-core-memory\n---\nBody about risk"
                },
                "vault_frontmatter:30-workflow-learnings/note.md": {
                    "frontmatter": {"title": "Risk Engine", "source": "nova-core-memory"}
                },
                "vault_search": {
                    "results": [
                        {"path": "40-research/risk-research.md", "title": "Risk Research"},
                        {"path": "20-agent-patterns/risk-pattern.md", "title": "Risk Pattern"},
                    ],
                    "results_count": 2,
                },
                "vault_update": {"status": "updated"},
            }
        )
        bf = VaultBackfiller(vault_call_fn=mock, dry_run=False)
        result = bf.process_note("30-workflow-learnings/note.md")
        assert result.action == "updated"
        assert result.related_count == 2
        # Verify vault_update was called
        update_calls = [(t, a) for t, a in mock.call_log if t == "vault_update"]
        assert len(update_calls) == 1
        assert update_calls[0][1]["section_heading"] == "## Related Notes"
        body = update_calls[0][1]["section_body"]
        assert "risk-research" in body or "risk-pattern" in body

    def test_rate_limit_tracking(self):
        """Rate limiter tracks write timestamps."""
        bf = VaultBackfiller(dry_run=True, rate_limit=3, rate_window=300)
        assert bf._check_rate_limit() is True
        bf._record_write()
        bf._record_write()
        bf._record_write()
        assert bf._check_rate_limit() is False

    def test_rate_limit_window_expiry(self):
        """Rate limiter expires old timestamps."""
        bf = VaultBackfiller(dry_run=True, rate_limit=2, rate_window=1)
        bf._write_timestamps = [time.time() - 2, time.time() - 2]  # expired
        assert bf._check_rate_limit() is True

    def test_skips_non_writable_folder(self):
        """Rejects folders not in WRITABLE_FOLDERS."""
        bf = VaultBackfiller(dry_run=True)
        bf.process_all(folders=["90-diary"])
        assert len(bf.results) == 0

    def test_summary(self):
        """Summary correctly aggregates results."""
        bf = VaultBackfiller(dry_run=True)
        bf.results = [
            BackfillResult("a.md", "updated", 3),
            BackfillResult("b.md", "skipped", reason="already has ## Related Notes"),
            BackfillResult("c.md", "dry-run", 2),
            BackfillResult("d.md", "skipped", reason="source: operator (human-authored)"),
        ]
        s = bf.summary()
        assert s["total_processed"] == 4
        assert s["updated"] == 1
        assert s["skipped"] == 2
        assert s["dry_run"] == 1
        assert s["total_links_added"] == 5

    def test_domain_inference(self):
        """Domain inference from content keywords."""
        bf = VaultBackfiller(dry_run=True)
        assert bf._infer_domain("Risk Engine", "risk management filters", {}) == "risk"
        assert bf._infer_domain("Trading Strategy", "backtest IRB results", {}) == "novatrade"
        assert bf._infer_domain("Boring Note", "nothing special here", {}) == "operations"
        assert bf._infer_domain("Memory System", "fusion pinecone vectors", {}) == "memory"
        assert bf._infer_domain("Agent Spawner", "multi-agent orchestrator", {}) == "agents"
