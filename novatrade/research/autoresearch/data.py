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
HISTDATA_M1 = ROOT / "data" / "candles" / "histdata" / "EURUSD_M1.csv"
CACHE_15M = ROOT / "data" / "candles" / "eurusd_15m.parquet"


def load_15m(cache: Path = CACHE_15M, m1: Path = HISTDATA_M1) -> pd.DataFrame:
    """15m OHLC (~UTC). Uses the parquet cache if present, else resamples M1."""
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

    def slice(self, start=None, end=None) -> Dataset:
        def _s(df):
            m = pd.Series(True, index=df.index)
            if start is not None:
                m &= df.index >= start
            if end is not None:
                m &= df.index < end
            return df[m]

        return Dataset(_s(self.bars15), _s(self.hourly))


@dataclass
class Split:
    train: Dataset
    holdout: Dataset
    cut: pd.Timestamp


def make_dataset(bars15: pd.DataFrame | None = None) -> Dataset:
    b = bars15 if bars15 is not None else load_15m()
    return Dataset(b, to_hourly(b))


def sealed_split(ds: Dataset, holdout_frac: float = 0.30) -> Split:
    """Hold out the most-recent `holdout_frac` of the timeline. Selection must
    only use `train`; `holdout` is touched once, for the final survivor."""
    idx = ds.bars15.index
    cut = idx[int(len(idx) * (1 - holdout_frac))]
    return Split(train=ds.slice(end=cut), holdout=ds.slice(start=cut), cut=cut)
