"""Autonomy Dashboard CLI — `python3 -m novatrade.autonomy`.

Displays the full autonomy state at a glance: dimension scores, trends,
decision engine recommendation, and goal tree progress.

Usage:
    python3 -m novatrade.autonomy              # full dashboard
    python3 -m novatrade.autonomy --scores      # scores only
    python3 -m novatrade.autonomy --decision    # decision only
    python3 -m novatrade.autonomy --goals       # goal tree only
    python3 -m novatrade.autonomy --json        # raw JSON output
    python3 -m novatrade.autonomy --snapshot    # read last snapshot (no live collection)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_PATH = "/home/nova/nova-core"
STATE_DIR = Path(BASE_PATH) / "STATE"


# ── Formatting helpers ──────────────────────────────────────────────────


def _alert_badge(score: float) -> str:
    """Return a text badge for a score."""
    if score >= 70:
        return "GREEN"
    elif score >= 40:
        return "YELLOW"
    else:
        return "RED"


def _bar(score: float, width: int = 20) -> str:
    """Render a text progress bar."""
    filled = int(score / 100 * width)
    return "[" + "=" * filled + " " * (width - filled) + "]"


def _trend_arrow(direction: str) -> str:
    arrows = {"improving": "^", "degrading": "v", "stable": "="}
    return arrows.get(direction, "?")


def _pad(text: str, width: int) -> str:
    """Left-pad or truncate text to width."""
    return text[:width].ljust(width)


# ── Dashboard sections ──────────────────────────────────────────────────


def print_header() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print()
    print("  NOVACORE AUTONOMY DASHBOARD")
    print(f"  {now}")
    print("  " + "-" * 55)


def print_scores(report) -> None:
    """Print dimension scores with bars and alerts."""
    print()
    print("  DIMENSION SCORES")
    print(f"  Overall: {report.overall_score:.1f}/100 [{report.overall_alert.value}]")
    print()

    # Header
    print(f"  {'Dimension':<24} {'Score':>5}  {'Bar':<22} {'Alert':<7} {'Trend':<5}")
    print(f"  {'-' * 24} {'-' * 5}  {'-' * 22} {'-' * 7} {'-' * 5}")

    for name, dim in report.dimensions.items():
        trend = report.trends.get(name)
        trend_str = _trend_arrow(trend.direction) if trend else "?"
        alert = _alert_badge(dim.score)
        print(f"  {_pad(name, 24)} {dim.score:5.1f}  {_bar(dim.score)}  {_pad(alert, 7)} {trend_str}")

        # Sub-metrics (compact)
        if dim.sub_metrics:
            parts = [f"{sm.name}={sm.value:.0f}" for sm in dim.sub_metrics]
            line = "    " + ", ".join(parts)
            if len(line) > 78:
                line = line[:75] + "..."
            print(f"  {line}")

    # Warnings
    if report.warnings:
        print()
        print("  WARNINGS:")
        for w in report.warnings:
            print(f"    ! {w}")


def print_decision(decision) -> None:
    """Print the decision engine recommendation."""
    print()
    print("  DECISION ENGINE")
    print(f"  Mode:   {decision.mode.value.upper()}")
    print(f"  Target: {decision.target_dimension or '(none)'}")
    print(f"  Reason: {decision.reason}")
    if decision.suggested_actions:
        print("  Actions:")
        for a in decision.suggested_actions:
            print(f"    - {a}")
    print(f"  Confidence: {decision.confidence}")


def print_goals(tree) -> None:
    """Print goal tree progress."""
    from novatrade.autonomy.decomposition.engine import GoalDecomposer

    decomposer = GoalDecomposer(BASE_PATH)
    actionable = decomposer.get_actionable_subgoals(tree)
    completed = sum(1 for sg in tree.sub_goals if sg.status.value == "completed")

    print()
    print(f"  GOAL TREE: {tree.goal_description}")
    print(f"  Progress: {completed}/{len(tree.sub_goals)} sub-goals complete")
    print()

    for sg in tree.sub_goals:
        status = sg.status.value
        check = "x" if status == "completed" else (" " if status == "not_started" else "~")
        progress_pct = f"{sg.progress * 100:.0f}%" if sg.progress > 0 else ""
        dim_tag = f" [{sg.dimension}]" if sg.dimension else ""
        print(f"  [{check}] {_pad(sg.title, 40)} {_pad(status, 12)} {progress_pct:>4}{dim_tag}")

    if actionable:
        print()
        print(f"  Actionable now ({len(actionable)}):")
        for sg in actionable[:5]:
            print(f"    -> {sg.title}")


def print_decision_history() -> None:
    """Print recent decision history."""
    history_path = STATE_DIR / "decision_history.json"
    if not history_path.exists():
        print("\n  DECISION HISTORY: (no history)")
        return

    try:
        history = json.loads(history_path.read_text())
        if not isinstance(history, list):
            return
    except (json.JSONDecodeError, OSError):
        return

    recent = history[-5:]
    if not recent:
        return

    print()
    print("  RECENT DECISIONS (last 5)")
    print(f"  {'Time':<20} {'Mode':<10} {'Target':<22} {'Reason (truncated)'}")
    print(f"  {'-' * 20} {'-' * 10} {'-' * 22} {'-' * 30}")
    for d in recent:
        ts = d.get("decided_at", "?")[:19]
        mode = d.get("mode", "?")
        target = d.get("target_dimension", "-") or "-"
        reason = (d.get("reason", "") or "")[:50]
        print(f"  {_pad(ts, 20)} {_pad(mode, 10)} {_pad(target, 22)} {reason}")


def print_snapshot_mode() -> None:
    """Read and display the last persisted snapshot (no live collection)."""
    snap_path = STATE_DIR / "autonomy_snapshot.json"
    if not snap_path.exists():
        print("\n  No snapshot found at STATE/autonomy_snapshot.json")
        return

    try:
        snap = json.loads(snap_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"\n  Failed to read snapshot: {e}")
        return

    print_header()
    print(f"\n  (from snapshot: {snap.get('generated_at', '?')})")
    print()
    score = snap.get("overall_score", 0)
    alert = snap.get("alert_level", "?")
    print(f"  Overall: {score:.1f}/100 [{alert}]")
    print()

    dims = snap.get("dimensions", {})
    print(f"  {'Dimension':<24} {'Score':>5}  {'Bar':<22} {'Trend':<5}")
    print(f"  {'-' * 24} {'-' * 5}  {'-' * 22} {'-' * 5}")
    for name, d in dims.items():
        s = d.get("score", 0)
        trend_dir = d.get("trend", "?") or "?"
        a = _alert_badge(s)
        print(f"  {_pad(name, 24)} {s:5.1f}  {_bar(s)}  {_trend_arrow(trend_dir)}")

    dec = snap.get("decision", {})
    if dec:
        print()
        print(f"  Decision: {dec.get('mode', '?').upper()} -> {dec.get('target', '-')}")
        print(f"  Reason: {dec.get('reason', '')[:80]}")

    gt = snap.get("goal_tree", {})
    if gt:
        print()
        print(f"  Goals: {gt.get('completed', 0)}/{gt.get('total', 0)} complete")
        for a in gt.get("actionable", [])[:3]:
            print(f"    -> {a}")

    print_decision_history()
    print()


# ── Main ────────────────────────────────────────────────────────────────


async def _run_live(args: argparse.Namespace) -> int:
    """Run live collection and display."""
    from novatrade.autonomy.decision_context import ContextAssembler
    from novatrade.autonomy.decision_engine import DecisionEngine
    from novatrade.autonomy.decomposition.engine import GoalDecomposer
    from novatrade.autonomy.decomposition.novatrade_tree import (
        GOAL_ID as NOVATRADE_GOAL_ID,
    )
    from novatrade.autonomy.decomposition.novatrade_tree import (
        build_novatrade_tree,
    )
    from novatrade.autonomy.progress_scorer import ProgressScorer

    # Step 1: Score
    scorer = ProgressScorer(base_path=BASE_PATH)
    report = await scorer.score()

    if args.json:
        print(json.dumps(json.loads(report.model_dump_json()), indent=2, default=str))
        return 0

    show_all = not (args.scores or args.decision or args.goals)

    print_header()

    if show_all or args.scores:
        print_scores(report)

    # Step 2: Decision (read-only — classify without persisting to history)
    if show_all or args.decision:
        assembler = ContextAssembler(BASE_PATH)
        context = await assembler.assemble(report)
        engine = DecisionEngine(base_path=BASE_PATH)
        # Use internal classification to avoid polluting decision history
        weakest_name, weakest_dim = engine._find_weakest_dimension(report)
        knowledge_gaps = engine._detect_knowledge_gaps(report)
        regression = engine._detect_regression(report, context.recent_decisions)
        decision = engine._classify(
            weakest_name=weakest_name,
            weakest_dim=weakest_dim,
            knowledge_gaps=knowledge_gaps,
            regression=regression,
            report=report,
            context=context,
        )
        print_decision(decision)

    # Step 3: Goals
    if show_all or args.goals:
        decomposer = GoalDecomposer(BASE_PATH)
        tree = decomposer.load(NOVATRADE_GOAL_ID)
        if tree is None:
            tree = build_novatrade_tree()
        tree = decomposer.update_progress(tree, report)
        print_goals(tree)

    if show_all:
        print_decision_history()

    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m novatrade.autonomy",
        description="NovaCore Autonomy Dashboard — system state at a glance",
    )
    parser.add_argument("--scores", action="store_true", help="Show dimension scores only")
    parser.add_argument("--decision", action="store_true", help="Show decision engine only")
    parser.add_argument("--goals", action="store_true", help="Show goal tree only")
    parser.add_argument("--json", action="store_true", help="Output raw JSON report")
    parser.add_argument("--snapshot", action="store_true", help="Read last snapshot (no live collection)")

    args = parser.parse_args()

    if args.snapshot:
        print_snapshot_mode()
        return 0

    try:
        return asyncio.run(_run_live(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
