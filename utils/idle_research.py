"""Idle Research Injection — trigger low-priority research during CRUISE mode.

When the system enters CRUISE mode (stable GREEN periods), instead of
doing nothing, inject low-priority research tasks from the TASKS/ backlog.
This prevents complete idle periods and keeps the system learning.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("novatrade.utils.idle_research")

TASKS_DIR = Path("/home/nova/nova-core/TASKS")
RESEARCH_TASK_PREFIX = "idle_research"
MAX_IDLE_TASKS_PER_CRUISE = 2  # Limit to prevent spam


def get_next_idle_research_task() -> str | None:
    """Find the next suitable task for idle research injection.

    Returns the task stem (without .md extension) if found, None otherwise.
    Looks for:
    1. Existing tasks marked as low-priority research
    2. Backlog items that could benefit from research
    3. System improvement investigations
    """
    try:
        if not TASKS_DIR.exists():
            return None

        # Look for existing low-priority tasks first
        for task_file in TASKS_DIR.glob("*.md"):
            if task_file.name.startswith("research_") or "investigation" in task_file.name.lower():
                return task_file.stem

        # Look for tasks with "research" or "analyze" in the filename
        for task_file in TASKS_DIR.glob("*.md"):
            name_lower = task_file.name.lower()
            if any(keyword in name_lower for keyword in ["research", "analyze", "study", "investigate"]):
                return task_file.stem

        # No suitable tasks found
        return None

    except Exception as e:
        log.error(f"Failed to find idle research task: {e}")
        return None


def create_idle_research_task(topic: str, description: str) -> str:
    """Create a new idle research task file.

    Args:
        topic: Short topic name (e.g., "memory_optimization")
        description: Detailed task description

    Returns:
        The task stem for the created task
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    task_stem = f"{RESEARCH_TASK_PREFIX}_{topic}_{timestamp}"
    task_path = TASKS_DIR / f"{task_stem}.md"

    task_content = f"""---
priority: low
type: research
created_by: idle_research_injection
created_at: {datetime.now(timezone.utc).isoformat()}
---

# Idle Research: {topic.replace("_", " ").title()}

{description}

## Instructions

This is a low-priority research task created during CRUISE mode idle time.
Complete when convenient, no urgency.

## Expected Output

Write findings to OUTPUT/{task_stem}_<timestamp>.md
"""

    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        task_path.write_text(task_content)
        log.info(f"Created idle research task: {task_stem}")
        return task_stem
    except Exception as e:
        log.error(f"Failed to create idle research task {task_stem}: {e}")
        raise


def should_inject_research() -> bool:
    """Check if we should inject a research task during CRUISE mode.

    Returns False if:
    - Too many idle research tasks already exist
    - Recent injection within last hour
    """
    try:
        # Count existing idle research tasks
        idle_count = len(list(TASKS_DIR.glob(f"{RESEARCH_TASK_PREFIX}_*.md")))
        if idle_count >= MAX_IDLE_TASKS_PER_CRUISE:
            log.debug(f"Skipping idle research: {idle_count} tasks already exist")
            return False

        # Could add time-based cooldown here if needed
        return True

    except Exception as e:
        log.error(f"Failed to check research injection eligibility: {e}")
        return False


def inject_system_improvement_research() -> str | None:
    """Inject a research task focused on system improvements.

    Returns the task stem if created, None if skipped.
    """
    if not should_inject_research():
        return None

    # Define potential research topics for system improvement
    research_topics = [
        ("memory_optimization", "Research memory usage patterns and optimization opportunities in NovaCore"),
        ("performance_analysis", "Analyze performance bottlenecks in the trading pipeline and heartbeat cycle"),
        ("error_pattern_analysis", "Study recent error logs to identify recurring failure patterns"),
        ("cost_optimization", "Research LLM cost patterns and identify optimization opportunities"),
        ("strategy_performance", "Deep analysis of IRB strategy performance and potential improvements"),
        ("infrastructure_health", "Research infrastructure reliability patterns and failure modes"),
        ("automation_gaps", "Identify manual processes that could be automated"),
        ("security_audit", "Research security posture and identify potential vulnerabilities"),
    ]

    # For now, pick the first topic. Could be made more intelligent later.
    topic, description = research_topics[0]

    try:
        return create_idle_research_task(topic, description)
    except Exception as e:
        log.error(f"Failed to inject research task: {e}")
        return None


def log_injection_attempt(task_stem: str | None, cruise_cycle: int) -> None:
    """Log the research injection attempt for debugging."""
    if task_stem:
        log.info(f"Idle research injected: {task_stem} (CRUISE cycle {cruise_cycle})")
    else:
        log.debug(f"No idle research injected (CRUISE cycle {cruise_cycle})")
