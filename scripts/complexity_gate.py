#!/usr/bin/env python3
"""Fail CI if complexity increased vs previous commit.

Phase 7 Step 7.6 — Complexity quality gate.
Usage: python3 scripts/complexity_gate.py [--threshold 15] [--paths .]
"""

import argparse
import json
import subprocess
import sys


def get_radon_complexity(paths: list[str]) -> dict[str, list[dict]]:
    """Run radon cc and return results as {file: [{name, complexity, lineno, rank}]}."""
    result = subprocess.run(
        ["radon", "cc"] + paths + ["-j"],  # JSON output
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def check_complexity(threshold: int = 15, paths: list[str] | None = None) -> int:
    """Check all functions for complexity > threshold. Returns exit code."""
    if paths is None:
        paths = ["."]

    results = get_radon_complexity(paths)
    violations: list[dict] = []

    for filepath, functions in results.items():
        for func in functions:
            # radon JSON nests methods inside classes; handle both flat and nested
            _collect_violations(filepath, func, threshold, violations)

    if violations:
        print(f"FAIL: {len(violations)} function(s) exceed complexity threshold {threshold}:")
        for v in sorted(violations, key=lambda x: -x["complexity"]):
            print(f"  {v['file']}:{v['line']} {v['function']} CC={v['complexity']} (rank {v['rank']})")
        return 1

    print(f"PASS: All functions at or below complexity {threshold}")
    return 0


def _collect_violations(
    filepath: str,
    item: dict,
    threshold: int,
    violations: list[dict],
) -> None:
    """Recursively collect complexity violations from radon JSON items."""
    cc = item.get("complexity", 0)
    if cc > threshold:
        violations.append(
            {
                "file": filepath,
                "function": item.get("name", "?"),
                "complexity": cc,
                "line": item.get("lineno", 0),
                "rank": item.get("rank", "?"),
            }
        )
    # radon nests class methods under "methods" key
    for method in item.get("methods", []):
        _collect_violations(filepath, method, threshold, violations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Complexity quality gate — fails if any function exceeds threshold")
    parser.add_argument(
        "--threshold",
        type=int,
        default=15,
        help="Maximum allowed cyclomatic complexity (default: 15)",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=["."],
        help="Paths to scan (default: current directory)",
    )
    args = parser.parse_args()
    sys.exit(check_complexity(args.threshold, args.paths))
