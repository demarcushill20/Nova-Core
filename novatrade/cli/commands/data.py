"""Data management functions for NovaTrade CLI.

Generates synthetic candle data, saves/loads CSV, and freezes snapshots.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from novatrade.models import Candle
from novatrade.storage.data_snapshot import save_snapshot

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path("/home/nova/nova-core")
DATA_DIR = BASE_DIR / "data"
CANDLE_DIR = DATA_DIR / "candles"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

_BARS_PER_DAY = {"H1": 24, "H4": 6, "M15": 96, "D1": 1}

# Realistic volatility parameters per symbol (daily ATR as fraction of price)
_VOLATILITY = {
    "EURUSD": 0.004,
    "GBPUSD": 0.005,
    "USDJPY": 0.005,
    "AUDUSD": 0.005,
    "USDCAD": 0.004,
}
_DEFAULT_VOLATILITY = 0.004

_START_PRICES = {
    "EURUSD": 1.1000,
    "GBPUSD": 1.2700,
    "USDJPY": 150.00,
    "AUDUSD": 0.6500,
    "USDCAD": 1.3600,
}

# 2024-01-01 00:00:00 UTC
_EPOCH_2024 = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())

_CSV_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------


def generate_synthetic_candles(
    symbol: str,
    timeframe: str,
    days: int,
    start_price: float = 1.1000,
    seed: int | None = 42,
) -> list[Candle]:
    """Generate realistic OHLCV candles using a geometric random walk.

    Args:
        symbol: Instrument symbol (e.g. "EURUSD").
        timeframe: Bar period — "H1" or "H4".
        days: Number of trading days to generate.
        start_price: Opening price of the first candle.
        seed: RNG seed for reproducibility (None for random).

    Returns:
        Sorted list of Candle objects with realistic OHLCV data.
    """
    rng = np.random.default_rng(seed)

    bars_per_day = _BARS_PER_DAY.get(timeframe, 24)
    total_bars = bars_per_day * days

    # Per-bar volatility scaled from daily
    daily_vol = _VOLATILITY.get(symbol, _DEFAULT_VOLATILITY)
    bar_vol = daily_vol / np.sqrt(bars_per_day)

    # Seconds per bar
    if timeframe == "H1":
        bar_seconds = 3600
    elif timeframe == "H4":
        bar_seconds = 14400
    elif timeframe == "M15":
        bar_seconds = 900
    elif timeframe == "D1":
        bar_seconds = 86400
    else:
        bar_seconds = 3600

    # Generate log-returns and convert to price path
    returns = rng.normal(0, bar_vol, total_bars)
    log_prices = np.log(start_price) + np.cumsum(returns)
    close_prices = np.exp(log_prices)

    # Build candles
    candles: list[Candle] = []
    current_open = start_price

    for i in range(total_bars):
        ts = float(_EPOCH_2024 + i * bar_seconds)
        close = float(close_prices[i])

        # Intra-bar range: random wick extensions above and below
        wick_up = abs(rng.normal(0, bar_vol * current_open * 0.5))
        wick_down = abs(rng.normal(0, bar_vol * current_open * 0.5))

        high = max(current_open, close) + wick_up
        low = min(current_open, close) - wick_down

        # Ensure OHLC consistency
        high = max(high, current_open, close)
        low = min(low, current_open, close)

        # Realistic volume (tick count proxy, roughly 500-5000 per H1 bar)
        base_volume = 2000.0 if timeframe == "H1" else 6000.0
        volume = float(max(100, rng.normal(base_volume, base_volume * 0.3)))

        # Round to 5 decimal places for forex
        digits = 3 if "JPY" in symbol else 5
        candles.append(
            Candle(
                timestamp=ts,
                open=round(current_open, digits),
                high=round(high, digits),
                low=round(low, digits),
                close=round(close, digits),
                volume=round(volume, 1),
                symbol=symbol,
                timeframe=timeframe,
            )
        )

        # Next bar opens at this bar's close
        current_open = close

    return candles


def aggregate_h1_to_h4(h1_candles: list[Candle]) -> list[Candle]:
    """Aggregate H1 candles into H4 candles (4 bars -> 1).

    This ensures H4 data is exactly consistent with H1 data.
    """
    if not h1_candles:
        return []

    h4_candles: list[Candle] = []
    symbol = h1_candles[0].symbol

    for i in range(0, len(h1_candles), 4):
        chunk = h1_candles[i : i + 4]
        if not chunk:
            break

        h4_candles.append(
            Candle(
                timestamp=chunk[0].timestamp,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=round(sum(c.volume for c in chunk), 1),
                symbol=symbol,
                timeframe="H4",
            )
        )

    return h4_candles


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def save_candles_csv(candles: list[Candle], output_path: Path) -> int:
    """Save candles as CSV (timestamp, open, high, low, close, volume).

    Creates parent directories if they don't exist.

    Args:
        candles: List of Candle objects to save.
        output_path: Destination CSV file path.

    Returns:
        Number of candles written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for c in candles:
            writer.writerow([c.timestamp, c.open, c.high, c.low, c.close, c.volume])

    return len(candles)


def load_candles_csv(csv_path: Path, symbol: str = "", timeframe: str = "") -> list[Candle]:
    """Load candles from CSV back to Candle objects.

    If symbol/timeframe are empty, attempts to infer from filename
    (expected pattern: {SYMBOL}_{TIMEFRAME}.csv).

    Args:
        csv_path: Path to the CSV file.
        symbol: Symbol to set on loaded candles (optional).
        timeframe: Timeframe to set on loaded candles (optional).

    Returns:
        List of Candle objects sorted by timestamp.

    Raises:
        FileNotFoundError: If csv_path does not exist.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Candle CSV not found: {csv_path}")

    # Infer symbol/timeframe from filename if not provided
    if not symbol or not timeframe:
        stem = csv_path.stem  # e.g. "EURUSD_H1"
        parts = stem.split("_")
        if len(parts) >= 2:
            if not symbol:
                symbol = parts[0]
            if not timeframe:
                timeframe = parts[1]

    candles: list[Candle] = []
    with csv_path.open("r") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            c = Candle(
                timestamp=float(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                symbol=symbol,
                timeframe=timeframe,
            )
            if any(math.isnan(v) for v in (c.open, c.high, c.low, c.close)):
                raise ValueError(f"NaN detected in candle at row {i}")
            candles.append(c)

    return sorted(candles, key=lambda c: c.timestamp)


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------


def freeze_candles(
    symbol: str,
    candle_dir: Path,
    snapshot_dir: Path,
) -> str:
    """Load H1 and H4 CSVs for a symbol and create a frozen Parquet snapshot.

    Expects files named {symbol}_H1.csv and {symbol}_H4.csv in candle_dir.

    Args:
        symbol: Instrument symbol (e.g. "EURUSD").
        candle_dir: Directory containing CSV candle files.
        snapshot_dir: Directory for Parquet snapshots.

    Returns:
        The snapshot_id (16-char hex string).

    Raises:
        FileNotFoundError: If either CSV file is missing.
    """
    candle_dir = Path(candle_dir)
    snapshot_dir = Path(snapshot_dir)

    h1_path = candle_dir / f"{symbol}_H1.csv"
    h4_path = candle_dir / f"{symbol}_H4.csv"

    h1_candles = load_candles_csv(h1_path, symbol=symbol, timeframe="H1")
    h4_candles = load_candles_csv(h4_path, symbol=symbol, timeframe="H4")

    snapshot = save_snapshot(
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        symbol=symbol,
        snapshot_dir=snapshot_dir,
    )

    return snapshot.snapshot_id
