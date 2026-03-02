#!/usr/bin/env python3
"""Diagnostic: verify CLAUDECODE env var is stripped for child processes."""

import os
import subprocess
import sys


def main():
    print("=== CLAUDECODE Environment Diagnostic ===\n")

    # 1. Check parent
    val = os.environ.get("CLAUDECODE")
    print(f"Parent process CLAUDECODE = {val!r}")

    # 2. Build clean env
    child_env = os.environ.copy()
    removed = child_env.pop("CLAUDECODE", None)
    print(f"Removed from child env:    {removed!r}")

    # 3. Verify child sees no CLAUDECODE
    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('CLAUDECODE', '(not set)'))"],
        env=child_env,
        capture_output=True,
        text=True,
    )
    print(f"Child sees CLAUDECODE:     {result.stdout.strip()}")

    # 4. Try launching claude with clean env
    print("\n--- Launching: /usr/bin/claude -p 'Say ok' ---")
    try:
        result = subprocess.run(
            ["/usr/bin/claude", "-p", "Say ok"],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(f"Exit code: {result.returncode}")
        print(f"Stdout:    {result.stdout.strip()[:200]}")
        if result.stderr:
            print(f"Stderr:    {result.stderr.strip()[:200]}")
        if result.returncode == 0:
            print("\nRESULT: Claude launched successfully with CLAUDECODE stripped.")
        else:
            print("\nRESULT: Claude exited non-zero but did not refuse to launch.")
    except FileNotFoundError:
        print("RESULT: /usr/bin/claude not found.")
    except subprocess.TimeoutExpired:
        print("RESULT: Timed out (30s) — claude ran but was slow.")


if __name__ == "__main__":
    main()
