"""Basic tests for utils/structured_output.py - critical output formatting with 0% coverage."""

from unittest.mock import Mock, patch

import pytest
from pydantic import BaseModel, ValidationError

# Import the structured output module
from utils.structured_output import (
    _extract_json,
    parse_and_validate,
    safe_json_parse,
    structured_call,
    validate_pydantic_response,
)


class SampleModel(BaseModel):
    """Sample Pydantic model for testing."""

    name: str
    age: int
    email: str | None = None
    tags: list[str] = []


class TestSafeJsonParse:
    """Test safe JSON parsing functionality."""

    def test_valid_json(self):
        """Test parsing valid JSON."""
        json_str = '{"name": "Alice", "age": 30}'
        result = safe_json_parse(json_str)
        assert result == {"name": "Alice", "age": 30}

    def test_invalid_json(self):
        """Test parsing invalid JSON returns None."""
        json_str = '{"name": "Alice", "age":}'
        result = safe_json_parse(json_str)
        assert result is None

    def test_empty_string(self):
        """Test parsing empty string returns None."""
        result = safe_json_parse("")
        assert result is None

    def test_none_input(self):
        """Test parsing None returns None."""
        result = safe_json_parse(None)
        assert result is None

    def test_json_with_extra_whitespace(self):
        """Test parsing JSON with extra whitespace."""
        json_str = '  {"name": "Alice"}  '
        result = safe_json_parse(json_str)
        assert result == {"name": "Alice"}

    def test_complex_json(self):
        """Test parsing complex JSON structure."""
        json_str = """
        {
            "name": "Alice",
            "age": 30,
            "tags": ["developer", "python"],
            "nested": {"city": "NYC"}
        }
        """
        result = safe_json_parse(json_str)
        assert result["name"] == "Alice"
        assert result["tags"] == ["developer", "python"]
        assert result["nested"]["city"] == "NYC"


class TestValidatePydanticResponse:
    """Test Pydantic response validation."""

    def test_valid_data(self):
        """Test validation with valid data."""
        data = {"name": "Alice", "age": 30}
        result = validate_pydantic_response(data, SampleModel)

        assert isinstance(result, SampleModel)
        assert result.name == "Alice"
        assert result.age == 30
        assert result.email is None

    def test_valid_data_with_optional_fields(self):
        """Test validation with optional fields."""
        data = {"name": "Bob", "age": 25, "email": "bob@example.com", "tags": ["tester", "qa"]}
        result = validate_pydantic_response(data, SampleModel)

        assert result.name == "Bob"
        assert result.email == "bob@example.com"
        assert result.tags == ["tester", "qa"]

    def test_missing_required_field(self):
        """Test validation with missing required field."""
        data = {"name": "Charlie"}  # Missing required 'age'

        with pytest.raises(ValidationError):
            validate_pydantic_response(data, SampleModel)

    def test_invalid_field_type(self):
        """Test validation with invalid field type."""
        data = {"name": "David", "age": "thirty"}  # Age should be int

        with pytest.raises(ValidationError):
            validate_pydantic_response(data, SampleModel)

    def test_extra_fields_ignored(self):
        """Test that extra fields are ignored."""
        data = {"name": "Eve", "age": 28, "extra_field": "should be ignored"}
        result = validate_pydantic_response(data, SampleModel)

        assert result.name == "Eve"
        assert result.age == 28
        assert not hasattr(result, "extra_field")


class TestParseAndValidate:
    """Test combined parsing and validation functionality."""

    def test_valid_json_response(self):
        """Test parsing and validation with valid JSON."""
        response = '{"name": "Alice", "age": 30}'
        result = parse_and_validate(response, SampleModel)

        assert isinstance(result, SampleModel)
        assert result.name == "Alice"
        assert result.age == 30

    def test_json_in_code_block(self):
        """Test parsing JSON from markdown code block."""
        response = """
        Here is the requested data:

        ```json
        {"name": "Bob", "age": 25}
        ```
        """
        result = parse_and_validate(response, SampleModel)

        assert isinstance(result, SampleModel)
        assert result.name == "Bob"
        assert result.age == 25

    def test_invalid_json(self):
        """Test parsing invalid JSON returns None."""
        response = "This is not JSON"
        result = parse_and_validate(response, SampleModel)
        assert result is None

    def test_json_validation_failure(self):
        """Test JSON that doesn't match schema returns None."""
        response = '{"name": "Charlie"}'  # Missing required 'age'
        result = parse_and_validate(response, SampleModel)
        assert result is None


class TestExtractJson:
    """Test JSON extraction functionality."""

    def test_extract_plain_json(self):
        """Test extracting plain JSON."""
        text = '{"name": "Alice", "age": 30}'
        result = _extract_json(text)
        assert result == '{"name": "Alice", "age": 30}'

    def test_extract_from_code_block(self):
        """Test extracting JSON from code block."""
        text = """
        ```json
        {"name": "Bob", "age": 25}
        ```
        """
        result = _extract_json(text)
        assert '"name": "Bob"' in result
        assert '"age": 25' in result

    def test_extract_nested_json(self):
        """Test extracting JSON with nested objects."""
        text = 'Here is some text {"name": "Alice", "data": {"city": "NYC"}} and more text'
        result = _extract_json(text)
        assert '"name": "Alice"' in result
        assert '"city": "NYC"' in result

    def test_no_json_found(self):
        """Test when no JSON is found."""
        text = "This is just plain text with no JSON"
        result = _extract_json(text)
        assert result is None


class TestStructuredCallMocked:
    """Test structured_call functionality with mocking."""

    @patch("utils.structured_output.subprocess.run")
    def test_successful_call(self, mock_run):
        """Test successful structured call."""
        # Mock subprocess response
        mock_result = Mock()
        mock_result.stdout = '{"name": "Alice", "age": 30}'
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = structured_call("Generate a person named Alice who is 30 years old", SampleModel)

        assert isinstance(result, SampleModel)
        assert result.name == "Alice"
        assert result.age == 30
        mock_run.assert_called_once()

    @patch("utils.structured_output.subprocess.run")
    def test_json_in_markdown(self, mock_run):
        """Test extraction from markdown response."""
        # Mock subprocess response with markdown
        mock_result = Mock()
        mock_result.stdout = """
        Here is the data you requested:

        ```json
        {"name": "Bob", "age": 25}
        ```
        """
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = structured_call("Generate a person", SampleModel)

        assert isinstance(result, SampleModel)
        assert result.name == "Bob"
        assert result.age == 25

    @patch("utils.structured_output.subprocess.run")
    def test_subprocess_timeout(self, mock_run):
        """Test handling of subprocess timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)

        result = structured_call("Generate a person", SampleModel, timeout=30)

        assert result is None

    @patch("utils.structured_output.subprocess.run")
    def test_invalid_response(self, mock_run):
        """Test handling of invalid JSON response."""
        # Mock subprocess response with invalid JSON
        mock_result = Mock()
        mock_result.stdout = "This is not valid JSON"
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = structured_call("Generate a person", SampleModel)

        assert result is None

    @patch("utils.structured_output.subprocess.run")
    def test_empty_response(self, mock_run):
        """Test handling of empty response."""
        # Mock subprocess response with empty output
        mock_result = Mock()
        mock_result.stdout = ""
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = structured_call("Generate a person", SampleModel)

        assert result is None
