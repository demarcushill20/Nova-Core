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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

log = logging.getLogger(__name__)


@dataclass
class ScalingEvent:
    """A calendar event related to FTMO scaling process."""

    event_id: str
    event_type: str  # "cycle_start", "scaling_eligible", "submission_deadline", "cycle_end"
    title: str
    description: str
    due_date: datetime
    status: str = "pending"  # "pending", "completed", "expired"
    metadata: Dict[str, Any] = field(default_factory=dict)
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
    scaling_eligible_date: Optional[datetime] = None
    scaling_submitted_date: Optional[datetime] = None
    cycle_status: str = "active"  # "active", "scaling_eligible", "scaling_submitted", "completed", "failed"
    events: List[ScalingEvent] = field(default_factory=list)

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
    reminder_days_before: List[int] = field(default_factory=lambda: [30, 14, 7, 3, 1])

    # Automation settings
    auto_submit_scaling: bool = False  # Requires manual approval for now
    notification_enabled: bool = True

    # State persistence
    state_file_path: str = "/tmp/novatrade_scaling_plan_state.json"
    auto_save: bool = True


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

    def __init__(self, config: Optional[ScalingPlanConfig] = None):
        """Initialize scaling plan calendar with configuration.

        Args:
            config: Scaling plan configuration. Uses defaults if None.
        """
        self.config = config or ScalingPlanConfig()
        self._current_cycle: Optional[ScalingCycle] = None
        self._state_loaded = False
        self._load_state()

    def initialize_cycle(
        self,
        starting_equity: float,
        cycle_start_date: Optional[datetime] = None
    ) -> ScalingCycle:
        """Initialize a new FTMO scaling cycle.

        Args:
            starting_equity: Account equity at cycle start
            cycle_start_date: Cycle start date (defaults to now in FTMO timezone)

        Returns:
            Newly created scaling cycle
        """
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
            cycle_status="active"
        )

        # Generate calendar events for this cycle
        self._generate_cycle_events(cycle)

        # Set as current cycle
        self._current_cycle = cycle

        # Save state
        if self.config.auto_save:
            self._save_state()

        log.info(f"Initialized new scaling cycle: {cycle_id} "
                f"(${starting_equity:,.2f} → ${starting_equity + target_profit_amount:,.2f})")

        return cycle

    def update_equity(self, current_equity: float) -> None:
        """Update current equity and check for scaling eligibility.

        Args:
            current_equity: Current account equity
        """
        if not self._current_cycle:
            log.warning("No active scaling cycle - cannot update equity")
            return

        # Update equity tracking
        previous_equity = self._current_cycle.current_equity
        self._current_cycle.current_equity = current_equity
        self._current_cycle.peak_equity = max(self._current_cycle.peak_equity, current_equity)

        # Check for scaling eligibility transition
        was_eligible = (previous_equity - self._current_cycle.starting_equity) >= self._current_cycle.target_profit_amount
        is_eligible = self._current_cycle.is_scaling_eligible

        if is_eligible and not was_eligible:
            # Just became scaling eligible!
            self._handle_scaling_eligible()

        # Update cycle status
        self._update_cycle_status()

        # Save state
        if self.config.auto_save:
            self._save_state()

    def get_current_cycle(self) -> Optional[ScalingCycle]:
        """Get the current active scaling cycle.

        Returns:
            Current scaling cycle or None if no active cycle
        """
        return self._current_cycle

    def get_cycle_status(self) -> Dict[str, Any]:
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
            "scaling_submitted_date": cycle.scaling_submitted_date.isoformat() if cycle.scaling_submitted_date else None,
            "pending_events": len([e for e in cycle.events if e.status == "pending"]),
            "total_events": len(cycle.events)
        }

    def get_upcoming_events(self, days_ahead: int = 30) -> List[ScalingEvent]:
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
            if (event.status == "pending" and
                now <= event.due_date <= cutoff_date):
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

        # TODO: Implement actual FTMO scaling submission process
        # For now, just mark as submitted
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
            metadata={"profit_pct": self._current_cycle.current_profit_pct}
        )
        self._current_cycle.events.append(submission_event)

        if self.config.auto_save:
            self._save_state()

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
            title=f"FTMO Scaling Cycle Start",
            description=f"New FTMO scaling cycle started with ${cycle.starting_equity:,.2f} target: ${cycle.starting_equity + cycle.target_profit_amount:,.2f} (+{cycle.target_profit_pct}%)",
            due_date=cycle.start_date,
            status="completed"
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
                    metadata={"days_before_deadline": days_before}
                )
                events.append(reminder_event)

        # Scaling deadline event
        deadline_event = ScalingEvent(
            event_id=f"{cycle.cycle_id}_deadline",
            event_type="submission_deadline",
            title="FTMO Scaling Submission Deadline",
            description=f"Final deadline to submit scaling application for cycle {cycle.cycle_id}",
            due_date=scaling_deadline,
            status="pending"
        )
        events.append(deadline_event)

        # Cycle end event
        end_event = ScalingEvent(
            event_id=f"{cycle.cycle_id}_end",
            event_type="cycle_end",
            title="FTMO Scaling Cycle End",
            description=f"End of FTMO scaling cycle {cycle.cycle_id}",
            due_date=cycle.end_date,
            status="pending"
        )
        events.append(end_event)

        cycle.events = events
        log.info(f"Generated {len(events)} calendar events for cycle {cycle.cycle_id}")

    def _handle_scaling_eligible(self) -> None:
        """Handle transition to scaling eligible status."""
        if not self._current_cycle:
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
                       f"Eligible for FTMO scaling. Submit application by {self._current_cycle.scaling_deadline.strftime('%Y-%m-%d')}",
            due_date=now,
            status="completed",
            metadata={
                "profit_pct": self._current_cycle.current_profit_pct,
                "profit_amount": self._current_cycle.current_profit_amount
            }
        )
        self._current_cycle.events.append(eligible_event)

        log.info(f"🎉 SCALING ELIGIBLE! Cycle {self._current_cycle.cycle_id} reached "
                f"{self._current_cycle.current_profit_pct:.2f}% profit")

        # TODO: Trigger notifications (Telegram, email, etc.)

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
                    "scaling_eligible_date": self._current_cycle.scaling_eligible_date.isoformat() if self._current_cycle.scaling_eligible_date else None,
                    "scaling_submitted_date": self._current_cycle.scaling_submitted_date.isoformat() if self._current_cycle.scaling_submitted_date else None,
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
                            "created_at": e.created_at.isoformat()
                        }
                        for e in self._current_cycle.events
                    ]
                },
                "config": {
                    "cycle_length_months": self.config.cycle_length_months,
                    "target_profit_pct": self.config.target_profit_pct,
                    "timezone": self.config.timezone
                },
                "saved_at": datetime.now(ZoneInfo("UTC")).isoformat()
            }

            # Ensure directory exists
            state_file = Path(self.config.state_file_path)
            state_file.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically
            temp_file = state_file.with_suffix('.tmp')
            with temp_file.open('w') as f:
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

            with state_file.open('r') as f:
                state_data = json.load(f)

            cycle_data = state_data.get('cycle')
            if not cycle_data:
                return

            # Reconstruct events
            events = []
            for event_data in cycle_data.get('events', []):
                event = ScalingEvent(
                    event_id=event_data['event_id'],
                    event_type=event_data['event_type'],
                    title=event_data['title'],
                    description=event_data['description'],
                    due_date=datetime.fromisoformat(event_data['due_date']),
                    status=event_data['status'],
                    metadata=event_data['metadata'],
                    created_at=datetime.fromisoformat(event_data['created_at'])
                )
                events.append(event)

            # Reconstruct cycle
            self._current_cycle = ScalingCycle(
                cycle_id=cycle_data['cycle_id'],
                start_date=datetime.fromisoformat(cycle_data['start_date']),
                end_date=datetime.fromisoformat(cycle_data['end_date']),
                starting_equity=cycle_data['starting_equity'],
                target_profit_amount=cycle_data['target_profit_amount'],
                target_profit_pct=cycle_data['target_profit_pct'],
                current_equity=cycle_data['current_equity'],
                peak_equity=cycle_data['peak_equity'],
                scaling_eligible_date=datetime.fromisoformat(cycle_data['scaling_eligible_date']) if cycle_data['scaling_eligible_date'] else None,
                scaling_submitted_date=datetime.fromisoformat(cycle_data['scaling_submitted_date']) if cycle_data['scaling_submitted_date'] else None,
                cycle_status=cycle_data['cycle_status'],
                events=events
            )

            log.info(f"Loaded scaling plan state: cycle {self._current_cycle.cycle_id}")

        except Exception as exc:
            log.error(f"Failed to load scaling plan state: {exc}")
        finally:
            self._state_loaded = True