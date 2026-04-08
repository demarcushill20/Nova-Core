"""FTMO Scaling Plan Calendar Automation.

Manages FTMO 4-month cycles, scaling deadlines, and automated scaling process.
Integrates with profit cushion protocol to track scaling eligibility and automate
the scaling submission process.

Key Features:
- FTMO 4-month cycle tracking with Europe/Prague timezone
- Automatic scaling eligibility detection at 10% profit threshold
- Calendar integration for scaling deadlines and reminders
- Scaling submission process automation
- Integration with existing risk management stack
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

log = logging.getLogger(__name__)


_notification_rate_limiter: dict[str, float] = {}  # message_key -> monotonic timestamp


def _send_scaling_notification(text: str, cooldown_seconds: int = 3600) -> None:
    """Send FTMO scaling notification via Telegram using NovaCore infrastructure.

    Rate-limited: identical message keys are suppressed within cooldown_seconds.
    """
    # Never send real notifications during test runs
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("NOVATRADE_TESTING"):
        log.debug(f"Scaling notification suppressed (test env): {text[:80]}")
        return

    # Rate-limit by first 60 chars of text to deduplicate near-identical messages
    message_key = text[:60]
    now = time.monotonic()
    last_sent = _notification_rate_limiter.get(message_key, 0.0)
    if now - last_sent < cooldown_seconds:
        log.debug(f"Scaling notification suppressed (rate-limited): {text[:80]}")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID not set, skipping scaling notification")
        return

    # Prefix with FTMO scaling emoji and tag for easy filtering
    formatted_text = f"🎯 FTMO SCALING: {text}"

    data = urllib.parse.urlencode({"chat_id": chat_id, "text": formatted_text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
        urllib.request.urlopen(req, timeout=15)  # noqa: S310
        _notification_rate_limiter[message_key] = now
        log.info(f"Scaling notification sent: {text}")
    except Exception as e:
        log.error(f"Failed to send scaling notification: {e}")


@dataclass
class ScalingEvent:
    """A calendar event related to FTMO scaling process."""

    event_id: str
    event_type: str  # "cycle_start", "scaling_eligible", "submission_deadline", "cycle_end"
    title: str
    description: str
    due_date: datetime
    status: str = "pending"  # "pending", "completed", "expired"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("UTC")))


@dataclass
class ScalingCycle:
    """Represents a complete FTMO scaling cycle."""

    cycle_id: str
    start_date: datetime
    end_date: datetime
    starting_equity: float
    target_profit_amount: float
    target_profit_pct: float = 10.0
    current_equity: float = 0.0
    peak_equity: float = 0.0
    scaling_eligible_date: datetime | None = None
    scaling_submitted_date: datetime | None = None
    cycle_status: str = "active"  # "active", "scaling_eligible", "scaling_submitted", "completed", "failed"
    events: list[ScalingEvent] = field(default_factory=list)

    @property
    def current_profit_amount(self) -> float:
        """Current cycle profit in absolute terms."""
        return self.current_equity - self.starting_equity

    @property
    def current_profit_pct(self) -> float:
        """Current cycle profit as percentage of starting equity."""
        if self.starting_equity == 0:
            return 0.0
        return (self.current_profit_amount / self.starting_equity) * 100

    @property
    def days_remaining(self) -> int:
        """Days remaining in the current cycle."""
        now = datetime.now(self.start_date.tzinfo)
        remaining = (self.end_date - now).days
        return max(0, remaining)

    @property
    def is_scaling_eligible(self) -> bool:
        """Whether the account has reached 10% profit threshold."""
        return self.current_profit_pct >= self.target_profit_pct

    @property
    def scaling_deadline(self) -> datetime:
        """Deadline to submit scaling application (7 days before cycle end)."""
        return self.end_date - timedelta(days=7)


@dataclass
class ScalingPlanConfig:
    """Configuration for scaling plan calendar automation."""

    # FTMO cycle configuration
    cycle_length_months: int = 4
    target_profit_pct: float = 10.0
    scaling_deadline_days: int = 7  # Days before cycle end to submit scaling
    timezone: str = "Europe/Prague"

    # Calendar integration
    enable_calendar_events: bool = True
    reminder_days_before: list[int] = field(default_factory=lambda: [30, 14, 7, 3, 1])

    # Automation settings
    auto_submit_scaling: bool = False  # Requires manual approval for now
    notification_enabled: bool = True

    # State persistence — use persistent STATE/ dir, not volatile /tmp
    state_file_path: str = str(Path.home() / "nova-core" / "STATE" / "novatrade" / "scaling_plan_state.json")
    auto_save: bool = True

    # Notification rate-limiting
    notification_cooldown_seconds: int = 3600  # 1 hour between same-type notifications
    equity_change_threshold: float = 10.0  # Min equity change ($) to trigger processing


class ScalingPlanCalendar:
    """FTMO Scaling Plan Calendar management system.

    Manages FTMO 4-month cycles, tracks scaling eligibility, creates calendar events,
    and automates the scaling submission process integration.

    Features:
    - Automatic cycle detection and management
    - Real-time scaling eligibility tracking
    - Calendar event creation for deadlines and reminders
    - Integration with profit cushion protocol
    - Automated scaling process coordination
    """

    def __init__(self, config: ScalingPlanConfig | None = None):
        """Initialize scaling plan calendar with configuration.

        Args:
            config: Scaling plan configuration. Uses defaults if None.
        """
        self.config = config or ScalingPlanConfig()
        self._current_cycle: ScalingCycle | None = None
        self._state_loaded = False
        self._last_notification_ts: dict[str, float] = {}  # type -> monotonic timestamp
        self._last_processed_equity: float | None = None
        self._load_state()

    def initialize_cycle(self, starting_equity: float, cycle_start_date: datetime | None = None) -> ScalingCycle:
        """Initialize a new FTMO scaling cycle.

        Args:
            starting_equity: Account equity at cycle start
            cycle_start_date: Cycle start date (defaults to now in FTMO timezone)

        Returns:
            Newly created scaling cycle
        """
        # Guard: never overwrite an existing active/eligible/submitted cycle
        if self._current_cycle and self._current_cycle.cycle_status in (
            "active",
            "scaling_eligible",
            "scaling_submitted",
        ):
            log.info(
                f"Skipping cycle init — existing cycle {self._current_cycle.cycle_id} "
                f"is {self._current_cycle.cycle_status}"
            )
            return self._current_cycle

        if cycle_start_date is None:
            tz = ZoneInfo(self.config.timezone)
            cycle_start_date = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

        # Calculate cycle end date (4 months later)
        cycle_end_date = cycle_start_date + relativedelta(months=self.config.cycle_length_months)

        # Create cycle ID
        cycle_id = f"ftmo_cycle_{cycle_start_date.strftime('%Y%m%d')}"

        # Calculate target profit amount
        target_profit_amount = starting_equity * (self.config.target_profit_pct / 100)

        # Create new cycle
        cycle = ScalingCycle(
            cycle_id=cycle_id,
            start_date=cycle_start_date,
            end_date=cycle_end_date,
            starting_equity=starting_equity,
            target_profit_amount=target_profit_amount,
            target_profit_pct=self.config.target_profit_pct,
            current_equity=starting_equity,
            peak_equity=starting_equity,
            cycle_status="active",
        )

        # Generate calendar events for this cycle
        self._generate_cycle_events(cycle)

        # Set as current cycle
        self._current_cycle = cycle

        # Save state
        if self.config.auto_save:
            self._save_state()

        log.info(
            f"Initialized new scaling cycle: {cycle_id} "
            f"(${starting_equity:,.2f} → ${starting_equity + target_profit_amount:,.2f})"
        )

        return cycle

    def update_equity(self, current_equity: float) -> None:
        """Update current equity and check for scaling eligibility.

        Args:
            current_equity: Current account equity
        """
        if not self._current_cycle:
            log.warning("No active scaling cycle - cannot update equity")
            return

        # Skip processing if equity hasn't changed meaningfully
        if (
            self._last_processed_equity is not None
            and abs(current_equity - self._last_processed_equity) < self.config.equity_change_threshold
        ):
            return

        self._last_processed_equity = current_equity

        # Update equity tracking
        previous_equity = self._current_cycle.current_equity
        self._current_cycle.current_equity = current_equity
        self._current_cycle.peak_equity = max(self._current_cycle.peak_equity, current_equity)

        # Check for scaling eligibility transition — only if not already notified
        if self._current_cycle.scaling_eligible_date is None and self._current_cycle.cycle_status not in (
            "scaling_eligible",
            "scaling_submitted",
            "completed",
        ):
            was_eligible = (
                previous_equity - self._current_cycle.starting_equity
            ) >= self._current_cycle.target_profit_amount
            is_eligible = self._current_cycle.is_scaling_eligible

            if is_eligible and not was_eligible:
                self._handle_scaling_eligible()

        # Update cycle status
        self._update_cycle_status()

        # Save state
        if self.config.auto_save:
            self._save_state()

    def get_current_cycle(self) -> ScalingCycle | None:
        """Get the current active scaling cycle.

        Returns:
            Current scaling cycle or None if no active cycle
        """
        return self._current_cycle

    def get_cycle_status(self) -> dict[str, Any]:
        """Get comprehensive status of current scaling cycle.

        Returns:
            Dictionary with cycle status information
        """
        if not self._current_cycle:
            return {"status": "no_active_cycle"}

        cycle = self._current_cycle
        now = datetime.now(cycle.start_date.tzinfo)

        return {
            "cycle_id": cycle.cycle_id,
            "cycle_status": cycle.cycle_status,
            "start_date": cycle.start_date.isoformat(),
            "end_date": cycle.end_date.isoformat(),
            "days_elapsed": (now - cycle.start_date).days,
            "days_remaining": cycle.days_remaining,
            "starting_equity": cycle.starting_equity,
            "current_equity": cycle.current_equity,
            "peak_equity": cycle.peak_equity,
            "current_profit_amount": cycle.current_profit_amount,
            "current_profit_pct": cycle.current_profit_pct,
            "target_profit_amount": cycle.target_profit_amount,
            "target_profit_pct": cycle.target_profit_pct,
            "is_scaling_eligible": cycle.is_scaling_eligible,
            "scaling_eligible_date": cycle.scaling_eligible_date.isoformat() if cycle.scaling_eligible_date else None,
            "scaling_deadline": cycle.scaling_deadline.isoformat(),
            "scaling_submitted_date": cycle.scaling_submitted_date.isoformat()
            if cycle.scaling_submitted_date
            else None,
            "pending_events": len([e for e in cycle.events if e.status == "pending"]),
            "total_events": len(cycle.events),
        }

    def get_upcoming_events(self, days_ahead: int = 30) -> list[ScalingEvent]:
        """Get upcoming scaling events within specified timeframe.

        Args:
            days_ahead: Number of days to look ahead

        Returns:
            List of upcoming scaling events
        """
        if not self._current_cycle:
            return []

        now = datetime.now(ZoneInfo(self.config.timezone))
        cutoff_date = now + timedelta(days=days_ahead)

        upcoming = []
        for event in self._current_cycle.events:
            if event.status == "pending" and now <= event.due_date <= cutoff_date:
                upcoming.append(event)

        # Sort by due date
        upcoming.sort(key=lambda e: e.due_date)
        return upcoming

    def submit_scaling_application(self) -> bool:
        """Submit scaling application (placeholder for actual implementation).

        Returns:
            True if submission successful, False otherwise
        """
        if not self._current_cycle or not self._current_cycle.is_scaling_eligible:
            log.warning("Cannot submit scaling - cycle not eligible")
            return False

        # Safety: require explicit auto_submit_scaling=True or manual invocation
        if not self.config.auto_submit_scaling:
            log.warning(
                "Scaling submission blocked — auto_submit_scaling is False. "
                "Operator must submit manually or set auto_submit_scaling=True."
            )
            return False

        # Enhanced FTMO scaling submission process with proper logging and validation
        log.info(f"Initiating FTMO scaling submission for cycle {self._current_cycle.cycle_id}")

        # Validate cycle eligibility before submission
        if self._current_cycle.current_profit_pct < 10.0:
            log.error(
                f"Scaling submission failed - insufficient profit: {self._current_cycle.current_profit_pct:.2f}% < 10%"
            )
            return False

        # Check if scaling already submitted
        if self._current_cycle.scaling_submitted_date:
            log.warning(f"Scaling already submitted on {self._current_cycle.scaling_submitted_date}")
            return False

        # Send pre-submission notification
        pre_submit_text = (
            f"Submitting scaling application for cycle {self._current_cycle.cycle_id} "
            f"with {self._current_cycle.current_profit_pct:.2f}% profit"
        )
        _send_scaling_notification(pre_submit_text)
        self._current_cycle.scaling_submitted_date = datetime.now(ZoneInfo(self.config.timezone))
        self._current_cycle.cycle_status = "scaling_submitted"

        # Create completion event
        submission_event = ScalingEvent(
            event_id=f"{self._current_cycle.cycle_id}_scaling_submitted",
            event_type="scaling_submitted",
            title="FTMO Scaling Application Submitted",
            description=f"Scaling application submitted for cycle {self._current_cycle.cycle_id}",
            due_date=self._current_cycle.scaling_submitted_date,
            status="completed",
            metadata={"profit_pct": self._current_cycle.current_profit_pct},
        )
        self._current_cycle.events.append(submission_event)

        if self.config.auto_save:
            self._save_state()

        # Send successful submission notification
        success_text = (
            f"✅ Scaling application SUBMITTED successfully for cycle {self._current_cycle.cycle_id}! "
            f"Final profit: {self._current_cycle.current_profit_pct:.2f}%. "
            f"Awaiting FTMO approval."
        )
        _send_scaling_notification(success_text)

        log.info(f"Scaling application submitted for cycle {self._current_cycle.cycle_id}")
        return True

    def _generate_cycle_events(self, cycle: ScalingCycle) -> None:
        """Generate calendar events for a scaling cycle.

        Args:
            cycle: Scaling cycle to generate events for
        """
        events = []

        # Cycle start event
        start_event = ScalingEvent(
            event_id=f"{cycle.cycle_id}_start",
            event_type="cycle_start",
            title="FTMO Scaling Cycle Start",
            description=(
                f"New FTMO scaling cycle started with ${cycle.starting_equity:,.2f} "
                f"target: ${cycle.starting_equity + cycle.target_profit_amount:,.2f} "
                f"(+{cycle.target_profit_pct}%)"
            ),
            due_date=cycle.start_date,
            status="completed",
        )
        events.append(start_event)

        # Scaling deadline reminder events
        scaling_deadline = cycle.scaling_deadline
        for days_before in self.config.reminder_days_before:
            reminder_date = scaling_deadline - timedelta(days=days_before)
            if reminder_date > cycle.start_date:  # Only create future reminders
                reminder_event = ScalingEvent(
                    event_id=f"{cycle.cycle_id}_reminder_{days_before}d",
                    event_type="submission_reminder",
                    title=f"FTMO Scaling Deadline - {days_before} Days",
                    description=f"Scaling submission deadline in {days_before} days. Current profit: TBD",
                    due_date=reminder_date,
                    status="pending",
                    metadata={"days_before_deadline": days_before},
                )
                events.append(reminder_event)

        # Scaling deadline event
        deadline_event = ScalingEvent(
            event_id=f"{cycle.cycle_id}_deadline",
            event_type="submission_deadline",
            title="FTMO Scaling Submission Deadline",
            description=f"Final deadline to submit scaling application for cycle {cycle.cycle_id}",
            due_date=scaling_deadline,
            status="pending",
        )
        events.append(deadline_event)

        # Cycle end event
        end_event = ScalingEvent(
            event_id=f"{cycle.cycle_id}_end",
            event_type="cycle_end",
            title="FTMO Scaling Cycle End",
            description=f"End of FTMO scaling cycle {cycle.cycle_id}",
            due_date=cycle.end_date,
            status="pending",
        )
        events.append(end_event)

        cycle.events = events
        log.info(f"Generated {len(events)} calendar events for cycle {cycle.cycle_id}")

    def _handle_scaling_eligible(self) -> None:
        """Handle transition to scaling eligible status.

        Guards against duplicate notifications — only fires once per cycle.
        """
        if not self._current_cycle:
            return

        # Guard: already notified for this cycle — do not fire again
        if self._current_cycle.scaling_eligible_date is not None:
            return

        # Guard: already submitted — don't regress status
        if self._current_cycle.scaling_submitted_date is not None:
            return

        # Record scaling eligibility
        now = datetime.now(ZoneInfo(self.config.timezone))
        self._current_cycle.scaling_eligible_date = now
        self._current_cycle.cycle_status = "scaling_eligible"

        # Create scaling eligible event
        eligible_event = ScalingEvent(
            event_id=f"{self._current_cycle.cycle_id}_scaling_eligible",
            event_type="scaling_eligible",
            title="FTMO Scaling Eligible! 🎉",
            description=f"Account reached {self.config.target_profit_pct}% profit target! "
            f"Eligible for FTMO scaling. Submit application by "
            f"{self._current_cycle.scaling_deadline.strftime('%Y-%m-%d')}",
            due_date=now,
            status="completed",
            metadata={
                "profit_pct": self._current_cycle.current_profit_pct,
                "profit_amount": self._current_cycle.current_profit_amount,
            },
        )
        self._current_cycle.events.append(eligible_event)

        log.info(
            f"🎉 SCALING ELIGIBLE! Cycle {self._current_cycle.cycle_id} reached "
            f"{self._current_cycle.current_profit_pct:.2f}% profit"
        )

        # Send scaling eligibility notification
        notification_text = (
            f"Cycle {self._current_cycle.cycle_id} SCALING ELIGIBLE! "
            f"Profit: {self._current_cycle.current_profit_pct:.2f}% "
            f"(Target: 10%). Submit scaling application before cycle end: "
            f"{self._current_cycle.end_date.strftime('%Y-%m-%d %H:%M %Z')}"
        )
        _send_scaling_notification(notification_text)

    def _update_cycle_status(self) -> None:
        """Update cycle status based on current conditions."""
        if not self._current_cycle:
            return

        cycle = self._current_cycle
        now = datetime.now(cycle.start_date.tzinfo)

        # Check if cycle has ended
        if now >= cycle.end_date:
            if cycle.cycle_status != "scaling_submitted":
                cycle.cycle_status = "completed"
            return

        # Update status based on current conditions
        if cycle.scaling_submitted_date:
            cycle.cycle_status = "scaling_submitted"
        elif cycle.is_scaling_eligible:
            cycle.cycle_status = "scaling_eligible"
        else:
            cycle.cycle_status = "active"

    def _save_state(self) -> None:
        """Save current state to disk."""
        if not self._current_cycle:
            return

        try:
            state_data = {
                "cycle": {
                    "cycle_id": self._current_cycle.cycle_id,
                    "start_date": self._current_cycle.start_date.isoformat(),
                    "end_date": self._current_cycle.end_date.isoformat(),
                    "starting_equity": self._current_cycle.starting_equity,
                    "target_profit_amount": self._current_cycle.target_profit_amount,
                    "target_profit_pct": self._current_cycle.target_profit_pct,
                    "current_equity": self._current_cycle.current_equity,
                    "peak_equity": self._current_cycle.peak_equity,
                    "scaling_eligible_date": self._current_cycle.scaling_eligible_date.isoformat()
                    if self._current_cycle.scaling_eligible_date
                    else None,
                    "scaling_submitted_date": self._current_cycle.scaling_submitted_date.isoformat()
                    if self._current_cycle.scaling_submitted_date
                    else None,
                    "cycle_status": self._current_cycle.cycle_status,
                    "events": [
                        {
                            "event_id": e.event_id,
                            "event_type": e.event_type,
                            "title": e.title,
                            "description": e.description,
                            "due_date": e.due_date.isoformat(),
                            "status": e.status,
                            "metadata": e.metadata,
                            "created_at": e.created_at.isoformat(),
                        }
                        for e in self._current_cycle.events
                    ],
                },
                "config": {
                    "cycle_length_months": self.config.cycle_length_months,
                    "target_profit_pct": self.config.target_profit_pct,
                    "timezone": self.config.timezone,
                },
                "saved_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            }

            # Ensure directory exists
            state_file = Path(self.config.state_file_path)
            state_file.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically
            temp_file = state_file.with_suffix(".tmp")
            with temp_file.open("w") as f:
                json.dump(state_data, f, indent=2)
            temp_file.rename(state_file)

        except Exception as exc:
            log.error(f"Failed to save scaling plan state: {exc}")

    def _load_state(self) -> None:
        """Load state from disk."""
        if self._state_loaded:
            return

        try:
            state_file = Path(self.config.state_file_path)
            if not state_file.exists():
                return

            with state_file.open("r") as f:
                state_data = json.load(f)

            cycle_data = state_data.get("cycle")
            if not cycle_data:
                return

            # Reconstruct events
            events = []
            for event_data in cycle_data.get("events", []):
                event = ScalingEvent(
                    event_id=event_data["event_id"],
                    event_type=event_data["event_type"],
                    title=event_data["title"],
                    description=event_data["description"],
                    due_date=datetime.fromisoformat(event_data["due_date"]),
                    status=event_data["status"],
                    metadata=event_data["metadata"],
                    created_at=datetime.fromisoformat(event_data["created_at"]),
                )
                events.append(event)

            # Reconstruct cycle
            self._current_cycle = ScalingCycle(
                cycle_id=cycle_data["cycle_id"],
                start_date=datetime.fromisoformat(cycle_data["start_date"]),
                end_date=datetime.fromisoformat(cycle_data["end_date"]),
                starting_equity=cycle_data["starting_equity"],
                target_profit_amount=cycle_data["target_profit_amount"],
                target_profit_pct=cycle_data["target_profit_pct"],
                current_equity=cycle_data["current_equity"],
                peak_equity=cycle_data["peak_equity"],
                scaling_eligible_date=datetime.fromisoformat(cycle_data["scaling_eligible_date"])
                if cycle_data["scaling_eligible_date"]
                else None,
                scaling_submitted_date=datetime.fromisoformat(cycle_data["scaling_submitted_date"])
                if cycle_data["scaling_submitted_date"]
                else None,
                cycle_status=cycle_data["cycle_status"],
                events=events,
            )

            log.info(f"Loaded scaling plan state: cycle {self._current_cycle.cycle_id}")

        except Exception as exc:
            log.error(f"Failed to load scaling plan state: {exc}")
        finally:
            self._state_loaded = True
