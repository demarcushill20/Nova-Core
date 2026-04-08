"""Post-trade verification — replays strategy logic after each trade close.

After the Python pipeline executes a trade, the verifier replays the
strategy's ``check_entry()`` on the candle buffer that was active at fill
time.  This confirms the trade matched what the strategy *would have*
produced, without burning trial days on shadow-mode validation.

Logging goes to ``STATE/novatrade/post_trade_verification.jsonl`` (append-
only JSONL) and a ``VERIFY`` event is appended to the trade journal.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.irb import IRBStrategy

log = logging.getLogger("novatrade.monitor.post_trade_verifier")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

VERIFICATION_DIR = Path("/home/nova/nova-core/STATE/novatrade")
VERIFICATION_FILE = VERIFICATION_DIR / "post_trade_verification.jsonl"


@dataclass
class VerificationResult:
    """Result of replaying the strategy for a single closed trade."""

    position_id: str
    symbol: str
    actual_side: str  # "LONG" or "SHORT"
    actual_entry_price: float
    actual_stop_loss: float
    actual_entry_bar_ts: float  # timestamp of bar that triggered entry

    # Replay outcome
    replay_found_signal: bool
    replay_side: str | None = None
    replay_entry_price: float | None = None
    replay_stop_loss: float | None = None
    replay_signal_bar_ts: float | None = None

    # Comparison
    direction_match: bool = False
    entry_price_delta_pips: float = 0.0
    stop_loss_delta_pips: float = 0.0

    # Scoring
    match_score: float = 0.0
    verdict: str = "NO_SIGNAL"  # MATCH / PARTIAL_MATCH / MISMATCH / NO_SIGNAL
    detail: str = ""

    # Metadata
    verified_at: float = field(default_factory=time.time)
    candle_window_bars: int = 0
    warmup_bars_available: int = 0


@dataclass
class VerificationSummary:
    """Rolling summary of verification results."""

    total_verified: int = 0
    matches: int = 0
    partial_matches: int = 0
    mismatches: int = 0
    no_signal: int = 0
    match_rate: float = 0.0
    mismatch_rate: float = 0.0
    alert_triggered: bool = False


@dataclass
class _VerificationContext:
    """Snapshot captured at fill time for later verification at close time."""

    position_id: str
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    entry_bar_timestamp: float
    h1_snapshot: list[Candle]
    h4_snapshot: list[Candle]


# ---------------------------------------------------------------------------
# Pip helpers
# ---------------------------------------------------------------------------


def _pip_size(symbol: str) -> float:
    """Return the pip size for a symbol (0.01 for JPY pairs, 0.0001 otherwise)."""
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def _price_delta_pips(price_a: float, price_b: float, symbol: str) -> float:
    """Absolute price difference in pips."""
    return abs(price_a - price_b) / _pip_size(symbol)


# ---------------------------------------------------------------------------
# PostTradeVerifier
# ---------------------------------------------------------------------------


class PostTradeVerifier:
    """Replays strategy logic after each trade close to verify execution fidelity.

    Uses the ``LiveStrategyEngine``'s candle buffers (snapshotted at fill time)
    to replay the IRB strategy and confirm the trade matched what the strategy
    would have produced.
    """

    def __init__(
        self,
        env: BacktestEnvironment,
        *,
        entry_price_tolerance_pips: float = 2.0,
        stop_loss_tolerance_pips: float = 3.0,
        mismatch_alert_threshold: float = 0.20,
        rolling_window_size: int = 10,
        signal_search_window_bars: int = 2,
        log_path: Path | None = None,
    ) -> None:
        self._env = env
        self._entry_tol_pips = entry_price_tolerance_pips
        self._sl_tol_pips = stop_loss_tolerance_pips
        self._alert_threshold = mismatch_alert_threshold
        self._window_size = rolling_window_size
        self._search_window = signal_search_window_bars
        self._log_path = log_path or VERIFICATION_FILE

        # Rolling results window for alert calculation
        self._results: deque[VerificationResult] = deque(maxlen=rolling_window_size)

    # -- Public API ---------------------------------------------------------

    def verify_trade(
        self,
        *,
        position_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        entry_bar_timestamp: float,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
    ) -> VerificationResult:
        """Replay strategy on the candle snapshot and compare to the actual trade.

        Args:
            position_id: Broker position ID.
            symbol: Trading symbol (e.g. "EURUSD").
            side: Actual trade side ("LONG" or "SHORT").
            entry_price: Actual fill price.
            stop_loss: Actual stop-loss price.
            entry_bar_timestamp: Timestamp of the bar that triggered the entry signal.
            h1_candles: H1 candle buffer snapshot from fill time.
            h4_candles: H4 candle buffer snapshot from fill time.

        Returns:
            VerificationResult with match score and verdict.
        """
        result = VerificationResult(
            position_id=position_id,
            symbol=symbol,
            actual_side=side,
            actual_entry_price=entry_price,
            actual_stop_loss=stop_loss,
            actual_entry_bar_ts=entry_bar_timestamp,
            replay_found_signal=False,
            candle_window_bars=len(h1_candles),
        )

        if len(h1_candles) < self._env.warmup_bars:
            result.verdict = "NO_SIGNAL"
            result.detail = f"Insufficient candles for replay: {len(h1_candles)} < {self._env.warmup_bars} warmup bars"
            self._record(result)
            return result

        # --- Replay strategy ---
        replay_signal = self._replay_strategy(
            h1_candles=h1_candles,
            h4_candles=h4_candles,
            entry_bar_timestamp=entry_bar_timestamp,
        )

        if replay_signal is None:
            result.verdict = "NO_SIGNAL"
            result.detail = (
                f"Replay found no entry signal within ±{self._search_window} bars "
                f"of entry bar (ts={entry_bar_timestamp})"
            )
            result.warmup_bars_available = max(0, len(h1_candles) - self._env.warmup_bars)
            self._record(result)
            return result

        # --- Compare ---
        result.replay_found_signal = True
        result.replay_side = replay_signal["side"]
        result.replay_entry_price = replay_signal["entry_price"]
        result.replay_stop_loss = replay_signal["stop_loss"]
        result.replay_signal_bar_ts = replay_signal["bar_timestamp"]

        result.direction_match = result.actual_side == result.replay_side
        result.entry_price_delta_pips = _price_delta_pips(entry_price, replay_signal["entry_price"], symbol)
        result.stop_loss_delta_pips = _price_delta_pips(stop_loss, replay_signal["stop_loss"], symbol)

        entry_ok = result.entry_price_delta_pips <= self._entry_tol_pips
        sl_ok = result.stop_loss_delta_pips <= self._sl_tol_pips

        # Weighted score: direction=0.5, entry_price=0.25, stop_loss=0.25
        score = 0.0
        if result.direction_match:
            score += 0.5
        if entry_ok:
            score += 0.25
        if sl_ok:
            score += 0.25
        result.match_score = score

        if score >= 0.75:
            result.verdict = "MATCH"
        elif score >= 0.5:
            result.verdict = "PARTIAL_MATCH"
        else:
            result.verdict = "MISMATCH"

        result.detail = (
            f"dir={'OK' if result.direction_match else 'FAIL'} "
            f"entry={result.entry_price_delta_pips:.1f}pip "
            f"sl={result.stop_loss_delta_pips:.1f}pip "
            f"score={score:.2f}"
        )
        result.warmup_bars_available = max(0, len(h1_candles) - self._env.warmup_bars)

        self._record(result)
        return result

    def get_summary(self) -> VerificationSummary:
        """Return a rolling-window summary of recent verification results."""
        total = len(self._results)
        if total == 0:
            return VerificationSummary()

        matches = sum(1 for r in self._results if r.verdict == "MATCH")
        partial = sum(1 for r in self._results if r.verdict == "PARTIAL_MATCH")
        mismatches = sum(1 for r in self._results if r.verdict == "MISMATCH")
        no_signal = sum(1 for r in self._results if r.verdict == "NO_SIGNAL")

        match_rate = (matches + partial) / total
        mismatch_rate = mismatches / total

        return VerificationSummary(
            total_verified=total,
            matches=matches,
            partial_matches=partial,
            mismatches=mismatches,
            no_signal=no_signal,
            match_rate=match_rate,
            mismatch_rate=mismatch_rate,
            alert_triggered=mismatch_rate > self._alert_threshold,
        )

    def should_alert(self) -> bool:
        """Check if the mismatch rate exceeds the alert threshold."""
        summary = self.get_summary()
        return summary.alert_triggered

    # -- Internal -----------------------------------------------------------

    def _replay_strategy(
        self,
        *,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        entry_bar_timestamp: float,
    ) -> dict[str, Any] | None:
        """Replay the IRB strategy on the candle snapshot.

        Scans a ±search_window around the entry bar timestamp, calling
        ``IRBStrategy.check_entry()`` at each bar. Returns the first
        matching signal found, or None.
        """
        strategy = IRBStrategy(self._env)
        indicators = strategy.compute_indicators(h1_candles)

        # Find the bar index closest to entry_bar_timestamp
        target_idx = self._find_bar_index(h1_candles, entry_bar_timestamp)
        if target_idx is None:
            log.debug(
                "replay: could not find bar with timestamp %.0f in %d candles",
                entry_bar_timestamp,
                len(h1_candles),
            )
            return None

        # Scan ±search_window bars
        lo = max(self._env.warmup_bars, target_idx - self._search_window)
        hi = min(len(h1_candles) - 1, target_idx + self._search_window)

        for j in range(lo, hi + 1):
            signal = strategy.check_entry(j, h1_candles, indicators, h4_candles or None)
            if signal is not None:
                return {
                    "side": signal.side,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "bar_timestamp": h1_candles[j].timestamp,
                }

        return None

    @staticmethod
    def _find_bar_index(candles: list[Candle], timestamp: float) -> int | None:
        """Find the bar index with the closest matching timestamp."""
        if not candles:
            return None

        best_idx = None
        best_delta = float("inf")

        for i, c in enumerate(candles):
            delta = abs(c.timestamp - timestamp)
            if delta < best_delta:
                best_delta = delta
                best_idx = i
            # Timestamps are monotonically increasing — once delta starts
            # growing we've passed the closest bar.
            elif delta > best_delta:
                break

        # Allow up to 2 hours tolerance (bars are H1, so 7200s = 2 bars)
        if best_delta > 7200:
            return None

        return best_idx

    def _record(self, result: VerificationResult) -> None:
        """Persist result to JSONL log and trade journal, then update rolling window."""
        self._results.append(result)

        # Write to verification log
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = asdict(result)
            line = json.dumps(entry, default=str) + "\n"
            with open(self._log_path, "a") as f:
                f.write(line)
        except OSError as exc:
            log.warning("Failed to write verification log: %s", exc)

        # Write VERIFY event to trade journal
        try:
            from novatrade.monitor.trade_journal import log_trade_verify

            log_trade_verify(
                position_id=result.position_id,
                symbol=result.symbol,
                side=result.actual_side,
                verdict=result.verdict,
                match_score=result.match_score,
                detail=result.detail,
            )
        except Exception as exc:
            log.warning("Failed to write VERIFY event to trade journal: %s", exc)

        # Log at appropriate level
        if result.verdict == "MATCH":
            log.info(
                "VERIFY %s: %s (score=%.2f) — %s",
                result.position_id,
                result.verdict,
                result.match_score,
                result.detail,
            )
        elif result.verdict == "PARTIAL_MATCH":
            log.warning(
                "VERIFY %s: %s (score=%.2f) — %s",
                result.position_id,
                result.verdict,
                result.match_score,
                result.detail,
            )
        else:
            log.warning(
                "VERIFY %s: %s (score=%.2f) — %s",
                result.position_id,
                result.verdict,
                result.match_score,
                result.detail,
            )
