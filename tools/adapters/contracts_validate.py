"""Adapter for contracts.validate tool.

Deterministic validator that checks whether a skill/task output satisfies
NovaCore's ## CONTRACT format.  No LLM calls, no external services.

Required contract fields: summary, files_changed, verification, confidence.
"""

import re

# --- Constants ---------------------------------------------------------------

REQUIRED_FIELDS = ("summary", "files_changed", "verification", "confidence")

_HEADER_RE = re.compile(r"^##\s+CONTRACT\s*$")
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


# --- Public API --------------------------------------------------------------


def contracts_validate(output: str) -> dict:
    """Validate a ## CONTRACT block in *output*.

    Returns a structured dict with keys:
        ok, valid, found_contract, fields, missing_fields, errors
    """
    if not isinstance(output, str):
        return {
            "ok": True,
            "valid": False,
            "found_contract": False,
            "fields": {},
            "missing_fields": list(REQUIRED_FIELDS),
            "errors": ["output must be a string"],
            "warnings": [],
        }

    # 1. Find the FIRST ## CONTRACT header
    contract_lines = _extract_first_contract(output)
    if contract_lines is None:
        return {
            "ok": True,
            "valid": False,
            "found_contract": False,
            "fields": {},
            "missing_fields": list(REQUIRED_FIELDS),
            "errors": ["no ## CONTRACT section found"],
            "warnings": [],
        }

    # 2. Parse key: value pairs from the contract block
    fields = _parse_fields(contract_lines)

    # 3. Check required fields
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    errors = [f"Missing required field: {f}" for f in missing]

    valid = len(errors) == 0

    return {
        "ok": True,
        "valid": valid,
        "found_contract": True,
        "fields": fields,
        "missing_fields": missing,
        "errors": errors,
        "warnings": [],
    }


# Task classes where files_changed should not be empty/none
_CODE_TASK_CLASSES = frozenset({"code_impl", "code_review"})


def evaluate_contract_quality(
    validation: dict,
    task_class: str = "",
) -> dict:
    """Evaluate contract quality with task-class-aware checks.

    Takes a validation result from contracts_validate() and returns it
    augmented with warnings and an adjusted confidence assessment.

    Args:
        validation: result dict from contracts_validate()
        task_class: task classification (e.g. 'code_impl', 'code_review')

    Returns:
        The same validation dict with added 'warnings' and
        'confidence_penalty' keys.
    """
    warnings = list(validation.get("warnings", []))
    confidence_penalty = 0.0

    fields = validation.get("fields", {})
    fc = fields.get("files_changed", "").strip().lower()

    # Flag code tasks that report no files changed
    if task_class in _CODE_TASK_CLASSES and (not fc or fc == "none"):
        warnings.append(f"files_changed is '{fc or 'empty'}' on a {task_class} task — expected file modifications")
        confidence_penalty += 0.2

    validation["warnings"] = warnings
    validation["confidence_penalty"] = confidence_penalty
    return validation


# --- Internal helpers --------------------------------------------------------


def _extract_first_contract(text: str) -> list[str] | None:
    """Return lines after the FIRST ``## CONTRACT`` header, up to the next
    heading or end of text.  Returns None if no header found."""
    lines = text.splitlines()
    start_idx = None

    for i, line in enumerate(lines):
        if _HEADER_RE.match(line.strip()):
            start_idx = i
            break

    if start_idx is None:
        return None

    # Collect lines until next heading (##) or end
    result = []
    for line in lines[start_idx + 1 :]:
        stripped = line.strip()
        # Stop at any markdown heading (including another ## CONTRACT)
        if stripped.startswith("## "):
            break
        result.append(line)

    return result


def _parse_fields(lines: list[str]) -> dict:
    """Parse key: value pairs from contract body lines."""
    result: dict[str, str] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = _KV_RE.match(stripped)
        if m:
            key = m.group(1).lower()
            value = m.group(2).strip()
            result[key] = value

    return result
