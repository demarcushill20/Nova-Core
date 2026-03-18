"""Tests for utils.ast_validator — AST-based code quality gate.

Phase 7, Step 7.8 of Enhancement Plan v32 (2026-03-18).
"""

from __future__ import annotations

import os
import textwrap

import pytest

from utils.ast_validator import (
    complexity_check,
    extract_python_blocks,
    validate_all_outputs,
    validate_output_file,
    validate_python,
)

# ======================================================================
# validate_python
# ======================================================================


class TestValidatePython:
    """Core syntax validation."""

    def test_valid_code_returns_true(self):
        ok, err = validate_python("x = 1\nprint(x)")
        assert ok is True
        assert err is None

    def test_invalid_code_returns_false(self):
        ok, err = validate_python("def foo(\n")
        assert ok is False
        assert err is not None
        assert "Line" in err

    def test_empty_string_is_valid(self):
        ok, err = validate_python("")
        assert ok is True
        assert err is None

    def test_whitespace_only_is_valid(self):
        ok, err = validate_python("   \n\n  \n")
        assert ok is True
        assert err is None

    def test_multiline_valid(self):
        code = textwrap.dedent("""\
            class Foo:
                def bar(self):
                    return 42
        """)
        ok, err = validate_python(code)
        assert ok is True
        assert err is None

    def test_syntax_error_reports_line_number(self):
        code = "x = 1\ny = 2\nif True\n"
        ok, err = validate_python(code)
        assert ok is False
        assert "Line 3" in err

    def test_indentation_error(self):
        code = "def f():\nx = 1\n"
        ok, err = validate_python(code)
        assert ok is False
        assert err is not None

    def test_single_expression(self):
        ok, err = validate_python("42")
        assert ok is True

    def test_complex_valid_code(self):
        code = textwrap.dedent("""\
            import os
            from typing import Optional

            def greet(name: str) -> str:
                return f"Hello, {name}!"

            class Greeter:
                def __init__(self, prefix: str = "Hi"):
                    self.prefix = prefix

                def greet(self, name: str) -> str:
                    return f"{self.prefix}, {name}!"

            if __name__ == "__main__":
                g = Greeter()
                print(g.greet("world"))
        """)
        ok, err = validate_python(code)
        assert ok is True


# ======================================================================
# extract_python_blocks
# ======================================================================


class TestExtractPythonBlocks:
    """Markdown code-block extraction."""

    def test_single_block(self):
        md = "Some text\n```python\nx = 1\n```\nMore text"
        blocks = extract_python_blocks(md)
        assert len(blocks) == 1
        assert "x = 1" in blocks[0]

    def test_multiple_blocks(self):
        md = "```python\na = 1\n```\n\n```python\nb = 2\n```"
        blocks = extract_python_blocks(md)
        assert len(blocks) == 2

    def test_no_blocks(self):
        blocks = extract_python_blocks("Just plain text, no code.")
        assert blocks == []

    def test_non_python_blocks_ignored(self):
        md = "```javascript\nvar x = 1;\n```\n```python\ny = 2\n```"
        blocks = extract_python_blocks(md)
        assert len(blocks) == 1
        assert "y = 2" in blocks[0]

    def test_empty_python_block(self):
        md = "```python\n```"
        blocks = extract_python_blocks(md)
        assert len(blocks) == 1
        assert blocks[0].strip() == ""

    def test_multiline_block(self):
        md = textwrap.dedent("""\
            ```python
            def foo():
                return 1

            def bar():
                return 2
            ```
        """)
        blocks = extract_python_blocks(md)
        assert len(blocks) == 1
        assert "def foo" in blocks[0]
        assert "def bar" in blocks[0]

    def test_empty_input(self):
        assert extract_python_blocks("") == []


# ======================================================================
# validate_output_file
# ======================================================================


class TestValidateOutputFile:
    """File-level validation."""

    def test_valid_py_file(self, tmp_path):
        f = tmp_path / "good.py"
        f.write_text("x = 1\n")
        ok, err = validate_output_file(str(f))
        assert ok is True
        assert err is None

    def test_invalid_py_file(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def broken(\n")
        ok, err = validate_output_file(str(f))
        assert ok is False
        assert err is not None

    def test_md_with_valid_python(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Title\n```python\nx = 1\n```\n")
        ok, err = validate_output_file(str(f))
        assert ok is True

    def test_md_with_invalid_python(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Title\n```python\ndef bad(\n```\n")
        ok, err = validate_output_file(str(f))
        assert ok is False
        assert "Block 1" in err

    def test_md_with_no_python(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("Just text, no code blocks.\n")
        ok, err = validate_output_file(str(f))
        assert ok is True

    def test_missing_file(self):
        ok, err = validate_output_file("/nonexistent/path/xyz.py")
        assert ok is False
        assert "not found" in err.lower()

    def test_mixed_valid_invalid_blocks(self, tmp_path):
        f = tmp_path / "mixed.md"
        content = textwrap.dedent("""\
            # Example

            ```python
            x = 1
            ```

            ```python
            def broken(
            ```
        """)
        f.write_text(content)
        ok, err = validate_output_file(str(f))
        assert ok is False
        assert "Block 2" in err

    def test_non_utf8_file(self, tmp_path):
        f = tmp_path / "binary.py"
        f.write_bytes(b"\x80\x81\x82\ndef foo():\n    pass\n")
        # Should not crash — errors="replace" handles this
        ok, err = validate_output_file(str(f))
        # The replacement chars may or may not cause a syntax error;
        # the key point is it doesn't raise an exception.
        assert isinstance(ok, bool)

    def test_txt_with_python_blocks(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("```python\nprint('hello')\n```\n")
        ok, err = validate_output_file(str(f))
        assert ok is True


# ======================================================================
# validate_all_outputs
# ======================================================================


class TestValidateAllOutputs:
    """Batch validation of OUTPUT/ directory."""

    def test_empty_directory(self, tmp_path):
        results = validate_all_outputs(str(tmp_path))
        assert results == []

    def test_nonexistent_directory(self, tmp_path):
        results = validate_all_outputs(str(tmp_path / "nope"))
        assert results == []

    def test_single_valid_py(self, tmp_path):
        (tmp_path / "good.py").write_text("x = 1\n")
        results = validate_all_outputs(str(tmp_path))
        assert len(results) == 1
        assert results[0]["valid"] is True
        assert results[0]["block_index"] == 0

    def test_single_invalid_py(self, tmp_path):
        (tmp_path / "bad.py").write_text("if True\n")
        results = validate_all_outputs(str(tmp_path))
        assert len(results) == 1
        assert results[0]["valid"] is False

    def test_md_with_blocks(self, tmp_path):
        content = "```python\nx = 1\n```\n\n```python\ny = 2\n```\n"
        (tmp_path / "code.md").write_text(content)
        results = validate_all_outputs(str(tmp_path))
        assert len(results) == 2
        assert all(r["valid"] for r in results)
        assert results[0]["block_index"] == 1
        assert results[1]["block_index"] == 2

    def test_non_python_files_ignored(self, tmp_path):
        (tmp_path / "notes.txt").write_text("Just text.\n")
        results = validate_all_outputs(str(tmp_path))
        # No Python blocks => nothing reported
        assert results == []

    def test_mixed_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("def bad(\n")
        (tmp_path / "c.md").write_text("```python\ny = 2\n```\n")
        results = validate_all_outputs(str(tmp_path))
        assert len(results) == 3
        valid_count = sum(1 for r in results if r["valid"])
        assert valid_count == 2

    def test_subdirectories_skipped(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "inner.py").write_text("x = 1\n")
        results = validate_all_outputs(str(tmp_path))
        # Only top-level files, subdir is skipped
        assert results == []

    def test_results_sorted_by_filename(self, tmp_path):
        (tmp_path / "z.py").write_text("x = 1\n")
        (tmp_path / "a.py").write_text("y = 2\n")
        results = validate_all_outputs(str(tmp_path))
        files = [os.path.basename(r["file"]) for r in results]
        assert files == ["a.py", "z.py"]


# ======================================================================
# complexity_check
# ======================================================================


class TestComplexityCheck:
    """Basic complexity heuristic."""

    def test_simple_function_no_warnings(self):
        code = "def f():\n    return 1\n"
        warnings = complexity_check(code, max_complexity=5)
        assert warnings == []

    def test_deeply_nested_triggers_warning(self):
        # Build deeply nested code: function depth 1 + 10 nested ifs
        lines = ["def deep():"]
        for i in range(10):
            indent = "    " * (i + 1)
            lines.append(f"{indent}if True:")
        lines.append("    " * 11 + "pass")
        code = "\n".join(lines) + "\n"
        warnings = complexity_check(code, max_complexity=5)
        assert len(warnings) >= 1
        assert warnings[0]["function"] == "deep"
        assert warnings[0]["depth"] > 5

    def test_threshold_boundary(self):
        # Function nesting = 1 (function itself) + 2 nested ifs = 3
        code = textwrap.dedent("""\
            def f():
                if True:
                    if True:
                        pass
        """)
        # Depth = 1 (func) + 2 (ifs) = 3; max_complexity=3 => no warning
        assert complexity_check(code, max_complexity=3) == []
        # max_complexity=2 => warning
        warnings = complexity_check(code, max_complexity=2)
        assert len(warnings) == 1

    def test_class_method_depth(self):
        code = textwrap.dedent("""\
            class Foo:
                def bar(self):
                    for i in range(10):
                        for j in range(10):
                            for k in range(10):
                                pass
        """)
        # depth = class(1) + method(1) + 3 fors = 5
        warnings = complexity_check(code, max_complexity=4)
        assert len(warnings) >= 1
        assert warnings[0]["function"] == "bar"

    def test_syntax_error_returns_parse_error(self):
        warnings = complexity_check("def bad(\n")
        assert len(warnings) == 1
        assert warnings[0]["function"] == "<parse-error>"
        assert "SyntaxError" in warnings[0]["reason"]

    def test_empty_code_no_warnings(self):
        assert complexity_check("") == []

    def test_multiple_functions_only_complex_flagged(self):
        code = textwrap.dedent("""\
            def simple():
                return 1

            def complex_one():
                if True:
                    if True:
                        if True:
                            if True:
                                if True:
                                    pass
        """)
        warnings = complexity_check(code, max_complexity=3)
        names = [w["function"] for w in warnings]
        assert "simple" not in names
        assert "complex_one" in names

    def test_nested_functions(self):
        code = textwrap.dedent("""\
            def outer():
                def inner():
                    if True:
                        if True:
                            if True:
                                pass
        """)
        warnings = complexity_check(code, max_complexity=3)
        # inner has depth 2 (outer+inner) + 3 ifs = 5
        names = [w["function"] for w in warnings]
        assert "inner" in names

    def test_async_function_counted(self):
        code = textwrap.dedent("""\
            import asyncio
            async def handler():
                if True:
                    if True:
                        if True:
                            if True:
                                if True:
                                    pass
        """)
        warnings = complexity_check(code, max_complexity=3)
        assert len(warnings) >= 1
        assert warnings[0]["function"] == "handler"

    def test_lineno_reported(self):
        code = textwrap.dedent("""\
            x = 1

            def target():
                if True:
                    if True:
                        if True:
                            if True:
                                pass
        """)
        warnings = complexity_check(code, max_complexity=3)
        assert len(warnings) >= 1
        assert warnings[0]["lineno"] == 3

    def test_high_threshold_no_warnings(self):
        code = textwrap.dedent("""\
            def f():
                for i in range(10):
                    for j in range(10):
                        pass
        """)
        assert complexity_check(code, max_complexity=100) == []


# ======================================================================
# Integration: actual OUTPUT/ directory (if present)
# ======================================================================


class TestOutputIntegration:
    """Smoke-test against the real OUTPUT/ directory, if it exists."""

    @pytest.fixture
    def output_dir(self):
        d = os.path.join(os.path.dirname(__file__), "..", "OUTPUT")
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            pytest.skip("OUTPUT/ directory not present")
        return d

    def test_validate_all_outputs_does_not_crash(self, output_dir):
        results = validate_all_outputs(output_dir)
        assert isinstance(results, list)
        for r in results:
            assert "file" in r
            assert "valid" in r
            assert isinstance(r["valid"], bool)
