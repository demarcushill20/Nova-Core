"""Live ATR-based regime detection for trade monitoring (v87 P2.1).

Wraps the existing classify_regimes() from evaluation/durability and provides
a lightweight interface for the heartbeat/ops monitor to determine current
market conditions: LOW_VOL, NORMAL, HIGH_VOL, TRENDING.

Includes 5-zone ATR percentile classification for risk scaling:
  compression (0-10th) → 0% risk (no trade)
  low_vol (10-30th) → 50% risk
  normal (30-70th) → 100% risk
  elevated (70-90th) → 75% risk
  extreme (90-100th) → 0% risk (no trade)

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

# ATR zone boundaries (percentile thresholds)
ATR_ZONE_THRESHOLDS: dict[str, tuple[float, float]] = {
    "compression": (0.0, 10.0),
    "low_vol": (10.0, 30.0),
    "normal": (30.0, 70.0),
    "elevated": (70.0, 90.0),
    "extreme": (90.0, 100.1),  # 100.1 to include 100th percentile
}

# Risk scaling factor per ATR zone
ATR_ZONE_RISK_FACTOR: dict[str, float] = {
    "compression": 0.0,
    "low_vol": 0.5,
    "normal": 1.0,
    "elevated": 0.75,
    "extreme": 0.0,
}


def classify_atr_zone(percentile: float) -> str:
    """Map an ATR percentile rank (0-100) to a 5-zone label."""
    for zone, (lo, hi) in ATR_ZONE_THRESHOLDS.items():
        if lo <= percentile < hi:
            return zone
    return "normal"


def atr_zone_risk_factor(zone: str) -> float:
    """Return the risk scaling factor for a given ATR zone."""
    return ATR_ZONE_RISK_FACTOR.get(zone, 1.0)


@dataclass
class RegimeSnapshot:
    """Current market regime classification with ATR percentile zones."""

    regime: str  # "quiet", "ranging", "trending", "volatile"
    atr_current: float = 0.0
    atr_mean: float = 0.0
    atr_percentile: float = 50.0  # 0-100 percentile rank
    atr_zone: str = "normal"  # compression/low_vol/normal/elevated/extreme
    atr_zone_risk_factor: float = 1.0  # 0.0-1.0 risk multiplier
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

    @property
    def trading_allowed(self) -> bool:
        """Whether ATR zone permits trading (non-zero risk factor)."""
        return self.atr_zone_risk_factor > 0.0


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

    # Compute ATR values for all candles to derive percentile ranking
    atr_current = 0.0
    atr_mean = 0.0
    atr_percentile = 50.0
    all_atrs: list[float] = []

    if len(candles) > atr_period:
        # Compute true ranges for all candles
        all_trs = []
        for i in range(1, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            all_trs.append(tr)

        # Rolling ATR for each candle (simple moving average of TRs)
        for i in range(atr_period - 1, len(all_trs)):
            window = all_trs[i - atr_period + 1 : i + 1]
            all_atrs.append(sum(window) / len(window))

        if all_atrs:
            atr_current = all_atrs[-1]
            atr_mean = sum(all_atrs) / len(all_atrs)

            # Percentile rank: % of historical ATRs that are <= current
            count_below = sum(1 for a in all_atrs if a <= atr_current)
            atr_percentile = (count_below / len(all_atrs)) * 100.0

    zone = classify_atr_zone(atr_percentile)
    risk_factor = atr_zone_risk_factor(zone)

    snap = RegimeSnapshot(
        regime=current_regime,
        atr_current=round(atr_current, 6),
        atr_mean=round(atr_mean, 6),
        atr_percentile=round(atr_percentile, 1),
        atr_zone=zone,
        atr_zone_risk_factor=risk_factor,
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


def load_regime_safe(
    staleness_seconds: int = 900,
) -> tuple[float, str, str]:
    """Load regime risk factor with staleness, missing-file, and malformed-data safeguards.

    Returns:
        (risk_factor, atr_zone, fallback_reason)
        - risk_factor: 0.0–1.0 from regime, or 1.0 on fallback
        - atr_zone: zone label, or "unknown" on fallback
        - fallback_reason: empty string if regime loaded OK, else explanation
    """
    _DEFAULT_FACTOR = 1.0
    _DEFAULT_ZONE = "unknown"

    # --- File existence ---
    if not REGIME_FILE.exists():
        reason = f"regime file missing: {REGIME_FILE}"
        log.warning("ATR regime fallback: %s", reason)
        return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason

    # --- Parse ---
    try:
        raw = REGIME_FILE.read_text()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        reason = f"regime file unreadable/malformed: {exc}"
        log.warning("ATR regime fallback: %s", reason)
        return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason

    # --- Required fields ---
    zone = data.get("atr_zone")
    factor = data.get("atr_zone_risk_factor")
    classified_at = data.get("classified_at", "")

    if zone is None or factor is None:
        reason = f"regime file missing required fields (atr_zone={zone}, atr_zone_risk_factor={factor})"
        log.warning("ATR regime fallback: %s", reason)
        return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason

    # --- Staleness check ---
    if classified_at:
        try:
            ts = datetime.fromisoformat(classified_at)
            age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
            if age_seconds > staleness_seconds:
                reason = (
                    f"regime stale: classified_at={classified_at}, "
                    f"age={age_seconds:.0f}s > threshold={staleness_seconds}s"
                )
                log.warning("ATR regime fallback: %s", reason)
                return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason
        except (ValueError, TypeError) as exc:
            reason = f"regime classified_at unparseable: {classified_at} ({exc})"
            log.warning("ATR regime fallback: %s", reason)
            return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason
    else:
        reason = "regime file has no classified_at timestamp"
        log.warning("ATR regime fallback: %s", reason)
        return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason

    # --- Valid regime ---
    try:
        factor = float(factor)
    except (ValueError, TypeError):
        reason = f"regime risk_factor not numeric: {factor}"
        log.warning("ATR regime fallback: %s", reason)
        return _DEFAULT_FACTOR, _DEFAULT_ZONE, reason

    return factor, str(zone), ""


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
