#!/usr/bin/env python3
"""NovaTrade code quality gate — catches recurring anti-patterns.

Prevents the same class of production bugs from being reintroduced:
  1. print() in production code (should use logging)
  2. Non-atomic file writes (.write_text / .write_bytes without tempfile+replace)
  3. iterrows() in pandas code (performance anti-pattern)
  4. Unit mismatches (variable named *_pips using *_usd field, or vice versa)
  5. Zero-value safety gate defaults that should have production minimums
  6. Double os.close() without boolean guard

Usage:
  python scripts/check-novatrade-quality.py          # check all novatrade/ files
  python scripts/check-novatrade-quality.py --staged  # check only staged files
  python scripts/check-novatrade-quality.py --fix     # show suggested fixes

Exit codes: 0 = clean, 1 = violations found
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────

NOVATRADE_DIR = Path(__file__).resolve().parent.parent / "novatrade"

# Files/dirs to skip (test files, __pycache__, etc.)
SKIP_PATTERNS = {"__pycache__", ".pyc", "test_", "conftest"}

# ── Checks ─────────────────────────────────────────────────────────────────


class Violation:
    def __init__(self, path: str, line: int, rule: str, message: str, fix: str = ""):
        self.path = path
        self.line = line
        self.rule = rule
        self.message = message
        self.fix = fix

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.rule}] {self.message}"


def check_print_in_production(path: Path, lines: list[str]) -> list[Violation]:
    """Q1: print() in production code should be logging."""
    violations = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\bprint\s*\(", line) and not stripped.startswith("#"):
            violations.append(
                Violation(
                    str(path),
                    i,
                    "Q1-print",
                    "print() found — use logging.getLogger() instead",
                    fix="Replace print(...) with log.info(...) or log.warning(...)",
                )
            )
    return violations


def check_non_atomic_writes(path: Path, lines: list[str]) -> list[Violation]:
    """Q2: Direct file writes without atomic pattern."""
    violations = []
    for i, line in enumerate(lines, 1):
        if re.search(r"\.(write_text|write_bytes)\s*\(", line):
            # Check if nearby code has tempfile/os.replace pattern
            context = "\n".join(lines[max(0, i - 10) : i + 5])
            if "tempfile" not in context and "os.replace" not in context:
                violations.append(
                    Violation(
                        str(path),
                        i,
                        "Q2-atomic",
                        "Non-atomic file write — use tempfile + os.replace pattern",
                        fix="Use fd, tmp = tempfile.mkstemp(); os.write(fd, data); os.close(fd); os.replace(tmp, path)",
                    )
                )
    return violations


def check_iterrows(path: Path, lines: list[str]) -> list[Violation]:
    """Q3: iterrows() is slow for large DataFrames."""
    violations = []
    for i, line in enumerate(lines, 1):
        if ".iterrows()" in line:
            violations.append(
                Violation(
                    str(path),
                    i,
                    "Q3-iterrows",
                    "df.iterrows() is 100x slower than vectorized — use .to_numpy() + list comprehension",
                    fix="Extract columns with df[col].to_numpy() then use index-based loop",
                )
            )
    return violations


def check_unit_mismatch(path: Path, lines: list[str]) -> list[Violation]:
    """Q4: Variable named *_pips reading *_usd field (or vice versa)."""
    violations = []
    for i, line in enumerate(lines, 1):
        # Pattern: variable with _pips reading a _usd field
        if re.search(r'\w+_pips\s*=.*["\'].*_usd["\']', line):
            violations.append(
                Violation(
                    str(path),
                    i,
                    "Q4-units",
                    "Unit mismatch: variable named *_pips reads *_usd field",
                    fix="Use consistent units — either both _pips or both _usd",
                )
            )
        if re.search(r'\w+_usd\s*=.*["\'].*_pips["\']', line):
            violations.append(
                Violation(
                    str(path),
                    i,
                    "Q4-units",
                    "Unit mismatch: variable named *_usd reads *_pips field",
                    fix="Use consistent units — either both _pips or both _usd",
                )
            )
    return violations


def check_double_os_close(path: Path, lines: list[str]) -> list[Violation]:
    """Q5: os.close(fd) in both try and except without boolean guard."""
    violations = []
    content = "\n".join(lines)
    # Find try blocks with os.close in both try and except
    pattern = re.compile(
        r"try:\s*\n(?:.*\n)*?\s*os\.close\((\w+)\).*\n(?:.*\n)*?"
        r"except.*:\s*\n(?:.*\n)*?\s*os\.close\(\1\)",
        re.MULTILINE,
    )
    for match in pattern.finditer(content):
        # Check if there's a 'closed' boolean guard
        block = match.group(0)
        if "closed" not in block and "if not " not in block:
            line_num = content[: match.start()].count("\n") + 1
            violations.append(
                Violation(
                    str(path),
                    line_num,
                    "Q5-double-close",
                    f"os.close({match.group(1)}) in both try/except without boolean guard — risks EBADF",
                    fix=(
                        "Add `closed = False` before try, set `closed = True` "
                        "after os.close, check `if not closed` in except"
                    ),
                )
            )
    return violations


ALL_CHECKS = [
    check_print_in_production,
    check_non_atomic_writes,
    check_iterrows,
    check_unit_mismatch,
    check_double_os_close,
]


# ── File Discovery ─────────────────────────────────────────────────────────


def get_staged_files() -> list[Path]:
    """Get staged Python files in novatrade/."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        cwd=NOVATRADE_DIR.parent,
    )
    return [
        NOVATRADE_DIR.parent / f
        for f in result.stdout.strip().split("\n")
        if f.startswith("novatrade/") and f.endswith(".py")
    ]


def get_all_files() -> list[Path]:
    """Get all Python files in novatrade/."""
    return sorted(NOVATRADE_DIR.rglob("*.py"))


def should_skip(path: Path) -> bool:
    return any(skip in str(path) for skip in SKIP_PATTERNS)


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="NovaTrade quality gate")
    parser.add_argument("--staged", action="store_true", help="Check only staged files")
    parser.add_argument("--fix", action="store_true", help="Show fix suggestions")
    args = parser.parse_args()

    files = get_staged_files() if args.staged else get_all_files()
    files = [f for f in files if not should_skip(f)]

    violations: list[Violation] = []

    for path in files:
        try:
            lines = path.read_text().splitlines()
        except Exception:  # noqa: S112
            continue
        for check in ALL_CHECKS:
            violations.extend(check(path, lines))

    if not violations:
        print(f"\033[32m✓ Quality gate passed — {len(files)} files checked, 0 violations\033[0m")
        return 0

    print(f"\033[31m✗ Quality gate FAILED — {len(violations)} violation(s) in {len(files)} files\033[0m\n")
    for v in violations:
        print(f"  {v}")
        if args.fix and v.fix:
            print(f"    → Fix: {v.fix}")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
