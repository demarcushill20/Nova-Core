"""Edge case and error path tests for utils.structured_output module."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from utils.schemas.heartbeat import HeartbeatDecision
from utils.structured_output import (
    _extract_json,
    parse_and_validate,
    safe_json_parse,
    validate_pydantic_response,
)


class TestExtractJsonEdgeCases:
    """Test edge cases and error paths in JSON extraction."""

    def test_invalid_json_strategy1_fallback(self):
        """Test Strategy 1 with invalid JSON falling back to Strategy 2."""
        # This looks like JSON but is invalid - should fallback to code block extraction
        text = '{"incomplete": "missing_brace"'  # Missing closing brace
        result = _extract_json(text)
        assert result is None  # Should return None since no valid JSON found

    def test_json_decode_error_continues_to_next_pattern(self):
        """Test that JSONDecodeError in code block continues to balanced brace strategy.

        Note: balanced brace strategy only tries the FIRST brace match. If that
        first match (from the code block content still visible in stripped text)
        fails json.loads, the function returns None.
        """
        text = """
        ```json
        {"invalid": "json", "missing": }
        ```

        And some text with balanced braces: {"valid": "json"}
        """
        result = _extract_json(text)
        # The balanced brace strategy finds the first '{' (the invalid one) and
        # gives up after it fails json.loads — returns None.
        assert result is None

    def test_code_block_with_valid_json(self):
        """Test plain code block with valid JSON."""
        text = """
        Some text here
        ```
        {"valid": "json"}
        ```
        """
        result = _extract_json(text)
        assert result == '{"valid": "json"}'

    def test_balanced_brace_with_escaped_quotes(self):
        """Test balanced brace extraction with escaped quotes."""
        text = 'Some text {"key": "value with \\"escaped\\" quotes"} more text'
        result = _extract_json(text)
        assert result == '{"key": "value with \\"escaped\\" quotes"}'

    def test_balanced_brace_with_nested_structures(self):
        """Test balanced brace extraction with deeply nested JSON."""
        text = 'Prefix {"outer": {"inner": {"deep": "value"}}} suffix'
        result = _extract_json(text)
        assert result == '{"outer": {"inner": {"deep": "value"}}}'

    def test_array_extraction_over_object(self):
        """Test array extraction when array comes before object."""
        text = 'Text [{"item1": 1}, {"item2": 2}] and {"object": "value"}'
        result = _extract_json(text)
        assert result == '[{"item1": 1}, {"item2": 2}]'

    def test_no_json_structures_found(self):
        """Test text with no JSON structures."""
        text = "This is just plain text without any JSON structures."
        result = _extract_json(text)
        assert result is None

    def test_empty_string(self):
        """Test empty string input."""
        result = _extract_json("")
        assert result is None

    def test_whitespace_only(self):
        """Test whitespace-only input."""
        result = _extract_json("   \n\t  \n  ")
        assert result is None

    def test_unbalanced_braces_no_match(self):
        """Test text with unbalanced braces."""
        text = 'Text { "incomplete": "json"'  # Missing closing brace
        result = _extract_json(text)
        assert result is None

    def test_json_with_escaped_backslash(self):
        """Test JSON with escaped backslash characters."""
        text = '{"path": "C:\\\\Users\\\\test\\\\file.txt"}'
        result = _extract_json(text)
        assert result == '{"path": "C:\\\\Users\\\\test\\\\file.txt"}'


class TestSafeJsonParseEdgeCases:
    """Test edge cases in safe_json_parse function."""

    def test_safe_json_parse_none_input(self):
        """Test safe_json_parse with None input."""
        result = safe_json_parse(None)
        assert result is None

    def test_safe_json_parse_empty_string(self):
        """Test safe_json_parse with empty string."""
        result = safe_json_parse("")
        assert result is None

    def test_safe_json_parse_invalid_json(self):
        """Test safe_json_parse with invalid JSON."""
        result = safe_json_parse('{"invalid": }')
        assert result is None

    def test_safe_json_parse_valid_dict(self):
        """Test safe_json_parse with valid JSON object."""
        result = safe_json_parse('{"key": "value"}')
        assert result == {"key": "value"}

    def test_safe_json_parse_valid_list(self):
        """Test safe_json_parse with valid JSON array."""
        result = safe_json_parse("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_safe_json_parse_json_decode_error(self):
        """Test safe_json_parse handles JSONDecodeError gracefully."""
        result = safe_json_parse("invalid json {")
        assert result is None


class TestValidatePydanticResponseEdgeCases:
    """Test edge cases in validate_pydantic_response."""

    def test_validate_with_valid_dict(self):
        """Test validation with valid dictionary."""
        data = {"action": "idle", "reason": "System is running well", "priority": 5}
        result = validate_pydantic_response(data, HeartbeatDecision)
        assert isinstance(result, HeartbeatDecision)
        assert result.action == "idle"

    def test_validate_with_invalid_data_type(self):
        """Test validation with invalid data type."""
        data = ["not", "a", "dict"]  # List instead of dict

        with pytest.raises(ValidationError):
            validate_pydantic_response(data, HeartbeatDecision)

    def test_validate_with_missing_required_fields(self):
        """Test validation with missing required fields."""
        data = {"reason": "test"}  # Missing required action field

        with pytest.raises(ValidationError):
            validate_pydantic_response(data, HeartbeatDecision)

    def test_validate_with_invalid_enum_value(self):
        """Test validation with invalid field value."""
        data = {
            "action": "research",
            "priority": 15,  # Invalid priority (should be 1-10)
        }

        with pytest.raises(ValidationError):
            validate_pydantic_response(data, HeartbeatDecision)


class TestParseAndValidateEdgeCases:
    """Test edge cases in parse_and_validate function."""

    def test_parse_and_validate_success(self):
        """Test successful parse and validation."""
        text = """
        Some response text with JSON:
        ```json
        {
            "action": "idle",
            "reason": "All systems operational",
            "priority": 5
        }
        ```
        """
        result = parse_and_validate(text, HeartbeatDecision)
        assert result is not None
        assert result.action == "idle"
        assert "All systems operational" in result.reason

    def test_parse_and_validate_no_json_found(self):
        """Test parse_and_validate when no JSON is found."""
        text = "This is just plain text with no JSON structures."
        result = parse_and_validate(text, HeartbeatDecision)
        assert result is None

    def test_parse_and_validate_invalid_json(self):
        """Test parse_and_validate with invalid JSON."""
        text = """
        ```json
        {"invalid": "json", "missing": }
        ```
        """
        result = parse_and_validate(text, HeartbeatDecision)
        assert result is None

    def test_parse_and_validate_json_doesnt_match_schema(self):
        """Test parse_and_validate with JSON that doesn't match schema."""
        text = """
        ```json
        {"wrong_field": "value"}
        ```
        """

        with patch("utils.structured_output.logger") as mock_logger:
            result = parse_and_validate(text, HeartbeatDecision)
            assert result is None
            # Should log validation error
            mock_logger.warning.assert_called()

    def test_parse_and_validate_with_logging_error(self):
        """Test parse_and_validate when logging fails — exception propagates."""
        text = """
        ```json
        {"wrong_field": "value"}
        ```
        """

        with patch("utils.structured_output.logger") as mock_logger:
            mock_logger.warning.side_effect = Exception("Logging failed")

            # Logger exception propagates since parse_and_validate doesn't catch it
            with pytest.raises(Exception, match="Logging failed"):
                parse_and_validate(text, HeartbeatDecision)

    def test_parse_and_validate_exception_in_validation(self):
        """Test parse_and_validate when model_validate raises unexpected exception.

        parse_and_validate only catches ValidationError, so a generic Exception
        from model_validate propagates to the caller.
        """
        text = '{"action": "idle", "reason": "test", "priority": 5}'

        with (
            patch.object(
                HeartbeatDecision,
                "model_validate",
                side_effect=Exception("Unexpected validation error"),
            ),
            pytest.raises(Exception, match="Unexpected validation error"),
        ):
            parse_and_validate(text, HeartbeatDecision)


class TestStructuredOutputPrivateHelpers:
    """Test private helper functions for coverage."""

    def test_build_schema_instruction_coverage(self):
        """Test _build_schema_instruction function."""
        from utils.structured_output import _build_schema_instruction

        instruction = _build_schema_instruction(HeartbeatDecision)
        assert isinstance(instruction, str)
        assert len(instruction) > 0
        # Should contain some indication of JSON format
        assert "json" in instruction.lower() or "JSON" in instruction

    def test_build_schema_instruction_with_non_pydantic_model(self):
        """Test _build_schema_instruction raises AttributeError for non-Pydantic model."""
        from utils.structured_output import _build_schema_instruction

        class NonPydanticModel:
            pass

        with pytest.raises(AttributeError):
            _build_schema_instruction(NonPydanticModel)
