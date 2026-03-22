"""AST-based code quality gate for NovaCore.

Validates Python code using ast.parse(), catching syntax errors before
they reach OUTPUT/.  Also provides markdown code-block extraction and
a basic complexity heuristic.

Phase 7, Step 7.8 of Enhancement Plan v32 (2026-03-18).
"""

from __future__ import annotations

import ast
import logging
import os
import re

_log = logging.getLogger("nova.ast_validator")

# ------------------------------------------------------------------
# Regex for fenced Python code blocks in Markdown
# ------------------------------------------------------------------
_PYTHON_BLOCK_RE = re.compile(
    r"```python\s*\n(.*?)```",
    re.DOTALL,
)


# ------------------------------------------------------------------
# Core validation
# ------------------------------------------------------------------


def validate_python(code: str) -> tuple[bool, str | None]:
    """Validate Python code is syntactically correct via AST parsing.

    Returns ``(True, None)`` if valid, ``(False, error_message)`` if invalid.
    """
    try:
        ast.parse(code)
        return True, None
    except SyntaxError as e:
        return False, f"Line {e.lineno}: {e.msg}"


# ------------------------------------------------------------------
# Markdown helpers
# ------------------------------------------------------------------


def extract_python_blocks(text: str) -> list[str]:
    """Extract fenced Python code blocks from markdown text.

    Looks for ````python ... ``` `` blocks and returns a list of the
    code inside each one (without the fence markers).
    """
    return _PYTHON_BLOCK_RE.findall(text)


# ------------------------------------------------------------------
# File-level validation
# ------------------------------------------------------------------


def validate_output_file(filepath: str) -> tuple[bool, str | None]:
    """Read a file and validate any Python it contains.

    * For ``.py`` files the entire content is validated.
    * For ``.md`` / ``.txt`` / other files, fenced Python blocks are
      extracted and each is validated independently.

    Returns ``(True, None)`` when all code is valid (or the file
    contains no Python), or ``(False, error_message)`` for the first
    failing block.
    """
    if not os.path.isfile(filepath):
        return False, f"File not found: {filepath}"

    try:
        with open(filepath, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"Cannot read file: {exc}"

    if filepath.endswith(".py"):
        return validate_python(content)

    # For non-.py files, check embedded code blocks
    blocks = extract_python_blocks(content)
    if not blocks:
        return True, None  # nothing to validate

    for idx, block in enumerate(blocks, 1):
        valid, err = validate_python(block)
        if not valid:
            return False, f"Block {idx}: {err}"

    return True, None


# ------------------------------------------------------------------
# Batch validation across OUTPUT/
# ------------------------------------------------------------------


def validate_all_outputs(output_dir: str = "OUTPUT") -> list[dict]:
    """Scan *output_dir* for files containing Python code and validate them.

    Returns a list of dicts, one per validated code unit::

        {"file": str, "block_index": int, "valid": bool, "error": str | None}

    ``block_index`` is 0 for ``.py`` files (the whole file) and 1-based
    for fenced blocks inside markdown/text files.
    """
    results: list[dict] = []

    if not os.path.isdir(output_dir):
        _log.warning("Output directory does not exist: %s", output_dir)
        return results

    for entry in sorted(os.listdir(output_dir)):
        full = os.path.join(output_dir, entry)
        if not os.path.isfile(full):
            continue

        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            results.append(
                {
                    "file": full,
                    "block_index": 0,
                    "valid": False,
                    "error": "Cannot read file",
                }
            )
            continue

        if entry.endswith(".py"):
            valid, err = validate_python(content)
            results.append(
                {
                    "file": full,
                    "block_index": 0,
                    "valid": valid,
                    "error": err,
                }
            )
        else:
            blocks = extract_python_blocks(content)
            for idx, block in enumerate(blocks, 1):
                valid, err = validate_python(block)
                results.append(
                    {
                        "file": full,
                        "block_index": idx,
                        "valid": valid,
                        "error": err,
                    }
                )

    return results


# ------------------------------------------------------------------
# Complexity check
# ------------------------------------------------------------------


class _ComplexityVisitor(ast.NodeVisitor):
    """Walk an AST and record per-function nesting depth and class count."""

    def __init__(self, max_complexity: int) -> None:
        self.max_complexity = max_complexity
        self.warnings: list[dict] = []
        self._depth = 0
        self._class_count = 0

    # -- depth tracking helpers ----------------------------------------

    def _enter(self) -> None:
        self._depth += 1

    def _leave(self) -> None:
        self._depth -= 1

    # -- visitors -------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_count += 1
        self._enter()
        self.generic_visit(node)
        self._leave()

    def _visit_func(self, node: ast.AST) -> None:
        self._enter()
        # Count nesting depth of control-flow constructs inside the func
        inner_depth = self._max_control_depth(node)
        total = self._depth + inner_depth
        name = getattr(node, "name", "<lambda>")
        lineno = getattr(node, "lineno", 0)
        if total > self.max_complexity:
            self.warnings.append(
                {
                    "function": name,
                    "lineno": lineno,
                    "depth": total,
                    "reason": (f"Nesting depth {total} exceeds max {self.max_complexity}"),
                }
            )
        self.generic_visit(node)
        self._leave()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    # -- static helper ---------------------------------------------------

    @staticmethod
    def _max_control_depth(node: ast.AST) -> int:
        """Return the maximum nesting depth of control-flow structures."""
        _CONTROL = (
            ast.If,
            ast.For,
            ast.While,
            ast.With,
            ast.Try,
            ast.AsyncFor,
            ast.AsyncWith,
        )
        # Also include ExceptHandler and match_case if present
        try:
            _CONTROL = (*_CONTROL, ast.ExceptHandler)  # type: ignore[assignment]
        except AttributeError:
            pass
        try:
            _CONTROL = (*_CONTROL, ast.match_case)  # type: ignore[attr-defined,assignment]
        except AttributeError:
            pass

        best = 0

        def _walk(n: ast.AST, depth: int) -> None:
            nonlocal best
            for child in ast.iter_child_nodes(n):
                if isinstance(child, _CONTROL):
                    new = depth + 1
                    if new > best:
                        best = new
                    _walk(child, new)
                else:
                    _walk(child, depth)

        _walk(node, 0)
        return best


def nesting_depth_check(code: str, max_depth: int = 15) -> list[dict]:
    """Analyse *code* for deeply nested functions.

    Measures **nesting depth** (enclosing classes/functions plus inner
    control-flow structures), *not* McCabe cyclomatic complexity.  Use
    ``radon`` or the CI complexity gate for true CC measurement.

    Returns a list of warning dicts for every function whose combined
    nesting depth exceeds *max_depth*::

        {"function": str, "lineno": int, "depth": int, "reason": str}

    If the code cannot be parsed, returns a single warning with
    ``function="<parse-error>"``.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            {
                "function": "<parse-error>",
                "lineno": exc.lineno or 0,
                "depth": 0,
                "reason": f"SyntaxError: {exc.msg}",
            }
        ]

    visitor = _ComplexityVisitor(max_depth)
    visitor.visit(tree)
    return visitor.warnings


# Backwards-compatible alias
complexity_check = nesting_depth_check
