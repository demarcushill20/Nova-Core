"""Live ATR-based regime detection for trade monitoring (v87 P2.1).

Wraps the existing classify_regimes() from evaluation/durability and provides
a lightweight interface for the heartbeat/ops monitor to determine current
market conditions: LOW_VOL, NORMAL, HIGH_VOL, TRENDING.

State persisted to STATE/novatrade/regime.json.
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")
REGIME_FILE = STATE_DIR / "regime.json"


@dataclass
class RegimeSnapshot:
    """Current market regime classification."""

    regime: str  # "quiet", "ranging", "trending", "volatile"
    atr_current: float = 0.0
    atr_mean: float = 0.0
    symbol: str = "EURUSD"
    timeframe: str = "M5"
    candle_count: int = 0
    classified_at: str = ""

    @property
    def is_low_vol(self) -> bool:
        return self.regime == "quiet"

    @property
    def is_high_vol(self) -> bool:
        return self.regime == "volatile"


def classify_live_regime(
    candles: list,
    symbol: str = "EURUSD",
    timeframe: str = "M5",
    atr_period: int = 14,
) -> RegimeSnapshot:
    """Classify current regime from recent candles.

    Args:
        candles: List of Candle objects (from novatrade.backtester.types).
        symbol: Instrument being classified.
        timeframe: Candle timeframe.
        atr_period: ATR look-back period.

    Returns:
        RegimeSnapshot with the most recent candle's regime.
    """
    from novatrade.evaluation.durability import classify_regimes

    if not candles or len(candles) < atr_period + 2:
        return RegimeSnapshot(
            regime="ranging",
            symbol=symbol,
            timeframe=timeframe,
            candle_count=len(candles) if candles else 0,
            classified_at=datetime.now(timezone.utc).isoformat(),
        )

    regime_map = classify_regimes(candles, atr_period=atr_period)

    # Get the most recent regime (last candle)
    last_candle = candles[-1]
    current_regime = regime_map.get(last_candle.timestamp, "ranging")

    # Compute current ATR for the snapshot
    atr_current = 0.0
    atr_mean = 0.0
    if len(candles) > atr_period:
        trs = []
        for i in range(max(1, len(candles) - atr_period), len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            trs.append(tr)
        if trs:
            atr_current = sum(trs) / len(trs)
            # Mean over all valid ATR windows
            atr_mean = atr_current  # simplified for live use

    snap = RegimeSnapshot(
        regime=current_regime,
        atr_current=round(atr_current, 6),
        atr_mean=round(atr_mean, 6),
        symbol=symbol,
        timeframe=timeframe,
        candle_count=len(candles),
        classified_at=datetime.now(timezone.utc).isoformat(),
    )

    # Persist for heartbeat visibility
    _persist_regime(snap)
    return snap


def load_regime() -> RegimeSnapshot | None:
    """Load the last persisted regime snapshot."""
    try:
        if REGIME_FILE.exists():
            data = json.loads(REGIME_FILE.read_text())
            return RegimeSnapshot(**{k: v for k, v in data.items() if k in RegimeSnapshot.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError, OSError):
        pass
    return None


def _persist_regime(snap: RegimeSnapshot) -> None:
    """Atomically write regime snapshot to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(snap), f, indent=2)
        os.replace(tmp, str(REGIME_FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
