"""Daily Continuity Engine — structured day-over-day context for NovaCore sessions.

Loads the last Fusion Memory checkpoint, computes deltas against prior state,
and provides a structured summary so each day builds on the previous one.

Stdlib only — MCP calls are optional and fail gracefully.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/home/nova/nova-core")
STATE_DIR = BASE / "STATE"
CONTINUITY_FILE = STATE_DIR / "daily_continuity.json"
METRICS_JSONL = STATE_DIR / "heartbeat_metrics.jsonl"


@dataclass
class DailyDelta:
    """Day-over-day change summary."""

    date: str  # ISO date (YYYY-MM-DD)
    prior_date: str  # date of previous continuity snapshot
    health_score: int  # current score out of 100
    health_delta: int  # change from prior day (+/- int)
    new_failures: list[str] = field(default_factory=list)
    resolved_failures: list[str] = field(default_factory=list)
    persistent_failures: list[str] = field(default_factory=list)
    open_threads: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    checkpoint_summary: str = ""
    test_count: int = 0
    test_delta: int = 0


def _load_prior_snapshot() -> dict:
    """Load the most recent daily continuity snapshot from disk."""
    if not CONTINUITY_FILE.exists():
        return {}
    try:
        return json.loads(CONTINUITY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_snapshot(data: dict) -> None:
    """Persist the current continuity snapshot."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONTINUITY_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(CONTINUITY_FILE)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _count_tests() -> int:
    """Count total test functions across test files (fast heuristic)."""
    tests_dir = BASE / "tests"
    if not tests_dir.is_dir():
        return 0
    count = 0
    for tf in tests_dir.glob("test_*.py"):
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
            count += text.count("\n    def test_")
            count += text.count("\ndef test_")
        except OSError:
            continue
    return count


def _latest_health() -> tuple[int, list[str]]:
    """Read the most recent heartbeat metric for current health score and failures."""
    if not METRICS_JSONL.exists():
        return 100, []
    try:
        lines = METRICS_JSONL.read_text().splitlines()
        if not lines:
            return 100, []
        last = json.loads(lines[-1])
        total = last.get("total_checks", 22)
        passed = last.get("passed", total)
        score = round(passed / total * 100) if total > 0 else 100
        failed_names = last.get("failed_names", [])
        return score, failed_names
    except (json.JSONDecodeError, OSError):
        return 100, []


def compute_daily_delta(
    *,
    checkpoint_summary: str = "",
    open_threads: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> DailyDelta:
    """Compute a day-over-day delta and persist the snapshot.

    Args:
        checkpoint_summary: Summary from Fusion Memory checkpoint (optional).
        open_threads: Unfinished work threads from checkpoint.
        next_actions: Planned next steps from checkpoint.

    Returns:
        DailyDelta with health deltas, thread continuity, and test counts.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prior = _load_prior_snapshot()
    prior_date = prior.get("date", "")
    prior_score = prior.get("health_score", 100)
    prior_failures = set(prior.get("current_failures", []))
    prior_tests = prior.get("test_count", 0)

    health_score, current_failures = _latest_health()
    current_fail_set = set(current_failures)

    test_count = _count_tests()

    delta = DailyDelta(
        date=today,
        prior_date=prior_date,
        health_score=health_score,
        health_delta=health_score - prior_score,
        new_failures=sorted(current_fail_set - prior_failures),
        resolved_failures=sorted(prior_failures - current_fail_set),
        persistent_failures=sorted(current_fail_set & prior_failures),
        open_threads=open_threads or prior.get("open_threads", []),
        next_actions=next_actions or prior.get("next_actions", []),
        checkpoint_summary=checkpoint_summary or prior.get("checkpoint_summary", ""),
        test_count=test_count,
        test_delta=test_count - prior_tests if prior_tests > 0 else 0,
    )

    # Persist for tomorrow's comparison
    snapshot = {
        "date": today,
        "health_score": health_score,
        "current_failures": current_failures,
        "test_count": test_count,
        "open_threads": delta.open_threads,
        "next_actions": delta.next_actions,
        "checkpoint_summary": delta.checkpoint_summary,
    }
    _save_snapshot(snapshot)

    return delta


def format_briefing(delta: DailyDelta) -> str:
    """Format a DailyDelta as a human-readable briefing string."""
    lines = [f"## Daily Continuity — {delta.date}"]

    if delta.prior_date:
        lines.append(f"Building on: {delta.prior_date}")
    lines.append("")

    # Health
    arrow = "+" if delta.health_delta > 0 else ""
    lines.append(f"**Health**: {delta.health_score}/100 ({arrow}{delta.health_delta} from prior)")
    if delta.resolved_failures:
        lines.append(f"  Resolved: {', '.join(delta.resolved_failures)}")
    if delta.new_failures:
        lines.append(f"  New failures: {', '.join(delta.new_failures)}")
    if delta.persistent_failures:
        lines.append(f"  Persistent: {', '.join(delta.persistent_failures)}")

    # Tests
    if delta.test_count:
        t_arrow = "+" if delta.test_delta > 0 else ""
        lines.append(f"**Tests**: {delta.test_count} ({t_arrow}{delta.test_delta})")

    # Continuity
    if delta.checkpoint_summary:
        lines.append(f"\n**Last session**: {delta.checkpoint_summary[:200]}")

    if delta.open_threads:
        lines.append("\n**Open threads**:")
        for t in delta.open_threads[:5]:
            lines.append(f"  - {t[:100]}")

    if delta.next_actions:
        lines.append("\n**Next actions**:")
        for a in delta.next_actions[:5]:
            lines.append(f"  - {a[:100]}")

    return "\n".join(lines)
