#!/usr/bin/env python3
"""NovaCore Daily Shift Schedule Generator.

Self-perpetuating schedule system that generates shift-block task files
per day, leveraging the watcher's ``scheduled_at`` frontmatter mechanism.

Supports two shifts per day:
  - Morning shift (blocks 1-8): 6:30 AM - 11:45 AM CT
  - Afternoon shift (blocks 9-16): 2:30 PM - 7:45 PM CT

Usage:
    python3 scripts/daily_shift_generator.py                        # generate both shifts for tomorrow (batch)
    python3 scripts/daily_shift_generator.py --today                # generate both shifts for today (batch)
    python3 scripts/daily_shift_generator.py --today --shift morning  # morning only (batch)
    python3 scripts/daily_shift_generator.py --today --shift afternoon # afternoon only (batch)
    python3 scripts/daily_shift_generator.py --jit                  # JIT: only blocks due within 75 min
    python3 scripts/daily_shift_generator.py --jit --lead-minutes 60 # JIT with custom lead time
    python3 scripts/daily_shift_generator.py --dry-run              # preview without writing
    python3 scripts/daily_shift_generator.py --today --dry-run --shift both

The watcher reads TASKS/*.md, checks ``scheduled_at`` YAML frontmatter, and
dispatches when ``now >= scheduled_at``.  The watcher uses naive local time
(server is UTC).  Central Time conversions happen here.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Scheduler integration (graceful degradation) ────────────────────────────
# Ensure project root is importable regardless of CWD (Claude workers may
# run this script from a different directory).
_PROJECT_ROOT = str(Path("/home/nova/nova-core"))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    import json as _json

    from utils.scheduler.orchestrator import (
        SchedulerResult,
        build_block_plan,
        build_shadow_comparison,
        load_scheduler_config,
    )
    from utils.scheduler.replanner import BlockState, save_block_state
    from utils.scheduler.work_unit import CommitmentLevel, TaskClass, WorkMode, WorkUnit

    _HAS_SCHEDULER = True
except Exception:  # ImportError, ModuleNotFoundError, etc.
    _HAS_SCHEDULER = False

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path("/home/nova/nova-core")
TASKS = ROOT / "TASKS"
TASKS_COMPLETED = TASKS / "archive"
OUTPUT = ROOT / "OUTPUT"
LOGS = ROOT / "LOGS"

# ── Timezone ─────────────────────────────────────────────────────────────────
CT = ZoneInfo("America/Chicago")  # Central Time (handles DST)
SERVER_TZ = ZoneInfo("UTC")  # Server is UTC

# ── Cascade Guard ───────────────────────────────────────────────────────────
MAX_LOOKAHEAD_DAYS = 2  # Never generate shifts more than 2 days into the future


# ── Morning Shift Block Definitions (blocks 1-8) ──────────────────────────
MORNING_SHIFT_BLOCKS: list[dict] = [
    {
        "block": 1,
        "hour": 6,
        "minute": 30,
        "slug": "system_health",
        "title": "System Health & Continuity",
        "focus": "System Health",
        "instructions": """\
1. Check system health:
   - Read HEARTBEAT.md for current state
   - Verify all systemd services are running (watcher, telegram, telegram-notifier)
   - Run test suite: `python -m pytest tests/ -q --tb=short`

2. Review yesterday's outcomes:
   - Check OUTPUT/ for completed task results
   - Identify any failed or incomplete work
   - Note any issues that need follow-up

3. Continuity check:
   - Query Fusion Memory for last session checkpoint
   - Check open_threads and next_actions
   - Prioritize carrying forward any unfinished work""",
        "output_suffix": "health",
    },
    {
        "block": 2,
        "hour": 7,
        "minute": 15,
        "slug": "novatrade_progress",
        "title": "NovaTrade Progress",
        "focus": "NovaTrade",
        "instructions": """\
1. Check NovaTrade status:
   - Review novatrade/ directory for current state
   - Check if novacore-novatrade.service is running
   - Review any open NovaTrade issues or gaps

2. Push forward:
   - Pick the highest-impact NovaTrade item to advance
   - Fix execution gaps, implement next steps, or expand test coverage
   - Focus on items that move toward live trading readiness

3. Test and verify:
   - Run NovaTrade-specific tests: `python -m pytest tests/ -k novatrade -q`
   - Verify any changes don't break existing functionality""",
        "output_suffix": "novatrade",
    },
    {
        "block": 3,
        "hour": 8,
        "minute": 0,
        "slug": "implementation",
        "title": "Implementation Work",
        "focus": "Implementation",
        "instructions": """\
1. Identify highest-impact work:
   - Check MEMORY.md for current plan status and open items
   - Look for items flagged as high priority or blocking other work
   - Consider what would have the biggest positive impact on the system

2. Implement:
   - Write clean, modular, well-tested code
   - Follow existing patterns and conventions in the codebase
   - Create tests alongside implementation

3. Validate:
   - Run affected test suites
   - Verify no regressions with: `python -m pytest tests/ -q --tb=short`""",
        "output_suffix": "implementation",
    },
    {
        "block": 4,
        "hour": 8,
        "minute": 45,
        "slug": "deep_research",
        "title": "Deep Research",
        "focus": "Research",
        "instructions": """\
1. Choose a research topic:
   - Pick something that advances NovaCore or NovaTrade capabilities
   - Consider: new libraries, architectural patterns, trading strategies,
     AI techniques, infrastructure improvements
   - Prefer topics that can translate into actionable improvements

2. Research deeply:
   - Use web search and documentation to gather information
   - Analyze trade-offs, compare approaches
   - Look for real-world production examples

3. Document findings:
   - Store key insights in Fusion Memory with category: research
   - Write a concise summary with actionable recommendations
   - Note any items that should become implementation tasks""",
        "output_suffix": "research",
    },
    {
        "block": 5,
        "hour": 9,
        "minute": 30,
        "slug": "free_will",
        "title": "Free Will / Autonomy",
        "focus": "Autonomy",
        "instructions": """\
1. Assess current state:
   - Look at the codebase, services, memory, and recent outputs
   - Consider what matters most right now based on current conditions
   - Trust your judgment — you have full context on the system

2. Act on your assessment:
   - Fix something that has been bothering you
   - Improve something you've noticed is suboptimal
   - Explore an idea you think has potential
   - Refactor code that could be cleaner
   - Build something new that would help

3. Document your choice:
   - Explain why you chose this particular action
   - Record the rationale in Fusion Memory""",
        "output_suffix": "autonomy",
    },
    {
        "block": 6,
        "hour": 10,
        "minute": 15,
        "slug": "testing_quality",
        "title": "Testing & Quality",
        "focus": "Quality",
        "instructions": """\
1. Coverage analysis:
   - Run: `python -m pytest tests/ --cov=. --cov-report=term-missing -q`
   - Identify modules with lowest coverage
   - Focus on critical runtime paths

2. Fix flaky tests:
   - Look for tests that intermittently fail
   - Fix timing dependencies, resource leaks, or ordering assumptions
   - Add proper mocking where external services are called

3. Expand coverage:
   - Write tests for untested edge cases and error paths
   - Add property-based tests (Hypothesis) where appropriate
   - Ensure new code from today's shifts has test coverage""",
        "output_suffix": "quality",
    },
    {
        "block": 7,
        "hour": 11,
        "minute": 0,
        "slug": "memory_hygiene",
        "title": "Memory & Knowledge Hygiene",
        "focus": "Memory",
        "instructions": """\
1. Memory consolidation:
   - Scan MEMORY/workflow_learnings/ for duplicates or stale entries
   - Merge overlapping learnings
   - Archive superseded entries to MEMORY/_archive/

2. Knowledge graph health:
   - Query Fusion Memory for orphaned or disconnected nodes
   - Strengthen relationships between related memories
   - Ensure recent decisions have proper context links

3. Vault sync:
   - Check Nova Vault for any items that should be reflected in memory
   - Update any stale vault entries
   - Ensure cross-system consistency""",
        "output_suffix": "memory",
    },
    {
        "block": 8,
        "hour": 11,
        "minute": 45,
        "slug": "session_wrap",
        "title": "Session Wrap & Tomorrow Prep",
        "focus": "Wrap-up",
        "instructions": """\
1. Summarize today's work:
   - Review all OUTPUT/shift_* files from today
   - List key accomplishments, decisions, and discoveries
   - Note any items that need follow-up tomorrow

2. Create session checkpoint:
   - Store checkpoint in Fusion Memory with:
     - open_threads: unfinished work items
     - next_actions: prioritized list for tomorrow
     - key_decisions: important choices made today

3. Tomorrow's schedule:
   - Shifts are generated automatically 30 minutes before each shift starts
   - Morning shift: systemd timer at 6:00 AM CT creates blocks 1-8
   - Afternoon shift: systemd timer at 2:00 PM CT creates blocks 9-16
   - Do NOT pre-generate tomorrow's shifts — the scheduler optimizes best with fresh context""",
        "output_suffix": "wrap",
    },
]

# ── Afternoon Shift Block Definitions (blocks 9-16) ────────────���─────────
AFTERNOON_SHIFT_BLOCKS: list[dict] = [
    {
        "block": 9,
        "hour": 14,
        "minute": 30,
        "slug": "novatrade_status",
        "title": "NovaTrade Status Check",
        "focus": "NovaTrade",
        "instructions": """\
1. Shadow mode health check:
   - Check if novacore-shadow.service is running: `systemctl status novacore-shadow`
   - Review shadow mode logs for errors or warnings
   - Check MetaApi connection status and feed health

2. Signal match rate:
   - Compare Python pipeline signals vs TV webhook signals from last period
   - Calculate match rate (target: 95%+)
   - Log any divergences with timestamps and details

3. Service and system health:
   - Verify novacore-novatrade.service is running
   - Check FTMO daily loss tracker state
   - Review morning shift NovaTrade outputs for continuity
   - Quick check on all NovaCore services (watcher, telegram)""",
        "output_suffix": "novatrade_status",
    },
    {
        "block": 10,
        "hour": 15,
        "minute": 15,
        "slug": "novatrade_impl",
        "title": "NovaTrade Implementation",
        "focus": "NovaTrade",
        "instructions": """\
1. Identify current sprint item:
   - Check NovaTrade Operational Sprint status (S1-S8)
   - Pick the highest-priority incomplete item
   - If sprint is complete, work on FTMO compliance gaps or strategy tuning

2. Implement:
   - Focus exclusively on NovaTrade code (novatrade/ directory)
   - Write clean, tested code following existing patterns
   - Priority order: FTMO compliance > execution reliability > strategy refinement
   - Ensure IRB champion strategy parameters are correctly wired

3. Validate:
   - Run NovaTrade tests: `python -m pytest tests/ -k novatrade -q --tb=short`
   - Verify no regressions in trade execution paths
   - Check that live stack builds correctly""",
        "output_suffix": "novatrade_impl",
    },
    {
        "block": 11,
        "hour": 16,
        "minute": 0,
        "slug": "novatrade_impl2",
        "title": "NovaTrade Implementation (Continued)",
        "focus": "NovaTrade",
        "instructions": """\
1. Continue NovaTrade implementation:
   - Continue work from block 10 if not yet complete
   - If previous work is done, pick next NovaTrade priority
   - Maintain focus — no context-switching to NovaCore infra

2. Test and commit:
   - Run full NovaTrade test suite
   - Ensure adequate test coverage for new code
   - Fix any test failures immediately
   - Commit clean, well-described changes

3. Shadow mode impact check:
   - If shadow mode is running, verify changes don't disrupt it
   - Check that any new code paths are shadow-mode compatible
   - Log any configuration changes that affect live behavior""",
        "output_suffix": "novatrade_impl2",
    },
    {
        "block": 12,
        "hour": 16,
        "minute": 45,
        "slug": "novatrade_testing",
        "title": "NovaTrade Testing & Validation",
        "focus": "NovaTrade",
        "instructions": """\
1. Full NovaTrade test suite:
   - Run: `python -m pytest tests/ -k novatrade --cov=novatrade --cov-report=term-missing -q`
   - Fix any failures from today's implementation blocks
   - Identify and fix flaky tests in trading paths

2. Shadow mode divergence analysis:
   - Review shadow mode signal log for any mismatches
   - For each divergence: identify root cause (timing, data, logic)
   - Prioritize fixes for divergences that would affect live trading

3. FTMO compliance validation:
   - Verify all 8 FTMO compliance checks pass
   - Test daily loss limit calculations against expected values
   - Validate weekend auto-close timing logic
   - Check lot-size consistency rules""",
        "output_suffix": "novatrade_testing",
    },
    {
        "block": 13,
        "hour": 17,
        "minute": 30,
        "slug": "novatrade_research",
        "title": "NovaTrade Research",
        "focus": "NovaTrade",
        "instructions": """\
1. Choose a NovaTrade-relevant research topic:
   - Market microstructure (spread behavior, liquidity windows, session overlaps)
   - FTMO rules and edge cases (challenge mechanics, scaling plan, rule changes)
   - IRB strategy refinement (parameter sensitivity, regime detection, filters)
   - Execution optimization (fill rates, slippage reduction, order timing)
   - Prop firm landscape (alternative firms, rule comparisons, best practices)

2. Research deeply:
   - Use web search for current FTMO rules and prop firm community insights
   - Analyze trade-offs specific to funded trading
   - Look for real-world prop firm trader experiences and common pitfalls

3. Document and store findings:
   - Store key insights in Fusion Memory with category: research, project: novatrade
   - Write a concise summary with actionable recommendations
   - Flag any findings that require code changes or strategy adjustments""",
        "output_suffix": "novatrade_research",
    },
    {
        "block": 14,
        "hour": 18,
        "minute": 15,
        "slug": "novatrade_monitoring",
        "title": "NovaTrade Monitoring & Analysis",
        "focus": "NovaTrade",
        "instructions": """\
1. Shadow signal analysis:
   - Review all shadow mode signals generated today
   - Compare entry/exit timing, direction, and sizing
   - Calculate running match rate and trend (improving or degrading?)

2. Feed health review:
   - Check MetaApi connection uptime for the day
   - Review feed health supervisor false-positive rate
   - Verify price data quality (gaps, stale quotes, anomalies)

3. Daily P&L and risk tracking:
   - Check FTMO daily loss tracker values against FTMO dashboard
   - Verify position sizing stays within risk limits (1% per trade)
   - Review any trades that approached daily loss threshold
   - Log daily monitoring summary to Fusion Memory

4. Cutover readiness assessment:
   - Update running tally: days of shadow validation completed
   - Note any issues that would block cutover
   - If 7 days complete with 95%+ match: flag ready for S8""",
        "output_suffix": "novatrade_monitoring",
    },
    {
        "block": 15,
        "hour": 19,
        "minute": 0,
        "slug": "free_will_pm",
        "title": "Free Will / Exploration",
        "focus": "Autonomy",
        "instructions": """\
1. Assess current state:
   - Look at the codebase, services, memory, and recent outputs
   - Consider what matters most right now based on current conditions
   - Trust your judgment — you have full context on the system

2. Act on your assessment:
   - Fix something that has been bothering you
   - Improve something you've noticed is suboptimal
   - Explore an idea you think has potential
   - Refactor code that could be cleaner
   - Build something new that would help

3. Document your choice:
   - Explain why you chose this particular action
   - Record the rationale in Fusion Memory""",
        "output_suffix": "free_will_pm",
    },
    {
        "block": 16,
        "hour": 19,
        "minute": 45,
        "slug": "evening_wrap",
        "title": "Evening Wrap & Tomorrow Prep",
        "focus": "Wrap-up",
        "instructions": """\
1. Summarize ALL of today's work (both shifts):
   - Review all OUTPUT/shift_* files from today (blocks 1-16)
   - List key accomplishments, decisions, and discoveries
   - Note any items that need follow-up tomorrow

2. Create comprehensive session checkpoint:
   - Store checkpoint in Fusion Memory with:
     - open_threads: all unfinished work items from both shifts
     - next_actions: prioritized list for tomorrow
     - key_decisions: important choices made today
   - This checkpoint covers the full day, not just the afternoon

3. Tomorrow's schedule:
   - Shifts are generated automatically 30 minutes before each shift starts
   - Morning shift: systemd timer at 6:00 AM CT creates blocks 1-8
   - Afternoon shift: systemd timer at 2:00 PM CT creates blocks 9-16
   - Do NOT pre-generate tomorrow's shifts — the scheduler optimizes best with fresh context

4. Send summary to operator:
   - Compile a concise day summary for Telegram
   - Include: what was done, what's pending, what's next""",
        "output_suffix": "evening_wrap",
    },
]

# Keep SHIFT_BLOCKS as alias for backward compatibility (all blocks combined)
SHIFT_BLOCKS = MORNING_SHIFT_BLOCKS

# ── Generator Tasks (self-perpetuation) ───────────────────────────────────
# Morning safety net: fires at 6:15 AM CT (15 min before shift)
MORNING_GENERATOR_HOUR = 6
MORNING_GENERATOR_MINUTE = 15

# Afternoon safety net: fires at 14:15 CT (before 2:30 PM start)
AFTERNOON_GENERATOR_HOUR = 14
AFTERNOON_GENERATOR_MINUTE = 15

MORNING_GENERATOR_TEMPLATE = """\
---
scheduled_at: {scheduled_at}
priority: critical
type: schedule_generator
shift_date: {shift_date}
shift_name: morning
---

# Morning Schedule Generator (Safety Net)

Generate all morning shift blocks (1-8):

```bash
python3 scripts/daily_shift_generator.py --today --shift morning
```

This task is a safety net — the systemd timer normally generates the full
morning shift 30 minutes before it starts. This only fires if the systemd
timer missed. Generates all morning blocks in one batch.
"""

AFTERNOON_GENERATOR_TEMPLATE = """\
---
scheduled_at: {scheduled_at}
priority: critical
type: schedule_generator
shift_date: {shift_date}
shift_name: afternoon
---

# Afternoon Schedule Generator (Safety Net)

Generate all afternoon shift blocks (9-16):

```bash
python3 scripts/daily_shift_generator.py --today --shift afternoon
```

This task is a safety net — the systemd timer normally generates the full
afternoon shift 30 minutes before it starts. This only fires if the systemd
timer missed. Generates all afternoon blocks in one batch.
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    """Log to stdout and LOGS/shift_generator.log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [shift-gen] {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "shift_generator.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── Scheduler helpers ────────────────────────────────────────────────────────

# Slug -> WorkMode mapping for shift blocks
_SLUG_WORK_MODE: dict[str, str] = {
    "system_health": "MONITORING",
    "monitoring": "MONITORING",
    "novatrade_status": "MONITORING",
    "novatrade_monitoring": "MONITORING",
    "research": "RESEARCH",
    "deep_research": "RESEARCH",
    "novatrade_research": "RESEARCH",
    "implementation": "DEEP_IMPLEMENTATION",
    "novatrade_impl": "DEEP_IMPLEMENTATION",
    "novatrade_impl2": "DEEP_IMPLEMENTATION",
    "code": "DEEP_IMPLEMENTATION",
    "review": "REVIEW",
    "testing": "REVIEW",
    "testing_quality": "REVIEW",
    "novatrade_testing": "REVIEW",
    "planning": "ANALYSIS",
    "strategy": "ANALYSIS",
    "free_will": "ANALYSIS",
    "free_will_pm": "ANALYSIS",
    "novatrade_progress": "ANALYSIS",
    "communication": "COMMUNICATION",
    "session_wrap": "COMMUNICATION",
    "evening_wrap": "COMMUNICATION",
    "deployment": "DEPLOYMENT",
    "admin": "ADMIN",
    "memory_hygiene": "ADMIN",
}


def _convert_block_to_work_unit(block: dict, shift_date: date) -> WorkUnit:
    """Convert a shift block dict into a scheduler WorkUnit."""
    slug = block["slug"]
    block_num = block["block"]
    wu_id = f"shift_{shift_date}_{block_num}_{slug}"

    mode_name = _SLUG_WORK_MODE.get(slug, "ANALYSIS")
    work_mode = WorkMode[mode_name]

    # Build scheduled_at datetime
    scheduled_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        block["hour"],
        block["minute"],
        tzinfo=CT,
    )

    return WorkUnit(
        id=wu_id,
        title=block.get("title", slug),
        done_condition=block.get("instructions", "")[:200],
        work_mode=work_mode,
        task_class=TaskClass.BOUNDED,
        commitment_level=CommitmentLevel.HARD,
        priority="high",
        urgency=0.7,
        strategic_value=0.6,
        estimated_optimistic_min=20.0,
        estimated_expected_min=35.0,
        estimated_pessimistic_min=50.0,
        confidence=0.7,
        must_do_today=True,
        schedulable_now=True,
        hard_deadline=scheduled_dt + timedelta(minutes=45),
    )


def _optimize_blocks_with_scheduler(
    blocks: list[dict],
    shift_date: date,
) -> tuple[list[dict], bool]:
    """Use the dynamic scheduler to optimize block ordering and timing.

    Returns (possibly-reordered blocks, was_optimized).
    """
    if not _HAS_SCHEDULER:
        return blocks, False
    try:
        config = load_scheduler_config()
        if not config.enabled:
            return blocks, False

        # Convert to WorkUnits
        work_units = [_convert_block_to_work_unit(b, shift_date) for b in blocks]

        # Calculate total shift duration
        if not blocks:
            return blocks, False
        block_duration = len(blocks) * 45.0  # ~45 min per block

        if config.shadow_mode:
            # Shadow mode: log comparison but don't change order
            comparison = build_shadow_comparison(
                work_units,
                block_duration_min=block_duration,
                config=config,
            )
            log(f"[scheduler-shadow] {_json.dumps(comparison, indent=2)}")
            return blocks, False

        # Run full pipeline
        result: SchedulerResult = build_block_plan(
            work_units,
            block_duration_min=block_duration,
            config=config,
        )

        if not result.block_plan.scheduled_slots:
            return blocks, False

        # Reorder blocks to match scheduler's optimal sequence
        slot_order = {slot.scored_task.work_unit.id: i for i, slot in enumerate(result.block_plan.scheduled_slots)}

        def _block_sort_key(b: dict) -> int:
            wu_id = f"shift_{shift_date}_{b['block']}_{b['slug']}"
            return slot_order.get(wu_id, b["block"])

        optimized = sorted(blocks, key=_block_sort_key)

        # Log scheduler diagnostics
        log(
            f"[scheduler] Optimized {len(blocks)} blocks: "
            f"scheduled={result.total_tasks_scheduled}, "
            f"deferred={result.total_tasks_deferred}, "
            f"variant={result.variant_used}"
        )
        if result.starvation_alerts:
            log(f"[scheduler] Starvation alerts: {len(result.starvation_alerts)}")
        if result.epic_warnings:
            log(f"[scheduler] Epic warnings: {result.epic_warnings}")

        # Persist BlockPlan for downstream consumers (watcher, heartbeat)
        try:
            plan_path = ROOT / "STATE" / "scheduler" / "current_block_plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_data = {
                "generated_at": datetime.now().isoformat(),
                "shift_date": str(shift_date),
                "block_plan": result.block_plan.model_dump(mode="json") if result.block_plan else None,
                "calibration_applied": result.calibration_applied,
                "starvation_alerts": result.starvation_alerts,
                "total_blocks": len(optimized),
            }
            plan_path.write_text(_json.dumps(plan_data, indent=2, default=str))
            log(f"[scheduler] Persisted BlockPlan to {plan_path} ({len(optimized)} blocks)")
        except Exception as e:
            log(f"[scheduler] Failed to persist BlockPlan: {e}")

        # Persist initial BlockState for the watcher's replanner
        try:
            block_id = f"shift_{shift_date:%Y%m%d}"
            state_dir = ROOT / "STATE" / "scheduler"
            # Use time-sorted order for start/end (not priority-sorted)
            earliest = min(optimized, key=lambda b: (b["hour"], b["minute"]))
            latest = max(optimized, key=lambda b: (b["hour"], b["minute"]))
            block_start = datetime(
                shift_date.year,
                shift_date.month,
                shift_date.day,
                earliest["hour"],
                earliest["minute"],
                tzinfo=CT,
            ).astimezone(timezone.utc)
            block_end = datetime(
                shift_date.year,
                shift_date.month,
                shift_date.day,
                latest["hour"],
                latest["minute"],
                tzinfo=CT,
            ).astimezone(timezone.utc) + timedelta(minutes=45)

            # In batch mode (morning + afternoon), merge into existing state
            from utils.scheduler.replanner import load_block_state

            existing = load_block_state(block_id, state_dir=state_dir)
            if existing and (existing.completed_task_ids or existing.in_progress_task_id):
                # Active shift — don't overwrite progress
                log(f"[scheduler] BlockState '{block_id}' already active, skipping overwrite")
            elif existing:
                # Extend existing plan (batch mode: morning created first, afternoon extends)
                merged_slots = list(existing.original_plan.scheduled_slots)
                if result.block_plan:
                    merged_slots.extend(result.block_plan.scheduled_slots)
                from utils.scheduler.block import BlockPlan

                merged_plan = BlockPlan(
                    scheduled_slots=merged_slots,
                    deferred_tasks=list(existing.original_plan.deferred_tasks)
                    + (list(result.block_plan.deferred_tasks) if result.block_plan else []),
                )
                existing.original_plan = merged_plan
                existing.block_start_utc = min(existing.block_start_utc, block_start)
                existing.block_end_utc = max(existing.block_end_utc, block_end)
                save_block_state(existing, state_dir=state_dir)
                log(f"[scheduler] Merged afternoon into BlockState '{block_id}'")
            else:
                initial_state = BlockState(
                    block_id=block_id,
                    original_plan=result.block_plan,
                    block_start_utc=block_start,
                    block_end_utc=block_end,
                )
                save_block_state(initial_state, state_dir=state_dir)
                log(f"[scheduler] Persisted initial BlockState '{block_id}' to {state_dir}")
        except Exception as e:
            log(f"[scheduler] Failed to persist BlockState: {e}")

        return optimized, True
    except Exception as exc:
        log(f"[scheduler] Optimization failed (using default order): {exc}")
        return blocks, False


def ct_to_server_naive(ct_dt: datetime) -> datetime:
    """Convert a Central-Time-aware datetime to a naive datetime in server local time (UTC).

    The watcher compares ``scheduled_at`` against ``datetime.now()`` (naive, server-local).
    The server is UTC, so we convert CT -> UTC and strip tzinfo.
    """
    utc_dt = ct_dt.astimezone(SERVER_TZ)
    return utc_dt.replace(tzinfo=None)


def ct_time_str(hour: int, minute: int) -> str:
    """Format hour:minute as a human-readable CT time string."""
    period = "AM" if hour < 12 else "PM"
    display_hour = hour if hour <= 12 else hour - 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minute:02d} {period} CT"


def next_business_day(from_date: date) -> date:
    """Return the next business day (Mon-Fri) after from_date.

    If from_date is Friday, returns Monday. Saturday returns Monday.
    Sunday returns Monday.  Otherwise returns from_date + 1 day.
    """
    d = from_date + timedelta(days=1)
    # 5 = Saturday, 6 = Sunday
    while d.weekday() in (5, 6):
        d += timedelta(days=1)
    return d


def get_yesterday_context() -> str:
    """Read recent OUTPUT/ files to build context from yesterday's work."""
    if not OUTPUT.exists():
        return "No previous output files found."

    now_utc = datetime.now(timezone.utc)
    yesterday_start = now_utc - timedelta(hours=36)  # generous window

    recent_outputs: list[tuple[float, Path]] = []
    for p in OUTPUT.iterdir():
        if p.suffix != ".md":
            continue
        try:
            mtime = p.stat().st_mtime
            if mtime >= yesterday_start.timestamp():
                recent_outputs.append((mtime, p))
        except OSError:
            continue

    if not recent_outputs:
        return "No recent output files found in the last 36 hours."

    # Sort newest first, take top 15
    recent_outputs.sort(key=lambda x: x[0], reverse=True)
    recent_outputs = recent_outputs[:15]

    lines = []
    for mtime, p in recent_outputs:
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        ts_str = dt.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")
        # Extract first meaningful line from the file as a summary
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            summary = _extract_file_summary(text, p.name)
            lines.append(f"- **{p.name}** ({ts_str}): {summary}")
        except OSError:
            lines.append(f"- **{p.name}** ({ts_str})")

    return "\n".join(lines)


def get_morning_context(shift_date: date) -> str:
    """Read today's morning OUTPUT/ files to build context for afternoon shift."""
    if not OUTPUT.exists():
        return "No morning output files found."

    date_str = shift_date.strftime("%Y%m%d")
    morning_outputs: list[tuple[int, Path]] = []

    for p in OUTPUT.iterdir():
        if not p.name.startswith(f"shift_{date_str}_"):
            continue
        if p.suffix != ".md":
            continue
        # Extract block number — morning blocks are 1-8
        m = re.match(rf"^shift_{date_str}_(\d+)_", p.name)
        if m:
            block_num = int(m.group(1))
            if 1 <= block_num <= 8:
                morning_outputs.append((block_num, p))

    if not morning_outputs:
        return "No morning shift output files found for today."

    morning_outputs.sort(key=lambda x: x[0])

    lines = []
    for block_num, p in morning_outputs:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            summary = _extract_file_summary(text, p.name)
            lines.append(f"- **Block {block_num}** ({p.name}): {summary}")
        except OSError:
            lines.append(f"- **Block {block_num}** ({p.name})")

    return "\n".join(lines)


def _extract_file_summary(text: str, filename: str) -> str:
    """Extract a one-line summary from an output file."""
    # Look for # heading
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 3:
            return stripped[2:].strip()[:120]
    # Fallback: first non-empty, non-frontmatter line
    in_frontmatter = False
    for line in text.splitlines()[:30]:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped and not stripped.startswith("#"):
            return stripped[:120]
    return "(see file)"


def cleanup_old_shifts(target_date: date | None = None) -> int:
    """Move completed shift task files to TASKS/archive/.

    Only moves .done files that match the shift_YYYYMMDD pattern.
    If target_date is given, only cleans shifts older than that date.
    Returns count of files moved.
    """
    TASKS_COMPLETED.mkdir(parents=True, exist_ok=True)
    moved = 0

    # Match shift_YYYYMMDD_N_slug.md.done and shift_gen_YYYYMMDD.md.done
    pattern = re.compile(r"^shift_(?:gen_(?:am|pm)_)?(\d{8})_")

    for p in TASKS.iterdir():
        if not p.name.endswith(".done"):
            continue
        m = pattern.match(p.name)
        if not m:
            # Also match legacy shift_gen_YYYYMMDD.md.done
            m = re.match(r"^shift_gen_(\d{8})\.", p.name)
            if not m:
                continue

        if target_date is not None:
            try:
                file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
                if file_date >= target_date:
                    continue  # don't clean today or future
            except ValueError:
                continue

        dest = TASKS_COMPLETED / p.name
        try:
            shutil.move(str(p), str(dest))
            moved += 1
        except OSError as e:
            log(f"Failed to move {p.name}: {e}")

    return moved


def build_shift_task(
    block: dict,
    shift_date: date,
    context: str,
    scheduler_optimized: bool = False,
) -> tuple[str, str]:
    """Build a single shift-block task file.

    Returns (filename, content).

    Args:
        block: Block definition dict.
        shift_date: Date for the shift.
        context: Context string from previous day or morning.
        scheduler_optimized: Whether the scheduler reordered this block.
    """
    n = block["block"]
    slug = block["slug"]
    filename = f"shift_{shift_date:%Y%m%d}_{n}_{slug}.md"

    # Build scheduled_at in server local time (naive, UTC)
    ct_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        block["hour"],
        block["minute"],
        tzinfo=CT,
    )
    server_dt = ct_to_server_naive(ct_dt)
    scheduled_at = server_dt.strftime("%Y-%m-%dT%H:%M:%S")

    time_str = ct_time_str(block["hour"], block["minute"])

    # Determine context section title based on shift
    if n <= 8:
        context_title = "Context from Previous Day"
    else:
        context_title = "Context from Morning Shift"

    # Build scheduler metadata lines for frontmatter
    sched_lines = ""
    if _HAS_SCHEDULER:
        mode_name = _SLUG_WORK_MODE.get(slug, "analysis")
        # Convert to the enum value format (lowercase with underscores)
        mode_value = mode_name.lower()
        sched_lines = (
            f"work_mode: {mode_value}\n"
            f"task_class: bounded\n"
            f"commitment_level: hard\n"
            f"scheduler_optimized: {str(scheduler_optimized).lower()}\n"
        )

    content = f"""\
---
scheduled_at: {scheduled_at}
priority: high
shift_block: {n}
shift_date: {shift_date}
type: shift_block
{sched_lines}---

# Shift Block {n}: {block["title"]}

**Date:** {shift_date} | **Time:** {time_str} | **Focus:** {block["focus"]}

## {context_title}

{context}

## Instructions

{block["instructions"]}

## Output

Write results to OUTPUT/shift_{shift_date:%Y%m%d}_{n}_{block["output_suffix"]}.md

## Required Contract (mandatory — your output will be rejected without this)

Your output report MUST end with a ## CONTRACT block containing ALL of these fields:

```
## CONTRACT
summary: <one-line description of what was done>
files_changed: <comma-separated list of files created or modified, or "none">
verification: <how correctness was confirmed — e.g. "tests passed", "service status verified">
confidence: <low | medium | high>
```

Rules:
- The ## CONTRACT heading must appear at the END of your output report
- Every field above is REQUIRED. Never omit a field
- If no files were changed, write: files_changed: none
- If verification was not possible, write: verification: not run
- Do not fabricate data. Use honest values
"""
    return filename, content


def build_generator_task(
    target_date: date,
    shift_name: str = "morning",
) -> tuple[str, str]:
    """Build the self-perpetuating generator task for the given date.

    Args:
        target_date: The date the generator task is for.
        shift_name: 'morning' or 'afternoon'.
    """
    if shift_name == "afternoon":
        filename = f"shift_gen_pm_{target_date:%Y%m%d}.md"
        gen_hour = AFTERNOON_GENERATOR_HOUR
        gen_minute = AFTERNOON_GENERATOR_MINUTE
        template = AFTERNOON_GENERATOR_TEMPLATE
    else:
        filename = f"shift_gen_am_{target_date:%Y%m%d}.md"
        gen_hour = MORNING_GENERATOR_HOUR
        gen_minute = MORNING_GENERATOR_MINUTE
        template = MORNING_GENERATOR_TEMPLATE

    ct_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        gen_hour,
        gen_minute,
        tzinfo=CT,
    )
    server_dt = ct_to_server_naive(ct_dt)
    scheduled_at = server_dt.strftime("%Y-%m-%dT%H:%M:%S")

    content = template.format(
        scheduled_at=scheduled_at,
        shift_date=target_date,
    )
    return filename, content


def build_telegram_summary(
    shift_date: date,
    files: list[str],
    shift_name: str = "both",
) -> str:
    """Build a Telegram notification summarizing the generated schedule."""
    ct_now = datetime.now(timezone.utc).astimezone(CT)
    day_name = shift_date.strftime("%A, %B %d")

    shift_label = {
        "morning": "Morning Shift",
        "afternoon": "Afternoon Shift",
        "both": "Full Day (Morning + Afternoon)",
    }.get(shift_name, shift_name)

    lines = [
        f"Shift Schedule — {day_name}",
        f"  [{shift_label}]",
        "",
    ]

    if shift_name in ("morning", "both"):
        if shift_name == "both":
            lines.append("  Morning (blocks 1-8):")
        for block in MORNING_SHIFT_BLOCKS:
            time_str = ct_time_str(block["hour"], block["minute"])
            lines.append(f"  {block['block']}. {time_str} — {block['title']}")
        lines.append(f"  + Morning safety net at {ct_time_str(MORNING_GENERATOR_HOUR, MORNING_GENERATOR_MINUTE)}")
        lines.append("")

    if shift_name in ("afternoon", "both"):
        if shift_name == "both":
            lines.append("  Afternoon (blocks 9-16):")
        for block in AFTERNOON_SHIFT_BLOCKS:
            time_str = ct_time_str(block["hour"], block["minute"])
            lines.append(f"  {block['block']}. {time_str} — {block['title']}")
        lines.append(f"  + Afternoon safety net at {ct_time_str(AFTERNOON_GENERATOR_HOUR, AFTERNOON_GENERATOR_MINUTE)}")
        lines.append("")

    lines.append(f"{len(files)} task files written to TASKS/")
    lines.append(f"Generated {ct_now.strftime('%H:%M CT')}")

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    """Send a message via Telegram using the same env vars as telegram_notifier.py."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        log("Telegram credentials not set — skipping notification")
        return

    try:
        import httpx
    except ImportError:
        log("httpx not installed — skipping Telegram notification")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Chunk if needed
    max_len = 3500
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < 500:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)

    try:
        with httpx.Client(timeout=25) as client:
            for chunk in chunks:
                payload = {"chat_id": chat_id, "text": chunk}
                r = client.post(url, json=payload)
                r.raise_for_status()
                time.sleep(0.25)
        log(f"Telegram notification sent ({len(chunks)} chunk(s))")
    except Exception as e:
        log(f"Telegram send failed: {e}")


def check_existing_shifts(  # noqa: C901
    shift_date: date,
    shift_name: str = "both",
) -> list[str]:
    """Check if shift tasks already exist for the given date.

    Args:
        shift_date: The date to check.
        shift_name: 'morning', 'afternoon', or 'both'.

    Returns list of existing filenames matching the requested shift.
    """
    date_str = shift_date.strftime("%Y%m%d")

    # Determine which block numbers belong to the requested shift
    if shift_name == "morning":
        valid_blocks = set(range(1, 9))  # 1-8
        gen_patterns = {f"shift_gen_am_{date_str}.md", f"shift_gen_{date_str}.md"}
    elif shift_name == "afternoon":
        valid_blocks = set(range(9, 17))  # 9-16
        gen_patterns = {f"shift_gen_pm_{date_str}.md"}
    else:  # both
        valid_blocks = set(range(1, 17))  # 1-16
        gen_patterns = {
            f"shift_gen_am_{date_str}.md",
            f"shift_gen_pm_{date_str}.md",
            f"shift_gen_{date_str}.md",
        }

    # Check all lifecycle states — not just .md — to prevent duplicate creation
    # when tasks are already dispatched (.inprogress), completed (.done), or failed (.failed).
    lifecycle_suffixes = (".md", ".md.inprogress", ".md.done", ".md.failed", ".md.cancelled")

    # Also expand gen_patterns to cover all lifecycle states
    gen_patterns_all = set()
    for pat in gen_patterns:
        for sfx in lifecycle_suffixes:
            gen_patterns_all.add(pat.replace(".md", sfx) if sfx != ".md" else pat)

    existing = []
    # Check TASKS/ and TASKS/archive/ to prevent re-creating archived shifts
    search_dirs = [TASKS]
    if TASKS_COMPLETED.exists():
        search_dirs.append(TASKS_COMPLETED)
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for p in search_dir.iterdir():
            if p.is_dir():
                continue
            # Check generator tasks across all lifecycle states
            if p.name in gen_patterns_all:
                existing.append(p.name)
                continue
            # Check shift block tasks across all lifecycle states
            # Strip lifecycle suffix to get the base name for regex matching
            fname = p.name
            for sfx in (".inprogress", ".done", ".failed", ".cancelled"):
                if fname.endswith(sfx):
                    fname = fname[: -len(sfx)]
                    break
            if not fname.endswith(".md"):
                continue
            m = re.match(rf"^shift_{date_str}_(\d+)_", fname)
            if m:
                block_num = int(m.group(1))
                if block_num in valid_blocks:
                    existing.append(p.name)

    return existing


def check_block_exists(shift_date: date, block_num: int, slug: str) -> bool:
    """Check if a specific shift block task already exists (any lifecycle state).

    Checks TASKS/ and TASKS/archive/ for the block file in any state.
    """
    date_str = shift_date.strftime("%Y%m%d")
    base = f"shift_{date_str}_{block_num}_{slug}.md"
    lifecycle_suffixes = ("", ".inprogress", ".done", ".failed", ".cancelled")

    search_dirs = [TASKS]
    if TASKS_COMPLETED.exists():
        search_dirs.append(TASKS_COMPLETED)

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for sfx in lifecycle_suffixes:
            if (search_dir / f"{base}{sfx}").exists():
                return True
    return False


# ── Main ─────────────────────────────────────────────────────────────────────


def generate_jit(
    shift_date: date,
    lead_minutes: int = 75,
    dry_run: bool = False,
) -> None:
    """Just-in-time shift generation: create only blocks due within lead_minutes.

    Instead of batch-generating all blocks at once, this creates each block
    individually ~1 hour before its scheduled time. Designed to be called
    frequently (every 30 min) by a systemd timer.

    Args:
        shift_date: The date to generate blocks for.
        lead_minutes: Only generate blocks starting within this many minutes.
        dry_run: If True, preview without writing files.
    """
    now_ct = datetime.now(timezone.utc).astimezone(CT)
    cutoff_ct = now_ct + timedelta(minutes=lead_minutes)

    log(
        f"JIT generation for {shift_date} (lead={lead_minutes}min, "
        f"now={now_ct:%H:%M CT}, cutoff={cutoff_ct:%H:%M CT}, dry_run={dry_run})"
    )

    # Lookahead guard
    today = now_ct.date()
    if (shift_date - today).days > MAX_LOOKAHEAD_DAYS:
        log(f"SKIPPED: target date {shift_date} beyond lookahead guard")
        return

    # Combine all blocks
    all_blocks = MORNING_SHIFT_BLOCKS + AFTERNOON_SHIFT_BLOCKS

    # Collect eligible blocks (within lead window and not already existing)
    eligible_blocks: list[dict] = []
    for block in all_blocks:
        block_ct = datetime(
            shift_date.year,
            shift_date.month,
            shift_date.day,
            block["hour"],
            block["minute"],
            tzinfo=CT,
        )
        if block_ct < now_ct or block_ct > cutoff_ct:
            continue
        if check_block_exists(shift_date, block["block"], block["slug"]):
            continue
        eligible_blocks.append(block)

    # Run scheduler optimization on eligible blocks
    optimized_blocks, was_optimized = _optimize_blocks_with_scheduler(eligible_blocks, shift_date)

    # Build task files from (potentially reordered) blocks
    files_to_write: list[tuple[str, str]] = []
    yesterday_context: str | None = None
    morning_context: str | None = None

    for block in optimized_blocks:
        # Lazy-load context (only fetched when actually needed)
        if block["block"] <= 8:
            if yesterday_context is None:
                yesterday_context = get_yesterday_context()
            context = yesterday_context
        else:
            if morning_context is None:
                morning_context = get_morning_context(shift_date)
            context = morning_context

        filename, content = build_shift_task(block, shift_date, context, scheduler_optimized=was_optimized)
        files_to_write.append((filename, content))

    if not files_to_write:
        log("JIT: no blocks due within lead window (or all already exist)")
        return

    if dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — JIT blocks for {shift_date}")
        print(f"{'=' * 60}\n")
        for filename, _content in files_to_write:
            print(f"  Would create: {filename}")
        print(f"\nTotal: {len(files_to_write)} files")
        return

    # Write files
    TASKS.mkdir(parents=True, exist_ok=True)
    written_files: list[str] = []

    for filename, content in files_to_write:
        path = TASKS / filename
        path.write_text(content, encoding="utf-8")
        written_files.append(filename)
        log(f"  JIT written: {filename}")

    log(f"JIT generated {len(written_files)} block(s) for {shift_date}")

    # Clean up old completed shift tasks
    moved = cleanup_old_shifts(target_date=shift_date)
    if moved:
        log(f"Archived {moved} completed shift task(s)")

    # Send a brief Telegram notification (only for first block of a shift)
    if written_files:
        ct_now_str = now_ct.strftime("%H:%M CT")
        msg = (
            f"JIT shift block(s) created: {', '.join(written_files)}\n"
            f"Due within {lead_minutes}min (generated {ct_now_str})"
        )
        send_telegram(msg)


def generate_schedule(  # noqa: C901
    shift_date: date,
    dry_run: bool = False,
    shift_name: str = "both",
) -> None:
    """Generate the shift schedule for the given date (batch mode).

    Creates all blocks for the requested shift at once. Use --jit mode
    for just-in-time generation (one block at a time, ~1 hour before).

    Args:
        shift_date: The date to generate the schedule for.
        dry_run: If True, preview without writing files.
        shift_name: 'morning', 'afternoon', or 'both'.
    """
    log(f"Generating {shift_name} shift schedule for {shift_date} (dry_run={dry_run})")

    # Lookahead guard: prevent cascade of tasks far into the future
    today = datetime.now(timezone.utc).astimezone(CT).date()
    if (shift_date - today).days > MAX_LOOKAHEAD_DAYS:
        log(
            f"SKIPPED: target date {shift_date} is more than {MAX_LOOKAHEAD_DAYS} days "
            f"ahead of today ({today}). Cascade guard prevented generation."
        )
        return

    # Check for existing shifts (per-shift, not global)
    existing = check_existing_shifts(shift_date, shift_name=shift_name)
    if existing:
        log(f"Found {len(existing)} existing {shift_name} shift files for {shift_date}:")
        for f in sorted(existing):
            log(f"  - {f}")
        if not dry_run:
            log(f"Skipping {shift_name} generation — shifts already exist. Delete existing files first to regenerate.")
            return

    # Gather context
    yesterday_context = get_yesterday_context()

    # Build all task files
    files_to_write: list[tuple[str, str]] = []

    # Morning blocks (with scheduler optimization)
    if shift_name in ("morning", "both"):
        morning_blocks, morning_optimized = _optimize_blocks_with_scheduler(
            list(MORNING_SHIFT_BLOCKS),
            shift_date,
        )
        for block in morning_blocks:
            filename, content = build_shift_task(
                block,
                shift_date,
                yesterday_context,
                scheduler_optimized=morning_optimized,
            )
            files_to_write.append((filename, content))

    # Afternoon blocks (with scheduler optimization)
    if shift_name in ("afternoon", "both"):
        morning_context = get_morning_context(shift_date)
        afternoon_blocks, afternoon_optimized = _optimize_blocks_with_scheduler(
            list(AFTERNOON_SHIFT_BLOCKS),
            shift_date,
        )
        for block in afternoon_blocks:
            filename, content = build_shift_task(
                block,
                shift_date,
                morning_context,
                scheduler_optimized=afternoon_optimized,
            )
            files_to_write.append((filename, content))

    # Build generator safety-net tasks
    if shift_name in ("morning", "both"):
        # Morning safety net for NEXT business day
        next_day = next_business_day(shift_date)
        gen_filename, gen_content = build_generator_task(next_day, shift_name="morning")
        gen_path = TASKS / gen_filename
        # Also check for legacy filename
        legacy_gen_path = TASKS / f"shift_gen_{next_day:%Y%m%d}.md"
        if gen_path.exists() or legacy_gen_path.exists():
            log(f"Morning generator task already exists for {next_day} — skipping to prevent cascade")
        else:
            files_to_write.append((gen_filename, gen_content))

    if shift_name in ("afternoon", "both"):
        # Afternoon safety net for the SAME day (fires at 14:15, before 14:30 start)
        # If generating for today, the safety net is for today
        # If generating for tomorrow, the safety net is for that day
        gen_filename, gen_content = build_generator_task(shift_date, shift_name="afternoon")
        gen_path = TASKS / gen_filename
        if gen_path.exists():
            log(f"Afternoon generator task already exists for {shift_date} — skipping to prevent cascade")
        else:
            files_to_write.append((gen_filename, gen_content))

    if dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — {shift_name.title()} Schedule for {shift_date}")
        print(f"{'=' * 60}\n")
        for filename, content in files_to_write:
            print(f"--- {filename} ---")
            # Show just the frontmatter and title
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if i > 0 and line.startswith("# "):
                    print(line)
                    break
                print(line)
            print(f"  (... {len(content)} chars total)\n")
        print(f"Total: {len(files_to_write)} files")
        return

    # Write files
    TASKS.mkdir(parents=True, exist_ok=True)
    written_files: list[str] = []

    for filename, content in files_to_write:
        path = TASKS / filename
        path.write_text(content, encoding="utf-8")
        written_files.append(filename)
        log(f"  Written: {filename}")

    log(f"Generated {len(written_files)} {shift_name} shift files for {shift_date}")

    # Clean up old completed shift tasks
    moved = cleanup_old_shifts(target_date=shift_date)
    if moved:
        log(f"Archived {moved} completed shift task(s) to TASKS/_completed/")

    # Send Telegram summary
    summary = build_telegram_summary(shift_date, written_files, shift_name=shift_name)
    send_telegram(summary)

    log(f"{shift_name.title()} schedule generation complete")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate daily shift schedule task files for NovaCore watcher.",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="Generate schedule for today instead of tomorrow",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be generated without writing files",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Generate for a specific date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--shift",
        type=str,
        choices=["morning", "afternoon", "both"],
        default="both",
        help="Which shift to generate: morning (blocks 1-8), afternoon (blocks 9-16), or both (default: both)",
    )
    parser.add_argument(
        "--jit",
        action="store_true",
        help="Just-in-time mode: only generate blocks due within --lead-minutes (default 75). "
        "Designed to be called frequently (every 30 min) by a systemd timer.",
    )
    parser.add_argument(
        "--lead-minutes",
        type=int,
        default=75,
        help="Lead time in minutes for JIT mode (default: 75). "
        "Blocks are generated this many minutes before their scheduled time.",
    )
    args = parser.parse_args()

    if args.date:
        try:
            shift_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {args.date} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)
    elif args.today or args.jit:
        # JIT always operates on today
        ct_now = datetime.now(timezone.utc).astimezone(CT)
        shift_date = ct_now.date()
    else:
        # Next business day from today (Central Time)
        ct_now = datetime.now(timezone.utc).astimezone(CT)
        shift_date = next_business_day(ct_now.date())

    if args.jit:
        generate_jit(shift_date, lead_minutes=args.lead_minutes, dry_run=args.dry_run)
    else:
        generate_schedule(shift_date, dry_run=args.dry_run, shift_name=args.shift)


if __name__ == "__main__":
    main()
