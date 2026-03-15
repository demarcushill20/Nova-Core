"""Trend visualization for the skill optimization pipeline.

Generates text-based trend reports from benchmark history.
Designed for terminal and markdown output (no matplotlib dependency).
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def load_history() -> list[dict]:
    """Load benchmark history."""
    path = BASE_DIR / "benchmarks" / "history" / "benchmark_history.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def skill_score_trend(skill_name: str, history: list[dict] | None = None) -> list[dict]:
    """Get score history for a skill over time."""
    if history is None:
        history = load_history()

    entries = [
        e
        for e in history
        if e.get("skill") == skill_name
        and isinstance(e.get("metrics", {}).get("weighted_score", e.get("baseline_score")), (int, float))
    ]

    return [
        {
            "timestamp": e.get("timestamp", ""),
            "type": e.get("type", ""),
            "score": e.get("metrics", {}).get("weighted_score", e.get("baseline_score", 0)),
            "run_id": e.get("run_id", ""),
            "decision": e.get("decision", ""),
        }
        for e in entries
    ]


def generate_leaderboard(history: list[dict] | None = None) -> str:
    """Generate a skill leaderboard from latest scores."""
    if history is None:
        history = load_history()

    # Get latest score per skill
    latest: dict[str, dict] = {}
    for entry in history:
        skill = entry.get("skill", "")
        if not skill:
            continue
        score = entry.get("metrics", {}).get("weighted_score", entry.get("baseline_score"))
        if isinstance(score, (int, float)):
            latest[skill] = {
                "score": score,
                "timestamp": entry.get("timestamp", ""),
                "type": entry.get("type", ""),
            }

    if not latest:
        return "# Leaderboard\n\nNo data yet.\n"

    # Sort by score descending
    sorted_skills = sorted(latest.items(), key=lambda x: -x[1]["score"])

    lines = [
        "# Skill Leaderboard",
        "",
        "| Rank | Skill | Score | Last Updated |",
        "|------|-------|-------|-------------|",
    ]

    for i, (skill, data) in enumerate(sorted_skills, 1):
        ts = data["timestamp"][:10] if data["timestamp"] else "-"
        lines.append(f"| {i} | {skill} | {data['score']:.4f} | {ts} |")

    lines.append("")
    return "\n".join(lines)


def generate_trend_sparkline(scores: list[float], width: int = 20) -> str:
    """Generate a text sparkline for score trend."""
    if not scores:
        return ""

    blocks = " ▁▂▃▄▅▆▇█"
    min_val = min(scores)
    max_val = max(scores)
    val_range = max_val - min_val

    if val_range == 0:
        return "─" * min(len(scores), width)

    # Resample if needed
    if len(scores) > width:
        step = len(scores) / width
        resampled = [scores[int(i * step)] for i in range(width)]
    else:
        resampled = scores

    sparkline = ""
    for val in resampled:
        idx = int((val - min_val) / val_range * (len(blocks) - 1))
        sparkline += blocks[idx]

    return sparkline


def generate_trends_report(history: list[dict] | None = None) -> str:
    """Generate a comprehensive trends report."""
    if history is None:
        history = load_history()

    if not history:
        return "# Trends Report\n\nNo history data available.\n"

    # Group by skill
    skill_entries: dict[str, list[dict]] = {}
    for entry in history:
        skill = entry.get("skill", "unknown")
        skill_entries.setdefault(skill, []).append(entry)

    lines = [
        "# Optimization Trends",
        "",
        f"**Total entries:** {len(history)}",
        f"**Skills tracked:** {len(skill_entries)}",
        "",
    ]

    # Per-skill trends
    for skill in sorted(skill_entries.keys()):
        entries = skill_entries[skill]
        scores = [
            e.get("metrics", {}).get("weighted_score", e.get("baseline_score", 0))
            for e in entries
            if isinstance(e.get("metrics", {}).get("weighted_score", e.get("baseline_score")), (int, float))
        ]

        if not scores:
            continue

        sparkline = generate_trend_sparkline(scores)
        latest = scores[-1]
        best = max(scores)
        runs = len(entries)

        lines.append(f"### {skill}")
        lines.append(f"  {sparkline}  latest={latest:.4f}  best={best:.4f}  runs={runs}")
        lines.append("")

    return "\n".join(lines)
