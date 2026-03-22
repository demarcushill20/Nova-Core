"""Expanded coverage tests for utils/secrets.py.

Targets uncovered lines: check_file_permissions, validate_secret_storage,
find_high_entropy_strings, _shannon_entropy, scan_text edge cases,
redact_text, has_secrets.
"""

import os

from utils.secrets import (
    _make_redacted_preview,
    _shannon_entropy,
    check_file_permissions,
    find_high_entropy_strings,
    has_secrets,
    redact_text,
    scan_text,
    validate_secret_storage,
)

# ---------------------------------------------------------------------------
# _shannon_entropy
# ---------------------------------------------------------------------------


class TestShannonEntropy:
    def test_empty_string(self):
        assert _shannon_entropy("") == 0.0

    def test_single_char(self):
        assert _shannon_entropy("a") == 0.0

    def test_repeated_char(self):
        assert _shannon_entropy("aaaaaa") == 0.0

    def test_two_equal_chars(self):
        # "ab" → 1.0 bit
        e = _shannon_entropy("ab")
        assert abs(e - 1.0) < 0.01

    def test_high_entropy(self):
        # All unique chars → high entropy
        import string

        s = string.ascii_lowercase + string.digits
        e = _shannon_entropy(s)
        assert e > 4.0

    def test_low_entropy(self):
        e = _shannon_entropy("aaabbb")
        assert e == 1.0


# ---------------------------------------------------------------------------
# _make_redacted_preview
# ---------------------------------------------------------------------------


class TestMakeRedactedPreview:
    def test_short_match(self):
        text = "hello secret world"
        # "secret" starts at 6, ends at 12
        preview = _make_redacted_preview(text, 6, 12)
        assert "***" in preview
        assert "..." in preview

    def test_long_match(self):
        text = "before " + "x" * 50 + " after"
        preview = _make_redacted_preview(text, 7, 57)
        # Should show first 4 + *** + last 4
        assert "xxxx***xxxx" in preview

    def test_match_at_start(self):
        text = "secret at start"
        preview = _make_redacted_preview(text, 0, 6)
        assert "***" in preview

    def test_match_at_end(self):
        text = "end secret"
        preview = _make_redacted_preview(text, 4, 10)
        assert "***" in preview


# ---------------------------------------------------------------------------
# scan_text
# ---------------------------------------------------------------------------


class TestScanText:
    def test_no_secrets(self):
        assert scan_text("Just a normal string") == []

    def test_detects_aws_key(self):
        text = "key is AKIAIOSFODNN7EXAMPLE "
        matches = scan_text(text)
        found = [m for m in matches if "aws" in m.pattern_name]
        assert len(found) >= 1

    def test_detects_github_pat(self):
        text = "ghp_" + "A" * 36
        matches = scan_text(text)
        found = [m for m in matches if "github" in m.pattern_name]
        assert len(found) >= 1

    def test_detects_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----"
        matches = scan_text(text)
        found = [m for m in matches if "rsa" in m.pattern_name]
        assert len(found) >= 1

    def test_detects_postgres_uri(self):
        text = "postgres://user:pass@host:5432/db"
        matches = scan_text(text)
        found = [m for m in matches if "postgres" in m.pattern_name]
        assert len(found) >= 1

    def test_detects_slack_token(self):
        # Construct token dynamically to avoid GitHub push-protection false positive
        prefix = "xoxb"
        text = f"{prefix}-1234567890-abcdefghijklmnop"
        matches = scan_text(text)
        found = [m for m in matches if "slack" in m.pattern_name]
        assert len(found) >= 1

    def test_deduplication_by_span(self):
        # A string that matches multiple patterns at same span
        # should be deduplicated, keeping highest severity
        text = "postgres://admin:secret@host/db"
        matches = scan_text(text)
        spans = [(m.start, m.end) for m in matches]
        assert len(spans) == len(set(spans)), "No duplicate spans"

    def test_sorted_by_position(self):
        text = "ghp_" + "A" * 36 + " and xoxb-1234567890-abc"
        matches = scan_text(text)
        for i in range(1, len(matches)):
            assert matches[i].start >= matches[i - 1].start

    def test_detects_stripe_key(self):
        text = "sk_live_" + "A" * 24
        matches = scan_text(text)
        found = [m for m in matches if "stripe" in m.pattern_name]
        assert len(found) >= 1

    def test_detects_sendgrid(self):
        text = "SG." + "A" * 22 + "." + "B" * 43
        matches = scan_text(text)
        found = [m for m in matches if "sendgrid" in m.pattern_name]
        assert len(found) >= 1


# ---------------------------------------------------------------------------
# redact_text
# ---------------------------------------------------------------------------


class TestRedactText:
    def test_no_secrets_unchanged(self):
        text = "Hello world"
        assert redact_text(text) == text

    def test_single_secret_redacted(self):
        text = "ghp_" + "A" * 36
        result = redact_text(text)
        assert "[REDACTED:github_pat]" in result
        assert "ghp_" not in result

    def test_multiple_secrets_redacted(self):
        text = "ghp_" + "A" * 36 + " and sk_live_" + "B" * 24
        result = redact_text(text)
        assert "[REDACTED:" in result
        assert "ghp_" not in result
        assert "sk_live_" not in result

    def test_preserves_surrounding_text(self):
        text = "before ghp_" + "A" * 36 + " after"
        result = redact_text(text)
        assert result.startswith("before ")
        assert result.endswith(" after")


# ---------------------------------------------------------------------------
# has_secrets
# ---------------------------------------------------------------------------


class TestHasSecrets:
    def test_no_secrets(self):
        assert has_secrets("normal text") is False

    def test_with_github_pat(self):
        assert has_secrets("ghp_" + "A" * 36) is True

    def test_with_private_key(self):
        assert has_secrets("-----BEGIN RSA PRIVATE KEY-----") is True

    def test_with_postgres_uri(self):
        assert has_secrets("postgres://u:p@h/d") is True


# ---------------------------------------------------------------------------
# check_file_permissions
# ---------------------------------------------------------------------------


class TestCheckFilePermissions:
    def test_nonexistent_file(self, tmp_path):
        warnings = check_file_permissions(tmp_path / "nope.txt")
        assert any("does not exist" in w for w in warnings)

    def test_directory_not_file(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        warnings = check_file_permissions(d)
        assert any("Not a regular file" in w for w in warnings)

    def test_safe_permissions(self, tmp_path):
        f = tmp_path / "secret.env"
        f.write_text("SECRET=value")
        os.chmod(f, 0o600)
        warnings = check_file_permissions(f)
        assert len(warnings) == 0

    def test_world_readable(self, tmp_path):
        f = tmp_path / "bad.env"
        f.write_text("SECRET=value")
        os.chmod(f, 0o644)
        warnings = check_file_permissions(f)
        assert any("world-readable" in w for w in warnings)

    def test_world_writable(self, tmp_path):
        f = tmp_path / "bad2.env"
        f.write_text("SECRET=value")
        os.chmod(f, 0o666)  # noqa: S103
        warnings = check_file_permissions(f)
        assert any("world-writable" in w for w in warnings)

    def test_group_writable(self, tmp_path):
        f = tmp_path / "bad3.env"
        f.write_text("SECRET=value")
        os.chmod(f, 0o660)
        warnings = check_file_permissions(f)
        assert any("group-writable" in w for w in warnings)


# ---------------------------------------------------------------------------
# find_high_entropy_strings
# ---------------------------------------------------------------------------


class TestFindHighEntropyStrings:
    def test_no_high_entropy(self):
        text = "hello world normal text"
        matches = find_high_entropy_strings(text)
        assert matches == []

    def test_detects_random_string(self):
        # A 30-char random-looking string
        import random
        import string

        random.seed(42)
        token = "".join(random.choices(string.ascii_letters + string.digits, k=30))  # noqa: S311
        text = f"api_key={token}"
        matches = find_high_entropy_strings(text, min_length=20, min_entropy=3.5)
        assert len(matches) >= 1
        assert matches[0].pattern_name == "high_entropy_string"
        assert matches[0].severity == "medium"

    def test_low_entropy_not_detected(self):
        text = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 32 a's, entropy 0
        matches = find_high_entropy_strings(text, min_length=20)
        assert len(matches) == 0

    def test_custom_thresholds(self):
        text = "abcdefghijklmnopqrstuvwxyz"  # 26 unique chars, high entropy
        matches = find_high_entropy_strings(text, min_length=10, min_entropy=3.0)
        assert len(matches) >= 1


# ---------------------------------------------------------------------------
# validate_secret_storage
# ---------------------------------------------------------------------------


class TestValidateSecretStorage:
    def test_runs_without_error(self):
        """validate_secret_storage should not crash even on clean systems."""
        warnings = validate_secret_storage()
        assert isinstance(warnings, list)
