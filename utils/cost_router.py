"""Cost-aware task routing for NovaCore.

Phase 4.4: Deterministic, auditable routing rules that select the
appropriate model based on task complexity, budget state, and priority.

Scoring is rule-based and transparent — every routing decision is logged
with the reasons that determined it.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE = Path("/home/nova/nova-core")
STATE_DIR = BASE / "STATE"
BUDGETS_DIR = STATE_DIR / "budgets"
HEARTBEAT_CONFIG_FILE = STATE_DIR / "heartbeat_config.json"

# Budget thresholds (percentage of daily limits)
# Raised from 75/80/90 → 85/95/98 to preserve autonomous research/planning
# quality. The P0-P3 risk management stack was built entirely by autonomous
# research→plan→implement cycles; aggressive throttling kills that capability.
BUDGET_WARN_PCT = 85.0
BUDGET_DOWNGRADE_PCT = 95.0
BUDGET_CRITICAL_PCT = 98.0

# Daily cost alert threshold (USD)
DAILY_COST_ALERT_USD = 20.0


# ---------------------------------------------------------------------------
# Complexity tiers
# ---------------------------------------------------------------------------


class TaskComplexity(enum.IntEnum):
    """Task complexity tiers. Higher value = more complex."""

    TRIVIAL = 0  # format fixes, contract repairs, simple queries
    SIMPLE = 1  # single-file edits, quick lookups, simple research
    MODERATE = 2  # multi-file changes, analysis, code review
    COMPLEX = 3  # architecture, multi-step implementation, deep research


# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

MODEL_TIERS: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-20250514",
    "haiku": "claude-haiku-3-20250307",
}

DEFAULT_MODEL = MODEL_TIERS["opus"]

# Task class → default complexity mapping
TASK_CLASS_COMPLEXITY: dict[str, TaskComplexity] = {
    "simple": TaskComplexity.TRIVIAL,
    "code_review": TaskComplexity.MODERATE,
    "research": TaskComplexity.MODERATE,
    "code_impl": TaskComplexity.COMPLEX,
    "system": TaskComplexity.COMPLEX,
    "unknown": TaskComplexity.MODERATE,
}

# Complexity → default model
COMPLEXITY_MODEL_MAP: dict[TaskComplexity, str] = {
    TaskComplexity.TRIVIAL: MODEL_TIERS["sonnet"],
    TaskComplexity.SIMPLE: MODEL_TIERS["sonnet"],
    TaskComplexity.MODERATE: MODEL_TIERS["opus"],
    TaskComplexity.COMPLEX: MODEL_TIERS["opus"],
}


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


@dataclass
class RoutingDecision:
    """Immutable record of a routing decision with full audit trail."""

    model: str
    complexity: TaskComplexity
    reasons: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    budget_utilization_pct: float = 0.0
    downgraded: bool = False
    lane: str = ""  # Phase 5: execution lane (light/medium/heavy)


# ---------------------------------------------------------------------------
# Complexity signal patterns
# ---------------------------------------------------------------------------

_COMPLEX_SIGNALS = [
    re.compile(r"\bmulti[- ]?(?:step|phase|file|agent|stage)\b", re.I),
    re.compile(r"\barchitect\w*\b", re.I),
    re.compile(r"\brefactor\b", re.I),
    re.compile(r"\bintegrat\w+\b", re.I),
    re.compile(r"\bpipeline\b", re.I),
    re.compile(r"\bdeep\s+(?:research|analysis|dive)\b", re.I),
    re.compile(r"\bcomprehensive\b", re.I),
    re.compile(r"\bproduction[- ]grade\b", re.I),
]

_SIMPLE_SIGNALS = [
    re.compile(r"\bformat\s+(?:fix|change)\b", re.I),
    re.compile(r"\brename\b", re.I),
    re.compile(r"\btypo\b", re.I),
    re.compile(r"\bcontract\s+repair\b", re.I),
    re.compile(r"\b__retry\d*\b", re.I),
    re.compile(r"\bquick\b", re.I),
    re.compile(r"\btrivial\b", re.I),
]


# ---------------------------------------------------------------------------
# Complexity scoring
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Estimate input tokens from text length (chars / 4 heuristic)."""
    return max(1, len(text) // 4)


def score_complexity(
    task_text: str,
    task_class: str = "unknown",
    priority: str = "normal",
) -> TaskComplexity:
    """Score task complexity using deterministic heuristics.

    Inputs are task content, classification from task_classifier, and
    priority from frontmatter. Returns a TaskComplexity tier.

    The scoring is deliberately transparent and auditable:
    1. Start with class-based default
    2. Adjust based on text signal matches
    3. Factor in size (word count)
    4. Respect priority constraints
    """
    # Start with class-based default
    base = TASK_CLASS_COMPLEXITY.get(task_class, TaskComplexity.MODERATE)

    # Count signal matches
    complex_hits = sum(1 for p in _COMPLEX_SIGNALS if p.search(task_text))
    simple_hits = sum(1 for p in _SIMPLE_SIGNALS if p.search(task_text))

    # Word count as size proxy
    word_count = len(task_text.split())

    # Critical priority → never downgrade below MODERATE (checked first)
    if priority == "critical":
        if complex_hits >= 2:
            return max(base, TaskComplexity.COMPLEX)
        return max(base, TaskComplexity.MODERATE)

    # Adjust based on signals
    if simple_hits >= 2 and complex_hits == 0:
        return TaskComplexity.TRIVIAL

    if simple_hits > 0 and complex_hits == 0 and word_count < 200:
        return min(base, TaskComplexity.SIMPLE)

    if complex_hits >= 2:
        return max(base, TaskComplexity.COMPLEX)

    if word_count > 1000 and complex_hits > 0:
        return max(base, TaskComplexity.COMPLEX)

    return base


# ---------------------------------------------------------------------------
# Budget state queries
# ---------------------------------------------------------------------------


def get_budget_utilization() -> float:
    """Get current daily budget utilization percentage.

    Returns 0.0 if budget data is unavailable.
    """
    try:
        from agents.budget_enforcer import budget

        summary = budget.get_usage_summary()
        daily = summary.get("daily", {})
        pct_input = daily.get("pct_input", 0.0)
        pct_output = daily.get("pct_output", 0.0)
        return max(pct_input, pct_output)
    except Exception:
        return 0.0


def get_cost_summary() -> dict[str, Any]:
    """Get current cost summary from budget enforcer.

    Returns dict with daily_cost_usd, monthly_cost_usd, monthly_cap_usd,
    monthly_pct, date. Falls back to safe defaults on error.
    """
    try:
        from agents.budget_enforcer import budget

        return budget.get_cost_summary()
    except Exception:
        return {
            "session_cost_usd": 0.0,
            "daily_cost_usd": 0.0,
            "monthly_cost_usd": 0.0,
            "monthly_cap_usd": 100.0,
            "monthly_pct": 0.0,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }


def get_rolling_average_cost(days: int = 7) -> float:
    """Compute rolling average daily cost from budget state files.

    Reads STATE/budgets/usage_YYYY-MM-DD.json files for the last N days.
    Returns average daily cost in USD. Returns 0.0 if no data available.
    """
    total = 0.0
    count = 0
    today = datetime.now(timezone.utc).date()

    for i in range(days):
        day = today - timedelta(days=i)
        usage_file = BUDGETS_DIR / f"usage_{day.isoformat()}.json"
        if usage_file.exists():
            try:
                data = json.loads(usage_file.read_text())
                total += data.get("cost_usd", 0.0)
                count += 1
            except (json.JSONDecodeError, OSError):
                continue

    return total / count if count > 0 else 0.0


def get_langfuse_daily_cost() -> float | None:
    """Query Langfuse for today's cost data.

    Returns total cost in USD for today's traces, or None if
    Langfuse is unavailable or query fails. Safe degradation.
    """
    try:
        from utils.langfuse_tracing import get_langfuse

        lf = get_langfuse()
        if lf is None:
            return None

        # Langfuse free tier doesn't expose a simple cost aggregation API.
        # We'd need to paginate through all generations and sum costs.
        # For v1, return None and rely on local budget data.
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Task routing
# ---------------------------------------------------------------------------


def route_task(
    task_text: str,
    task_class: str = "unknown",
    priority: str = "normal",
    cache_eligible: bool = True,
) -> RoutingDecision:
    """Route a task to the appropriate model.

    Deterministic, rule-based routing. Every decision is explainable.
    All reasons are logged for audit.

    Args:
        task_text: The task content.
        task_class: From classify_and_route().
        priority: From task frontmatter (critical/high/normal/low).
        cache_eligible: Whether this task might benefit from caching.

    Returns:
        RoutingDecision with model, complexity, reasons, and budget state.
    """
    reasons: list[str] = []

    # 1. Score complexity
    complexity = score_complexity(task_text, task_class, priority)
    estimated_tokens = _estimate_tokens(task_text)
    word_count = len(task_text.split())
    reasons.append(
        f"complexity={complexity.name} (class={task_class}, words={word_count}, est_tokens={estimated_tokens})"
    )

    # 2. Get budget state
    budget_pct = get_budget_utilization()
    reasons.append(f"budget_utilization={budget_pct:.1f}%")

    # 3. Langfuse cost data (best-effort)
    langfuse_cost = get_langfuse_daily_cost()
    if langfuse_cost is not None:
        reasons.append(f"langfuse_daily_cost=${langfuse_cost:.4f}")
    else:
        reasons.append("langfuse=unavailable (using local budget data)")

    # 4. Select model based on complexity
    model = COMPLEXITY_MODEL_MAP.get(complexity, DEFAULT_MODEL)
    downgraded = False
    reasons.append(f"base_model={model} (from complexity={complexity.name})")

    # 5. Budget-based downgrades
    if budget_pct >= BUDGET_CRITICAL_PCT and priority not in ("critical",):
        model = MODEL_TIERS["haiku"]
        downgraded = True
        reasons.append(
            f"BUDGET_CRITICAL: >={BUDGET_CRITICAL_PCT:.0f}% utilization, downgraded to haiku (priority={priority})"
        )
    elif budget_pct >= BUDGET_DOWNGRADE_PCT and priority not in ("critical", "high"):
        if model == MODEL_TIERS["opus"]:
            model = MODEL_TIERS["sonnet"]
            downgraded = True
            reasons.append(
                f"BUDGET_DOWNGRADE: >={BUDGET_DOWNGRADE_PCT:.0f}% utilization, opus→sonnet (priority={priority})"
            )
    elif budget_pct >= BUDGET_WARN_PCT:
        reasons.append(f"BUDGET_WARN: >={BUDGET_WARN_PCT:.0f}% utilization, monitoring")

    # 6. Override: CRITICAL priority always gets Opus
    if priority == "critical":
        if model != MODEL_TIERS["opus"]:
            model = MODEL_TIERS["opus"]
            downgraded = False
            reasons.append("CRITICAL_OVERRIDE: restored to opus for critical priority")
        else:
            reasons.append("CRITICAL: using opus (no downgrade applied)")

    # 7. Retry tasks are simpler — prefer Sonnet
    task_head = task_text[:500].lower()
    if (
        ("__retry" in task_head or "contract repair" in task_head)
        and model == MODEL_TIERS["opus"]
        and priority not in ("critical",)
    ):
        model = MODEL_TIERS["sonnet"]
        downgraded = True
        reasons.append("RETRY_DOWNGRADE: contract repair task → sonnet")

    # 8. Cache eligibility note
    if cache_eligible:
        reasons.append("cache_eligible=true")

    return RoutingDecision(
        model=model,
        complexity=complexity,
        reasons=reasons,
        estimated_tokens=estimated_tokens,
        budget_utilization_pct=budget_pct,
        downgraded=downgraded,
    )


# ---------------------------------------------------------------------------
# Budget alerts
# ---------------------------------------------------------------------------


def check_budget_alerts() -> list[str]:
    """Check budget state and return alert messages.

    Combines budget_enforcer alerts with cost-router specific checks.
    Returns empty list if everything is healthy.
    """
    alerts: list[str] = []

    try:
        from agents.budget_enforcer import budget

        # Existing budget enforcer alerts
        alerts.extend(budget.check_alerts())

        # Cost router specific: daily cost threshold
        cost = budget.get_cost_summary()
        daily = cost.get("daily_cost_usd", 0.0)
        if daily >= DAILY_COST_ALERT_USD:
            alerts.append(f"COST_ROUTER: daily cost ${daily:.2f} exceeds ${DAILY_COST_ALERT_USD:.2f} threshold")

        # Rolling average comparison
        avg = get_rolling_average_cost(days=7)
        if avg > 0 and daily > avg * 2:
            alerts.append(
                f"COST_ROUTER: today's cost ${daily:.2f} is {daily / avg:.1f}x the 7-day average (${avg:.2f}/day)"
            )
    except Exception:  # noqa: S110 — rolling avg is best-effort
        pass

    return alerts


# ---------------------------------------------------------------------------
# Adaptive heartbeat configuration
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatConfig:
    """Adaptive heartbeat interval configuration.

    Computed from current system load and budget state.
    Written to STATE/heartbeat_config.json for observability.
    """

    mode: str  # "active", "idle", "budget_constrained"
    interval_minutes: int  # desired interval
    skip_research: bool
    skip_planning: bool
    reason: str


def compute_heartbeat_config(
    tasks_dir: Path | None = None,
) -> HeartbeatConfig:
    """Compute the optimal heartbeat configuration.

    Modes:
    - active: tasks are running → 5 min interval, full features
    - idle: nothing running → 30 min interval, full features
    - budget_constrained: budget > 80% → 30 min, skip research/planning

    The computed interval is a recommendation. The systemd timer
    determines actual execution frequency.
    """
    if tasks_dir is None:
        tasks_dir = BASE / "TASKS"

    # Check for active tasks
    has_active = False
    if tasks_dir.exists():
        lifecycle_suffixes = (".done", ".failed", ".cancelled", ".inprogress")
        inprogress = list(tasks_dir.glob("*.inprogress"))
        pending = [p for p in tasks_dir.glob("*.md") if not any(p.name.endswith(s) for s in lifecycle_suffixes)]
        has_active = bool(inprogress or pending)

    # Check budget
    budget_pct = get_budget_utilization()
    cost = get_cost_summary()
    monthly_pct = cost.get("monthly_pct", 0.0)

    # Decision logic (ordered by priority)
    if monthly_pct >= BUDGET_DOWNGRADE_PCT:
        return HeartbeatConfig(
            mode="budget_constrained",
            interval_minutes=30,
            skip_research=True,
            skip_planning=True,
            reason=(f"monthly budget at {monthly_pct:.1f}% — conserving resources (daily={budget_pct:.1f}%)"),
        )

    if budget_pct >= BUDGET_DOWNGRADE_PCT:
        return HeartbeatConfig(
            mode="budget_constrained",
            interval_minutes=30,
            skip_research=True,
            skip_planning=True,
            reason=f"daily budget at {budget_pct:.1f}% — skipping expensive cycles",
        )

    if has_active:
        return HeartbeatConfig(
            mode="active",
            interval_minutes=5,
            skip_research=False,
            skip_planning=False,
            reason=f"tasks active (budget={budget_pct:.1f}%)",
        )

    return HeartbeatConfig(
        mode="idle",
        interval_minutes=30,
        skip_research=False,
        skip_planning=False,
        reason=f"idle (budget={budget_pct:.1f}%)",
    )


def write_heartbeat_config(config: HeartbeatConfig) -> None:
    """Persist heartbeat config to STATE/ for observability."""
    HEARTBEAT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": config.mode,
        "interval_minutes": config.interval_minutes,
        "skip_research": config.skip_research,
        "skip_planning": config.skip_planning,
        "reason": config.reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    HEARTBEAT_CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")


def read_heartbeat_config() -> HeartbeatConfig | None:
    """Read persisted heartbeat config. Returns None if not set."""
    if not HEARTBEAT_CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_CONFIG_FILE.read_text())
        return HeartbeatConfig(
            mode=data.get("mode", "idle"),
            interval_minutes=data.get("interval_minutes", 30),
            skip_research=data.get("skip_research", False),
            skip_planning=data.get("skip_planning", False),
            reason=data.get("reason", "loaded from file"),
        )
    except Exception:
        return None
