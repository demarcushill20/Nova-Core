"""Sacred session calendar — defines trading session hours and gaps.

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify these defaults.
Any changes to session boundaries invalidate all prior backtest results
and require full re-evaluation. Version hash tracks configuration identity.

Session calendar governs:
- Trading session definitions (London, NY, Asian, Overlap)
- Weekend boundaries (Friday close → Sunday open)
- Session-aware logic for spread, volume, and gap handling
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum

VERSION: str = "1.0.0"


class Session(Enum):
    """Major forex trading sessions."""

    LONDON = "london"
    NEW_YORK = "new_york"
    ASIAN = "asian"
    OVERLAP_LON_NY = "overlap_lon_ny"


@dataclass(frozen=True)
class SessionCalendarConfig:
    """Immutable session calendar configuration.

    UTC-based session boundaries for major forex sessions.
    Weekend detection uses Friday/Sunday boundaries.
    """

    timezone: str = "UTC"
    """Calendar reference timezone."""

    trading_hours: str = "24/5"
    """Market operating hours (24h, 5 days/week)."""

    london_open_utc: int = 8
    """London session open hour (UTC)."""

    london_close_utc: int = 16
    """London session close hour (UTC)."""

    ny_open_utc: int = 13
    """New York session open hour (UTC)."""

    ny_close_utc: int = 21
    """New York session close hour (UTC)."""

    asian_open_utc: int = 0
    """Asian session open hour (UTC)."""

    asian_close_utc: int = 8
    """Asian session close hour (UTC)."""

    weekend_start_utc_hour: int = 21
    """Friday close hour (UTC) — weekend begins."""

    weekend_end_utc_hour: int = 22
    """Sunday open hour (UTC) — weekend ends."""

    def get_session(self, hour_utc: int) -> Session | None:
        """Determine which session a given UTC hour falls in.

        Overlap takes priority when both London and NY are active.
        Returns None if outside all defined sessions (should not happen
        during forex trading hours, but possible on weekends).

        Args:
            hour_utc: Hour of day in UTC (0-23).

        Returns:
            Session enum value, or None if no session matches.
        """
        london_active = self.london_open_utc <= hour_utc < self.london_close_utc
        ny_active = self.ny_open_utc <= hour_utc < self.ny_close_utc

        if london_active and ny_active:
            return Session.OVERLAP_LON_NY
        if london_active:
            return Session.LONDON
        if ny_active:
            return Session.NEW_YORK
        if self.asian_open_utc <= hour_utc < self.asian_close_utc:
            return Session.ASIAN
        return None

    def is_weekend(self, timestamp: float) -> bool:
        """Check if a Unix timestamp falls within the weekend gap.

        Weekend is defined as Friday after weekend_start_utc_hour
        through Sunday before weekend_end_utc_hour.

        Args:
            timestamp: Unix timestamp (seconds since epoch).

        Returns:
            True if the timestamp is during the weekend gap.
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        weekday = dt.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        hour = dt.hour

        if weekday == 4 and hour >= self.weekend_start_utc_hour:
            return True
        if weekday == 5:
            return True
        return bool(weekday == 6 and hour < self.weekend_end_utc_hour)

    def version_hash(self) -> str:
        """SHA-256 hash of all config fields + VERSION for identity tracking."""
        payload = asdict(self)
        payload["__version__"] = VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


DEFAULT_SESSION_CALENDAR = SessionCalendarConfig()
