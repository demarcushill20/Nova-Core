#!/usr/bin/env python3
"""Tests for scripts/complexity_gate.py — Phase 7 Step 7.6.

All tests mock subprocess.run so they never invoke radon externally.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the scripts directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from complexity_gate import (
    _collect_violations,
    check_complexity,
    get_radon_complexity,
)

# ---------------------------------------------------------------------------
# Fixture: sample radon JSON output
# ---------------------------------------------------------------------------

SIMPLE_RADON_OUTPUT = json.dumps(
    {
        "module_a.py": [
            {"name": "simple_func", "complexity": 3, "lineno": 1, "rank": "A", "type": "function"},
            {"name": "another_func", "complexity": 5, "lineno": 10, "rank": "A", "type": "function"},
        ],
    }
)

COMPLEX_RADON_OUTPUT = json.dumps(
    {
        "big_module.py": [
            {"name": "easy", "complexity": 2, "lineno": 1, "rank": "A", "type": "function"},
            {"name": "monster", "complexity": 42, "lineno": 50, "rank": "F", "type": "function"},
            {"name": "borderline", "complexity": 15, "lineno": 100, "rank": "C", "type": "function"},
        ],
    }
)

NESTED_CLASS_RADON_OUTPUT = json.dumps(
    {
        "classy.py": [
            {
                "name": "MyClass",
                "complexity": 1,
                "lineno": 1,
                "rank": "A",
                "type": "class",
                "methods": [
                    {"name": "MyClass.ok_method", "complexity": 4, "lineno": 5, "rank": "A", "type": "method"},
                    {"name": "MyClass.bad_method", "complexity": 25, "lineno": 30, "rank": "D", "type": "method"},
                ],
            },
        ],
    }
)

EMPTY_RADON_OUTPUT = json.dumps({})

MULTI_FILE_RADON_OUTPUT = json.dumps(
    {
        "file_a.py": [
            {"name": "func_a", "complexity": 20, "lineno": 1, "rank": "D", "type": "function"},
        ],
        "file_b.py": [
            {"name": "func_b", "complexity": 3, "lineno": 1, "rank": "A", "type": "function"},
        ],
        "file_c.py": [
            {"name": "func_c", "complexity": 18, "lineno": 10, "rank": "C", "type": "function"},
        ],
    }
)


def _mock_run(stdout: str, returncode: int = 0) -> MagicMock:
    """Create a mock subprocess.CompletedProcess."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# Tests: get_radon_complexity
# ---------------------------------------------------------------------------


class TestGetRadonComplexity:
    """Tests for the get_radon_complexity helper."""

    @patch("complexity_gate.subprocess.run")
    def test_returns_parsed_json(self, mock_run):
        mock_run.return_value = _mock_run(SIMPLE_RADON_OUTPUT)
        result = get_radon_complexity(["."])
        assert "module_a.py" in result
        assert len(result["module_a.py"]) == 2

    @patch("complexity_gate.subprocess.run")
    def test_nonzero_returncode_returns_empty(self, mock_run):
        mock_run.return_value = _mock_run("", returncode=1)
        result = get_radon_complexity(["."])
        assert result == {}

    @patch("complexity_gate.subprocess.run")
    def test_invalid_json_returns_empty(self, mock_run):
        mock_run.return_value = _mock_run("NOT VALID JSON {{{")
        result = get_radon_complexity(["."])
        assert result == {}

    @patch("complexity_gate.subprocess.run")
    def test_empty_output_returns_empty(self, mock_run):
        mock_run.return_value = _mock_run(EMPTY_RADON_OUTPUT)
        result = get_radon_complexity(["."])
        assert result == {}

    @patch("complexity_gate.subprocess.run")
    def test_passes_paths_to_radon(self, mock_run):
        mock_run.return_value = _mock_run(EMPTY_RADON_OUTPUT)
        get_radon_complexity(["src/", "lib/"])
        cmd = mock_run.call_args[0][0]
        assert cmd == ["radon", "cc", "src/", "lib/", "-j"]


# ---------------------------------------------------------------------------
# Tests: _collect_violations
# ---------------------------------------------------------------------------


class TestCollectViolations:
    """Tests for the recursive violation collector."""

    def test_flat_function_above_threshold(self):
        violations = []
        item = {"name": "big_func", "complexity": 20, "lineno": 5, "rank": "D"}
        _collect_violations("file.py", item, 15, violations)
        assert len(violations) == 1
        assert violations[0]["function"] == "big_func"
        assert violations[0]["complexity"] == 20

    def test_flat_function_at_threshold_passes(self):
        violations = []
        item = {"name": "ok_func", "complexity": 15, "lineno": 1, "rank": "C"}
        _collect_violations("file.py", item, 15, violations)
        assert len(violations) == 0

    def test_flat_function_below_threshold_passes(self):
        violations = []
        item = {"name": "simple", "complexity": 3, "lineno": 1, "rank": "A"}
        _collect_violations("file.py", item, 15, violations)
        assert len(violations) == 0

    def test_nested_methods_checked(self):
        violations = []
        item = {
            "name": "MyClass",
            "complexity": 1,
            "lineno": 1,
            "rank": "A",
            "methods": [
                {"name": "MyClass.bad", "complexity": 25, "lineno": 10, "rank": "D"},
                {"name": "MyClass.ok", "complexity": 5, "lineno": 20, "rank": "A"},
            ],
        }
        _collect_violations("file.py", item, 15, violations)
        assert len(violations) == 1
        assert violations[0]["function"] == "MyClass.bad"

    def test_missing_fields_handled_gracefully(self):
        violations = []
        item = {"complexity": 20}  # missing name, lineno, rank
        _collect_violations("file.py", item, 15, violations)
        assert len(violations) == 1
        assert violations[0]["function"] == "?"
        assert violations[0]["rank"] == "?"
        assert violations[0]["line"] == 0


# ---------------------------------------------------------------------------
# Tests: check_complexity (full integration with mocked radon)
# ---------------------------------------------------------------------------


class TestCheckComplexity:
    """Tests for the main check_complexity function."""

    @patch("complexity_gate.subprocess.run")
    def test_simple_functions_pass(self, mock_run):
        mock_run.return_value = _mock_run(SIMPLE_RADON_OUTPUT)
        rc = check_complexity(threshold=15, paths=["."])
        assert rc == 0

    @patch("complexity_gate.subprocess.run")
    def test_complex_function_fails(self, mock_run):
        mock_run.return_value = _mock_run(COMPLEX_RADON_OUTPUT)
        rc = check_complexity(threshold=15, paths=["."])
        assert rc == 1

    @patch("complexity_gate.subprocess.run")
    def test_borderline_at_threshold_passes(self, mock_run):
        """A function with complexity == threshold should pass (only > threshold fails)."""
        mock_run.return_value = _mock_run(COMPLEX_RADON_OUTPUT)
        # borderline is 15, monster is 42. With threshold=42, only monster matches
        # but at threshold=42 monster is exactly at limit, so it passes
        rc = check_complexity(threshold=42, paths=["."])
        assert rc == 0

    @patch("complexity_gate.subprocess.run")
    def test_nested_class_method_detected(self, mock_run):
        mock_run.return_value = _mock_run(NESTED_CLASS_RADON_OUTPUT)
        rc = check_complexity(threshold=15, paths=["."])
        assert rc == 1

    @patch("complexity_gate.subprocess.run")
    def test_empty_codebase_passes(self, mock_run):
        mock_run.return_value = _mock_run(EMPTY_RADON_OUTPUT)
        rc = check_complexity(threshold=15, paths=["."])
        assert rc == 0

    @patch("complexity_gate.subprocess.run")
    def test_radon_failure_passes_gracefully(self, mock_run):
        """If radon itself fails, we get no results — should pass (no violations)."""
        mock_run.return_value = _mock_run("", returncode=1)
        rc = check_complexity(threshold=15, paths=["."])
        assert rc == 0

    @patch("complexity_gate.subprocess.run")
    def test_threshold_parameter_respected(self, mock_run):
        mock_run.return_value = _mock_run(SIMPLE_RADON_OUTPUT)
        # All functions have CC <= 5, threshold 2 should catch them
        rc = check_complexity(threshold=2, paths=["."])
        assert rc == 1

    @patch("complexity_gate.subprocess.run")
    def test_high_threshold_passes_everything(self, mock_run):
        mock_run.return_value = _mock_run(COMPLEX_RADON_OUTPUT)
        rc = check_complexity(threshold=100, paths=["."])
        assert rc == 0

    @patch("complexity_gate.subprocess.run")
    def test_multiple_files_counted(self, mock_run):
        mock_run.return_value = _mock_run(MULTI_FILE_RADON_OUTPUT)
        rc = check_complexity(threshold=15, paths=["."])
        assert rc == 1  # func_a(20) and func_c(18) exceed 15

    @patch("complexity_gate.subprocess.run")
    def test_default_paths_is_dot(self, mock_run):
        mock_run.return_value = _mock_run(EMPTY_RADON_OUTPUT)
        check_complexity(threshold=15)  # no paths arg
        cmd = mock_run.call_args[0][0]
        assert "." in cmd

    @patch("complexity_gate.subprocess.run")
    def test_nonexistent_path_handled(self, mock_run):
        """Radon returns error for nonexistent paths — we handle gracefully."""
        mock_run.return_value = _mock_run("", returncode=1)
        rc = check_complexity(threshold=15, paths=["/nonexistent/path"])
        assert rc == 0  # graceful — no violations found

    @patch("complexity_gate.subprocess.run")
    def test_output_contains_violation_details(self, mock_run, capsys):
        mock_run.return_value = _mock_run(COMPLEX_RADON_OUTPUT)
        check_complexity(threshold=15, paths=["."])
        captured = capsys.readouterr()
        assert "FAIL:" in captured.out
        assert "monster" in captured.out
        assert "CC=42" in captured.out

    @patch("complexity_gate.subprocess.run")
    def test_pass_message_on_success(self, mock_run, capsys):
        mock_run.return_value = _mock_run(SIMPLE_RADON_OUTPUT)
        check_complexity(threshold=15, paths=["."])
        captured = capsys.readouterr()
        assert "PASS:" in captured.out
