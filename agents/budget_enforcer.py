"""Token & cost budget enforcement for NovaCore agent runtime.

Tracks per-task, per-session, and daily token usage with configurable limits.
Provides pre-flight checks and post-call accounting.
"""

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
BUDGETS_DIR = STATE / "budgets"
LIMITS_FILE = BUDGETS_DIR / "limits.json"

# ---------------------------------------------------------------------------
# Anthropic pricing per 1M tokens (USD)
# ---------------------------------------------------------------------------
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "opus": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "claude-haiku-3-20250307": {"input": 0.80, "output": 4.00},
    "haiku": {"input": 0.80, "output": 4.00},
}

# Fallback if model string not recognised — assume Opus pricing (safe upper bound)
DEFAULT_PRICING = {"input": 15.00, "output": 75.00}

# ---------------------------------------------------------------------------
# Default budget limits
# ---------------------------------------------------------------------------
DEFAULT_LIMITS: dict[str, Any] = {
    "per_task": {
        "input_tokens": 200_000,
        "output_tokens": 50_000,
    },
    "per_session": {
        "input_tokens": 500_000,
        "output_tokens": 100_000,
    },
    "per_day": {
        "input_tokens": 5_000_000,
        "output_tokens": 1_000_000,
    },
    "monthly_cost_cap_usd": 100.00,
}

ALERT_THRESHOLDS = [0.75, 0.90]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class TokenCounter:
    input_tokens: int = 0
    output_tokens: int = 0

    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass
class UsageRecord:
    timestamp: str
    input_tokens: int
    output_tokens: int
    model: str
    task_id: str
    cost_usd: float


# ---------------------------------------------------------------------------
# BudgetEnforcer
# ---------------------------------------------------------------------------
class BudgetEnforcer:
    """Enforces token and cost budgets across task, session, and daily scopes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # In-memory counters
        self._session = TokenCounter()
        self._tasks: dict[str, TokenCounter] = {}
        self._session_cost_usd: float = 0.0

        # Load (or create) limits config
        self._limits = self._load_limits()

        # Load today's persisted daily usage
        self._daily = TokenCounter()
        self._daily_cost_usd: float = 0.0
        self._monthly_cost_usd: float = 0.0
        self._today_str = self._today()
        self._load_daily_usage()
        self._load_monthly_cost()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_proceed(
        self, estimated_tokens: int = 0, scope: str = "daily"
    ) -> tuple[bool, str]:
        """Pre-flight check: can we afford *estimated_tokens* more input tokens?

        Args:
            estimated_tokens: Expected input tokens for the upcoming call.
            scope: One of "task", "session", "daily", "monthly".  For "task"
                   scope the check uses the *current* active task counter.

        Returns:
            (allowed, reason) — allowed is True if within budget.
        """
        with self._lock:
            self._maybe_roll_day()

            if scope == "task":
                task_counter = self._current_task_counter()
                limit = self._limits["per_task"]["input_tokens"]
                projected = task_counter.input_tokens + estimated_tokens
                if projected > limit:
                    return (
                        False,
                        f"Task input budget exceeded: {projected:,} / {limit:,} tokens",
                    )

            if scope in ("session", "task"):
                limit = self._limits["per_session"]["input_tokens"]
                projected = self._session.input_tokens + estimated_tokens
                if projected > limit:
                    return (
                        False,
                        f"Session input budget exceeded: {projected:,} / {limit:,} tokens",
                    )

            # Daily check applies to all scopes
            limit = self._limits["per_day"]["input_tokens"]
            projected = self._daily.input_tokens + estimated_tokens
            if projected > limit:
                return (
                    False,
                    f"Daily input budget exceeded: {projected:,} / {limit:,} tokens",
                )

            if scope == "monthly":
                cap = self._limits["monthly_cost_cap_usd"]
                if self._monthly_cost_usd >= cap:
                    return (
                        False,
                        f"Monthly cost cap reached: ${self._monthly_cost_usd:.2f} / ${cap:.2f}",
                    )

            return (True, "OK")

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "claude-opus-4-6",
        task_id: str = "",
    ) -> None:
        """Record token usage after an API call completes."""
        cost = self._calculate_cost(input_tokens, output_tokens, model)

        with self._lock:
            self._maybe_roll_day()

            # Session counters
            self._session.add(input_tokens, output_tokens)
            self._session_cost_usd += cost

            # Task counters
            if task_id:
                if task_id not in self._tasks:
                    self._tasks[task_id] = TokenCounter()
                self._tasks[task_id].add(input_tokens, output_tokens)

            # Daily counters
            self._daily.add(input_tokens, output_tokens)
            self._daily_cost_usd += cost
            self._monthly_cost_usd += cost

            # Persist daily usage
            self._save_daily_usage()

    def get_usage_summary(self) -> dict[str, Any]:
        """Return current usage vs limits for all scopes."""
        with self._lock:
            self._maybe_roll_day()
            limits = self._limits
            return {
                "session": {
                    "used": self._session.to_dict(),
                    "limits": limits["per_session"],
                    "pct_input": self._pct(
                        self._session.input_tokens,
                        limits["per_session"]["input_tokens"],
                    ),
                    "pct_output": self._pct(
                        self._session.output_tokens,
                        limits["per_session"]["output_tokens"],
                    ),
                },
                "daily": {
                    "date": self._today_str,
                    "used": self._daily.to_dict(),
                    "limits": limits["per_day"],
                    "pct_input": self._pct(
                        self._daily.input_tokens,
                        limits["per_day"]["input_tokens"],
                    ),
                    "pct_output": self._pct(
                        self._daily.output_tokens,
                        limits["per_day"]["output_tokens"],
                    ),
                },
                "tasks": {
                    tid: {
                        "used": counter.to_dict(),
                        "limits": limits["per_task"],
                        "pct_input": self._pct(
                            counter.input_tokens,
                            limits["per_task"]["input_tokens"],
                        ),
                        "pct_output": self._pct(
                            counter.output_tokens,
                            limits["per_task"]["output_tokens"],
                        ),
                    }
                    for tid, counter in self._tasks.items()
                },
            }

    def get_cost_summary(self) -> dict[str, Any]:
        """Return dollar amounts for today, this session, and this month."""
        with self._lock:
            self._maybe_roll_day()
            return {
                "session_cost_usd": round(self._session_cost_usd, 4),
                "daily_cost_usd": round(self._daily_cost_usd, 4),
                "monthly_cost_usd": round(self._monthly_cost_usd, 4),
                "monthly_cap_usd": self._limits["monthly_cost_cap_usd"],
                "monthly_pct": self._pct(
                    self._monthly_cost_usd,
                    self._limits["monthly_cost_cap_usd"],
                ),
                "date": self._today_str,
            }

    def reset_task_budget(self, task_id: str) -> None:
        """Reset per-task counters for a new task."""
        with self._lock:
            self._tasks[task_id] = TokenCounter()

    def check_alerts(self) -> list[str]:
        """Return alert messages for any scope that has crossed 75% or 90% thresholds."""
        alerts: list[str] = []

        with self._lock:
            self._maybe_roll_day()
            limits = self._limits

            # Daily alerts
            for label, used, limit in [
                (
                    "Daily input tokens",
                    self._daily.input_tokens,
                    limits["per_day"]["input_tokens"],
                ),
                (
                    "Daily output tokens",
                    self._daily.output_tokens,
                    limits["per_day"]["output_tokens"],
                ),
            ]:
                self._check_threshold_alerts(alerts, label, used, limit)

            # Session alerts
            for label, used, limit in [
                (
                    "Session input tokens",
                    self._session.input_tokens,
                    limits["per_session"]["input_tokens"],
                ),
                (
                    "Session output tokens",
                    self._session.output_tokens,
                    limits["per_session"]["output_tokens"],
                ),
            ]:
                self._check_threshold_alerts(alerts, label, used, limit)

            # Monthly cost alert
            self._check_threshold_alerts(
                alerts,
                "Monthly cost",
                self._monthly_cost_usd,
                limits["monthly_cost_cap_usd"],
            )

            # Task alerts
            for tid, counter in self._tasks.items():
                for label, used, limit in [
                    (
                        f"Task '{tid}' input tokens",
                        counter.input_tokens,
                        limits["per_task"]["input_tokens"],
                    ),
                    (
                        f"Task '{tid}' output tokens",
                        counter.output_tokens,
                        limits["per_task"]["output_tokens"],
                    ),
                ]:
                    self._check_threshold_alerts(alerts, label, used, limit)

        return alerts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _this_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _maybe_roll_day(self) -> None:
        """If the UTC date has changed, persist current day and reset counters."""
        today = self._today()
        if today != self._today_str:
            self._save_daily_usage()  # persist previous day
            self._today_str = today
            self._daily.reset()
            self._daily_cost_usd = 0.0
            self._load_monthly_cost()  # re-aggregate from files

    def _current_task_counter(self) -> TokenCounter:
        """Return the most recently added task counter, or a dummy zero counter."""
        if self._tasks:
            last_key = list(self._tasks.keys())[-1]
            return self._tasks[last_key]
        return TokenCounter()

    @staticmethod
    def _pct(used: float, limit: float) -> float:
        if limit <= 0:
            return 0.0
        return round((used / limit) * 100, 1)

    @staticmethod
    def _check_threshold_alerts(
        alerts: list[str], label: str, used: float, limit: float
    ) -> None:
        if limit <= 0:
            return
        ratio = used / limit
        for threshold in ALERT_THRESHOLDS:
            if ratio >= threshold:
                pct = int(threshold * 100)
                alerts.append(
                    f"WARNING: {label} at {ratio:.0%} of limit "
                    f"({used:,.0f} / {limit:,.0f}) — exceeds {pct}% threshold"
                )
                break  # only emit the highest crossed threshold

    def _calculate_cost(
        self, input_tokens: int, output_tokens: int, model: str
    ) -> float:
        """Calculate cost in USD for a given token usage and model."""
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        cost = (input_tokens / 1_000_000) * pricing["input"] + (
            output_tokens / 1_000_000
        ) * pricing["output"]
        return cost

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        BUDGETS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_limits(self) -> dict[str, Any]:
        """Load limits from config file, creating with defaults if absent."""
        self._ensure_dir()
        if LIMITS_FILE.exists():
            try:
                data = json.loads(LIMITS_FILE.read_text())
                # Merge with defaults so new keys are always present
                merged = {**DEFAULT_LIMITS}
                for key in DEFAULT_LIMITS:
                    if key in data:
                        if isinstance(DEFAULT_LIMITS[key], dict) and isinstance(
                            data[key], dict
                        ):
                            merged[key] = {**DEFAULT_LIMITS[key], **data[key]}
                        else:
                            merged[key] = data[key]
                return merged
            except (json.JSONDecodeError, KeyError):
                pass  # fall through to defaults

        # Write defaults
        LIMITS_FILE.write_text(json.dumps(DEFAULT_LIMITS, indent=2) + "\n")
        return dict(DEFAULT_LIMITS)

    def _daily_usage_file(self, date_str: str | None = None) -> Path:
        ds = date_str or self._today_str
        return BUDGETS_DIR / f"usage_{ds}.json"

    def _load_daily_usage(self) -> None:
        """Load today's usage from disk."""
        path = self._daily_usage_file()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._daily.input_tokens = data.get("input_tokens", 0)
                self._daily.output_tokens = data.get("output_tokens", 0)
                self._daily_cost_usd = data.get("cost_usd", 0.0)
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_daily_usage(self) -> None:
        """Persist today's usage to disk."""
        self._ensure_dir()
        path = self._daily_usage_file()
        data = {
            "date": self._today_str,
            "input_tokens": self._daily.input_tokens,
            "output_tokens": self._daily.output_tokens,
            "cost_usd": round(self._daily_cost_usd, 4),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, indent=2) + "\n")

    def _load_monthly_cost(self) -> None:
        """Aggregate cost from all daily usage files in the current month."""
        month_prefix = self._this_month()
        total = 0.0
        for path in BUDGETS_DIR.glob(f"usage_{month_prefix}-*.json"):
            try:
                data = json.loads(path.read_text())
                total += data.get("cost_usd", 0.0)
            except (json.JSONDecodeError, KeyError):
                continue
        self._monthly_cost_usd = total


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
budget = BudgetEnforcer()
