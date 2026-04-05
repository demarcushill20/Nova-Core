"""Comprehensive coverage tests for utils.structured_output module.

Tests all untested edge cases, error conditions, and fallback strategies identified in coverage analysis.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from utils.structured_output import (
    BASE,
    DEFAULT_CLAUDE_BIN,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    StructuredOutputError,
    _build_schema_instruction,
    _extract_json,
    parse_and_validate,
    safe_json_parse,
    structured_call,
    validate_pydantic_response,
)


class SimpleModel(BaseModel):
    """Simple test model for validation."""

    name: str
    value: int


class NestedModel(BaseModel):
    """Nested test model."""

    simple: SimpleModel
    items: list[str]
    optional_field: str = "default"


class TestSchemaInstructionBuilder:
    """Test schema instruction building."""

    def test_build_schema_instruction_simple(self):
        """Test schema instruction for simple model."""
        instruction = _build_schema_instruction(SimpleModel)

        assert "JSON Schema:" in instruction
        assert "IMPORTANT: You MUST respond with ONLY a valid JSON object" in instruction
        assert "name" in instruction
        assert "value" in instruction
        assert "string" in instruction
        assert "integer" in instruction

    def test_build_schema_instruction_removes_title(self):
        """Test that title is removed from schema for cleanliness."""
        instruction = _build_schema_instruction(SimpleModel)
        # Extract the JSON schema part
        start_marker = "```json\n"
        end_marker = "\n```"
        schema_start = instruction.find(start_marker) + len(start_marker)
        schema_end = instruction.find(end_marker, schema_start)
        schema_text = instruction[schema_start:schema_end]

        schema_dict = json.loads(schema_text)
        assert "title" not in schema_dict

    def test_build_schema_instruction_nested(self):
        """Test schema instruction for nested model."""
        instruction = _build_schema_instruction(NestedModel)

        assert "simple" in instruction
        assert "items" in instruction
        assert "optional_field" in instruction
        assert "array" in instruction


class TestJsonExtraction:
    """Test JSON extraction from various text formats."""

    def test_extract_json_pure_json(self):
        """Test extraction from pure JSON."""
        json_text = '{"name": "test", "value": 42}'
        result = _extract_json(json_text)
        assert result == '{"name": "test", "value": 42}'

    def test_extract_json_markdown_block(self):
        """Test extraction from markdown code block."""
        text = """
        Some explanation text.

        ```json
        {"name": "test", "value": 42}
        ```

        More text after.
        """
        result = _extract_json(text)
        assert '{"name": "test", "value": 42}' in result

    def test_extract_json_mixed_text(self):
        """Test extraction from mixed text with embedded JSON."""
        text = """
        Here is the result:
        {"name": "test", "value": 42}
        That's the answer.
        """
        result = _extract_json(text)
        assert result is not None
        assert "test" in result

    def test_extract_json_no_valid_json(self):
        """Test extraction when no valid JSON exists."""
        text = "Just some text without any JSON content."
        result = _extract_json(text)
        assert result is None

    def test_extract_json_multiple_objects(self):
        """Test extraction when multiple JSON objects exist."""
        text = """
        First: {"name": "first", "value": 1}
        Second: {"name": "second", "value": 2}
        """
        result = _extract_json(text)
        assert result is not None
        assert "first" in result or "second" in result


class TestSafeJsonParse:
    """Test safe JSON parsing functionality."""

    def test_safe_json_parse_valid_dict(self):
        """Test parsing valid JSON object."""
        json_str = '{"name": "test", "value": 42}'
        result = safe_json_parse(json_str)
        assert result == {"name": "test", "value": 42}

    def test_safe_json_parse_valid_list(self):
        """Test parsing valid JSON array."""
        json_str = '[1, 2, 3, "test"]'
        result = safe_json_parse(json_str)
        assert result == [1, 2, 3, "test"]

    def test_safe_json_parse_invalid_json(self):
        """Test parsing invalid JSON."""
        json_str = '{"name": "test", "value": invalid}'
        result = safe_json_parse(json_str)
        assert result is None

    def test_safe_json_parse_none_input(self):
        """Test parsing None input."""
        result = safe_json_parse(None)
        assert result is None

    def test_safe_json_parse_empty_string(self):
        """Test parsing empty string."""
        result = safe_json_parse("")
        assert result is None

    def test_safe_json_parse_whitespace(self):
        """Test parsing whitespace-only string."""
        result = safe_json_parse("   \n  \t  ")
        assert result is None


class TestPydanticValidation:
    """Test Pydantic model validation."""

    def test_validate_pydantic_response_success(self):
        """Test successful validation."""
        data = {"name": "test", "value": 42}
        result = validate_pydantic_response(data, SimpleModel)
        assert isinstance(result, SimpleModel)
        assert result.name == "test"
        assert result.value == 42

    def test_validate_pydantic_response_validation_error(self):
        """Test validation error handling."""
        data = {"name": "test", "value": "not_an_int"}
        with pytest.raises(ValidationError):
            validate_pydantic_response(data, SimpleModel)

    def test_validate_pydantic_response_missing_required(self):
        """Test validation with missing required fields."""
        data = {"name": "test"}  # missing 'value'
        with pytest.raises(ValidationError):
            validate_pydantic_response(data, SimpleModel)

    def test_validate_pydantic_response_extra_fields(self):
        """Test validation with extra fields (should be ignored)."""
        data = {"name": "test", "value": 42, "extra": "ignored"}
        result = validate_pydantic_response(data, SimpleModel)
        assert result.name == "test"
        assert result.value == 42

    def test_validate_pydantic_response_nested_model(self):
        """Test validation with nested model."""
        data = {"simple": {"name": "nested", "value": 123}, "items": ["item1", "item2"], "optional_field": "custom"}
        result = validate_pydantic_response(data, NestedModel)
        assert isinstance(result, NestedModel)
        assert result.simple.name == "nested"
        assert result.simple.value == 123
        assert result.items == ["item1", "item2"]
        assert result.optional_field == "custom"


class TestParseAndValidate:
    """Test the parse_and_validate function."""

    def test_parse_and_validate_success(self):
        """Test successful parse and validation."""
        text = '{"name": "test", "value": 42}'
        result = parse_and_validate(text, SimpleModel)
        assert isinstance(result, SimpleModel)
        assert result.name == "test"
        assert result.value == 42

    def test_parse_and_validate_from_markdown(self):
        """Test parse and validation from markdown."""
        text = """
        Here's the result:
        ```json
        {"name": "extracted", "value": 123}
        ```
        """
        result = parse_and_validate(text, SimpleModel)
        assert isinstance(result, SimpleModel)
        assert result.name == "extracted"
        assert result.value == 123

    def test_parse_and_validate_invalid_json(self):
        """Test parse and validation with invalid JSON."""
        text = "invalid json content"
        result = parse_and_validate(text, SimpleModel)
        assert result is None

    def test_parse_and_validate_validation_error(self):
        """Test parse and validation with validation error."""
        text = '{"name": "test", "value": "invalid"}'
        result = parse_and_validate(text, SimpleModel)
        assert result is None

    def test_parse_and_validate_empty_input(self):
        """Test parse and validation with empty input."""
        result = parse_and_validate("", SimpleModel)
        assert result is None


class TestStructuredCall:
    """Test the main structured_call function."""

    def test_structured_call_success(self):
        """Test successful structured call."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"name": "test", "value": 42}'
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = structured_call("test prompt", SimpleModel)

        assert isinstance(result, SimpleModel)
        assert result.name == "test"
        assert result.value == 42

    def test_structured_call_from_markdown(self):
        """Test structured call with markdown response."""
        response_text = """
        Here's your answer:

        ```json
        {"name": "extracted", "value": 123}
        ```
        """

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = response_text
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = structured_call("test prompt", SimpleModel)

        assert isinstance(result, SimpleModel)
        assert result.name == "extracted"
        assert result.value == 123

    def test_structured_call_failure(self):
        """Test structured call failure (returns None)."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "No JSON content here"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = structured_call("test prompt", SimpleModel)

        assert result is None

    def test_structured_call_subprocess_error(self):
        """Test structured call with subprocess error."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Command failed"

        with patch("subprocess.run", return_value=mock_result):
            result = structured_call("test prompt", SimpleModel)

        assert result is None

    def test_structured_call_subprocess_timeout(self):
        """Test structured call with subprocess timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            result = structured_call("test prompt", SimpleModel)

        assert result is None

    def test_structured_call_subprocess_exception(self):
        """Test structured call with subprocess exception."""
        with patch("subprocess.run", side_effect=Exception("Unexpected error")):
            result = structured_call("test prompt", SimpleModel)

        assert result is None

    def test_structured_call_custom_parameters(self):
        """Test structured call with custom parameters."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"name": "custom", "value": 789}'
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = structured_call(
                "test prompt", SimpleModel, claude_bin="/custom/claude", model="haiku", timeout=60, cwd="/custom/dir"
            )

        # Verify subprocess was called with custom parameters
        call_args = mock_run.call_args
        assert "/custom/claude" in call_args[0][0]
        assert "--model" in call_args[0][0]
        assert "haiku" in call_args[0][0]
        assert call_args[1]["timeout"] == 60
        assert call_args[1]["cwd"] == "/custom/dir"

        assert isinstance(result, SimpleModel)
        assert result.name == "custom"
        assert result.value == 789

    def test_structured_call_working_directory_passthrough(self):
        """Test working directory is passed through to subprocess."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"name": "test", "value": 42}'
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            structured_call("test prompt", SimpleModel, cwd="/tmp/test")

        call_args = mock_run.call_args
        assert call_args[1]["cwd"] == "/tmp/test"

    def test_structured_call_empty_stdout(self):
        """Test structured call with empty stdout."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = structured_call("test prompt", SimpleModel)

        assert result is None


class TestStructuredOutputError:
    """Test StructuredOutputError exception."""

    def test_structured_output_error_creation(self):
        """Test StructuredOutputError can be created and raised."""
        error = StructuredOutputError("Test error")
        assert str(error) == "Test error"
        assert isinstance(error, Exception)

    def test_structured_output_error_with_cause(self):
        """Test StructuredOutputError with underlying cause."""
        try:
            json.loads("invalid json")
        except json.JSONDecodeError as e:
            error = StructuredOutputError("Parsing failed")
            error.__cause__ = e
            assert str(error) == "Parsing failed"
            assert error.__cause__ == e


class TestConstants:
    """Test module constants."""

    def test_default_constants(self):
        """Test default constant values."""
        assert DEFAULT_CLAUDE_BIN == "/home/nova/.local/bin/claude"
        assert DEFAULT_MODEL == "sonnet"
        assert DEFAULT_TIMEOUT == 120
        assert isinstance(BASE, Path)

    def test_base_path_resolution(self):
        """Test BASE path points to correct directory."""
        # BASE should point to the parent directory of utils/
        assert BASE.name == "nova-core" or "nova" in str(BASE)


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_unicode_handling(self):
        """Test handling of Unicode characters."""
        data = {"name": "tést 🚀", "value": 42}
        json_str = json.dumps(data)

        result = safe_json_parse(json_str)
        assert result["name"] == "tést 🚀"

    def test_large_json_handling(self):
        """Test handling of large JSON responses."""
        large_data = {"name": "test", "value": 42, "large_field": "x" * 10000}
        json_str = json.dumps(large_data)

        result = safe_json_parse(json_str)
        assert result["name"] == "test"
        assert len(result["large_field"]) == 10000

    def test_nested_json_parsing(self):
        """Test parsing deeply nested JSON."""
        nested_data = {"level1": {"level2": {"level3": {"name": "deep", "value": 123}}}}
        json_str = json.dumps(nested_data)

        result = safe_json_parse(json_str)
        assert result["level1"]["level2"]["level3"]["name"] == "deep"

    def test_special_characters_in_json(self):
        """Test JSON with special characters."""
        special_data = {
            "name": 'test\n\t\r"\\',
            "value": 42,
            "quotes": 'He said "Hello"',
            "backslashes": "C:\\Users\\test",
        }
        json_str = json.dumps(special_data)

        result = safe_json_parse(json_str)
        assert result["name"] == 'test\n\t\r"\\'
        assert result["quotes"] == 'He said "Hello"'
        assert result["backslashes"] == "C:\\Users\\test"
