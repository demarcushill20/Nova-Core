"""P4: Enhanced Session-Aware Risk Management

Extends basic forex session checking to focus on optimal trading sessions,
particularly London-NY overlap for maximum liquidity and minimal spreads.

This module implements session-aware risk filtering that goes beyond simple
weekend detection to include:
- London-NY overlap prioritization (13-16 UTC)
- Session quality scoring
- Configurable session preferences
- Integration with existing risk engine

Priority ranking (best to worst):
1. London-NY Overlap (13-16 UTC) — peak liquidity, minimal spreads
2. London session (8-13 UTC) — good volume, EU market open
3. New York session (16-21 UTC) — good volume, US market open
4. Asian session (0-8 UTC) — lower volume but can be acceptable
5. Weekend (Fri 21+ UTC, all Saturday, Sun <22 UTC) — market closed

FTMO compliance: Session filtering reduces execution risk by avoiding
thin liquidity periods that can cause slippage and poor fills.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

from novatrade.evaluation.session_calendar import DEFAULT_SESSION_CALENDAR, Session


class SessionQuality(Enum):
    """Session quality ranking for trade execution."""

    PREMIUM = "premium"  # London-NY overlap — peak liquidity
    GOOD = "good"  # Major single sessions
    ACCEPTABLE = "acceptable"  # Asian session
    POOR = "poor"  # Session transitions
    CLOSED = "closed"  # Weekend/market closed


class SessionInfo(NamedTuple):
    """Session classification result."""

    session: Session | None
    quality: SessionQuality
    reason: str
    hour_utc: int


@dataclass
class SessionAwareConfig:
    """Configuration for session-aware risk filtering."""

    # Session preferences
    allow_overlap_only: bool = False  # Only trade during London-NY overlap
    allow_premium_sessions: bool = True  # Allow overlap + major sessions
    allow_asian_session: bool = True  # Allow Asian session trading
    block_transition_periods: bool = False  # Block 30min before/after major session changes

    # Quality thresholds
    minimum_quality: SessionQuality = SessionQuality.ACCEPTABLE

    # Custom session hours (UTC) — None uses defaults from session_calendar
    custom_overlap_start: int | None = None
    custom_overlap_end: int | None = None


class SessionAwareFilter:
    """Enhanced session-aware risk filter with London-NY overlap focus."""

    def __init__(self, config: SessionAwareConfig | None = None):
        """Initialize session filter with configuration.

        Args:
            config: Session filtering configuration. Uses defaults if None.
        """
        self._config = config or SessionAwareConfig()
        self._calendar = DEFAULT_SESSION_CALENDAR

    def check_session(self, timestamp: dt.datetime | None = None) -> SessionInfo:
        """Check if the current/given time is suitable for trading.

        Args:
            timestamp: UTC datetime to check. Uses current time if None.

        Returns:
            SessionInfo with session classification and quality assessment.
        """
        if timestamp is None:
            timestamp = dt.datetime.now(dt.timezone.utc)

        # Check weekend first
        if self._calendar.is_weekend(timestamp.timestamp()):
            return SessionInfo(
                session=None,
                quality=SessionQuality.CLOSED,
                reason=f"Forex market closed (weekend): {timestamp.strftime('%A %H:%M UTC')}",
                hour_utc=timestamp.hour,
            )

        hour_utc = timestamp.hour
        session = self._calendar.get_session(hour_utc)

        # Determine session quality and suitability
        quality, reason = self._assess_session_quality(session, hour_utc, timestamp)

        return SessionInfo(session=session, quality=quality, reason=reason, hour_utc=hour_utc)

    def is_trading_allowed(self, timestamp: dt.datetime | None = None) -> bool:
        """Check if trading is allowed at the given time.

        Args:
            timestamp: UTC datetime to check. Uses current time if None.

        Returns:
            True if trading is allowed based on session configuration.
        """
        info = self.check_session(timestamp)

        # Always block closed market
        if info.quality == SessionQuality.CLOSED:
            return False

        # Apply configuration filters
        if self._config.allow_overlap_only:
            return info.session == Session.OVERLAP_LON_NY

        # Check minimum quality threshold
        quality_levels = [
            SessionQuality.CLOSED,  # 0 - never allowed
            SessionQuality.POOR,  # 1 - rarely allowed
            SessionQuality.ACCEPTABLE,  # 2 - default minimum
            SessionQuality.GOOD,  # 3 - preferred
            SessionQuality.PREMIUM,  # 4 - best
        ]

        current_level = quality_levels.index(info.quality)
        min_level = quality_levels.index(self._config.minimum_quality)

        return current_level >= min_level

    def _assess_session_quality(
        self, session: Session | None, hour_utc: int, timestamp: dt.datetime
    ) -> tuple[SessionQuality, str]:
        """Assess the quality of the current session for trading.

        Args:
            session: Current session from session calendar.
            hour_utc: Current UTC hour.
            timestamp: Full timestamp for detailed analysis.

        Returns:
            (quality, reason) tuple explaining the assessment.
        """
        if session is None:
            # Gap between NY close (21 UTC) and Asian open (0 UTC).
            # Market is still open on weekdays — lower volume but tradeable.
            # Weekend handling is done upstream in check_session().
            return (SessionQuality.ACCEPTABLE, f"Off-session hours ({hour_utc:02d}:00 UTC) — lower volume, market open")

        # London-NY overlap is premium (highest liquidity, tightest spreads)
        if session == Session.OVERLAP_LON_NY:
            return (SessionQuality.PREMIUM, f"London-NY overlap active ({hour_utc:02d}:00 UTC) — peak liquidity")

        # Major single sessions are good
        if session in (Session.LONDON, Session.NEW_YORK):
            session_name = "London" if session == Session.LONDON else "New York"
            return (SessionQuality.GOOD, f"{session_name} session active ({hour_utc:02d}:00 UTC) — good liquidity")

        # Asian session is acceptable but lower volume
        if session == Session.ASIAN:
            if not self._config.allow_asian_session:
                return (SessionQuality.POOR, f"Asian session ({hour_utc:02d}:00 UTC) — disabled by config")
            return (SessionQuality.ACCEPTABLE, f"Asian session active ({hour_utc:02d}:00 UTC) — lower volume")

        # Fallback for any other session state
        return (SessionQuality.POOR, f"Unknown session state: {session} at {hour_utc:02d}:00 UTC")

    def get_next_optimal_session(self, timestamp: dt.datetime | None = None) -> SessionInfo:
        """Find the next optimal trading session.

        Args:
            timestamp: UTC datetime to start search from. Uses current time if None.

        Returns:
            SessionInfo for the next premium or good quality session.
        """
        if timestamp is None:
            timestamp = dt.datetime.now(dt.timezone.utc)

        # Search up to 48 hours ahead (2 full days)
        for hours_ahead in range(48):
            check_time = timestamp + dt.timedelta(hours=hours_ahead)
            info = self.check_session(check_time)

            # Return first premium or good session
            if info.quality in (SessionQuality.PREMIUM, SessionQuality.GOOD):
                if hours_ahead == 0:
                    info = info._replace(reason=f"Currently in {info.reason}")
                else:
                    hours_text = "1 hour" if hours_ahead == 1 else f"{hours_ahead} hours"
                    info = info._replace(reason=f"Next optimal session in {hours_text}: {info.reason}")
                return info

        # Fallback if no good session found (shouldn't happen)
        return SessionInfo(
            session=None,
            quality=SessionQuality.POOR,
            reason="No optimal session found in next 48 hours",
            hour_utc=timestamp.hour,
        )


def create_london_ny_focus_filter() -> SessionAwareFilter:
    """Create a session filter optimized for London-NY overlap focus.

    This is the recommended configuration for FTMO compliance and execution quality.
    Allows all major sessions but prioritizes London-NY overlap.

    Returns:
        SessionAwareFilter configured for optimal execution quality.
    """
    config = SessionAwareConfig(
        allow_overlap_only=False,  # Don't restrict to overlap only
        allow_premium_sessions=True,  # Allow all premium sessions
        allow_asian_session=True,  # Allow Asian but it's lower quality
        minimum_quality=SessionQuality.ACCEPTABLE,  # Accept Asian if needed
        block_transition_periods=False,  # Don't block transitions for now
    )
    return SessionAwareFilter(config)


def create_overlap_only_filter() -> SessionAwareFilter:
    """Create a highly selective session filter for overlap-only trading.

    This configuration only allows trading during London-NY overlap (13-16 UTC)
    for maximum execution quality. Use for high-frequency strategies or when
    execution quality is critical.

    Returns:
        SessionAwareFilter that only allows London-NY overlap trading.
    """
    config = SessionAwareConfig(
        allow_overlap_only=True,
        minimum_quality=SessionQuality.PREMIUM,
    )
    return SessionAwareFilter(config)
