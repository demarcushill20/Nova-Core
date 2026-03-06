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
    }


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
    for line in lines[start_idx + 1:]:
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
