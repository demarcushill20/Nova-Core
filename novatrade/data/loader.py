"""Multi-format candle data loader for NovaTrade.

Supports CSV, JSON, MetaTrader 5 export, TradingView export, and
pandas DataFrame sources. Auto-detects format from file extension
and column headers.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Format enum
# ---------------------------------------------------------------------------


class CandleFormat(Enum):
    CSV = "csv"
    JSON = "json"
    METATRADER = "mt5"
    TRADINGVIEW = "tradingview"
    DATAFRAME = "dataframe"


# ---------------------------------------------------------------------------
# Column name mappings (case-insensitive)
# ---------------------------------------------------------------------------

# Canonical field -> known aliases (all lowercase)
_TIMESTAMP_ALIASES = {"timestamp", "time", "date", "datetime", "date_time", "ts", "t"}
_OPEN_ALIASES = {"open", "o", "open_price", "openprice"}
_HIGH_ALIASES = {"high", "h", "high_price", "highprice", "max"}
_LOW_ALIASES = {"low", "l", "low_price", "lowprice", "min"}
_CLOSE_ALIASES = {"close", "c", "close_price", "closeprice", "last"}
_VOLUME_ALIASES = {"volume", "vol", "v", "tick_volume", "tickvolume", "real_volume"}

# MetaTrader-specific: separate Date and Time columns
_MT_DATE_ALIASES = {"date"}
_MT_TIME_ALIASES = {"time"}


def _find_column(headers: list[str], aliases: set[str]) -> str | None:
    """Find a column name matching any alias (case-insensitive)."""
    lower_map = {h.lower().strip(): h for h in headers}
    for alias in aliases:
        if alias in lower_map:
            return lower_map[alias]
    return None


def _parse_timestamp(value: str) -> float:
    """Parse a timestamp string to UTC float.

    Handles:
    - Unix timestamp (int or float)
    - ISO 8601 (2024-01-01T00:00:00Z, 2024-01-01T00:00:00+00:00)
    - Date + time (2024.01.01 00:00:00, 2024/01/01 00:00, 2024-01-01 00:00:00)
    - Date only (2024-01-01, 2024.01.01)
    """
    value = value.strip()

    # Try numeric first (Unix timestamp)
    try:
        ts = float(value)
        if not math.isnan(ts):
            return ts
    except ValueError:
        pass

    # Normalize separators
    normalized = value.replace("/", "-").replace(".", "-", 2)

    # Try ISO 8601 with timezone
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(normalized, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue

    # Try original value with dot separators (MetaTrader: 2024.01.01)
    for fmt in (
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue

    raise ValueError(f"Cannot parse timestamp: {value!r}")


def _parse_mt_datetime(date_str: str, time_str: str) -> float:
    """Parse MetaTrader separate Date and Time columns to UTC float."""
    combined = f"{date_str.strip()} {time_str.strip()}"
    return _parse_timestamp(combined)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def _detect_csv_subformat(path: Path) -> CandleFormat:
    """Peek at CSV headers to distinguish CSV vs MetaTrader vs TradingView."""
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return CandleFormat.CSV

    lower_headers = [h.lower().strip() for h in headers]

    # MetaTrader: has separate "date" and "time" columns (not "timestamp" or "datetime")
    has_date = "date" in lower_headers
    has_time = "time" in lower_headers
    has_timestamp = any(h in lower_headers for h in ("timestamp", "datetime", "date_time", "ts"))

    if has_date and has_time and not has_timestamp:
        return CandleFormat.METATRADER

    # TradingView: has "time" column (single) with open/high/low/close, often no volume
    if has_time and not has_date and not has_timestamp:
        has_open = any(h in lower_headers for h in _OPEN_ALIASES)
        has_close = any(h in lower_headers for h in _CLOSE_ALIASES)
        if has_open and has_close:
            return CandleFormat.TRADINGVIEW

    return CandleFormat.CSV


def detect_format(path: Path) -> CandleFormat:
    """Auto-detect the candle data format from file extension and content.

    Args:
        path: Path to the data file.

    Returns:
        Detected CandleFormat enum value.

    Raises:
        ValueError: If the format cannot be determined.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".json":
        return CandleFormat.JSON
    elif suffix in (".csv", ".tsv", ".txt"):
        return _detect_csv_subformat(path)
    else:
        # Try to detect by peeking at content
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                first_char = f.read(1).strip()
            if first_char in ("[", "{"):
                return CandleFormat.JSON
            return _detect_csv_subformat(path)
        except Exception as exc:
            raise ValueError(f"Cannot detect format for: {path}") from exc


# ---------------------------------------------------------------------------
# Format-specific loaders
# ---------------------------------------------------------------------------


def _load_csv_generic(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Load generic CSV with auto-detected column names."""
    candles: list[Candle] = []

    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []

        headers = list(reader.fieldnames)
        ts_col = _find_column(headers, _TIMESTAMP_ALIASES)
        open_col = _find_column(headers, _OPEN_ALIASES)
        high_col = _find_column(headers, _HIGH_ALIASES)
        low_col = _find_column(headers, _LOW_ALIASES)
        close_col = _find_column(headers, _CLOSE_ALIASES)
        vol_col = _find_column(headers, _VOLUME_ALIASES)

        if not all([ts_col, open_col, high_col, low_col, close_col]):
            missing = []
            if not ts_col:
                missing.append("timestamp")
            if not open_col:
                missing.append("open")
            if not high_col:
                missing.append("high")
            if not low_col:
                missing.append("low")
            if not close_col:
                missing.append("close")
            raise ValueError(f"CSV missing required columns: {missing}. Found: {headers}")

        for row in reader:
            ts = _parse_timestamp(row[ts_col])  # type: ignore[arg-type]
            candles.append(
                Candle(
                    timestamp=ts,
                    open=float(row[open_col]),  # type: ignore[arg-type]
                    high=float(row[high_col]),  # type: ignore[arg-type]
                    low=float(row[low_col]),  # type: ignore[arg-type]
                    close=float(row[close_col]),  # type: ignore[arg-type]
                    volume=float(row[vol_col]) if vol_col and row.get(vol_col) else 0.0,  # type: ignore[arg-type]
                    symbol=symbol,
                    timeframe=timeframe,
                )
            )

    return candles


def _load_metatrader_csv(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Load MetaTrader CSV export (Date,Time,Open,High,Low,Close,Volume)."""
    candles: list[Candle] = []

    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []

        headers = list(reader.fieldnames)
        date_col = _find_column(headers, _MT_DATE_ALIASES)
        time_col = _find_column(headers, _MT_TIME_ALIASES)
        open_col = _find_column(headers, _OPEN_ALIASES)
        high_col = _find_column(headers, _HIGH_ALIASES)
        low_col = _find_column(headers, _LOW_ALIASES)
        close_col = _find_column(headers, _CLOSE_ALIASES)
        vol_col = _find_column(headers, _VOLUME_ALIASES)

        if not all([date_col, time_col, open_col, high_col, low_col, close_col]):
            raise ValueError(f"MetaTrader CSV missing required columns. Found: {headers}")

        for row in reader:
            ts = _parse_mt_datetime(row[date_col], row[time_col])  # type: ignore[arg-type]
            candles.append(
                Candle(
                    timestamp=ts,
                    open=float(row[open_col]),  # type: ignore[arg-type]
                    high=float(row[high_col]),  # type: ignore[arg-type]
                    low=float(row[low_col]),  # type: ignore[arg-type]
                    close=float(row[close_col]),  # type: ignore[arg-type]
                    volume=float(row[vol_col]) if vol_col and row.get(vol_col) else 0.0,  # type: ignore[arg-type]
                    symbol=symbol,
                    timeframe=timeframe,
                )
            )

    return candles


def _load_tradingview_csv(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Load TradingView export (time,open,high,low,close[,volume])."""
    candles: list[Candle] = []

    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []

        headers = list(reader.fieldnames)
        # TradingView uses "time" as the timestamp column
        ts_col = _find_column(headers, {"time"})
        open_col = _find_column(headers, _OPEN_ALIASES)
        high_col = _find_column(headers, _HIGH_ALIASES)
        low_col = _find_column(headers, _LOW_ALIASES)
        close_col = _find_column(headers, _CLOSE_ALIASES)
        vol_col = _find_column(headers, _VOLUME_ALIASES)

        if not all([ts_col, open_col, high_col, low_col, close_col]):
            raise ValueError(f"TradingView CSV missing required columns. Found: {headers}")

        for row in reader:
            ts = _parse_timestamp(row[ts_col])  # type: ignore[arg-type]
            candles.append(
                Candle(
                    timestamp=ts,
                    open=float(row[open_col]),  # type: ignore[arg-type]
                    high=float(row[high_col]),  # type: ignore[arg-type]
                    low=float(row[low_col]),  # type: ignore[arg-type]
                    close=float(row[close_col]),  # type: ignore[arg-type]
                    volume=float(row[vol_col]) if vol_col and row.get(vol_col) else 0.0,  # type: ignore[arg-type]
                    symbol=symbol,
                    timeframe=timeframe,
                )
            )

    return candles


def _load_json(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Load JSON array of candle objects."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"JSON file must contain a list of candle objects, got {type(data).__name__}")

    candles: list[Candle] = []
    for i, obj in enumerate(data):
        if not isinstance(obj, dict):
            raise ValueError(f"JSON item {i} must be a dict, got {type(obj).__name__}")

        # Find timestamp field
        ts_raw = None
        for key in _TIMESTAMP_ALIASES:
            if key in obj:
                ts_raw = obj[key]
                break
            # Case-insensitive fallback
            for k in obj:
                if k.lower() == key:
                    ts_raw = obj[k]
                    break
            if ts_raw is not None:
                break

        if ts_raw is None:
            raise ValueError(f"JSON item {i} missing timestamp field")

        ts = _parse_timestamp(str(ts_raw)) if isinstance(ts_raw, str) else float(ts_raw)

        # Find OHLCV fields (case-insensitive)
        def _get_field(obj: dict[str, Any], aliases: set[str], required: bool = True) -> float:
            for alias in aliases:
                if alias in obj:
                    return float(obj[alias])
                for k in obj:
                    if k.lower() == alias:
                        return float(obj[k])
            if required:
                raise ValueError(f"Missing field (expected one of {aliases}) in: {obj}")
            return 0.0

        candles.append(
            Candle(
                timestamp=ts,
                open=_get_field(obj, _OPEN_ALIASES),
                high=_get_field(obj, _HIGH_ALIASES),
                low=_get_field(obj, _LOW_ALIASES),
                close=_get_field(obj, _CLOSE_ALIASES),
                volume=_get_field(obj, _VOLUME_ALIASES, required=False),
                symbol=obj.get("symbol", symbol),
                timeframe=obj.get("timeframe", timeframe),
            )
        )

    return candles


def _load_dataframe(df: Any, symbol: str, timeframe: str) -> list[Candle]:
    """Load candles from a pandas DataFrame with OHLCV columns.

    Args:
        df: pandas DataFrame with columns matching OHLCV aliases.
        symbol: Symbol to assign.
        timeframe: Timeframe to assign.

    Returns:
        List of Candle objects.
    """
    # Import pandas locally to avoid hard dependency
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("pandas is required for DataFrame loading: pip install pandas") from exc

    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"Expected pandas DataFrame, got {type(df).__name__}")

    headers = list(df.columns)
    ts_col = _find_column(headers, _TIMESTAMP_ALIASES)
    open_col = _find_column(headers, _OPEN_ALIASES)
    high_col = _find_column(headers, _HIGH_ALIASES)
    low_col = _find_column(headers, _LOW_ALIASES)
    close_col = _find_column(headers, _CLOSE_ALIASES)
    vol_col = _find_column(headers, _VOLUME_ALIASES)

    # If no explicit timestamp column, check index
    use_index_as_ts = False
    if ts_col is None:
        if isinstance(df.index, pd.DatetimeIndex):
            use_index_as_ts = True
        else:
            raise ValueError(f"DataFrame missing timestamp column. Columns: {headers}")

    if not all([open_col, high_col, low_col, close_col]):
        missing = []
        if not open_col:
            missing.append("open")
        if not high_col:
            missing.append("high")
        if not low_col:
            missing.append("low")
        if not close_col:
            missing.append("close")
        raise ValueError(f"DataFrame missing required columns: {missing}. Found: {headers}")

    candles: list[Candle] = []
    for idx, row in df.iterrows():
        if use_index_as_ts:
            ts = float(idx.timestamp())  # type: ignore[union-attr]
        else:
            raw_ts = row[ts_col]  # type: ignore[index]
            if isinstance(raw_ts, pd.Timestamp):
                ts = float(raw_ts.timestamp())
            elif isinstance(raw_ts, str):
                ts = _parse_timestamp(raw_ts)
            else:
                ts = float(raw_ts)

        candles.append(
            Candle(
                timestamp=ts,
                open=float(row[open_col]),  # type: ignore[index]
                high=float(row[high_col]),  # type: ignore[index]
                low=float(row[low_col]),  # type: ignore[index]
                close=float(row[close_col]),  # type: ignore[index]
                volume=float(row[vol_col]) if vol_col else 0.0,  # type: ignore[index]
                symbol=symbol,
                timeframe=timeframe,
            )
        )

    return candles


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_candles(
    source: Path | str | Any,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    fmt: CandleFormat | None = None,
) -> list[Candle]:
    """Load candles from multiple source formats.

    Supports CSV, JSON, MetaTrader export, TradingView export, and
    pandas DataFrames. Auto-detects format when ``fmt`` is None.

    Args:
        source: File path (str/Path) or pandas DataFrame.
        symbol: Symbol to assign to loaded candles.
        timeframe: Timeframe to assign to loaded candles.
        fmt: Explicit format override. If None, auto-detected.

    Returns:
        List of Candle objects sorted by timestamp.

    Raises:
        FileNotFoundError: If file path does not exist.
        ValueError: If format cannot be detected or data is invalid.
    """
    # Handle DataFrame input
    is_dataframe = False
    try:
        import pandas as pd

        if isinstance(source, pd.DataFrame):
            is_dataframe = True
    except ImportError:
        pass

    if is_dataframe or fmt == CandleFormat.DATAFRAME:
        candles = _load_dataframe(source, symbol, timeframe)
        return sorted(candles, key=lambda c: c.timestamp)

    # File-based loading
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")

    if fmt is None:
        fmt = detect_format(path)

    if fmt == CandleFormat.JSON:
        candles = _load_json(path, symbol, timeframe)
    elif fmt == CandleFormat.METATRADER:
        candles = _load_metatrader_csv(path, symbol, timeframe)
    elif fmt == CandleFormat.TRADINGVIEW:
        candles = _load_tradingview_csv(path, symbol, timeframe)
    elif fmt == CandleFormat.CSV:
        candles = _load_csv_generic(path, symbol, timeframe)
    else:
        raise ValueError(f"Unsupported format: {fmt}")

    return sorted(candles, key=lambda c: c.timestamp)
