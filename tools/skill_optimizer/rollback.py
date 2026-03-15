"""Rollback mechanism for the skill optimization pipeline.

Supports rollback by run ID or by skill name to a specific version.
Uses git revert to create an auditable rollback trail.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BENCHMARKS_DIR = BASE_DIR / "benchmarks"


def _git(args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    )


def find_commits_for_run(run_id: str) -> list[str]:
    """Find all commit SHAs associated with a run ID."""
    # Search git log for commits mentioning the run ID
    result = _git(["log", "--all", "--grep", f"Run: {run_id}", "--format=%H"])
    if result.returncode != 0:
        return []
    return [sha.strip() for sha in result.stdout.strip().splitlines() if sha.strip()]


def rollback_by_run(run_id: str, dry_run: bool = False) -> dict:
    """Rollback all changes from a specific run.

    Creates a rollback branch with revert commits.
    """
    commits = find_commits_for_run(run_id)

    if not commits:
        return {"success": False, "error": f"No commits found for run {run_id}"}

    branch_name = f"rollback/{run_id}"

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "branch": branch_name,
            "commits_to_revert": commits,
        }

    try:
        # Create rollback branch
        result = _git(["checkout", "-b", branch_name])
        if result.returncode != 0:
            return {"success": False, "error": f"Failed to create branch: {result.stderr}"}

        reverted = []
        for sha in reversed(commits):  # Revert in reverse order
            result = _git(["revert", "--no-edit", sha])
            if result.returncode == 0:
                reverted.append(sha)
            else:
                # Abort revert on failure
                _git(["revert", "--abort"])
                break

        # Switch back
        _git(["checkout", "-"])

        # Write rollback manifest
        manifest_dir = BENCHMARKS_DIR / "rollbacks" / run_id
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run_id,
            "branch": branch_name,
            "commits_reverted": reverted,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (manifest_dir / "rollback_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

        return {
            "success": True,
            "branch": branch_name,
            "commits_reverted": reverted,
        }

    except Exception as e:
        _git(["checkout", "-"])
        return {"success": False, "error": str(e)}


# CLI
def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Rollback skill optimizer changes")
    parser.add_argument("--run-id", required=True, help="Run ID to rollback")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    args = parser.parse_args()

    result = rollback_by_run(args.run_id, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
