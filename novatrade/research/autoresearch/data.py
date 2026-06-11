"""Data loading + the SEALED train/hold-out split for the autoresearch loop.

15m bars come from HistData M1 (resampled, cached). Hourly bars are derived for
the fast drift families. The hold-out is the most-recent slice of the timeline:
selection only ever sees `train`; the survivor is confirmed once on `holdout`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
CANDLES = ROOT / "data" / "candles"

# Instruments with sub-hour HistData M1 (resampled to 15m). FX majors + gold.
FX_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "AUDNZD", "XAUUSD")
# Instruments stored as a ready-made hourly parquet (no sub-hour bars), e.g. crypto.
HOURLY_PARQUET = {"BTCUSD": CANDLES / "btcusd_1h.parquet"}
# High-priced instruments: a "pip" is bps of price, so a fixed stop_pips stays
# meaningful (gold ~$2000, crypto ~$60k). FX majors use absolute 1e-4 pips.
RELATIVE_PIP_SYMBOLS = frozenset({"USDJPY", "XAUUSD", "BTCUSD"})


def _m1_path(symbol: str) -> Path:
    return CANDLES / "histdata" / f"{symbol}_M1.csv"


def _cache15_path(symbol: str) -> Path:
    return CANDLES / f"{symbol.lower()}_15m.parquet"


def load_15m(instrument: str = "EURUSD") -> pd.DataFrame:
    """15m OHLC (~UTC) for an FX symbol — parquet cache if present, else
    resample HistData M1 and cache."""
    cache, m1 = _cache15_path(instrument), _m1_path(instrument)
    if cache.exists():
        b = pd.read_parquet(cache)
    else:
        df = pd.read_csv(m1, parse_dates=["time"]).set_index("time").sort_index()
        b = pd.DataFrame(
            {
                "open": df["open"].resample("15min").first(),
                "high": df["high"].resample("15min").max(),
                "low": df["low"].resample("15min").min(),
                "close": df["close"].resample("15min").last(),
            }
        ).dropna()
        cache.parent.mkdir(parents=True, exist_ok=True)
        b.to_parquet(cache)
    b.index = pd.to_datetime(b.index)
    return b


def to_hourly(bars15: pd.DataFrame) -> pd.DataFrame:
    """Derive 1h OHLC from 15m bars (for the fast drift families' stop sim)."""
    h = pd.DataFrame(
        {
            "open": bars15["open"].resample("1h").first(),
            "high": bars15["high"].resample("1h").max(),
            "low": bars15["low"].resample("1h").min(),
            "close": bars15["close"].resample("1h").last(),
        }
    ).dropna()
    h.index = pd.to_datetime(h.index)
    return h


@dataclass
class Dataset:
    bars15: pd.DataFrame
    hourly: pd.DataFrame
    # `relative_pip`: interpret a "pip" as a fraction of price (bps) rather than
    # an absolute 1e-4. FX uses absolute pips; crypto uses bps-of-price so a fixed
    # stop_pips stays meaningful across a 10x price range.
    relative_pip: bool = False

    def slice(self, start=None, end=None) -> Dataset:
        def _s(df):
            m = pd.Series(True, index=df.index)
            if start is not None:
                m &= df.index >= start
            if end is not None:
                m &= df.index < end
            return df[m]

        return Dataset(_s(self.bars15), _s(self.hourly), relative_pip=self.relative_pip)


@dataclass
class Split:
    train: Dataset
    holdout: Dataset
    cut: pd.Timestamp


def load_hourly(instrument: str) -> pd.DataFrame:
    """Ready-made hourly OHLC (e.g. crypto, 24/7). No sub-hour data exists."""
    h = pd.read_parquet(HOURLY_PARQUET[instrument])
    h.index = pd.to_datetime(h.index)
    return h


def make_dataset(instrument: str = "EURUSD", bars15: pd.DataFrame | None = None) -> Dataset:
    """Build a Dataset for any registered instrument. For hourly-only
    instruments (crypto) bars15 is set to the hourly frame — the drift families
    use hourly anyway; the 15m control families just run coarser."""
    if bars15 is not None:
        return Dataset(bars15, to_hourly(bars15))
    if instrument in HOURLY_PARQUET:
        h = load_hourly(instrument)
        return Dataset(h, h, relative_pip=True)  # crypto: stops in bps of price
    b = load_15m(instrument)
    return Dataset(b, to_hourly(b), relative_pip=instrument in RELATIVE_PIP_SYMBOLS)


def sealed_split(ds: Dataset, holdout_frac: float = 0.30) -> Split:
    """Hold out the most-recent `holdout_frac` of the timeline. Selection must
    only use `train`; `holdout` is touched once, for the final survivor."""
    idx = ds.bars15.index
    cut = idx[int(len(idx) * (1 - holdout_frac))]
    return Split(train=ds.slice(end=cut), holdout=ds.slice(start=cut), cut=cut)
