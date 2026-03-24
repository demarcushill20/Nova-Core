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

All components are stateful (in-memory with optional JSON persistence)
and designed to integrate into the existing PreTradeGate pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

        ratio = proposed_volume / med

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
        return median(r.volume for r in self._history)

    @property
    def trade_count(self) -> int:
        return len(self._history)

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
# We use a buffer (default 80% of that limit) to block new orders before
# the hard breach, giving room for floating P&L to move.
_DEFAULT_DAILY_LOSS_PCT = 5.0  # FTMO standard
_DEFAULT_BUFFER_PCT = 80.0  # block new orders at 80% of daily limit


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
            "daily_limit_usd": round(daily_limit, 2),
            "buffer_limit_usd": round(self.buffer_limit_usd, 2),
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
                self._initialized = True
                log.info(
                    "Loaded daily loss tracker: day=%s ref=$%.2f peak=$%.2f",
                    today,
                    self._day_reference,
                    self._peak_equity_today,
                )
                return True
            # Different day — will re-initialize on next check
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load daily loss tracker from %s: %s", path, exc)
            return False
