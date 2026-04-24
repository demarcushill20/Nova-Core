"""Signal rate monitoring — detects abnormally low signal frequency.

Tracks signal generation rate and provides early warning when the strategy
stops generating expected signals, which could indicate hidden configuration
or data issues even when other health checks pass.

v2: Regime-aware thresholds + session-aware alerting.
  - Reads market regime from STATE/novatrade/regime.json to adjust expectations.
  - Detects forex sessions (London/NY/Asian) and weekend closure.
  - Suppresses false alerts during low-activity periods.

Persists state to STATE/novatrade/signal_stats.json for heartbeat visibility.
"""

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")
SIGNAL_STATS_FILE = STATE_DIR / "signal_stats.json"
REGIME_FILE = STATE_DIR / "regime.json"


# ---------------------------------------------------------------------------
# Forex session definitions (UTC hours)
# ---------------------------------------------------------------------------
# EURUSD activity mapping:
#   London:  07:00-16:00 UTC  (highest for EUR pairs)
#   NY:      12:00-21:00 UTC  (high, overlap 12-16 is peak)
#   Asian:   22:00-07:00 UTC  (low activity for EURUSD)
#   Weekend: Fri 22:00 UTC → Sun 22:00 UTC (market closed)


def get_session(dt: datetime) -> str:
    """Return the current forex session label for the given UTC datetime.

    Returns one of: 'weekend', 'asian', 'london', 'overlap', 'newyork'.
    Overlap = London+NY concurrent hours (12:00-16:00 UTC).
    """
    if is_weekend(dt):
        return "weekend"

    hour = dt.hour
    if 12 <= hour < 16:
        return "overlap"  # London + NY overlap — peak activity
    elif 7 <= hour < 12:
        return "london"  # London morning (pre-NY)
    elif 16 <= hour < 21:
        return "newyork"  # NY afternoon (post-London)
    else:
        return "asian"  # 21:00-07:00 UTC — low activity for EUR


def is_weekend(dt: datetime) -> bool:
    """Check if the forex market is closed (Fri 22:00 UTC → Sun 22:00 UTC)."""
    wd = dt.weekday()  # Mon=0, Sun=6
    hour = dt.hour

    # Friday after 22:00 UTC
    if wd == 4 and hour >= 22:
        return True
    # All of Saturday
    if wd == 5:
        return True
    # Sunday before 22:00 UTC
    return bool(wd == 6 and hour < 22)


# ---------------------------------------------------------------------------
# Regime-aware threshold multipliers
# ---------------------------------------------------------------------------
# Base thresholds (for "ranging" / default regime):
#   4h == 0 signals   → RED
#   4h < 2 & 1h == 0  → YELLOW
#   stale > 8h        → RED
#   stale > 4h        → YELLOW
#
# Multipliers widen/tighten these thresholds.

REGIME_MULTIPLIERS: dict[str, dict] = {
    "quiet": {
        # Low volatility — fewer signals expected
        "stale_red_hours": 16,  # 2x default
        "stale_yellow_hours": 8,  # 2x default
        "low_4h_threshold": 0,  # Don't alert on 0 signals in 4h
        "low_combined_4h": 1,  # Only YELLOW if <1 in 4h
    },
    "ranging": {
        # Default thresholds
        "stale_red_hours": 8,
        "stale_yellow_hours": 4,
        "low_4h_threshold": 0,  # 4h == 0 → RED
        "low_combined_4h": 2,  # 4h < 2 & 1h == 0 → YELLOW
    },
    "trending": {
        # Trending — should see regular signals
        "stale_red_hours": 6,
        "stale_yellow_hours": 3,
        "low_4h_threshold": 0,
        "low_combined_4h": 3,
    },
    "volatile": {
        # High volatility — expect many signals, tighter thresholds
        "stale_red_hours": 4,
        "stale_yellow_hours": 2,
        "low_4h_threshold": 1,  # Even 1 signal in 4h is suspicious
        "low_combined_4h": 4,
    },
}

# Session leniency — how much to relax thresholds per session
SESSION_LENIENCY: dict[str, float] = {
    "weekend": 0.0,  # Market closed — all alerts suppressed
    "asian": 0.5,  # 50% expected activity for EURUSD
    "london": 1.0,  # Full expectations
    "overlap": 1.2,  # Peak — slightly tighter
    "newyork": 0.9,  # Slightly below London
}


@dataclass
class SignalRateStats:
    """Signal rate statistics and health assessment."""

    # Current window stats
    signals_1h: int = 0
    signals_4h: int = 0
    signals_24h: int = 0

    # Last activity
    last_signal_at: str = ""
    last_trade_signal_at: str = ""  # excludes flat/no_action signals

    # Health status
    status: str = "UNKNOWN"  # OK, LOW_RATE, STALE, NO_DATA, MARKET_CLOSED
    concern_level: str = "GREEN"  # GREEN, YELLOW, RED

    # Context
    regime: str = ""  # quiet, ranging, trending, volatile
    session: str = ""  # weekend, asian, london, overlap, newyork

    # Metadata
    strategy: str = "IRB"
    timeframe: str = "M5"
    symbol: str = "EURUSD"
    updated_at: str = ""


def _load_regime() -> str:
    """Read current regime from persisted state. Returns 'ranging' as default."""
    try:
        if REGIME_FILE.exists():
            age_min = (time.time() - REGIME_FILE.stat().st_mtime) / 60.0
            if age_min > 30:  # stale regime — fall back to default
                return "ranging"
            data = json.loads(REGIME_FILE.read_text())
            regime = data.get("regime", "ranging")
            if regime in REGIME_MULTIPLIERS:
                return regime
    except (json.JSONDecodeError, OSError):
        pass
    return "ranging"


class SignalRateMonitor:
    """Monitor signal generation rates and detect anomalies."""

    def __init__(self, strategy: str = "IRB", symbol: str = "EURUSD"):
        self.strategy = strategy
        self.symbol = symbol
        self.signal_timestamps: list[datetime] = []
        self.trade_signal_timestamps: list[datetime] = []

    def record_signal(self, signal_type: str = "any") -> None:
        """Record a signal generation event.

        Args:
            signal_type: 'trade' for actual buy/sell signals, 'any' for all signals including flat
        """
        now = datetime.now(timezone.utc)
        self.signal_timestamps.append(now)

        if signal_type == "trade":
            self.trade_signal_timestamps.append(now)

        # Keep only recent signals to prevent memory growth
        cutoff = now - timedelta(days=1)
        self.signal_timestamps = [ts for ts in self.signal_timestamps if ts > cutoff]
        self.trade_signal_timestamps = [ts for ts in self.trade_signal_timestamps if ts > cutoff]

    def get_stats(self, now: datetime | None = None) -> SignalRateStats:
        """Calculate current signal rate statistics.

        Args:
            now: Override current time (for testing). Defaults to UTC now.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # Count signals in time windows
        h1_cutoff = now - timedelta(hours=1)
        h4_cutoff = now - timedelta(hours=4)
        h24_cutoff = now - timedelta(hours=24)

        signals_1h = len([ts for ts in self.signal_timestamps if ts > h1_cutoff])
        signals_4h = len([ts for ts in self.signal_timestamps if ts > h4_cutoff])
        signals_24h = len([ts for ts in self.signal_timestamps if ts > h24_cutoff])

        # Last signal times
        last_signal_at = ""
        last_trade_signal_at = ""

        if self.signal_timestamps:
            last_signal_at = max(self.signal_timestamps).isoformat()

        if self.trade_signal_timestamps:
            last_trade_signal_at = max(self.trade_signal_timestamps).isoformat()

        # Load context
        regime = _load_regime()
        session = get_session(now)

        # Health assessment with regime + session context
        status, concern_level = self._assess_health(signals_1h, signals_4h, signals_24h, now, regime, session)

        stats = SignalRateStats(
            signals_1h=signals_1h,
            signals_4h=signals_4h,
            signals_24h=signals_24h,
            last_signal_at=last_signal_at,
            last_trade_signal_at=last_trade_signal_at,
            status=status,
            concern_level=concern_level,
            regime=regime,
            session=session,
            strategy=self.strategy,
            symbol=self.symbol,
            updated_at=now.isoformat(),
        )

        # Persist for heartbeat visibility
        self._persist_stats(stats)
        return stats

    def _assess_health(
        self,
        signals_1h: int,
        signals_4h: int,
        signals_24h: int,
        now: datetime,
        regime: str = "ranging",
        session: str = "london",
    ) -> tuple[str, str]:
        """Assess signal rate health with regime and session awareness.

        Returns (status, concern_level).
        """
        # Weekend — market is closed, no signals expected
        if session == "weekend":
            return "MARKET_CLOSED", "GREEN"

        # Get regime-specific thresholds
        thresholds = REGIME_MULTIPLIERS.get(regime, REGIME_MULTIPLIERS["ranging"])
        leniency = SESSION_LENIENCY.get(session, 1.0)

        # Apply session leniency to stale thresholds (wider during Asian)
        stale_red_h = thresholds["stale_red_hours"] / max(leniency, 0.1)
        stale_yellow_h = thresholds["stale_yellow_hours"] / max(leniency, 0.1)

        # Check for complete absence of signals
        if signals_24h == 0:
            if not self.signal_timestamps:
                return "NO_DATA", "YELLOW"  # Fresh start
            else:
                # Get time since last signal
                last_signal = max(self.signal_timestamps)
                hours_since = (now - last_signal).total_seconds() / 3600
                if hours_since > stale_red_h:
                    return "STALE", "RED"
                elif hours_since > stale_yellow_h:
                    return "STALE", "YELLOW"

        # Apply session leniency to rate thresholds
        # During Asian session, we expect ~50% of normal rates
        effective_4h_red = thresholds["low_4h_threshold"]
        effective_4h_yellow = max(1, int(thresholds["low_combined_4h"] * leniency))

        # Red: signals below the red threshold for the 4h window
        if signals_4h <= effective_4h_red:
            # During Asian session with quiet regime, downgrade from RED to YELLOW
            if session == "asian" and regime == "quiet":
                return "LOW_RATE", "YELLOW"
            return "LOW_RATE", "RED"

        # Yellow: low signals combined check
        if signals_4h < effective_4h_yellow and signals_1h == 0:
            return "LOW_RATE", "YELLOW"

        return "OK", "GREEN"

    def _persist_stats(self, stats: SignalRateStats) -> None:
        """Atomically write signal stats to disk."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(stats), f, indent=2)
            os.replace(tmp, str(SIGNAL_STATS_FILE))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def load_signal_stats() -> SignalRateStats | None:
    """Load the last persisted signal stats."""
    try:
        if SIGNAL_STATS_FILE.exists():
            data = json.loads(SIGNAL_STATS_FILE.read_text())
            return SignalRateStats(**{k: v for k, v in data.items() if k in SignalRateStats.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError, OSError):
        pass
    return None


# Global instance for easy integration
_global_monitor = SignalRateMonitor()


def record_signal(signal_type: str = "any") -> None:
    """Convenience function to record a signal with the global monitor."""
    _global_monitor.record_signal(signal_type)


def get_current_stats() -> SignalRateStats:
    """Get current signal rate statistics from the global monitor."""
    return _global_monitor.get_stats()
