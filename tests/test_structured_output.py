"""Tests for utils/structured_output.py — structured Claude CLI output parsing.

Covers all 4 tiers of the fallback strategy:
  Tier 1: Direct JSON parse
  Tier 2: Extract from markdown/code blocks
  Tier 3: Retry with error feedback
  Tier 4: Return None
"""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from utils.schemas.contract import ContractOutput
from utils.schemas.heartbeat import HeartbeatDecision
from utils.schemas.task import TaskResult
from utils.schemas.trading import SignalAction, TradeSignal
from utils.structured_output import (
    _build_schema_instruction,
    _extract_json,
    parse_and_validate,
    structured_call,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class SimpleModel(BaseModel):
    """Minimal model for testing."""

    name: str
    value: int = 0


class StrictModel(BaseModel):
    """Model with required fields only — useful for missing-field tests."""

    x: int
    y: int
    label: str


VALID_HEARTBEAT_JSON = json.dumps(
    {
        "action": "research",
        "reason": "Need to investigate caching",
        "priority": 7,
        "target": "llm_cache",
        "estimated_duration_minutes": 45,
    }
)

VALID_TASK_RESULT_JSON = json.dumps(
    {
        "summary": "Implemented the feature",
        "files_changed": ["utils/foo.py", "tests/test_foo.py"],
        "confidence": "high",
        "errors": [],
        "warnings": [],
        "next_actions": ["deploy"],
    }
)

VALID_TRADE_SIGNAL_JSON = json.dumps(
    {
        "action": "signal",
        "symbol": "EURUSD",
        "side": "buy",
        "entry_price": 1.0850,
        "stop_loss": 1.0800,
        "take_profit": 1.0950,
        "lot_size": 0.1,
        "timeframe": "4H",
        "strategy": "breakout",
        "confidence": 0.85,
    }
)

VALID_SIMPLE_JSON = json.dumps({"name": "test", "value": 42})


def _mock_subprocess_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


# ===========================================================================
# 1. _extract_json() tests
# ===========================================================================


class TestExtractJson:
    """Tests for JSON extraction from various text formats."""

    def test_pure_json_object(self):
        """Pure JSON string returns as-is."""
        raw = '{"name": "hello", "value": 1}'
        assert _extract_json(raw) == raw

    def test_pure_json_with_whitespace(self):
        """JSON with leading/trailing whitespace is handled."""
        raw = '  \n  {"name": "hello"}\n  '
        result = _extract_json(raw)
        assert result is not None
        assert json.loads(result) == {"name": "hello"}

    def test_json_in_json_code_block(self):
        """JSON inside ```json ... ``` code block is extracted."""
        text = 'Here is the result:\n```json\n{"name": "test", "value": 5}\n```\nDone.'
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result) == {"name": "test", "value": 5}

    def test_json_in_plain_code_block(self):
        """JSON inside plain ``` ... ``` code block is extracted."""
        text = 'Output:\n```\n{"name": "plain", "value": 99}\n```'
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result) == {"name": "plain", "value": 99}

    def test_json_embedded_in_prose(self):
        """JSON embedded in surrounding prose text is extracted via brace matching."""
        text = (
            "I analyzed the situation and here is my decision:\n\n"
            '{"action": "research", "reason": "found gap", "priority": 3, '
            '"target": "caching", "estimated_duration_minutes": 30}\n\n'
            "Let me know if you need more details."
        )
        result = _extract_json(text)
        assert result is not None
        data = json.loads(result)
        assert data["action"] == "research"
        assert data["priority"] == 3

    def test_nested_json_objects(self):
        """Nested JSON objects are correctly matched with balanced braces."""
        nested = json.dumps(
            {
                "outer": {"inner": {"deep": "value"}},
                "list": [1, 2, {"nested": True}],
            }
        )
        text = f"Result: {nested} end."
        result = _extract_json(text)
        assert result is not None
        data = json.loads(result)
        assert data["outer"]["inner"]["deep"] == "value"
        assert data["list"][2]["nested"] is True

    def test_json_array(self):
        """JSON array is extracted."""
        text = 'Items: [1, 2, 3, "four"]'
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result) == [1, 2, 3, "four"]

    def test_json_array_of_objects(self):
        """JSON array of objects is extracted."""
        arr = json.dumps([{"a": 1}, {"b": 2}])
        result = _extract_json(f"Here: {arr}")
        assert result is not None
        assert json.loads(result) == [{"a": 1}, {"b": 2}]

    def test_no_json_returns_none(self):
        """Text with no JSON returns None."""
        assert _extract_json("Just some plain text with no JSON at all.") is None
        assert _extract_json("") is None

    def test_invalid_json_returns_none(self):
        """Malformed JSON that can't be parsed returns None."""
        assert _extract_json('{"broken": missing_quotes}') is None

    def test_multiple_json_blocks_returns_first(self):
        """When multiple JSON blocks exist, the first valid one is returned."""
        text = '```json\n{"name": "first", "value": 1}\n```\n```json\n{"name": "second", "value": 2}\n```'
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result)["name"] == "first"

    def test_json_with_escaped_quotes(self):
        """JSON containing escaped quotes in strings is handled."""
        raw = '{"name": "say \\"hello\\"", "value": 0}'
        result = _extract_json(raw)
        assert result is not None
        data = json.loads(result)
        assert "hello" in data["name"]

    def test_json_with_braces_in_strings(self):
        """JSON with brace characters inside string values doesn't confuse parsing."""
        raw = '{"name": "has { braces } inside", "value": 1}'
        result = _extract_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["name"] == "has { braces } inside"
        assert data["value"] == 1

    def test_object_preferred_over_array_when_first(self):
        """When { comes before [, object is extracted."""
        text = 'Result: {"a": 1} and also [1, 2]'
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result) == {"a": 1}

    def test_array_preferred_when_first(self):
        """When [ comes before {, array is extracted."""
        text = 'Result: [1, 2, 3] and {"a": 1}'
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result) == [1, 2, 3]


# ===========================================================================
# 2. _build_schema_instruction() tests
# ===========================================================================


class TestBuildSchemaInstruction:
    """Tests for schema instruction generation."""

    def test_contains_json_schema_label(self):
        instruction = _build_schema_instruction(SimpleModel)
        assert "JSON Schema" in instruction

    def test_contains_field_names_simple(self):
        instruction = _build_schema_instruction(SimpleModel)
        assert '"name"' in instruction
        assert '"value"' in instruction

    def test_contains_field_names_heartbeat(self):
        instruction = _build_schema_instruction(HeartbeatDecision)
        assert '"action"' in instruction
        assert '"reason"' in instruction
        assert '"priority"' in instruction
        assert '"target"' in instruction
        assert '"estimated_duration_minutes"' in instruction

    def test_contains_field_names_task_result(self):
        instruction = _build_schema_instruction(TaskResult)
        assert '"summary"' in instruction
        assert '"files_changed"' in instruction
        assert '"confidence"' in instruction

    def test_contains_field_names_trade_signal(self):
        instruction = _build_schema_instruction(TradeSignal)
        assert '"action"' in instruction
        assert '"symbol"' in instruction
        assert '"side"' in instruction
        assert '"entry_price"' in instruction

    def test_contains_respond_only_instruction(self):
        instruction = _build_schema_instruction(SimpleModel)
        assert "ONLY" in instruction
        assert "valid JSON" in instruction

    def test_does_not_include_title_key(self):
        """The top-level 'title' key is stripped from the schema."""
        instruction = _build_schema_instruction(SimpleModel)
        # Parse the JSON from the instruction to check
        # The schema should not have "title" at the top level
        # But it might appear in property descriptions — that's fine
        assert '"title": "SimpleModel"' not in instruction

    def test_valid_json_in_instruction(self):
        """The embedded schema is valid JSON."""
        instruction = _build_schema_instruction(HeartbeatDecision)
        # Extract JSON block from the instruction
        import re

        match = re.search(r"```json\n(.*?)\n```", instruction, re.DOTALL)
        assert match is not None
        schema = json.loads(match.group(1))
        assert "properties" in schema


# ===========================================================================
# 3. parse_and_validate() tests
# ===========================================================================


class TestParseAndValidate:
    """Tests for combined parsing and validation."""

    def test_valid_heartbeat_json(self):
        """Valid HeartbeatDecision JSON returns model instance."""
        result = parse_and_validate(VALID_HEARTBEAT_JSON, HeartbeatDecision)
        assert result is not None
        assert isinstance(result, HeartbeatDecision)
        assert result.action == "research"
        assert result.priority == 7
        assert result.target == "llm_cache"

    def test_valid_task_result_json(self):
        """Valid TaskResult JSON returns model instance."""
        result = parse_and_validate(VALID_TASK_RESULT_JSON, TaskResult)
        assert result is not None
        assert isinstance(result, TaskResult)
        assert result.summary == "Implemented the feature"
        assert "utils/foo.py" in result.files_changed

    def test_valid_json_wrong_fields(self):
        """Valid JSON but wrong fields for model returns None."""
        wrong = json.dumps({"completely": "wrong", "fields": "here"})
        result = parse_and_validate(wrong, StrictModel)
        assert result is None

    def test_valid_json_in_code_block_validates(self):
        """JSON inside code block is extracted and validated."""
        text = f"```json\n{VALID_HEARTBEAT_JSON}\n```"
        result = parse_and_validate(text, HeartbeatDecision)
        assert result is not None
        assert result.action == "research"

    def test_no_json_returns_none(self):
        """Text with no JSON returns None."""
        result = parse_and_validate("No JSON here at all.", SimpleModel)
        assert result is None

    def test_extra_fields_still_validates(self):
        """JSON with extra fields still validates (Pydantic ignores extras by default)."""
        data = {"name": "test", "value": 10, "extra_field": "should be ignored"}
        result = parse_and_validate(json.dumps(data), SimpleModel)
        assert result is not None
        assert result.name == "test"
        assert result.value == 10

    def test_missing_required_field_returns_none(self):
        """JSON missing a required field returns None."""
        data = {"x": 1, "y": 2}  # missing 'label'
        result = parse_and_validate(json.dumps(data), StrictModel)
        assert result is None

    def test_missing_optional_field_uses_default(self):
        """JSON missing optional field uses default value."""
        data = {"name": "minimal"}
        result = parse_and_validate(json.dumps(data), SimpleModel)
        assert result is not None
        assert result.value == 0  # default

    def test_heartbeat_defaults(self):
        """HeartbeatDecision with only required fields uses defaults."""
        minimal = json.dumps({"action": "idle"})
        result = parse_and_validate(minimal, HeartbeatDecision)
        assert result is not None
        assert result.action == "idle"
        assert result.priority == 5  # default
        assert result.estimated_duration_minutes == 30  # default

    def test_trade_signal_validates(self):
        """TradeSignal with full data validates correctly."""
        result = parse_and_validate(VALID_TRADE_SIGNAL_JSON, TradeSignal)
        assert result is not None
        assert result.action == SignalAction.SIGNAL
        assert result.symbol == "EURUSD"
        assert result.confidence == 0.85

    def test_contract_output_validates(self):
        """ContractOutput validates correctly."""
        data = json.dumps(
            {
                "summary": "Did the work",
                "files_changed": ["a.py"],
                "confidence": "high",
                "tests_passed": True,
                "test_count": 5,
            }
        )
        result = parse_and_validate(data, ContractOutput)
        assert result is not None
        assert result.tests_passed is True
        assert result.test_count == 5

    def test_validation_error_logged(self, caplog):
        """Validation errors are logged as warnings."""
        import logging

        with caplog.at_level(logging.WARNING):
            data = json.dumps({"x": "not_an_int", "y": 2, "label": "test"})
            result = parse_and_validate(data, StrictModel)
            assert result is None
        assert "validation error" in caplog.text.lower()

    def test_no_json_logged(self, caplog):
        """Missing JSON is logged as warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            result = parse_and_validate("nothing here", SimpleModel)
            assert result is None
        assert "no JSON found" in caplog.text.lower() or "no json found" in caplog.text.lower()


# ===========================================================================
# 4. structured_call() tests (mock subprocess)
# ===========================================================================


class TestStructuredCall:
    """Tests for the full structured_call() function with mocked subprocess."""

    @patch("utils.structured_output.subprocess.run")
    def test_successful_response_returns_model(self, mock_run):
        """Valid JSON response returns validated model."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_HEARTBEAT_JSON)

        result = structured_call("test prompt", HeartbeatDecision, max_retries=0)
        assert result is not None
        assert isinstance(result, HeartbeatDecision)
        assert result.action == "research"
        assert result.priority == 7

    @patch("utils.structured_output.subprocess.run")
    def test_empty_response_returns_none(self, mock_run):
        """Empty stdout returns None."""
        mock_run.return_value = _mock_subprocess_result(stdout="")

        result = structured_call("test prompt", HeartbeatDecision, max_retries=0)
        assert result is None

    @patch("utils.structured_output.subprocess.run")
    def test_timeout_returns_none(self, mock_run):
        """Timeout returns None after exhausting retries."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)

        result = structured_call("test prompt", HeartbeatDecision, max_retries=0)
        assert result is None

    @patch("utils.structured_output.subprocess.run")
    def test_file_not_found_returns_none(self, mock_run):
        """Missing binary returns None immediately (no retry)."""
        mock_run.side_effect = FileNotFoundError("No such file")

        result = structured_call("test prompt", HeartbeatDecision, max_retries=1)
        assert result is None
        # FileNotFoundError should return immediately, not retry
        assert mock_run.call_count == 1

    @patch("utils.structured_output.subprocess.run")
    def test_retry_on_validation_failure(self, mock_run):
        """First attempt fails validation, retry succeeds."""
        bad_response = json.dumps({"x": 1})  # missing y and label
        good_response = json.dumps({"x": 1, "y": 2, "label": "ok"})

        mock_run.side_effect = [
            _mock_subprocess_result(stdout=bad_response),
            _mock_subprocess_result(stdout=good_response),
        ]

        result = structured_call("test", StrictModel, max_retries=1)
        assert result is not None
        assert result.x == 1
        assert result.y == 2
        assert result.label == "ok"
        assert mock_run.call_count == 2

    @patch("utils.structured_output.subprocess.run")
    def test_both_attempts_fail_returns_none(self, mock_run):
        """Both initial and retry fail validation → returns None."""
        bad = json.dumps({"x": 1})  # always missing y and label
        mock_run.return_value = _mock_subprocess_result(stdout=bad)

        result = structured_call("test", StrictModel, max_retries=1)
        assert result is None
        assert mock_run.call_count == 2

    @patch("utils.structured_output.subprocess.run")
    def test_inject_schema_false(self, mock_run):
        """inject_schema=False does not append schema to prompt."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        structured_call("my prompt", SimpleModel, inject_schema=False, max_retries=0)

        # Check the prompt passed to subprocess
        call_args = mock_run.call_args
        cmd_list = call_args[0][0]
        prompt_arg = cmd_list[-1]  # last element is the prompt
        assert "JSON Schema" not in prompt_arg
        assert prompt_arg == "my prompt"

    @patch("utils.structured_output.subprocess.run")
    def test_inject_schema_true_appends_schema(self, mock_run):
        """inject_schema=True (default) appends schema instruction to prompt."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        structured_call("my prompt", SimpleModel, max_retries=0)

        call_args = mock_run.call_args
        cmd_list = call_args[0][0]
        prompt_arg = cmd_list[-1]
        assert "JSON Schema" in prompt_arg
        assert prompt_arg.startswith("my prompt")

    @patch("utils.structured_output.subprocess.run")
    def test_child_env_strips_claudecode(self, mock_run):
        """Child environment has CLAUDECODE and CLAUDE_CODE_ENTRYPOINT removed."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        # Set the env vars that should be stripped
        with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "test"}):
            structured_call("test", SimpleModel, max_retries=0)

        call_args = mock_run.call_args
        child_env = call_args[1].get("env") or call_args.kwargs.get("env")
        assert child_env is not None
        assert "CLAUDECODE" not in child_env
        assert "CLAUDE_CODE_ENTRYPOINT" not in child_env

    @patch("utils.structured_output.subprocess.run")
    def test_claude_bin_from_env(self, mock_run):
        """CLAUDE_BIN env var is used when no claude_bin argument given."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        with patch.dict(os.environ, {"CLAUDE_BIN": "/custom/claude"}):
            structured_call("test", SimpleModel, max_retries=0)

        cmd_list = mock_run.call_args[0][0]
        assert cmd_list[0] == "/custom/claude"

    @patch("utils.structured_output.subprocess.run")
    def test_claude_bin_argument_overrides_env(self, mock_run):
        """claude_bin argument overrides CLAUDE_BIN env var."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        with patch.dict(os.environ, {"CLAUDE_BIN": "/env/claude"}):
            structured_call("test", SimpleModel, claude_bin="/arg/claude", max_retries=0)

        cmd_list = mock_run.call_args[0][0]
        assert cmd_list[0] == "/arg/claude"

    @patch("utils.structured_output.subprocess.run")
    def test_subprocess_generic_exception(self, mock_run):
        """Generic subprocess exception returns None immediately."""
        mock_run.side_effect = OSError("Some OS error")

        result = structured_call("test", SimpleModel, max_retries=1)
        assert result is None
        assert mock_run.call_count == 1

    @patch("utils.structured_output.subprocess.run")
    def test_response_in_code_block_validates(self, mock_run):
        """Response with JSON in code block is extracted and validated."""
        wrapped = f"Here is my response:\n```json\n{VALID_HEARTBEAT_JSON}\n```\n"
        mock_run.return_value = _mock_subprocess_result(stdout=wrapped)

        result = structured_call("test", HeartbeatDecision, max_retries=0)
        assert result is not None
        assert result.action == "research"

    @patch("utils.structured_output.subprocess.run")
    def test_retry_prompt_includes_error_feedback(self, mock_run):
        """Retry prompt includes the validation error from previous attempt."""
        bad = json.dumps({"x": 1})  # missing y and label
        good = json.dumps({"x": 1, "y": 2, "label": "ok"})

        mock_run.side_effect = [
            _mock_subprocess_result(stdout=bad),
            _mock_subprocess_result(stdout=good),
        ]

        structured_call("test", StrictModel, max_retries=1)

        # Second call should have error feedback in the prompt
        assert mock_run.call_count == 2
        second_call_cmd = mock_run.call_args_list[1][0][0]
        second_prompt = second_call_cmd[-1]
        assert "failed validation" in second_prompt
        assert "fix the JSON" in second_prompt

    @patch("utils.structured_output.subprocess.run")
    def test_no_json_in_response_sets_error_for_retry(self, mock_run):
        """When response has no JSON, error message tells model to respond with JSON."""
        no_json = "I don't have any structured data for you."
        good = json.dumps({"x": 1, "y": 2, "label": "ok"})

        mock_run.side_effect = [
            _mock_subprocess_result(stdout=no_json),
            _mock_subprocess_result(stdout=good),
        ]

        result = structured_call("test", StrictModel, max_retries=1)
        assert result is not None

        second_prompt = mock_run.call_args_list[1][0][0][-1]
        assert "No JSON object found" in second_prompt

    @patch("utils.structured_output.subprocess.run")
    def test_max_retries_zero_no_retry(self, mock_run):
        """max_retries=0 means only one attempt."""
        bad = json.dumps({"x": 1})  # missing y and label
        mock_run.return_value = _mock_subprocess_result(stdout=bad)

        result = structured_call("test", StrictModel, max_retries=0)
        assert result is None
        assert mock_run.call_count == 1

    @patch("utils.structured_output.subprocess.run")
    def test_timeout_then_success(self, mock_run):
        """First call times out, second succeeds."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            _mock_subprocess_result(stdout=VALID_SIMPLE_JSON),
        ]

        result = structured_call("test", SimpleModel, max_retries=1)
        assert result is not None
        assert result.name == "test"
        assert result.value == 42

    @patch("utils.structured_output.subprocess.run")
    def test_model_argument_passed_to_subprocess(self, mock_run):
        """Custom model name is passed to claude CLI."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        structured_call("test", SimpleModel, model="opus", max_retries=0)

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"

    @patch("utils.structured_output.subprocess.run")
    def test_cwd_argument_passed_to_subprocess(self, mock_run):
        """Custom cwd is passed to subprocess."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        structured_call("test", SimpleModel, cwd="/tmp/test", max_retries=0)

        call_kwargs = mock_run.call_args[1] if mock_run.call_args[1] else {}
        # cwd could be in positional or keyword args
        if "cwd" in call_kwargs:
            assert call_kwargs["cwd"] == "/tmp/test"
        else:
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["cwd"] == "/tmp/test"

    @patch("utils.structured_output.subprocess.run")
    def test_dangerously_skip_permissions_flag(self, mock_run):
        """--dangerously-skip-permissions flag is included in subprocess call."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        structured_call("test", SimpleModel, max_retries=0)

        cmd = mock_run.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("utils.structured_output.subprocess.run")
    def test_trade_signal_end_to_end(self, mock_run):
        """End-to-end test with TradeSignal model."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_TRADE_SIGNAL_JSON)

        result = structured_call("analyze EURUSD", TradeSignal, max_retries=0)
        assert result is not None
        assert isinstance(result, TradeSignal)
        assert result.action == SignalAction.SIGNAL
        assert result.symbol == "EURUSD"
        assert result.side == "buy"
        assert result.confidence == 0.85

    @patch("utils.structured_output.subprocess.run")
    def test_task_result_end_to_end(self, mock_run):
        """End-to-end test with TaskResult model."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_TASK_RESULT_JSON)

        result = structured_call("do the work", TaskResult, max_retries=0)
        assert result is not None
        assert isinstance(result, TaskResult)
        assert result.summary == "Implemented the feature"
        assert len(result.files_changed) == 2


# ===========================================================================
# 5. Langfuse prompt versioning integration tests
# ===========================================================================


class TestLangfusePromptVersioning:
    """Tests for prompt_name/prompt_version parameters and Langfuse logging."""

    @patch("utils.structured_output.subprocess.run")
    @patch("utils.langfuse_tracing.trace_event")
    def test_prompt_name_logs_to_langfuse_on_success(self, mock_trace, mock_run):
        """structured_call with prompt_name logs to Langfuse on success."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        result = structured_call(
            "test prompt",
            SimpleModel,
            max_retries=0,
            prompt_name="test_prompt",
            prompt_version="1.5",
        )
        assert result is not None
        mock_trace.assert_called_once_with(
            event_type="structured_call",
            component="structured_output",
            prompt_name="test_prompt",
            prompt_version="1.5",
            model_class="SimpleModel",
            attempt=1,
            success=True,
        )

    @patch("utils.structured_output.subprocess.run")
    @patch("utils.langfuse_tracing.trace_event")
    def test_prompt_name_logs_to_langfuse_on_failure(self, mock_trace, mock_run):
        """structured_call with prompt_name logs to Langfuse on failure."""
        bad = json.dumps({"x": 1})  # missing y and label
        mock_run.return_value = _mock_subprocess_result(stdout=bad)

        result = structured_call(
            "test prompt",
            StrictModel,
            max_retries=0,
            prompt_name="failing_prompt",
            prompt_version="2.0",
        )
        assert result is None
        mock_trace.assert_called_once_with(
            event_type="structured_call",
            component="structured_output",
            prompt_name="failing_prompt",
            prompt_version="2.0",
            model_class="StrictModel",
            attempt=1,
            success=False,
        )

    @patch("utils.structured_output.subprocess.run")
    @patch("utils.langfuse_tracing.trace_event")
    def test_no_prompt_name_skips_langfuse(self, mock_trace, mock_run):
        """structured_call without prompt_name does not call trace_event."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        result = structured_call("test prompt", SimpleModel, max_retries=0)
        assert result is not None
        mock_trace.assert_not_called()

    @patch("utils.structured_output.subprocess.run")
    @patch("utils.langfuse_tracing.trace_event")
    def test_empty_prompt_name_skips_langfuse(self, mock_trace, mock_run):
        """structured_call with empty prompt_name does not call trace_event."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        result = structured_call("test prompt", SimpleModel, max_retries=0, prompt_name="")
        assert result is not None
        mock_trace.assert_not_called()

    @patch("utils.structured_output.subprocess.run")
    def test_langfuse_import_failure_does_not_break(self, mock_run):
        """If Langfuse import fails, structured_call still works normally."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        # Patch the import inside _log_structured_call to raise
        with patch.dict("sys.modules", {"utils.langfuse_tracing": None}):
            result = structured_call(
                "test prompt",
                SimpleModel,
                max_retries=0,
                prompt_name="should_not_break",
                prompt_version="1.0",
            )
            assert result is not None
            assert result.name == "test"
            assert result.value == 42

    @patch("utils.structured_output.subprocess.run")
    @patch("utils.langfuse_tracing.trace_event", side_effect=RuntimeError("Langfuse down"))
    def test_langfuse_exception_does_not_break(self, mock_trace, mock_run):
        """If trace_event raises, structured_call still returns result."""
        mock_run.return_value = _mock_subprocess_result(stdout=VALID_SIMPLE_JSON)

        result = structured_call(
            "test prompt",
            SimpleModel,
            max_retries=0,
            prompt_name="error_prompt",
            prompt_version="1.0",
        )
        assert result is not None
        assert result.name == "test"

    @patch("utils.structured_output.subprocess.run")
    @patch("utils.langfuse_tracing.trace_event")
    def test_retry_success_logs_correct_attempt(self, mock_trace, mock_run):
        """After retry, logged attempt number reflects the successful attempt."""
        bad = json.dumps({"x": 1})
        good = json.dumps({"x": 1, "y": 2, "label": "ok"})

        mock_run.side_effect = [
            _mock_subprocess_result(stdout=bad),
            _mock_subprocess_result(stdout=good),
        ]

        result = structured_call(
            "test",
            StrictModel,
            max_retries=1,
            prompt_name="retry_prompt",
            prompt_version="1.0",
        )
        assert result is not None
        mock_trace.assert_called_once_with(
            event_type="structured_call",
            component="structured_output",
            prompt_name="retry_prompt",
            prompt_version="1.0",
            model_class="StrictModel",
            attempt=2,
            success=True,
        )
