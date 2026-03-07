"""Tests for tools/adapters/pdf_generate.py"""

import pytest
from pathlib import Path

from tools.adapters.pdf_generate import pdf_generate, _inline_markup, _markdown_to_paragraphs, _build_styles


@pytest.fixture
def tmp_sandbox(tmp_path):
    """Create a temporary sandbox with OUTPUT/ directory."""
    (tmp_path / "OUTPUT").mkdir()
    return tmp_path


class TestPdfGenerate:
    """Tests for the pdf_generate adapter function."""

    def test_basic_text(self, tmp_sandbox):
        """Generate a PDF from plain text."""
        result = pdf_generate(
            content="Hello, world!",
            filename="test_basic.pdf",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is True
        assert result["verified"] is True
        assert result["path"] == "OUTPUT/test_basic.pdf"
        assert result["size_bytes"] > 0
        assert (tmp_sandbox / "OUTPUT" / "test_basic.pdf").is_file()

    def test_markdown_content(self, tmp_sandbox):
        """Generate a PDF from markdown with headings and bullets."""
        md = """# Report Title

## Summary

This is a test report with **bold** and *italic* text.

### Details

- Item one
- Item two
- Item three

1. First step
2. Second step
"""
        result = pdf_generate(
            content=md,
            filename="test_markdown.pdf",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is True
        assert result["verified"] is True
        assert result["size_bytes"] > 0

    def test_auto_pdf_extension(self, tmp_sandbox):
        """Automatically append .pdf extension if missing."""
        result = pdf_generate(
            content="Content here",
            filename="no_extension",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is True
        assert result["path"] == "OUTPUT/no_extension.pdf"

    def test_filename_sanitization(self, tmp_sandbox):
        """Strip path separators from filename to prevent traversal."""
        result = pdf_generate(
            content="Content here",
            filename="../../etc/passwd.pdf",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is True
        assert result["path"] == "OUTPUT/passwd.pdf"

    def test_empty_content_rejected(self, tmp_sandbox):
        """Reject empty content."""
        result = pdf_generate(
            content="",
            filename="empty.pdf",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is False
        assert "content is required" in result["error"]

    def test_none_content_rejected(self, tmp_sandbox):
        """Reject None content."""
        result = pdf_generate(
            content=None,
            filename="none.pdf",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is False

    def test_empty_filename_rejected(self, tmp_sandbox):
        """Reject empty filename."""
        result = pdf_generate(
            content="Content",
            filename="",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is False
        assert "filename is required" in result["error"]

    def test_creates_output_dir(self, tmp_path):
        """Create OUTPUT/ directory if it doesn't exist."""
        # tmp_path has no OUTPUT/ subdirectory
        result = pdf_generate(
            content="Auto-create test",
            filename="autocreate.pdf",
            _sandbox=tmp_path,
        )
        assert result["ok"] is True
        assert (tmp_path / "OUTPUT" / "autocreate.pdf").is_file()


class TestInlineMarkup:
    """Tests for markdown inline markup conversion."""

    def test_bold(self):
        assert "<b>bold</b>" in _inline_markup("**bold**")

    def test_italic(self):
        assert "<i>italic</i>" in _inline_markup("*italic*")

    def test_code(self):
        assert '<font face="Courier">code</font>' in _inline_markup("`code`")

    def test_xml_escaping(self):
        result = _inline_markup("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_mixed_markup(self):
        result = _inline_markup("**bold** and *italic* and `code`")
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result
        assert '<font face="Courier">code</font>' in result


class TestMarkdownToParagraphs:
    """Tests for markdown to flowable conversion."""

    def test_headings(self):
        styles = _build_styles()
        flowables = _markdown_to_paragraphs("# H1\n## H2\n### H3", styles)
        # Should produce heading paragraphs and spacers
        assert len(flowables) > 0

    def test_bullet_list(self):
        styles = _build_styles()
        flowables = _markdown_to_paragraphs("- item 1\n- item 2", styles)
        assert len(flowables) >= 2

    def test_empty_content(self):
        styles = _build_styles()
        flowables = _markdown_to_paragraphs("", styles)
        # Empty string → no flowables (or just spacers)
        assert isinstance(flowables, list)
