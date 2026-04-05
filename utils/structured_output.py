"""Structured output extraction from Claude CLI responses.

Provides structured_call() — a function that prompts Claude via CLI subprocess,
extracts JSON from the response, and validates it against a Pydantic v2 model.

4-tier fallback strategy:
  Tier 1: Direct JSON parse of full response
  Tier 2: Extract JSON from markdown code blocks or mixed text
  Tier 3: Retry with validation error feedback (1 retry)
  Tier 4: Return None (caller handles graceful degradation)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """Exception raised when structured output parsing or validation fails."""

    pass


BASE = Path(__file__).resolve().parent.parent
DEFAULT_CLAUDE_BIN = "/home/nova/.local/bin/claude"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 120


def _build_schema_instruction(model_class: type[T]) -> str:
    """Build a prompt suffix that instructs Claude to output JSON matching the schema."""
    schema = model_class.model_json_schema()
    # Clean up schema for readability — remove title clutter at top level
    clean = {k: v for k, v in schema.items() if k not in ("title",)}
    schema_str = json.dumps(clean, indent=2)
    return (
        "\n\n---\n"
        "IMPORTANT: You MUST respond with ONLY a valid JSON object matching this exact schema. "
        "No markdown, no explanation, no code fences — just the raw JSON.\n\n"
        f"JSON Schema:\n```json\n{schema_str}\n```\n"
    )


def _extract_json(text: str) -> str | None:
    """Extract JSON from text, trying multiple strategies.

    Strategy 1: Full text is valid JSON
    Strategy 2: JSON in ```json ... ``` or ``` ... ``` code blocks
    Strategy 3: First { ... } or [ ... ] block in text (greedy balanced brace matching)
    """
    stripped = text.strip()

    # Strategy 1: Full text is JSON
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            pass

    # Strategy 2: Code block extraction
    # Try ```json first, then plain ```
    patterns = [
        r"```json\s*\n?(.*?)\n?\s*```",
        r"```\s*\n?(.*?)\n?\s*```",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue

    # Strategy 3: Balanced brace extraction
    # Find first { and match to its closing }
    brace_start = stripped.find("{")
    bracket_start = stripped.find("[")

    # Pick whichever comes first (if both exist)
    if brace_start == -1 and bracket_start == -1:
        return None

    if brace_start == -1:
        start = bracket_start
    elif bracket_start == -1:
        start = brace_start
    else:
        start = min(brace_start, bracket_start)

    open_char = stripped[start]
    close_char = "}" if open_char == "{" else "]"

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(stripped)):
        c = stripped[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                candidate = stripped[start : i + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    break

    return None


def safe_json_parse(text: str | None) -> dict | list | None:
    """Safe JSON parsing that returns None instead of raising exceptions.

    Args:
        text: JSON string to parse, or None

    Returns:
        Parsed JSON object/array, or None if parsing fails or input is None/empty
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def validate_pydantic_response(data: dict | list, model_class: type[T]) -> T:
    """Validate data against a Pydantic model.

    Args:
        data: Dictionary or list to validate
        model_class: Pydantic model class to validate against

    Returns:
        Validated model instance

    Raises:
        ValidationError: If validation fails
    """
    return model_class.model_validate(data)


def parse_and_validate(text: str, model_class: type[T]) -> T | None:
    """Parse text to extract JSON and validate against a Pydantic model.

    Returns validated model instance or None if extraction/validation fails.
    Raises no exceptions — all errors are logged.
    """
    json_str = _extract_json(text)
    if json_str is None:
        logger.warning("structured_output: no JSON found in response (%d chars)", len(text))
        return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("structured_output: JSON parse error: %s", e)
        return None

    try:
        return model_class.model_validate(data)
    except ValidationError as e:
        logger.warning("structured_output: validation error for %s: %s", model_class.__name__, e)
        return None


def _log_structured_call(
    *,
    prompt_name: str,
    prompt_version: str,
    model_class_name: str,
    attempt: int,
    success: bool,
) -> None:
    """Log structured call metadata to Langfuse if available."""
    if not prompt_name:
        return
    try:
        from utils.langfuse_tracing import trace_event

        trace_event(
            event_type="structured_call",
            component="structured_output",
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            model_class=model_class_name,
            attempt=attempt,
            success=success,
        )
    except Exception:  # noqa: S110
        pass  # Langfuse not available — degrade silently


def structured_call(
    prompt: str,
    model_class: type[T],
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    claude_bin: str | None = None,
    max_retries: int = 1,
    inject_schema: bool = True,
    cwd: str | Path | None = None,
    prompt_name: str = "",
    prompt_version: str = "",
) -> T | None:
    """Call Claude CLI and parse the response into a validated Pydantic model.

    Args:
        prompt: The prompt to send to Claude
        model_class: Pydantic model class to validate against
        model: Claude model name (default: sonnet)
        timeout: Subprocess timeout in seconds
        claude_bin: Path to claude binary
        max_retries: Number of retries with error feedback (default: 1)
        inject_schema: Whether to append JSON schema instructions to prompt
        cwd: Working directory for subprocess
        prompt_name: Optional name for Langfuse prompt tracking
        prompt_version: Optional version for Langfuse prompt tracking

    Returns:
        Validated Pydantic model instance, or None if all tiers fail.

    Tier 1: Direct JSON parse of full response
    Tier 2: Extract JSON from markdown/mixed text
    Tier 3: Retry with validation error feedback
    Tier 4: Return None
    """
    if claude_bin is None:
        claude_bin = os.environ.get("CLAUDE_BIN", DEFAULT_CLAUDE_BIN)
    if cwd is None:
        cwd = str(BASE)

    full_prompt = prompt
    if inject_schema:
        full_prompt += _build_schema_instruction(model_class)

    child_env = os.environ.copy()
    child_env.pop("CLAUDECODE", None)
    child_env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    last_error: str | None = None

    for attempt in range(1 + max_retries):
        current_prompt = full_prompt
        if attempt > 0 and last_error:
            current_prompt += (
                f"\n\nYour previous response failed validation with this error:\n{last_error}\n"
                "Please fix the JSON and try again. Respond with ONLY valid JSON."
            )

        try:
            result = subprocess.run(
                [claude_bin, "-p", "--model", model, "--dangerously-skip-permissions", current_prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd),
                env=child_env,
            )
            response = result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("structured_call: timeout after %ds (attempt %d)", timeout, attempt + 1)
            continue
        except FileNotFoundError:
            logger.error("structured_call: claude binary not found: %s", claude_bin)
            return None
        except Exception as e:
            logger.error("structured_call: subprocess error: %s", e)
            return None

        if not response:
            logger.warning("structured_call: empty response (attempt %d, exit=%d)", attempt + 1, result.returncode)
            continue

        # Tier 1 + 2: Parse and validate (extract_json handles both strategies)
        parsed = parse_and_validate(response, model_class)
        if parsed is not None:
            logger.info("structured_call: success on attempt %d for %s", attempt + 1, model_class.__name__)
            _log_structured_call(
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                model_class_name=model_class.__name__,
                attempt=attempt + 1,
                success=True,
            )
            return parsed

        # Tier 3 prep: capture validation error for retry feedback
        json_str = _extract_json(response)
        if json_str:
            try:
                data = json.loads(json_str)
                model_class.model_validate(data)
            except ValidationError as e:
                last_error = str(e)
            except json.JSONDecodeError as e:
                last_error = f"Invalid JSON: {e}"
        else:
            last_error = "No JSON object found in response. Respond with ONLY a JSON object."

    # Tier 4: All attempts exhausted
    logger.error("structured_call: all %d attempts failed for %s", 1 + max_retries, model_class.__name__)
    _log_structured_call(
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        model_class_name=model_class.__name__,
        attempt=1 + max_retries,
        success=False,
    )
    return None
