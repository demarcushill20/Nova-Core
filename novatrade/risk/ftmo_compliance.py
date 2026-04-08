"""FTMO compliance enforcement for NovaTrade.

Implements CRITICAL FTMO rules that are not covered by the generic
pre-trade gate:

1. **Lot-Size Consistency** — FTMO prohibits "substantially larger" position
   sizes relative to your own trading history.  We track trailing lot sizes
   and reject orders that deviate beyond a configurable band.

2. **Server Request Counter** — FTMO prohibits >2,000 server requests/day
   on trade/pending-order operations.  We enforce a hard ceiling at 1,500
   (75% of limit) with an alert threshold at 1,000.

3. **Minimum Trading Days Tracker** — FTMO requires trading on at least N
   unique calendar days during the challenge period (typically 4 days for
   FTMO Challenge/Verification).  We track unique trading dates and expose
   progress via an informational check.

4. **Daily Loss Tracker** — FTMO Maximum Daily Loss rule: the daily loss
   (measured from the higher of balance/equity at the start of the day)
   must not reach 5% of the initial account size.  We enforce a configurable
   buffer (default 80%) that blocks new orders before the hard limit.

5. **Weekend Auto-Closer** — Signals position closure before Friday 22:00 UTC
   market close to avoid weekend gap risk.  Close buffer defaults to 5 min.

6. **News Calendar Blocker** — Blocks trading around high-impact news events
   (NFP, FOMC, ECB).  Uses a static 2026 calendar (MVP — API-based later).

7. **Gap Trading Preventer** — Blocks orders when spread is abnormally wide
   relative to session average (gap/spike detection).

8. **Best Day Rule Tracker** — Monitors whether any single day's profit
   exceeds a percentage of total profit (FTMO best-day rule).  Now wired
   into PreTradeGate for enforcement (50% threshold).

9. **Weekly Loss Tracker** — Rolling 7-day cumulative loss tracker.  Blocks
   new entries when weekly losses reach 5% of initial account (buffer at 80%).

10. **SL Modification Counter** — Tracks per-trade and per-day SL modification
    counts.  Prevents EA detection fingerprinting from excessive trailing-stop
    updates.

All components are stateful (in-memory with optional JSON persistence)
and designed to integrate into the existing PreTradeGate pipeline.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import NamedTuple
from zoneinfo import ZoneInfo

from novatrade.models import RiskCheckResult

_FTMO_TZ = ZoneInfo("Europe/Prague")

log = logging.getLogger("novatrade.risk.ftmo_compliance")

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_STATE_DIR = _BASE_DIR / "STATE" / "novatrade"

# ---------------------------------------------------------------------------
# Lot-Size Consistency
# ---------------------------------------------------------------------------

# Defaults
_DEFAULT_WINDOW = 20  # trailing trades to consider
_DEFAULT_MAX_DEVIATION = 3.0  # max multiplier of median
_DEFAULT_MIN_TRADES = 3  # need at least N trades before enforcing


class LotRecord(NamedTuple):
    """A single lot-size observation."""

    timestamp: float
    volume: float
    symbol: str


@dataclass
class LotSizeConsistencyChecker:
    """Tracks historical lot sizes and rejects outliers.

    FTMO rule: no "substantially larger position sizes compared to your
    other simulated trades."  We define "substantially larger" as exceeding
    ``max_deviation_factor`` × median of the trailing ``window_size`` trades.
    """

    window_size: int = _DEFAULT_WINDOW
    max_deviation_factor: float = _DEFAULT_MAX_DEVIATION
    min_trades_for_enforcement: int = _DEFAULT_MIN_TRADES
    _history: deque[LotRecord] = field(default_factory=lambda: deque(maxlen=_DEFAULT_WINDOW))

    def __post_init__(self) -> None:
        # Ensure deque has the right maxlen
        if self._history.maxlen != self.window_size:
            old = list(self._history)
            self._history = deque(old, maxlen=self.window_size)

    def record(self, volume: float, symbol: str = "") -> None:
        """Record a completed trade's lot size."""
        self._history.append(LotRecord(time.time(), volume, symbol))

    def check(self, proposed_volume: float) -> RiskCheckResult:
        """Check if proposed volume is consistent with recent history."""
        if len(self._history) < self.min_trades_for_enforcement:
            return RiskCheckResult(
                name="lot_consistency",
                passed=True,
                detail=f"only {len(self._history)} trades in history "
                f"(need {self.min_trades_for_enforcement} for enforcement)",
            )

        volumes = [r.volume for r in self._history]
        med = median(volumes)

        if med <= 0:
            return RiskCheckResult(
                name="lot_consistency",
                passed=True,
                detail="median volume is 0 — skipped",
            )

        # DEFENSIVE: Check for stale historical data first (older than 30 days)
        now = time.time()
        recent_history = [r for r in self._history if (now - r.timestamp) < (30 * 24 * 3600)]

        if len(recent_history) < self.min_trades_for_enforcement:
            log.info(
                "LotSizeConsistencyChecker: Only %d recent trades (<%d days old), "
                "insufficient for enforcement. Total history: %d",
                len(recent_history),
                30,
                len(self._history),
            )
            return RiskCheckResult(
                name="lot_consistency",
                passed=True,
                detail=f"only {len(recent_history)} recent trades in last 30 days "
                f"(need {self.min_trades_for_enforcement} for enforcement)",
            )

        # Recalculate median with only recent data
        recent_volumes = [r.volume for r in recent_history]

        # DEFENSIVE: Handle empty recent_volumes (shouldn't happen due to checks above, but be safe)
        if not recent_volumes:
            return RiskCheckResult(
                name="lot_consistency",
                passed=True,
                detail="no recent volumes found — skipped enforcement",
            )

        med = median(recent_volumes)

        if med <= 0:
            return RiskCheckResult(
                name="lot_consistency",
                passed=True,
                detail="recent median volume is 0 — skipped",
            )

        # DEFENSIVE: Check for suspected test/demo data contamination in production.
        # Only bypass in extremely suspicious cases: tiny median (≤0.15) with
        # all identical values AND extreme ratio (≥10x), which typically indicates
        # demo/test lot sizes (0.01, 0.10) contaminating production trading of
        # standard lots (1.00+). Real trading has natural lot size variation.
        ratio = proposed_volume / med
        if med <= 0.15 and ratio >= 10.0 and len(set(recent_volumes)) == 1 and len(recent_volumes) >= self.window_size:
            log.warning(
                "LotSizeConsistencyChecker: Detected suspected demo/test data contamination — "
                "%d identical lots of %.2f blocking production %.2f lot (%.1fx ratio). "
                "Allowing trade but investigate lot history. Recent: %s",
                len(recent_volumes),
                med,
                proposed_volume,
                ratio,
                recent_volumes,
            )
            return RiskCheckResult(
                name="lot_consistency",
                passed=True,
                detail=(
                    f"suspected demo data: median={med:.2f} "
                    f"({len(recent_volumes)} identical) vs {proposed_volume:.2f} "
                    f"(ratio={ratio:.1f}x) — allowing with warning"
                ),
            )

        # ratio already calculated above for defensive logic

        # Add detailed logging for debugging
        log.info(
            "LotSizeConsistencyChecker: proposed=%.2f, median=%.2f (from %d recent volumes), ratio=%.1fx",
            proposed_volume,
            med,
            len(recent_volumes),
            ratio,
        )

        if ratio > self.max_deviation_factor:
            return RiskCheckResult(
                name="lot_consistency",
                passed=False,
                detail=f"volume {proposed_volume:.2f} is {ratio:.1f}x median "
                f"({med:.2f}) — exceeds {self.max_deviation_factor:.1f}x limit "
                f"(FTMO lot-size consistency rule)",
            )

        # Also check if volume is too small (< 1/max_deviation_factor of median)
        min_ratio = 1.0 / self.max_deviation_factor
        if ratio < min_ratio:
            return RiskCheckResult(
                name="lot_consistency",
                passed=False,
                detail=f"volume {proposed_volume:.2f} is {ratio:.2f}x median "
                f"({med:.2f}) — below {min_ratio:.2f}x floor "
                f"(FTMO lot-size consistency rule)",
            )

        return RiskCheckResult(
            name="lot_consistency",
            passed=True,
            detail=f"volume={proposed_volume:.2f}, median={med:.2f}, "
            f"ratio={ratio:.2f} (limit={self.max_deviation_factor:.1f}x)",
        )

    @property
    def current_median(self) -> float | None:
        """Return current median lot size, or None if insufficient data."""
        if len(self._history) < self.min_trades_for_enforcement:
            return None

        # Use same defensive logic as check() - only consider recent data (last 30 days)
        now = time.time()
        recent_history = [r for r in self._history if (now - r.timestamp) < (30 * 24 * 3600)]

        if len(recent_history) < self.min_trades_for_enforcement:
            return None

        return median(r.volume for r in recent_history)

    @property
    def trade_count(self) -> int:
        return len(self._history)

    @property
    def rolling_average(self) -> float | None:
        """Return rolling average lot size, or None if no trades recorded."""
        if not self._history:
            return None
        return sum(r.volume for r in self._history) / len(self._history)

    def report(self) -> dict:
        """Return a monitoring-friendly snapshot of lot-size metrics.

        Includes median, rolling average, min, max, count, and the
        deviation band that will trigger rejection.
        """
        if not self._history:
            return {
                "trade_count": 0,
                "median": None,
                "rolling_average": None,
                "min": None,
                "max": None,
                "deviation_band_low": None,
                "deviation_band_high": None,
                "enforcement_active": False,
            }

        volumes = [r.volume for r in self._history]
        med = median(volumes)
        avg = sum(volumes) / len(volumes)

        return {
            "trade_count": len(volumes),
            "median": round(med, 4),
            "rolling_average": round(avg, 4),
            "min": round(min(volumes), 4),
            "max": round(max(volumes), 4),
            "deviation_band_low": round(med / self.max_deviation_factor, 4) if med > 0 else None,
            "deviation_band_high": round(med * self.max_deviation_factor, 4) if med > 0 else None,
            "enforcement_active": len(volumes) >= self.min_trades_for_enforcement,
        }

    def save_state(self, path: Path | None = None) -> None:
        """Persist lot history to JSON for crash recovery."""
        path = path or (_STATE_DIR / "lot_history.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"ts": r.timestamp, "vol": r.volume, "sym": r.symbol} for r in self._history]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_state(self, path: Path | None = None) -> int:
        """Load lot history from JSON.  Returns count loaded."""
        path = path or (_STATE_DIR / "lot_history.json")
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data:
                self._history.append(LotRecord(entry["ts"], entry["vol"], entry.get("sym", "")))
            return len(data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load lot history from %s: %s", path, exc)
            return 0


# ---------------------------------------------------------------------------
# Server Request Counter
# ---------------------------------------------------------------------------

_FTMO_DAILY_LIMIT = 2000
_DEFAULT_HARD_CEILING = 1500  # 75% of limit
_DEFAULT_ALERT_THRESHOLD = 1000  # 50% of limit


@dataclass
class ServerRequestCounter:
    """Tracks daily server request count against FTMO's 2,000/day limit.

    FTMO rule: >2,000 server requests/day on trades/pending orders is
    prohibited.  We enforce a hard ceiling at 75% (1,500) and alert at
    50% (1,000).

    "Server requests" = order open, modify (SL/TP), close, pending order
    operations.  Position reads do NOT count (they go through the
    adapter infrastructure, not directly to FTMO MT5 as order messages).
    """

    hard_ceiling: int = _DEFAULT_HARD_CEILING
    alert_threshold: int = _DEFAULT_ALERT_THRESHOLD
    # Internal state
    _count: int = field(default=0, repr=False)
    _day_key: str = field(default="", repr=False)
    _log: list[tuple[float, str]] = field(default_factory=list, repr=False)

    def _ensure_day(self) -> None:
        """Reset counter on new trading day (Europe/Prague, DST-safe)."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        if today != self._day_key:
            if self._day_key:
                log.info(
                    "FTMO request counter reset: %s had %d requests",
                    self._day_key,
                    self._count,
                )
            self._day_key = today
            self._count = 0
            self._log.clear()

    def record(self, operation: str) -> None:
        """Record an order-related server request.

        Args:
            operation: Description of the operation (e.g., "order_open",
                "modify_sl", "close_position", "pending_create").
        """
        self._ensure_day()
        self._count += 1
        self._log.append((time.time(), operation))
        log.debug("FTMO request %d/%d: %s", self._count, self.hard_ceiling, operation)

        if self._count == self.alert_threshold:
            log.warning(
                "FTMO request counter at ALERT threshold: %d/%d (hard ceiling %d)",
                self._count,
                _FTMO_DAILY_LIMIT,
                self.hard_ceiling,
            )

    def check(self) -> RiskCheckResult:
        """Check if we've hit the daily request ceiling."""
        self._ensure_day()

        if self._count >= self.hard_ceiling:
            return RiskCheckResult(
                name="server_request_limit",
                passed=False,
                detail=f"daily request count {self._count} >= hard ceiling "
                f"{self.hard_ceiling} (FTMO limit: {_FTMO_DAILY_LIMIT})",
            )

        return RiskCheckResult(
            name="server_request_limit",
            passed=True,
            detail=f"requests_today={self._count}, ceiling={self.hard_ceiling}, ftmo_limit={_FTMO_DAILY_LIMIT}",
        )

    @property
    def count(self) -> int:
        self._ensure_day()
        return self._count

    @property
    def remaining(self) -> int:
        self._ensure_day()
        return max(0, self.hard_ceiling - self._count)

    @property
    def at_alert(self) -> bool:
        self._ensure_day()
        return self._count >= self.alert_threshold

    @property
    def at_ceiling(self) -> bool:
        self._ensure_day()
        return self._count >= self.hard_ceiling

    def save_state(self, path: Path | None = None) -> None:
        """Persist counter state for crash recovery."""
        self._ensure_day()
        path = path or (_STATE_DIR / "request_counter.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "day": self._day_key,
            "count": self._count,
            "hard_ceiling": self.hard_ceiling,
            "alert_threshold": self.alert_threshold,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_state(self, path: Path | None = None) -> bool:
        """Load counter state.  Returns True if loaded successfully."""
        path = path or (_STATE_DIR / "request_counter.json")
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
            if data.get("day") == today:
                self._day_key = today
                self._count = data.get("count", 0)
                log.info("Loaded request counter: %d requests for %s", self._count, today)
                return True
            # Different day — start fresh
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load request counter from %s: %s", path, exc)
            return False


# ---------------------------------------------------------------------------
# Minimum Trading Days Tracker
# ---------------------------------------------------------------------------

_DEFAULT_MIN_TRADING_DAYS = 4  # FTMO Challenge / Verification requirement


@dataclass
class TradingDaysTracker:
    """Tracks unique calendar days on which trades were executed.

    FTMO rule: traders must trade on at least N distinct calendar days
    during the challenge / verification period.  Typically 4 days for
    FTMO Challenge and Verification phases.

    This is primarily **informational** — it does not block trades but
    provides a check method that signals whether the minimum has been met
    and how many more days are needed.
    """

    min_days_required: int = _DEFAULT_MIN_TRADING_DAYS
    challenge_start: str = ""  # ISO date, e.g. "2026-03-01"
    challenge_end: str = ""  # ISO date, e.g. "2026-03-31"
    _trading_dates: set[str] = field(default_factory=set)

    def record_trade_day(self, date_str: str | None = None) -> None:
        """Record that a trade was placed on a given date.

        Args:
            date_str: ISO date string (YYYY-MM-DD).  Defaults to today UTC.
        """
        if date_str is None:
            date_str = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        self._trading_dates.add(date_str)

    @property
    def days_traded(self) -> int:
        """Number of unique calendar days traded."""
        return len(self._trading_dates)

    @property
    def days_remaining(self) -> int:
        """How many more unique trading days are needed (0 if met)."""
        return max(0, self.min_days_required - self.days_traded)

    @property
    def requirement_met(self) -> bool:
        """Whether the minimum trading days requirement has been met."""
        return self.days_traded >= self.min_days_required

    @property
    def trading_dates(self) -> list[str]:
        """Sorted list of unique trading dates."""
        return sorted(self._trading_dates)

    def check(self) -> RiskCheckResult:
        """Informational check on minimum trading days progress.

        Always passes (informational only) — the detail string shows
        progress toward the minimum.
        """
        met = self.days_traded >= self.min_days_required

        if met:
            return RiskCheckResult(
                name="min_trading_days",
                passed=True,
                detail=f"requirement met: {self.days_traded}/{self.min_days_required} unique days traded",
            )

        return RiskCheckResult(
            name="min_trading_days",
            passed=True,  # informational — never blocks
            detail=f"progress: {self.days_traded}/{self.min_days_required} "
            f"unique days traded — need {self.days_remaining} more day(s)",
        )

    def save_state(self, path: Path | None = None) -> None:
        """Persist trading days to JSON for crash recovery."""
        path = path or (_STATE_DIR / "trading_days.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "min_days_required": self.min_days_required,
            "challenge_start": self.challenge_start,
            "challenge_end": self.challenge_end,
            "trading_dates": sorted(self._trading_dates),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_state(self, path: Path | None = None) -> bool:
        """Load trading days from JSON.  Returns True if loaded."""
        path = path or (_STATE_DIR / "trading_days.json")
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            dates = data.get("trading_dates", [])
            self._trading_dates = set(dates)
            # Optionally restore challenge bounds
            if data.get("challenge_start"):
                self.challenge_start = data["challenge_start"]
            if data.get("challenge_end"):
                self.challenge_end = data["challenge_end"]
            log.info(
                "Loaded trading days tracker: %d days traded (%d required)",
                self.days_traded,
                self.min_days_required,
            )
            return True
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load trading days from %s: %s", path, exc)
            return False


# ---------------------------------------------------------------------------
# FTMO Daily Loss Tracker
# ---------------------------------------------------------------------------

# FTMO Maximum Daily Loss = 5% of initial account balance.
# We use multiple protection levels:
# - 70% early warning: alert and reduce position sizing
# - 80% buffer: block new orders before hard breach
# - 100% hard limit: emergency halt
_DEFAULT_DAILY_LOSS_PCT = 5.0  # FTMO standard
_DEFAULT_BUFFER_PCT = 80.0  # block new orders at 80% of daily limit
_DEFAULT_WARNING_PCT = 70.0  # early warning for drawdown scaling


@dataclass
class FtmoDailyLossTracker:
    """Tracks intraday loss against the FTMO Maximum Daily Loss rule.

    FTMO rule: "The Maximum Daily Loss is 5% of the initial account balance.
    All results plus commissions and swaps for the day must not reach this."

    Reference point: the **higher** of (balance, equity) at the start of
    each trading day (midnight Europe/Prague, DST-safe).

    Formula::

        daily_limit     = initial_account_size × (daily_loss_pct / 100)
        day_reference   = max(balance_at_day_start, equity_at_day_start)
        current_loss    = day_reference − current_equity
        buffer_limit    = daily_limit × (buffer_pct / 100)

        DENY if current_loss >= buffer_limit   (pre-trade gate)
        HALT if current_loss >= daily_limit     (hard supervisor territory)

    The pre-trade gate uses the buffer to block new orders before the hard
    limit is breached, leaving margin for floating P&L movement.

    Args:
        initial_account_size: The nominal initial balance (e.g. 10_000, 100_000).
        daily_loss_pct: Maximum daily loss as % of initial account (default 5.0).
        buffer_pct: Percentage of daily limit at which to deny new orders
            (default 80.0, i.e. deny at 80% of the 5% limit = 4% of account).
    """

    initial_account_size: float = 0.0
    daily_loss_pct: float = _DEFAULT_DAILY_LOSS_PCT
    buffer_pct: float = _DEFAULT_BUFFER_PCT
    warning_pct: float = _DEFAULT_WARNING_PCT  # 70% early warning threshold
    # Internal state
    _day_key: str = field(default="", repr=False)
    _day_reference: float = field(default=0.0, repr=False)
    _peak_equity_today: float = field(default=0.0, repr=False)
    _initialized: bool = field(default=False, repr=False)

    # -- Derived limits ------------------------------------------------

    @property
    def daily_limit_usd(self) -> float:
        """Absolute daily loss limit in USD."""
        return self.initial_account_size * (self.daily_loss_pct / 100.0)

    @property
    def buffer_limit_usd(self) -> float:
        """Pre-trade buffer: deny new orders above this loss amount."""
        return self.daily_limit_usd * (self.buffer_pct / 100.0)

    @property
    def warning_limit_usd(self) -> float:
        """Early warning threshold: trigger drawdown scaling and alerts."""
        return self.daily_limit_usd * (self.warning_pct / 100.0)

    @property
    def day_reference(self) -> float:
        """The day's starting reference point (max of balance, equity)."""
        return self._day_reference

    @property
    def peak_equity_today(self) -> float:
        """Highest equity seen so far today."""
        return self._peak_equity_today

    # -- Initialization ------------------------------------------------

    def initialize(self, balance: float, equity: float) -> None:
        """Set the initial account size and first day reference.

        Call once at session start (typically from build_stack / build_live_stack).
        """
        if self.initial_account_size <= 0:
            self.initial_account_size = balance
        self._set_day_reference(balance, equity)
        self._initialized = True
        log.info(
            "FtmoDailyLossTracker initialized: account=$%.2f daily_limit=$%.2f buffer=$%.2f (%.0f%%) day_ref=$%.2f",
            self.initial_account_size,
            self.daily_limit_usd,
            self.buffer_limit_usd,
            self.buffer_pct,
            self._day_reference,
        )

    def _set_day_reference(self, balance: float, equity: float) -> None:
        """Set the day reference to the higher of balance and equity."""
        self._day_reference = max(balance, equity)
        self._peak_equity_today = max(balance, equity)
        self._day_key = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")

    # -- Day boundary management ---------------------------------------

    def _maybe_new_day(self, balance: float, equity: float) -> None:
        """Reset reference on day boundary (Europe/Prague)."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        if today != self._day_key:
            prev_ref = self._day_reference
            self._set_day_reference(balance, equity)
            log.info(
                "FTMO daily loss tracker: new day %s — prev_ref=$%.2f new_ref=$%.2f",
                today,
                prev_ref,
                self._day_reference,
            )

    # -- Equity updates ------------------------------------------------

    def update_equity(self, equity: float) -> None:
        """Track peak equity for the day.

        Call on every tick or health check to maintain the high-water mark.
        """
        if equity > self._peak_equity_today:
            self._peak_equity_today = equity

    # -- Pre-trade check -----------------------------------------------

    def check(self, balance: float, equity: float) -> RiskCheckResult:
        """Check if the FTMO daily loss buffer has been reached.

        Args:
            balance: Current account balance (closed P&L only).
            equity: Current account equity (balance + floating P&L).

        Returns:
            RiskCheckResult. Fails if current daily loss >= buffer_limit.
        """
        if not self._initialized:
            if balance > 0:
                self.initialize(balance, equity)
            else:
                return RiskCheckResult(
                    name="ftmo_daily_loss",
                    passed=True,
                    detail="daily loss tracker not initialized — skipped",
                )

        self._maybe_new_day(balance, equity)
        self.update_equity(equity)

        current_loss = self._day_reference - equity
        daily_limit = self.daily_limit_usd
        buffer_limit = self.buffer_limit_usd
        warning_limit = self.warning_limit_usd
        utilization_pct = (current_loss / daily_limit * 100.0) if daily_limit > 0 else 0.0

        if current_loss >= daily_limit:
            return RiskCheckResult(
                name="ftmo_daily_loss",
                passed=False,
                detail=(
                    f"FTMO DAILY LOSS BREACH: loss=${current_loss:.2f} >= "
                    f"limit=${daily_limit:.2f} ({self.daily_loss_pct:.1f}% of "
                    f"${self.initial_account_size:.0f}) — ref=${self._day_reference:.2f} "
                    f"equity=${equity:.2f}"
                ),
            )

        # 70% early warning: trigger position sizing reduction (non-blocking)
        if current_loss >= warning_limit and current_loss < buffer_limit:
            log.warning(
                "FTMO 70%% daily drawdown reached: loss=$%.2f >= warning=$%.2f (%.1f%% utilized) — "
                "triggering position size scaling",
                current_loss,
                warning_limit,
                utilization_pct,
            )

        if current_loss >= buffer_limit:
            return RiskCheckResult(
                name="ftmo_daily_loss",
                passed=False,
                detail=(
                    f"FTMO daily loss buffer reached: loss=${current_loss:.2f} >= "
                    f"buffer=${buffer_limit:.2f} ({self.buffer_pct:.0f}% of "
                    f"${daily_limit:.2f} limit) — {utilization_pct:.1f}% utilized — "
                    f"blocking new orders to protect daily limit"
                ),
            )

        return RiskCheckResult(
            name="ftmo_daily_loss",
            passed=True,
            detail=(
                f"daily_loss=${current_loss:.2f}, buffer=${buffer_limit:.2f}, "
                f"limit=${daily_limit:.2f}, utilized={utilization_pct:.1f}%, "
                f"ref=${self._day_reference:.2f}"
            ),
        )

    # -- Snapshot -------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a point-in-time snapshot of the tracker state."""
        daily_limit = self.daily_limit_usd
        return {
            "initialized": self._initialized,
            "initial_account_size": self.initial_account_size,
            "daily_loss_pct": self.daily_loss_pct,
            "buffer_pct": self.buffer_pct,
            "warning_pct": self.warning_pct,
            "daily_limit_usd": round(daily_limit, 2),
            "buffer_limit_usd": round(self.buffer_limit_usd, 2),
            "warning_limit_usd": round(self.warning_limit_usd, 2),
            "day_reference": round(self._day_reference, 2),
            "peak_equity_today": round(self._peak_equity_today, 2),
            "day_key": self._day_key,
        }

    # -- State persistence ---------------------------------------------

    def save_state(self, path: Path | None = None) -> None:
        """Persist daily loss tracker state for crash recovery."""
        path = path or (_STATE_DIR / "daily_loss_tracker.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "initial_account_size": self.initial_account_size,
            "daily_loss_pct": self.daily_loss_pct,
            "buffer_pct": self.buffer_pct,
            "warning_pct": self.warning_pct,
            "day_key": self._day_key,
            "day_reference": self._day_reference,
            "peak_equity_today": self._peak_equity_today,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_state(self, path: Path | None = None) -> bool:
        """Load daily loss tracker state. Returns True if loaded for today."""
        path = path or (_STATE_DIR / "daily_loss_tracker.json")
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
            if data.get("day_key") == today:
                self._day_key = today
                self._day_reference = data.get("day_reference", 0.0)
                self._peak_equity_today = data.get("peak_equity_today", 0.0)
                self.initial_account_size = data.get("initial_account_size", self.initial_account_size)
                # Load warning_pct if present (backward compatibility)
                self.warning_pct = data.get("warning_pct", self.warning_pct)
                self._initialized = True
                log.info(
                    "Loaded daily loss tracker: day=%s ref=$%.2f peak=$%.2f",
                    today,
                    self._day_reference,
                    self._peak_equity_today,
                )
                return True
            # Different day — clear initialization state to force re-init
            log.info(
                "Daily loss tracker day change detected: stored=%s current=%s — clearing state for re-initialization",
                data.get("day_key", "unknown"),
                today,
            )
            self._day_key = ""
            self._day_reference = 0.0
            self._peak_equity_today = 0.0
            self._initialized = False
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load daily loss tracker from %s: %s", path, exc)
            return False


# ---------------------------------------------------------------------------
# Weekend Auto-Closer
# ---------------------------------------------------------------------------

_FRIDAY = 4  # datetime.weekday(): Mon=0 .. Sun=6
_SATURDAY = 5
_SUNDAY = 6
_NY_TZ = ZoneInfo("America/New_York")


@dataclass
class WeekendAutoCloser:
    """Signals position closure before the Friday market close.

    Forex markets close at NY 5:00 PM (17:00 ET), which is:
    - 21:00 UTC during EDT (Mar-Nov)
    - 22:00 UTC during EST (Nov-Mar)

    Markets reopen Sunday at the same NY 5:00 PM local time.

    This class provides a configurable buffer (default 5 min) before close
    to allow orderly position flattening before the weekend gap.

    Usage::

        closer = WeekendAutoCloser()
        if closer.should_close_positions(time.time()):
            # close all open positions
        if closer.is_weekend(time.time()):
            # do not open new positions
    """

    CLOSE_BUFFER_MINUTES: int = 5
    last_close_attempt_ts: float = field(default=0.0, repr=False)

    @staticmethod
    def _market_close_hour_utc(ts: float) -> int:
        """Return the forex market close hour in UTC for the given timestamp.

        NY 5:00 PM is the forex market close/open boundary:
        - During EDT (US daylight saving, ~Mar-Nov): 21:00 UTC
        - During EST (US standard time, ~Nov-Mar): 22:00 UTC
        """
        dt_ny = datetime.fromtimestamp(ts, tz=_NY_TZ)
        is_dst = dt_ny.dst() != timedelta(0)
        return 21 if is_dst else 22

    def should_close_positions(self, timestamp: float) -> bool:
        """Return True if it's Friday and within the close buffer window.

        The window starts at (market_close - CLOSE_BUFFER_MINUTES) on Friday
        and extends through the entire weekend.
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        weekday = dt.weekday()
        hour = dt.hour
        minute = dt.minute

        # During the weekend proper — should already be closed
        if self.is_weekend(timestamp):
            return True

        # Friday close buffer: close_hour - buffer_minutes through close_hour
        if weekday == _FRIDAY:
            close_hour = self._market_close_hour_utc(timestamp)
            close_threshold_minutes = close_hour * 60 - self.CLOSE_BUFFER_MINUTES
            current_minutes = hour * 60 + minute
            if current_minutes >= close_threshold_minutes:
                return True

        return False

    def is_weekend(self, timestamp: float) -> bool:
        """Return True during the weekend gap (Friday close - Sunday open UTC).

        Close/open hour varies with US DST: 21:00 UTC (EDT) or 22:00 UTC (EST).
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        weekday = dt.weekday()
        hour = dt.hour
        close_hour = self._market_close_hour_utc(timestamp)

        # Friday at/after close → weekend
        if weekday == _FRIDAY and hour >= close_hour:
            return True
        # Saturday → weekend
        if weekday == _SATURDAY:
            return True
        # Sunday before open → weekend (open hour == close hour)
        return weekday == _SUNDAY and hour < close_hour

    def should_force_close_all(self, timestamp: float) -> bool:
        """Return True if all positions must be force-closed for weekend safety.

        The force-close deadline is 20:00 UTC on Friday — 1-2 hours before
        actual market close.  This gives ample time for orderly closure and
        avoids the deteriorating liquidity near close.

        This method is distinct from should_close_positions() which only
        blocks NEW entries.  should_force_close_all() demands the runtime
        CLOSE existing positions.
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        weekday = dt.weekday()
        hour = dt.hour

        # Force close at 20:00+ UTC on Friday
        if weekday == _FRIDAY and hour >= 20:
            return True

        # Also force close during full weekend (should already be flat)
        if weekday == _SATURDAY:
            return True
        if weekday == _SUNDAY:
            close_hour = self._market_close_hour_utc(timestamp)
            if hour < close_hour:
                return True

        return False

    def friday_close_phase(self, timestamp: float) -> str:
        """Return the current Friday close phase for graduated action.

        Returns:
            - "normal": No close action needed
            - "warning": 19:45-19:50 UTC Friday — emit warning
            - "partial": 19:50-20:00 UTC Friday — close largest positions first
            - "force": 20:00+ UTC Friday — force-close everything
            - "weekend": Saturday/Sunday — should already be flat
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        weekday = dt.weekday()

        if weekday == _SATURDAY or (weekday == _SUNDAY and dt.hour < self._market_close_hour_utc(timestamp)):
            return "weekend"

        if weekday != _FRIDAY:
            return "normal"

        minutes = dt.hour * 60 + dt.minute

        if minutes >= 20 * 60:  # 20:00+
            return "force"
        if minutes >= 19 * 60 + 50:  # 19:50-20:00
            return "partial"
        if minutes >= 19 * 60 + 45:  # 19:45-19:50
            return "warning"

        return "normal"

    def record_close_attempt(self, timestamp: float) -> None:
        """Record that a close attempt was made to prevent duplicates."""
        self.last_close_attempt_ts = timestamp


# ---------------------------------------------------------------------------
# News Calendar Blocker
# ---------------------------------------------------------------------------

# 2026 high-impact event calendar (MVP — hardcoded dates).
# NFP: first Friday of each month, 13:30 UTC
# FOMC: 8 meetings per year, 19:00 UTC (rate decision)
# ECB: 6 meetings per year, 13:15 UTC (rate decision)

_HIGH_IMPACT_EVENTS: list[dict] = [
    {
        "name": "NFP",
        "time_utc": "13:30",
        "currency": "USD",
        "dates": [
            "2026-01-02",  # Jan
            "2026-02-06",  # Feb
            "2026-03-06",  # Mar
            "2026-04-03",  # Apr
            "2026-05-01",  # May
            "2026-06-05",  # Jun
            "2026-07-03",  # Jul
            "2026-08-07",  # Aug
            "2026-09-04",  # Sep
            "2026-10-02",  # Oct
            "2026-11-06",  # Nov
            "2026-12-04",  # Dec
        ],
    },
    {
        "name": "FOMC",
        "time_utc": "19:00",
        "currency": "USD",
        "dates": [
            "2026-01-28",
            "2026-03-18",
            "2026-05-06",
            "2026-06-17",
            "2026-07-29",
            "2026-09-16",
            "2026-11-04",
            "2026-12-16",
        ],
    },
    {
        "name": "ECB",
        "time_utc": "13:15",
        "currency": "EUR",
        "dates": [
            "2026-01-22",
            "2026-03-05",
            "2026-04-16",
            "2026-06-04",
            "2026-09-10",
            "2026-10-29",
        ],
    },
    {
        "name": "US_CPI",
        "time_utc": "13:30",
        "currency": "USD",
        "dates": [
            "2026-01-14",  # Jan (released mid-Jan for Dec data)
            "2026-02-12",  # Feb (released mid-Feb for Jan data)
            "2026-03-11",  # Mar (released mid-Mar for Feb data)
            "2026-04-10",  # Apr (released mid-Apr for Mar data)
            "2026-05-13",  # May (released mid-May for Apr data)
            "2026-06-10",  # Jun (released mid-Jun for May data)
            "2026-07-16",  # Jul (released mid-Jul for Jun data)
            "2026-08-12",  # Aug (released mid-Aug for Jul data)
            "2026-09-11",  # Sep (released mid-Sep for Aug data)
            "2026-10-14",  # Oct (released mid-Oct for Sep data)
            "2026-11-12",  # Nov (released mid-Nov for Oct data)
            "2026-12-10",  # Dec (released mid-Dec for Nov data)
        ],
    },
    {
        "name": "EUR_CPI",
        "time_utc": "10:00",
        "currency": "EUR",
        "dates": [
            "2026-01-31",  # Jan (released end-Jan for Dec data)
            "2026-02-28",  # Feb (released end-Feb for Jan data)
            "2026-03-31",  # Mar (released end-Mar for Feb data)
            "2026-04-30",  # Apr (released end-Apr for Mar data)
            "2026-05-29",  # May (released end-May for Apr data)
            "2026-06-30",  # Jun (released end-Jun for May data)
            "2026-07-31",  # Jul (released end-Jul for Jun data)
            "2026-08-31",  # Aug (released end-Aug for Jul data)
            "2026-09-30",  # Sep (released end-Sep for Aug data)
            "2026-10-30",  # Oct (released end-Oct for Sep data)
            "2026-11-30",  # Nov (released end-Nov for Oct data)
            "2026-12-31",  # Dec (released end-Dec for Nov data)
        ],
    },
]


@dataclass
class NewsCalendarBlocker:
    """Blocks trading around high-impact macroeconomic news events.

    MVP implementation with a static 2026 calendar.  Checks whether the
    current timestamp falls within the blackout window (default 15 min
    before and after the scheduled event time) for any event whose
    currency matches the trade symbol.

    Usage::

        blocker = NewsCalendarBlocker()
        blocked, reason = blocker.is_blocked(ts, "EURUSD", blackout_minutes=15)
        if blocked:
            log.warning("Trade blocked: %s", reason)
    """

    events: list[dict] = field(default_factory=lambda: copy.deepcopy(_HIGH_IMPACT_EVENTS))

    def __post_init__(self) -> None:
        year = datetime.now(timezone.utc).year
        if year != 2026:
            log.warning(
                "NewsCalendarBlocker: static calendar is for 2026, current year is %d — events may be stale",
                year,
            )

    @staticmethod
    def _symbol_has_currency(symbol: str, currency: str) -> bool:
        """Check if a forex symbol contains the given currency code.

        E.g., "EURUSD" contains both "EUR" and "USD".
        """
        symbol_upper = symbol.upper()
        return currency.upper() in symbol_upper

    def is_blocked(
        self,
        timestamp: float,
        symbol: str,
        blackout_minutes: int = 15,
    ) -> tuple[bool, str]:
        """Check if trading is blocked due to an upcoming or recent news event.

        Args:
            timestamp: Current Unix timestamp.
            symbol: Trading symbol (e.g., "EURUSD").
            blackout_minutes: Minutes before AND after the event to block.

        Returns:
            (blocked, reason) — blocked is True if within a blackout window.
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")

        for event in self.events:
            if date_str not in event["dates"]:
                continue

            if not self._symbol_has_currency(symbol, event["currency"]):
                continue

            # Parse event time
            hour, minute = (int(x) for x in event["time_utc"].split(":"))
            event_dt = datetime(dt.year, dt.month, dt.day, hour, minute, tzinfo=timezone.utc)

            diff_seconds = (dt - event_dt).total_seconds()
            diff_minutes = diff_seconds / 60.0

            if -blackout_minutes <= diff_minutes <= blackout_minutes:
                if diff_minutes < 0:
                    reason = f"{event['name']} in {abs(diff_minutes):.0f} minutes"
                elif diff_minutes == 0:
                    reason = f"{event['name']} right now"
                else:
                    reason = f"{event['name']} was {diff_minutes:.0f} minutes ago"
                return True, reason

        return False, ""


# ---------------------------------------------------------------------------
# Gap Trading Preventer
# ---------------------------------------------------------------------------


@dataclass
class GapTradingPreventer:
    """Blocks orders when the current spread is abnormally wide.

    A simple stateless check that compares the current spread to the
    session average.  If the current spread exceeds ``multiplier`` times
    the average, trading is blocked (gap / spike protection).

    Usage::

        preventer = GapTradingPreventer()
        allowed, reason = preventer.check(current_spread_pips=3.0, session_avg_spread_pips=0.8)
        if not allowed:
            log.warning("Gap detected: %s", reason)
    """

    def check(
        self,
        current_spread_pips: float,
        session_avg_spread_pips: float,
        multiplier: float = 3.0,
    ) -> tuple[bool, str]:
        """Check if the current spread is acceptable.

        Args:
            current_spread_pips: Current spread in pips.
            session_avg_spread_pips: Session average spread in pips.
            multiplier: Maximum acceptable ratio of current to average.

        Returns:
            (allowed, reason) — allowed is False if spread is too wide.
        """
        if session_avg_spread_pips <= 0:
            return True, "session_avg_spread=0 — skipped"

        ratio = current_spread_pips / session_avg_spread_pips

        if ratio > multiplier:
            return False, (
                f"spread {current_spread_pips:.1f} pips is {ratio:.2f}x "
                f"session avg {session_avg_spread_pips:.1f} pips "
                f"(limit: {multiplier:.1f}x)"
            )

        return True, (
            f"spread={current_spread_pips:.1f}, avg={session_avg_spread_pips:.1f}, "
            f"ratio={ratio:.2f}x (limit={multiplier:.1f}x)"
        )


# ---------------------------------------------------------------------------
# Best Day Rule Tracker
# ---------------------------------------------------------------------------


@dataclass
class BestDayRuleTracker:
    """Monitors the FTMO Best Day Rule (informational / monitoring only).

    FTMO rule (funded accounts): no single trading day's profit may account
    for more than a percentage (typically 30%) of total lifetime profit.
    This tracker is MONITORING ONLY — it warns but does not block trades.

    Usage::

        tracker = BestDayRuleTracker()
        tracker.record_daily_pnl("2026-03-24", 150.0)
        tracker.record_daily_pnl("2026-03-25", 80.0)
        ok, reason = tracker.check(max_pct=0.30)
    """

    _daily_pnl: dict[str, float] = field(default_factory=dict)

    def record_daily_pnl(self, date: str, pnl: float) -> None:
        """Add to a day's running P&L total.

        Args:
            date: ISO date string (YYYY-MM-DD).
            pnl: P&L amount to add (can be positive or negative).
        """
        self._daily_pnl[date] = self._daily_pnl.get(date, 0.0) + pnl

    def check(self, max_pct: float = 0.30) -> tuple[bool, str]:
        """Check if any single day's profit dominates total profit.

        Args:
            max_pct: Maximum allowable fraction of total profit from one day
                (default 0.30 = 30%).

        Returns:
            (ok, reason) — ok is False if a single day exceeds the threshold.
            Negative-P&L days are excluded from total profit calculation.
        """
        if not self._daily_pnl:
            return True, "no trades recorded"

        # Total profit = sum of only profitable days
        total_profit = sum(v for v in self._daily_pnl.values() if v > 0)

        if total_profit <= 0:
            return True, "no profitable days yet"

        # Find the best (most profitable) day
        best_date = max(
            (d for d, v in self._daily_pnl.items() if v > 0),
            key=lambda d: self._daily_pnl[d],
        )
        best_pnl = self._daily_pnl[best_date]
        best_pct = best_pnl / total_profit

        if best_pct > max_pct:
            return False, (
                f"best day {best_date} profit ${best_pnl:.2f} is "
                f"{best_pct:.1%} of total profit ${total_profit:.2f} "
                f"(limit: {max_pct:.0%})"
            )

        return True, (
            f"best_day={best_date} ${best_pnl:.2f} ({best_pct:.1%} of ${total_profit:.2f} total, limit={max_pct:.0%})"
        )

    def get_stats(self) -> dict:
        """Return summary statistics for the best-day rule.

        Returns:
            Dict with best_day_pnl, total_profit, best_day_pct, best_day_date,
            days_traded, and daily_breakdown.
        """
        total_profit = sum(v for v in self._daily_pnl.values() if v > 0)

        if total_profit <= 0 or not self._daily_pnl:
            return {
                "best_day_pnl": 0.0,
                "total_profit": 0.0,
                "best_day_pct": 0.0,
                "best_day_date": "",
                "days_traded": len(self._daily_pnl),
                "daily_breakdown": dict(self._daily_pnl),
            }

        profitable_days = {d: v for d, v in self._daily_pnl.items() if v > 0}
        best_date = max(profitable_days, key=profitable_days.get)  # type: ignore[arg-type]
        best_pnl = profitable_days[best_date]

        return {
            "best_day_pnl": round(best_pnl, 2),
            "total_profit": round(total_profit, 2),
            "best_day_pct": round(best_pnl / total_profit, 4),
            "best_day_date": best_date,
            "days_traded": len(self._daily_pnl),
            "daily_breakdown": dict(self._daily_pnl),
        }


# ---------------------------------------------------------------------------
# Weekly Loss Tracker
# ---------------------------------------------------------------------------

_DEFAULT_WEEKLY_LOSS_PCT = 5.0  # Max cumulative loss per rolling 7-day window
_DEFAULT_WEEKLY_LOSS_BUFFER_PCT = 80.0  # Pre-trade buffer at 80% of weekly limit


@dataclass
class WeeklyLossTracker:
    """Tracks cumulative loss over a rolling 7-day window (Europe/Prague).

    FTMO compliance: prevents consecutive losing days from draining the
    account by enforcing a weekly loss ceiling.  When cumulative loss in
    any 7-day window reaches the threshold, new entries are blocked (exits
    still allowed).

    The window uses calendar days in Europe/Prague timezone for FTMO
    consistency.  Daily P&L is recorded per day and the rolling sum is
    computed over the most recent 7 entries.

    Args:
        initial_account_size: Nominal initial balance (e.g. 100_000).
        weekly_loss_pct: Maximum weekly loss as % of initial account (default 5.0).
        buffer_pct: Percentage of weekly limit at which to deny new orders
            (default 80.0, i.e. deny at 80% of the 5% limit = 4% of account).
    """

    initial_account_size: float = 0.0
    weekly_loss_pct: float = _DEFAULT_WEEKLY_LOSS_PCT
    buffer_pct: float = _DEFAULT_WEEKLY_LOSS_BUFFER_PCT
    _daily_pnl: dict[str, float] = field(default_factory=dict)
    _initialized: bool = field(default=False, repr=False)

    # -- Derived limits ------------------------------------------------

    @property
    def weekly_limit_usd(self) -> float:
        """Absolute weekly loss limit in USD."""
        return self.initial_account_size * (self.weekly_loss_pct / 100.0)

    @property
    def buffer_limit_usd(self) -> float:
        """Pre-trade buffer: deny new orders above this loss amount."""
        return self.weekly_limit_usd * (self.buffer_pct / 100.0)

    # -- Initialization ------------------------------------------------

    def initialize(self, balance: float) -> None:
        """Set initial account size.  Call once at session start."""
        if self.initial_account_size <= 0:
            self.initial_account_size = balance
        self._initialized = True
        log.info(
            "WeeklyLossTracker initialized: account=$%.2f weekly_limit=$%.2f buffer=$%.2f",
            self.initial_account_size,
            self.weekly_limit_usd,
            self.buffer_limit_usd,
        )

    # -- P&L recording -------------------------------------------------

    def record_daily_pnl(self, pnl: float, date_str: str | None = None) -> None:
        """Add to a day's running P&L total.

        Args:
            pnl: P&L amount (positive = profit, negative = loss).
            date_str: ISO date (YYYY-MM-DD).  Defaults to today in Prague tz.
        """
        if date_str is None:
            date_str = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + pnl

    def _rolling_7day_loss(self) -> float:
        """Compute cumulative loss over the most recent 7 calendar days.

        Only sums negative daily P&L values (losses).  Positive days do NOT
        offset losses — this is conservative and matches FTMO's approach.
        """
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).date()
        total_loss = 0.0
        for i in range(7):
            day = today - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            daily = self._daily_pnl.get(day_str, 0.0)
            if daily < 0:
                total_loss += abs(daily)
        return total_loss

    def _rolling_7day_net(self) -> float:
        """Compute net P&L over the most recent 7 calendar days.

        This sums ALL daily P&L (positive and negative) for a net view.
        """
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).date()
        total = 0.0
        for i in range(7):
            day = today - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            total += self._daily_pnl.get(day_str, 0.0)
        return total

    # -- Pre-trade check -----------------------------------------------

    def check(self) -> RiskCheckResult:
        """Check if weekly loss limit has been reached.

        Returns:
            RiskCheckResult.  Fails if rolling 7-day loss >= buffer_limit.
        """
        if not self._initialized:
            return RiskCheckResult(
                name="weekly_loss",
                passed=True,
                detail="weekly loss tracker not initialized — skipped",
            )

        rolling_loss = self._rolling_7day_loss()
        weekly_limit = self.weekly_limit_usd
        buffer_limit = self.buffer_limit_usd
        utilization_pct = (rolling_loss / weekly_limit * 100.0) if weekly_limit > 0 else 0.0

        if rolling_loss >= weekly_limit:
            return RiskCheckResult(
                name="weekly_loss",
                passed=False,
                detail=(
                    f"WEEKLY LOSS BREACH: 7-day loss=${rolling_loss:.2f} >= "
                    f"limit=${weekly_limit:.2f} ({self.weekly_loss_pct:.1f}% of "
                    f"${self.initial_account_size:.0f}) — blocking all new entries"
                ),
            )

        if rolling_loss >= buffer_limit:
            return RiskCheckResult(
                name="weekly_loss",
                passed=False,
                detail=(
                    f"weekly loss buffer reached: 7-day loss=${rolling_loss:.2f} >= "
                    f"buffer=${buffer_limit:.2f} ({self.buffer_pct:.0f}% of "
                    f"${weekly_limit:.2f} limit) — {utilization_pct:.1f}% utilized"
                ),
            )

        return RiskCheckResult(
            name="weekly_loss",
            passed=True,
            detail=(
                f"7day_loss=${rolling_loss:.2f}, buffer=${buffer_limit:.2f}, "
                f"limit=${weekly_limit:.2f}, utilized={utilization_pct:.1f}%"
            ),
        )

    # -- Snapshot -------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a point-in-time snapshot of the tracker state."""
        return {
            "initialized": self._initialized,
            "initial_account_size": self.initial_account_size,
            "weekly_loss_pct": self.weekly_loss_pct,
            "buffer_pct": self.buffer_pct,
            "weekly_limit_usd": round(self.weekly_limit_usd, 2),
            "buffer_limit_usd": round(self.buffer_limit_usd, 2),
            "rolling_7day_loss": round(self._rolling_7day_loss(), 2),
            "rolling_7day_net": round(self._rolling_7day_net(), 2),
            "daily_pnl": dict(self._daily_pnl),
        }

    # -- State persistence ---------------------------------------------

    def save_state(self, path: Path | None = None) -> None:
        """Persist weekly loss tracker state for crash recovery."""
        path = path or (_STATE_DIR / "weekly_loss_tracker.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "initial_account_size": self.initial_account_size,
            "weekly_loss_pct": self.weekly_loss_pct,
            "buffer_pct": self.buffer_pct,
            "daily_pnl": dict(self._daily_pnl),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_state(self, path: Path | None = None) -> bool:
        """Load weekly loss tracker state.  Returns True if loaded."""
        path = path or (_STATE_DIR / "weekly_loss_tracker.json")
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.initial_account_size = data.get("initial_account_size", self.initial_account_size)
            self._daily_pnl = data.get("daily_pnl", {})
            self._initialized = self.initial_account_size > 0
            log.info(
                "Loaded weekly loss tracker: account=$%.2f, %d daily entries",
                self.initial_account_size,
                len(self._daily_pnl),
            )
            return True
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load weekly loss tracker from %s: %s", path, exc)
            return False


# ---------------------------------------------------------------------------
# SL Modification Counter
# ---------------------------------------------------------------------------

_DEFAULT_SL_MOD_CEILING_PER_TRADE = 50  # max SL mods per individual trade
_DEFAULT_SL_MOD_CEILING_PER_DAY = 200  # max total SL mods across all trades per day


@dataclass
class SlModificationCounter:
    """Tracks stop-loss modifications per position and per day.

    FTMO EA compliance: excessive SL modifications are a strong EA
    fingerprint.  The IRB EMA trailing stop modifies SL on each bar
    close, which can accumulate quickly on lower timeframes.  This
    tracker enforces per-trade and per-day ceilings.

    Usage::

        counter = SlModificationCounter()
        counter.record("pos_123")
        ok = counter.check("pos_123")  # RiskCheckResult
    """

    per_trade_ceiling: int = _DEFAULT_SL_MOD_CEILING_PER_TRADE
    per_day_ceiling: int = _DEFAULT_SL_MOD_CEILING_PER_DAY
    # Internal state
    _per_position: dict[str, int] = field(default_factory=dict)
    _day_key: str = field(default="", repr=False)
    _day_count: int = field(default=0, repr=False)

    def _ensure_day(self) -> None:
        """Reset daily counter on new trading day (Europe/Prague)."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        if today != self._day_key:
            if self._day_key:
                log.info(
                    "SL mod counter day reset: %s had %d modifications",
                    self._day_key,
                    self._day_count,
                )
            self._day_key = today
            self._day_count = 0
            # Do NOT clear per-position counts — positions can span days

    def record(self, position_id: str) -> None:
        """Record a SL modification for a position.

        Args:
            position_id: The broker position ID being modified.
        """
        self._ensure_day()
        self._per_position[position_id] = self._per_position.get(position_id, 0) + 1
        self._day_count += 1

        count = self._per_position[position_id]
        if count == self.per_trade_ceiling:
            log.warning(
                "SL mod counter: position %s hit ceiling (%d mods)",
                position_id,
                count,
            )

    def check(self, position_id: str = "") -> RiskCheckResult:
        """Check if SL modification limits have been reached.

        Args:
            position_id: If provided, also checks per-trade ceiling.
                If empty, only checks daily ceiling.
        """
        self._ensure_day()

        # Check daily ceiling
        if self._day_count >= self.per_day_ceiling:
            return RiskCheckResult(
                name="sl_mod_limit",
                passed=False,
                detail=(
                    f"daily SL modifications {self._day_count} >= ceiling "
                    f"{self.per_day_ceiling} — blocking further modifications"
                ),
            )

        # Check per-trade ceiling
        if position_id:
            count = self._per_position.get(position_id, 0)
            if count >= self.per_trade_ceiling:
                return RiskCheckResult(
                    name="sl_mod_limit",
                    passed=False,
                    detail=(f"position {position_id} SL modifications {count} >= ceiling {self.per_trade_ceiling}"),
                )

        return RiskCheckResult(
            name="sl_mod_limit",
            passed=True,
            detail=(
                f"sl_mods_today={self._day_count}/{self.per_day_ceiling}"
                + (
                    f", position={self._per_position.get(position_id, 0)}/{self.per_trade_ceiling}"
                    if position_id
                    else ""
                )
            ),
        )

    def clear_position(self, position_id: str) -> None:
        """Remove tracking for a closed position."""
        self._per_position.pop(position_id, None)

    @property
    def daily_count(self) -> int:
        self._ensure_day()
        return self._day_count

    def position_count(self, position_id: str) -> int:
        """Return SL modification count for a specific position."""
        return self._per_position.get(position_id, 0)

    def save_state(self, path: Path | None = None) -> None:
        """Persist SL mod counter state for crash recovery."""
        self._ensure_day()
        path = path or (_STATE_DIR / "sl_mod_counter.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "day": self._day_key,
            "day_count": self._day_count,
            "per_position": dict(self._per_position),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_state(self, path: Path | None = None) -> bool:
        """Load SL mod counter state.  Returns True if loaded."""
        path = path or (_STATE_DIR / "sl_mod_counter.json")
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
            if data.get("day") == today:
                self._day_key = today
                self._day_count = data.get("day_count", 0)
                self._per_position = data.get("per_position", {})
                log.info("Loaded SL mod counter: %d mods for %s", self._day_count, today)
                return True
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load SL mod counter from %s: %s", path, exc)
            return False
