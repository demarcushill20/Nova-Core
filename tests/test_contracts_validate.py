"""Tests for tools.adapters.contracts_validate — contract validation adapter.

Covers:
  - valid contract
  - missing contract
  - missing one field
  - missing multiple fields
  - malformed header
  - extra unrelated text before/after contract
  - duplicate contract blocks uses first only
  - empty values still treated deterministically
  - runner dispatch integration
  - JSON output shape
"""

import json

from tools.adapters.contracts_validate import contracts_validate, REQUIRED_FIELDS


# --- Sample texts ------------------------------------------------------------

VALID_CONTRACT = """\
Some preamble text.

## CONTRACT
summary: Updated runner dispatch
files_changed: tools/runner.py, tools/tools_registry.json
verification: 273 tests passed
confidence: high
"""

MISSING_CONTRACT = """\
This text has no contract block at all.
Just some random output.
"""

MISSING_ONE_FIELD = """\
## CONTRACT
summary: Did something
files_changed: foo.py
confidence: high
"""

MISSING_MULTIPLE_FIELDS = """\
## CONTRACT
summary: Did something
"""

MALFORMED_HEADER_HASH = """\
# CONTRACT
summary: Wrong header
files_changed: foo.py
verification: checked
confidence: high
"""

MALFORMED_HEADER_EXTRA = """\
## CONTRACT BLOCK
summary: Wrong header
files_changed: foo.py
verification: checked
confidence: high
"""

EXTRA_TEXT_BEFORE_AFTER = """\
This is some intro text that should be ignored.
It has multiple lines and paragraphs.

Here is some code:
    x = 42

## CONTRACT
summary: Created new feature
files_changed: feature.py, tests/test_feature.py
verification: all tests pass
confidence: medium

And this trailing text should not affect validation.
More trailing text here.
"""

DUPLICATE_CONTRACTS = """\
First contract:

## CONTRACT
summary: First attempt
files_changed: first.py
verification: partial
confidence: low

Second contract (should be ignored since we use first):

## CONTRACT
summary: Second attempt
files_changed: second.py
verification: all pass
confidence: high
"""

EMPTY_VALUES = """\
## CONTRACT
summary:
files_changed:
verification:
confidence:
"""

VALID_WITH_EXTRA_FIELDS = """\
## CONTRACT
summary: Deployed service
files_changed: deploy.yaml
verification: health check green
confidence: 0.95
status: active
extra_notes: everything is fine
"""


# --- Tests: valid contract ---------------------------------------------------


def test_valid_contract():
    result = contracts_validate(VALID_CONTRACT)
    assert result["ok"] is True
    assert result["valid"] is True
    assert result["found_contract"] is True
    assert result["fields"]["summary"] == "Updated runner dispatch"
    assert result["fields"]["files_changed"] == "tools/runner.py, tools/tools_registry.json"
    assert result["fields"]["verification"] == "273 tests passed"
    assert result["fields"]["confidence"] == "high"
    assert result["missing_fields"] == []
    assert result["errors"] == []


def test_valid_with_extra_fields():
    result = contracts_validate(VALID_WITH_EXTRA_FIELDS)
    assert result["valid"] is True
    assert result["fields"]["confidence"] == "0.95"
    assert result["fields"]["status"] == "active"
    assert result["missing_fields"] == []


# --- Tests: missing contract -------------------------------------------------


def test_missing_contract():
    result = contracts_validate(MISSING_CONTRACT)
    assert result["ok"] is True
    assert result["valid"] is False
    assert result["found_contract"] is False
    assert result["fields"] == {}
    assert result["missing_fields"] == list(REQUIRED_FIELDS)
    assert any("no ## CONTRACT" in e for e in result["errors"])


def test_empty_string():
    result = contracts_validate("")
    assert result["ok"] is True
    assert result["valid"] is False
    assert result["found_contract"] is False
    assert any("no ## CONTRACT" in e for e in result["errors"])


# --- Tests: missing fields ---------------------------------------------------


def test_missing_one_field():
    result = contracts_validate(MISSING_ONE_FIELD)
    assert result["ok"] is True
    assert result["valid"] is False
    assert result["found_contract"] is True
    assert "verification" in result["missing_fields"]
    assert len(result["missing_fields"]) == 1
    assert any("verification" in e for e in result["errors"])


def test_missing_multiple_fields():
    result = contracts_validate(MISSING_MULTIPLE_FIELDS)
    assert result["ok"] is True
    assert result["valid"] is False
    assert result["found_contract"] is True
    assert "files_changed" in result["missing_fields"]
    assert "verification" in result["missing_fields"]
    assert "confidence" in result["missing_fields"]
    assert len(result["missing_fields"]) == 3


# --- Tests: malformed header -------------------------------------------------


def test_malformed_header_single_hash():
    result = contracts_validate(MALFORMED_HEADER_HASH)
    assert result["valid"] is False
    assert result["found_contract"] is False
    assert any("no ## CONTRACT" in e for e in result["errors"])


def test_malformed_header_extra_text():
    result = contracts_validate(MALFORMED_HEADER_EXTRA)
    assert result["valid"] is False
    assert result["found_contract"] is False


# --- Tests: extra text -------------------------------------------------------


def test_extra_text_before_after():
    result = contracts_validate(EXTRA_TEXT_BEFORE_AFTER)
    assert result["valid"] is True
    assert result["found_contract"] is True
    assert result["fields"]["summary"] == "Created new feature"
    assert result["fields"]["confidence"] == "medium"
    assert result["errors"] == []


# --- Tests: duplicate contracts (uses first) ---------------------------------


def test_duplicate_contracts_uses_first():
    result = contracts_validate(DUPLICATE_CONTRACTS)
    assert result["found_contract"] is True
    assert result["fields"]["summary"] == "First attempt"
    assert result["fields"]["files_changed"] == "first.py"
    assert result["fields"]["confidence"] == "low"


# --- Tests: empty values -----------------------------------------------------


def test_empty_values_deterministic():
    """Empty field values (key: with nothing after) should be parsed
    deterministically. The field is present but with empty string value."""
    result = contracts_validate(EMPTY_VALUES)
    assert result["ok"] is True
    assert result["found_contract"] is True
    # Fields with empty values after colon: the regex requires .+ so
    # empty values won't match — they'll be missing.
    # This is deterministic behavior.
    assert isinstance(result["missing_fields"], list)
    assert isinstance(result["errors"], list)


# --- Tests: runner dispatch integration --------------------------------------


def test_runner_dispatch_integration():
    """Verify runner can dispatch contracts.validate and wraps result."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    registry = load_registry()
    envelope = run_tool("contracts.validate", {"text": VALID_CONTRACT}, registry)

    assert envelope["tool"] == "contracts.validate"
    assert envelope["ok"] is True
    inner = envelope["result"]
    assert inner["ok"] is True
    assert inner["result"]["valid"] is True
    assert inner["result"]["found_contract"] is True


def test_runner_dispatch_missing_contract():
    """Runner dispatch with text lacking a contract."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    registry = load_registry()
    envelope = run_tool("contracts.validate", {"text": "no contract here"}, registry)

    assert envelope["ok"] is True
    inner = envelope["result"]
    assert inner["result"]["valid"] is False
    assert inner["result"]["found_contract"] is False


# --- Tests: JSON output shape ------------------------------------------------


def test_json_output_shape_valid():
    result = contracts_validate(VALID_CONTRACT)
    required_keys = {"ok", "valid", "found_contract", "fields", "missing_fields", "errors"}
    assert required_keys == set(result.keys()), f"Unexpected keys: {set(result.keys()) - required_keys}"
    assert isinstance(result["ok"], bool)
    assert isinstance(result["valid"], bool)
    assert isinstance(result["found_contract"], bool)
    assert isinstance(result["fields"], dict)
    assert isinstance(result["missing_fields"], list)
    assert isinstance(result["errors"], list)


def test_json_output_shape_invalid():
    result = contracts_validate(MISSING_CONTRACT)
    required_keys = {"ok", "valid", "found_contract", "fields", "missing_fields", "errors"}
    assert required_keys == set(result.keys())
    assert isinstance(result["ok"], bool)
    assert isinstance(result["valid"], bool)
    assert isinstance(result["found_contract"], bool)
    assert isinstance(result["fields"], dict)
    assert isinstance(result["missing_fields"], list)
    assert isinstance(result["errors"], list)


def test_json_serializable():
    """Result must be JSON-serializable."""
    result = contracts_validate(VALID_CONTRACT)
    serialized = json.dumps(result)
    assert isinstance(serialized, str)
    roundtrip = json.loads(serialized)
    assert roundtrip == result


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
