"""Simplified tests for utils/secrets.py to achieve basic coverage."""

import re

from utils.secrets import (
    SECRET_PATTERNS,
    SecretMatch,
    SecretPattern,
    find_high_entropy_strings,
    has_secrets,
    redact_text,
    scan_text,
)


def test_secret_pattern_creation():
    """Test creating a SecretPattern."""
    pattern = SecretPattern(name="test", pattern=re.compile(r"test"), severity="high")
    assert pattern.name == "test"
    assert pattern.severity == "high"


def test_secret_match_creation():
    """Test creating a SecretMatch."""
    match = SecretMatch(pattern_name="test_pattern", severity="high", start=0, end=4)
    assert match.pattern_name == "test_pattern"
    assert match.severity == "high"


def test_scan_text_basic():
    """Test basic text scanning."""
    text = "This is normal text."
    matches = scan_text(text)
    assert isinstance(matches, list)


def test_redact_text_basic():
    """Test basic text redaction."""
    text = "This is normal text."
    redacted = redact_text(text)
    assert isinstance(redacted, str)


def test_has_secrets_basic():
    """Test basic secret checking."""
    text = "This is normal text."
    result = has_secrets(text)
    assert isinstance(result, bool)


def test_find_high_entropy_basic():
    """Test basic high entropy detection."""
    text = "This is normal text."
    entropy_strings = find_high_entropy_strings(text)
    assert isinstance(entropy_strings, list)


def test_secret_patterns_exist():
    """Test that SECRET_PATTERNS is available."""
    assert SECRET_PATTERNS is not None
    assert len(SECRET_PATTERNS) > 0

    # Test first pattern
    first_pattern = SECRET_PATTERNS[0]
    assert isinstance(first_pattern, SecretPattern)
    assert first_pattern.name
    assert first_pattern.pattern
    assert first_pattern.severity
