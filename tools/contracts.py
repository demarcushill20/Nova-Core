"""Contract validator for skill outputs.

Parses the LAST ``## CONTRACT`` block in a text body and checks that
required fields (summary, files_changed, verification, confidence) are
present and confidence is valid.

No LLM calls — purely deterministic string parsing.
"""

import re

# --- Constants ---------------------------------------------------------------

_REQUIRED_FIELDS = ("summary", "files_changed", "verification", "confidence")

_ACTION_DETAIL_FIELDS = frozenset((
    "commands_executed",
    "git_commands_executed",
    "task_id",
    "status",
    "checks_performed",
))

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
            errors.append(
                f"invalid confidence value: {conf!r} "
                "(expected float 0.0–1.0 or low/medium/high)"
            )

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

    return lines[last_idx + 1:]


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
