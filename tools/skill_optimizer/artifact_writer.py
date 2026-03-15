"""Artifact persistence for the skill optimization pipeline.

Writes structured artifacts to benchmarks/ with append-only history.
Each run gets a unique run_id directory. Supports manifest, summary,
per-skill results, and benchmark history tracking.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BENCHMARKS_DIR = BASE_DIR / "benchmarks"
HISTORY_FILE = BENCHMARKS_DIR / "history" / "benchmark_history.jsonl"


def generate_run_id() -> str:
    """Generate a unique run ID."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"run_{ts}_{short_uuid}"


def ensure_run_dir(run_id: str) -> Path:
    """Create and return the run directory."""
    run_dir = BENCHMARKS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "baselines").mkdir(exist_ok=True)
    (run_dir / "candidates").mkdir(exist_ok=True)
    (run_dir / "decisions").mkdir(exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    return run_dir


def write_manifest(run_dir: Path, manifest: dict) -> Path:
    """Write the run manifest."""
    path = run_dir / "manifest.json"
    manifest.setdefault("run_id", run_dir.name)
    manifest.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    manifest.setdefault("status", "in_progress")
    path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return path


def update_manifest(run_dir: Path, updates: dict) -> None:
    """Update fields in an existing manifest."""
    path = run_dir / "manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text())
    else:
        manifest = {}
    manifest.update(updates)
    path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")


def write_baseline(run_dir: Path, skill_name: str, result: dict) -> Path:
    """Write a baseline result for a skill."""
    path = run_dir / "baselines" / f"{skill_name}.json"
    path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    return path


def write_candidate(run_dir: Path, skill_name: str, candidate_id: int, result: dict) -> Path:
    """Write a candidate evaluation result."""
    path = run_dir / "candidates" / f"{skill_name}_candidate_{candidate_id}.json"
    path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    return path


def write_decision(run_dir: Path, skill_name: str, decision: dict) -> Path:
    """Write a decision record for a skill."""
    path = run_dir / "decisions" / f"{skill_name}.json"
    path.write_text(json.dumps(decision, indent=2, default=str) + "\n")
    return path


def write_summary(run_dir: Path, summary: dict) -> Path:
    """Write the run summary."""
    # JSON summary
    json_path = run_dir / "summary.json"
    json_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")

    # TSV summary for quick inspection
    tsv_path = run_dir / "summary.tsv"
    skills = summary.get("skills", [])
    if skills:
        headers = ["skill", "decision", "reason", "baseline_score", "candidate_score", "delta"]
        lines = ["\t".join(headers)]
        for s in skills:
            lines.append(
                "\t".join(
                    [
                        str(s.get("skill", "")),
                        str(s.get("decision", "")),
                        str(s.get("reason", "")),
                        str(s.get("baseline_score", "")),
                        str(s.get("candidate_score", "")),
                        str(s.get("delta", "")),
                    ]
                )
            )
        tsv_path.write_text("\n".join(lines) + "\n")

    return json_path


def append_history(entry: dict) -> None:
    """Append an entry to the benchmark history (append-only JSONL)."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_history() -> list[dict]:
    """Load all benchmark history entries."""
    if not HISTORY_FILE.exists():
        return []
    entries = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def get_latest_baseline(skill_name: str) -> dict | None:
    """Find the most recent baseline for a skill from history."""
    history = load_history()
    for entry in reversed(history):
        if entry.get("skill") == skill_name and entry.get("type") == "baseline":
            return entry
    return None


def get_cached_baseline(skill_name: str, max_age_hours: float = 24.0) -> dict | None:
    """Get a cached baseline if it's recent enough."""
    import datetime

    baseline = get_latest_baseline(skill_name)
    if not baseline:
        return None

    ts_str = baseline.get("timestamp", "")
    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600
        if age_hours <= max_age_hours:
            return baseline
    except (ValueError, TypeError):
        pass

    return None


def write_log(run_dir: Path, skill_name: str, log_entries: list[dict]) -> Path:
    """Write JSONL log for a skill's optimization run."""
    path = run_dir / "logs" / f"{skill_name}.jsonl"
    with open(path, "w") as f:
        for entry in log_entries:
            f.write(json.dumps(entry, default=str) + "\n")
    return path
