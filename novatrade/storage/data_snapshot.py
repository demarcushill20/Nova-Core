"""Frozen data snapshot management for campaign reproducibility.

Saves and loads candle data as Parquet files with SHA-256 fingerprints
to guarantee bit-exact reproduction of backtesting datasets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import struct
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from novatrade.models import Candle

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DataSnapshot (frozen metadata)
# ---------------------------------------------------------------------------


def _validate_snapshot_id(value: str) -> None:
    if not re.fullmatch(r"[0-9a-z_-]{1,64}", value):
        raise ValueError(f"Invalid snapshot_id: must be 1-64 alphanumeric/underscore/dash chars, got {value!r}")


@dataclass(frozen=True)
class DataSnapshot:
    """Immutable metadata describing a saved candle dataset."""

    snapshot_id: str
    symbol: str
    primary_tf: str
    higher_tf: str
    primary_bars: int
    higher_bars: int
    date_range_start: str
    date_range_end: str
    created_at: str
    frozen: bool = True


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def fingerprint_candles(candles: list[Candle]) -> str:
    """Compute SHA-256 of all candle data (deterministic ordering).

    Each candle contributes its timestamp + OHLCV as packed binary doubles.
    Returns the first 16 hex characters of the digest.
    """
    h = hashlib.sha256()
    for c in candles:
        h.update(struct.pack(">6d", c.timestamp, c.open, c.high, c.low, c.close, c.volume))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Candle <-> DataFrame conversion
# ---------------------------------------------------------------------------

_CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "symbol", "timeframe"]


def _candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "symbol": c.symbol,
            "timeframe": c.timeframe,
        }
        for c in candles
    ]
    return pd.DataFrame(rows, columns=_CANDLE_COLUMNS)


def _df_to_candles(df: pd.DataFrame) -> list[Candle]:
    # Vectorized NaN check — much faster than per-row iteration
    price_cols = ["open", "high", "low", "close"]
    nan_mask = df[price_cols].isna().any(axis=1)
    if nan_mask.any():
        first_bad = nan_mask.idxmax()
        raise ValueError(f"NaN detected in candle at row {first_bad}")

    # Vectorized extraction — avoids iterrows() overhead on large datasets
    timestamps = df["timestamp"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    volumes = df["volume"].to_numpy(dtype=float)
    symbols = df["symbol"].astype(str).to_numpy()
    timeframes = df["timeframe"].astype(str).to_numpy()

    return [
        Candle(
            timestamp=timestamps[i],
            open=opens[i],
            high=highs[i],
            low=lows[i],
            close=closes[i],
            volume=volumes[i],
            symbol=symbols[i],
            timeframe=timeframes[i],
        )
        for i in range(len(df))
    ]


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------


def save_snapshot(
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    symbol: str,
    snapshot_dir: Path,
) -> DataSnapshot:
    """Persist candle data as Parquet files with a fingerprinted metadata record.

    Files created:
        {snapshot_dir}/{snapshot_id}_h1.parquet
        {snapshot_dir}/{snapshot_id}_h4.parquet
        {snapshot_dir}/{snapshot_id}_meta.json

    Returns the DataSnapshot describing the saved dataset.
    """
    if not h1_candles:
        raise ValueError("h1_candles must not be empty")
    if not h4_candles:
        raise ValueError("h4_candles must not be empty")

    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Deterministic fingerprint from both candle sets
    combined_hash = hashlib.sha256()
    combined_hash.update(fingerprint_candles(h1_candles).encode())
    combined_hash.update(fingerprint_candles(h4_candles).encode())
    snapshot_id = combined_hash.hexdigest()[:16]

    # Convert and save Parquet
    h1_df = _candles_to_df(h1_candles)
    h4_df = _candles_to_df(h4_candles)
    h1_df.to_parquet(snapshot_dir / f"{snapshot_id}_h1.parquet", index=False)
    h4_df.to_parquet(snapshot_dir / f"{snapshot_id}_h4.parquet", index=False)

    # Derive date range from H1 timestamps
    timestamps = [c.timestamp for c in h1_candles]
    start_dt = datetime.fromtimestamp(min(timestamps), tz=timezone.utc).date().isoformat()
    end_dt = datetime.fromtimestamp(max(timestamps), tz=timezone.utc).date().isoformat()

    # Infer timeframes from candle data
    primary_tf = h1_candles[0].timeframe if h1_candles[0].timeframe else "H1"
    higher_tf = h4_candles[0].timeframe if h4_candles[0].timeframe else "H4"

    snapshot = DataSnapshot(
        snapshot_id=snapshot_id,
        symbol=symbol,
        primary_tf=primary_tf,
        higher_tf=higher_tf,
        primary_bars=len(h1_candles),
        higher_bars=len(h4_candles),
        date_range_start=start_dt,
        date_range_end=end_dt,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
    )

    # Save metadata JSON
    meta_path = snapshot_dir / f"{snapshot_id}_meta.json"
    meta_path.write_text(json.dumps(asdict(snapshot), indent=2, sort_keys=True))

    return snapshot


def load_snapshot(
    snapshot_id: str,
    snapshot_dir: Path,
) -> tuple[list[Candle], list[Candle], DataSnapshot]:
    """Load a previously saved snapshot and verify its fingerprint.

    Returns (h1_candles, h4_candles, metadata).

    Raises FileNotFoundError if files are missing, ValueError if fingerprint
    does not match (data corruption).
    """
    _validate_snapshot_id(snapshot_id)
    snapshot_dir = Path(snapshot_dir)

    meta_path = snapshot_dir / f"{snapshot_id}_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Snapshot metadata not found: {meta_path}")

    try:
        meta_dict = json.loads(meta_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed snapshot metadata JSON at {meta_path}: {exc}") from exc
    snapshot = DataSnapshot(**meta_dict)

    h1_path = snapshot_dir / f"{snapshot_id}_h1.parquet"
    h4_path = snapshot_dir / f"{snapshot_id}_h4.parquet"
    if not h1_path.exists():
        raise FileNotFoundError(f"H1 Parquet not found: {h1_path}")
    if not h4_path.exists():
        raise FileNotFoundError(f"H4 Parquet not found: {h4_path}")

    h1_candles = _df_to_candles(pd.read_parquet(h1_path))
    h4_candles = _df_to_candles(pd.read_parquet(h4_path))

    # Verify fingerprint
    verify_hash = hashlib.sha256()
    verify_hash.update(fingerprint_candles(h1_candles).encode())
    verify_hash.update(fingerprint_candles(h4_candles).encode())
    recomputed = verify_hash.hexdigest()[:16]

    if recomputed != snapshot_id:
        raise ValueError(f"Fingerprint mismatch: expected {snapshot_id}, got {recomputed}. Data may be corrupted.")

    return h1_candles, h4_candles, snapshot


def list_snapshots(snapshot_dir: Path) -> list[DataSnapshot]:
    """Find all *_meta.json files and return their DataSnapshot records."""
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return []

    snapshots: list[DataSnapshot] = []
    for meta_file in sorted(snapshot_dir.glob("*_meta.json")):
        try:
            meta_dict = json.loads(meta_file.read_text())
        except json.JSONDecodeError:
            log.warning("Skipping malformed snapshot metadata: %s", meta_file)
            continue
        snapshots.append(DataSnapshot(**meta_dict))
    return snapshots
