"""Contract validator for skill outputs.

Parses the LAST ``## CONTRACT`` block in a text body and checks that
required fields (summary, files_changed, verification, confidence) are
present and confidence is valid.

No LLM calls — purely deterministic string parsing.
"""

import re
import typing

# --- Constants ---------------------------------------------------------------

_REQUIRED_FIELDS = ("summary", "files_changed", "verification", "confidence")

_ACTION_DETAIL_FIELDS = frozenset(
    (
        "commands_executed",
        "git_commands_executed",
        "task_id",
        "status",
        "checks_performed",
    )
)

_CONFIDENCE_WORDS = frozenset(("low", "medium", "high"))

_HEADER_RE = re.compile(r"^##\s+CONTRACT\s*$")
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$")
_FENCE_RE = re.compile(r"^```")


# --- Public API --------------------------------------------------------------


def validate_contract(text: str) -> dict:
    """Validate the last ``## CONTRACT`` block in *text*.

    Returns:
        dict with keys: valid, errors, warnings, contract
    """
    if not isinstance(text, str):
        return {
            "valid": False,
            "errors": ["text must be a string"],
            "warnings": [],
            "contract": {},
        }

    # 1. Find the LAST ## CONTRACT header
    contract_lines = _extract_last_contract(text)
    if contract_lines is None:
        return {
            "valid": False,
            "errors": ["no ## CONTRACT section found"],
            "warnings": [],
            "contract": {},
        }

    # 2. Parse key: value pairs
    contract = _parse_kv(contract_lines)

    # 3. Validate
    errors: list[str] = []
    warnings: list[str] = []

    for field in _REQUIRED_FIELDS:
        if field not in contract:
            errors.append(f"missing required field: {field}")

    # Confidence validation
    if "confidence" in contract:
        conf = contract["confidence"]
        if not _valid_confidence(conf):
            errors.append(f"invalid confidence value: {conf!r} (expected float 0.0–1.0 or low/medium/high)")

    valid = len(errors) == 0

    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "contract": contract,
    }


# --- Internal helpers --------------------------------------------------------


def _extract_last_contract(text: str) -> list[str] | None:
    """Return lines after the last ``## CONTRACT`` header, or None."""
    lines = text.splitlines()
    last_idx = None
    for i, line in enumerate(lines):
        if _HEADER_RE.match(line.strip()):
            last_idx = i

    if last_idx is None:
        return None

    return lines[last_idx + 1 :]


def _parse_kv(lines: list[str]) -> dict:
    """Parse key: value pairs, skipping blanks and code fences."""
    result: dict[str, str] = {}
    in_fence = False

    for line in lines:
        stripped = line.strip()

        # Toggle code fence state
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue

        if in_fence:
            continue

        if not stripped:
            continue

        m = _KV_RE.match(stripped)
        if m:
            key = m.group(1).lower()
            value = m.group(2).strip()
            result[key] = value

    return result


def _valid_confidence(value: str) -> bool:
    """Check if confidence is a valid float 0.0–1.0 or low/medium/high."""
    if value.lower() in _CONFIDENCE_WORDS:
        return True
    try:
        f = float(value)
        return 0.0 <= f <= 1.0
    except (ValueError, TypeError):
        return False


# --- Pydantic structured validation -----------------------------------------


def _map_parsed_to_contract_fields(parsed: dict) -> dict:
    """Map a regex-parsed contract dict to ContractOutput constructor fields.

    The regex parser produces flat string values; this function coerces them
    into types that ContractOutput expects while collecting any extra fields.
    """
    from utils.schemas.contract import ContractConfidence

    known_keys = {
        "summary",
        "files_changed",
        "verification",
        "confidence",
        "tests_passed",
        "test_count",
        "lint_clean",
    }

    fields: dict = {}

    # summary
    if "summary" in parsed:
        fields["summary"] = parsed["summary"]

    # files_changed — ContractOutput accepts a comma-separated string via its
    # field_validator, so pass through as-is.
    if "files_changed" in parsed:
        val = parsed["files_changed"]
        if val.lower() == "none":
            fields["files_changed"] = []
        else:
            fields["files_changed"] = val

    # verification
    if "verification" in parsed:
        fields["verification"] = parsed["verification"]

    # confidence — normalise numeric strings to the closest word
    if "confidence" in parsed:
        raw = parsed["confidence"].strip().lower()
        if raw in ("low", "medium", "high"):
            fields["confidence"] = ContractConfidence(raw)
        else:
            # Try to interpret as float and map to a word
            try:
                f = float(raw)
                if f <= 0.4:
                    fields["confidence"] = ContractConfidence.LOW
                elif f <= 0.7:
                    fields["confidence"] = ContractConfidence.MEDIUM
                else:
                    fields["confidence"] = ContractConfidence.HIGH
            except (ValueError, TypeError):
                # Let Pydantic reject it
                fields["confidence"] = raw

    # Optional boolean/int fields
    if "tests_passed" in parsed:
        fields["tests_passed"] = parsed["tests_passed"].lower() in ("true", "1", "yes")

    if "test_count" in parsed:
        try:
            fields["test_count"] = int(parsed["test_count"])
        except (ValueError, TypeError):
            pass  # skip — Pydantic will use default None

    if "lint_clean" in parsed:
        fields["lint_clean"] = parsed["lint_clean"].lower() in ("true", "1", "yes")

    # Extra fields — anything not in known_keys
    from utils.schemas.contract import ContractField

    extras = []
    for k, v in parsed.items():
        if k not in known_keys:
            extras.append(ContractField(key=k, value=v))
    if extras:
        fields["extra_fields"] = extras

    return fields


def validate_contract_structured(text_or_dict) -> dict:
    """Validate a contract using Pydantic structured validation.

    Accepts either:
    - Raw text containing a ## CONTRACT block (parses first, then validates)
    - A dict with contract fields (validates directly)

    Returns:
        {
            "valid": bool,
            "contract": ContractOutput | None,  # Pydantic model if valid
            "errors": list[str],
            "warnings": list[str],
            "legacy_compat": dict | None,  # Legacy dict format for backwards compat
        }
    """
    from utils.schemas.contract import ContractOutput

    warnings: list[str] = []

    # --- Determine source fields ---
    if isinstance(text_or_dict, str):
        # Parse the text using the existing regex parser first
        legacy_result = validate_contract(text_or_dict)
        if not legacy_result["valid"]:
            return {
                "valid": False,
                "contract": None,
                "errors": legacy_result["errors"],
                "warnings": legacy_result.get("warnings", []),
                "legacy_compat": None,
            }
        parsed = legacy_result["contract"]
        fields = _map_parsed_to_contract_fields(parsed)
    elif isinstance(text_or_dict, dict):
        fields = dict(text_or_dict)
    else:
        return {
            "valid": False,
            "contract": None,
            "errors": [f"expected str or dict, got {type(text_or_dict).__name__}"],
            "warnings": [],
            "legacy_compat": None,
        }

    # --- Validate with Pydantic ---
    try:
        model = ContractOutput.model_validate(fields)
        return {
            "valid": True,
            "contract": model,
            "errors": [],
            "warnings": warnings,
            "legacy_compat": model.model_dump(),
        }
    except Exception as exc:
        error_messages: list[str] = []
        # Pydantic ValidationError has a .errors() method
        if hasattr(exc, "errors"):
            for err in exc.errors():
                loc = " -> ".join(str(part) for part in err.get("loc", []))
                msg = err.get("msg", str(err))
                error_messages.append(f"{loc}: {msg}" if loc else msg)
        else:
            error_messages.append(str(exc))

        return {
            "valid": False,
            "contract": None,
            "errors": error_messages,
            "warnings": warnings,
            "legacy_compat": None,
        }


def contract_from_dict(d: dict):
    """Create a ContractOutput from a dict, or None if validation fails."""
    try:
        from utils.schemas.contract import ContractOutput

        return ContractOutput.model_validate(d)
    except Exception:
        return None


def evaluate_contract_success(contract, results: dict) -> dict:
    """Evaluate whether a completed task meets its contract's success criteria.

    Args:
        contract: The ContractOutput with success_criteria defined
        results: Dict with keys like 'tests_passed', 'lint_clean', 'files_changed', etc.

    Returns:
        {"met": bool, "passed": list[str], "failed": list[str], "skipped": list[str]}
    """
    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    # Built-in criteria mapping: criterion string -> (result_key, check_fn)
    builtin_checks: dict[str, tuple[str, typing.Callable]] = {
        "tests_pass": ("tests_passed", lambda v: bool(v)),
        "lint_clean": ("lint_clean", lambda v: bool(v)),
        "no_errors": ("errors", lambda v: not v),
    }

    for criterion in contract.success_criteria:
        criterion_key = criterion.strip().lower()
        if criterion_key in builtin_checks:
            result_key, check_fn = builtin_checks[criterion_key]
            if result_key in results:
                if check_fn(results[result_key]):
                    passed.append(criterion)
                else:
                    failed.append(criterion)
            else:
                skipped.append(criterion)
        else:
            skipped.append(criterion)

    # Vacuously true: no criteria means success
    met = len(failed) == 0

    return {
        "met": met,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }
