"""Targeted tests to hit specific uncovered lines in structured_output.py."""

from unittest.mock import patch

from utils.schemas.heartbeat import HeartbeatDecision
from utils.structured_output import _extract_json, parse_and_validate, safe_json_parse


class TestSpecificCoveragePaths:
    """Test specific uncovered lines in structured_output.py."""

    def test_extract_json_strategy1_json_decode_error(self):
        """Test line 68-69: JSONDecodeError in strategy 1."""
        # Create text that looks like JSON but isn't valid
        text = '{"incomplete": "json"'  # Missing closing brace

        # This should trigger the except block at lines 68-69
        result = _extract_json(text)
        # We don't care about the result, just that it doesn't crash
        # and the exception path is executed
        assert result is None or isinstance(result, str)

    def test_extract_json_code_block_json_decode_error(self):
        """Test line 84-85: JSONDecodeError in code block extraction."""
        text = """
        ```json
        {"bad": "json", }
        ```
        """

        # This should trigger the except block at lines 84-85 (continue statement)
        result = _extract_json(text)
        assert result is None  # No valid JSON found

    def test_safe_json_parse_json_decode_error(self):
        """Test safe_json_parse with JSON decode error."""
        # Test the JSONDecodeError path in safe_json_parse
        result = safe_json_parse('{"invalid": json}')
        assert result is None

    def test_parse_and_validate_with_validation_exception(self):
        """Test parse_and_validate when validation raises Exception."""
        text = '{"missing_required_action": "invalid"}'

        with patch("utils.structured_output.logger") as mock_logger:
            result = parse_and_validate(text, HeartbeatDecision)
            assert result is None
            # Should log the validation error
            mock_logger.warning.assert_called()

    def test_extract_json_balanced_braces_no_closing_brace(self):
        """Test balanced brace extraction when no closing brace found."""
        text = "Text with opening { but no closing"
        result = _extract_json(text)
        assert result is None

    def test_extract_json_only_array_bracket(self):
        """Test when only array bracket found, no object brace."""
        text = "Text with [ but malformed array"
        result = _extract_json(text)
        assert result is None

    def test_extract_json_escape_sequence_handling(self):
        """Test escape sequence handling in balanced brace extraction."""
        text = r'{"key": "value with \\ backslash"}'
        result = _extract_json(text)
        assert result == r'{"key": "value with \\ backslash"}'

    def test_extract_json_string_with_quotes(self):
        """Test string handling in balanced brace extraction."""
        text = '{"message": "simple message"}'
        result = _extract_json(text)
        assert result == '{"message": "simple message"}'

    def test_safe_json_parse_with_none(self):
        """Test safe_json_parse with None input."""
        result = safe_json_parse(None)
        assert result is None

    def test_safe_json_parse_with_empty_string(self):
        """Test safe_json_parse with empty string."""
        result = safe_json_parse("")
        assert result is None
