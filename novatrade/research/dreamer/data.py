"""Phase A — data loading, multi-timeframe resampling, no-lookahead alignment.

The single hardest correctness property in this whole build is: **at the close
of M5 bar t, the model may only see higher-timeframe bars that have already
closed.** A naive resample-then-forward-fill leaks the future, because a daily
or H4 bar's value is "known" the instant its open prints — but in reality you
do not know that bar's high/low/close until it finishes, well after bar t.

The fix here is mechanical and conservative: every higher-TF bar is indexed by
its **close time** (= open label + one full bar duration), and aligned onto the
M5 grid with a *backward* ``merge_asof`` so each M5 row only ever picks up
higher-TF bars whose close time is ``<= t``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config

OHLC_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}

# Default on-disk M1 feed (see analyze-step output for provenance/extent).
DEFAULT_M1_PATH = Path("data/candles/histdata/XAUUSD_M1.csv")


def load_m1(path: str | Path = DEFAULT_M1_PATH) -> pd.DataFrame:
    """Load the M1 OHLC CSV into a tz-naive, time-indexed, sorted frame."""
    df = pd.read_csv(path, parse_dates=["time"])
    df = df.set_index("time").sort_index()
    df = df[["open", "high", "low", "close"]].astype("float64")
    return df


def resample_ohlc(m1: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample an M1 frame to timeframe ``tf`` using OHLC aggregation.

    Bars are labelled by their **open** time (``label="left"``); empty buckets
    (weekends, holidays) are dropped so no synthetic bars are invented.
    """
    rule = config.RESAMPLE_RULE[tf]
    out = m1.resample(rule, label="left", closed="left").agg(OHLC_AGG)
    return out.dropna(how="any")


def to_close_indexed(tf_frame: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Re-index a (open-labelled) TF frame by each bar's *close* time.

    Close time = open label + one bar duration. This is the timestamp at which
    the bar's full OHLC first becomes knowable, and is what alignment keys on.
    """
    shifted = tf_frame.copy()
    shifted.index = tf_frame.index + config.TF_DURATION[tf]
    shifted.index.name = "close_time"
    return shifted


def align_backward(base_index: pd.DatetimeIndex, tf_frame: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Align a higher-TF frame onto ``base_index`` with no lookahead.

    Returns a frame indexed by ``base_index`` whose row ``t`` carries the most
    recent ``tf`` bar that had already *closed* at or before ``t``. Rows before
    the first closed bar are NaN (insufficient history), never future-filled.
    """
    closed = to_close_indexed(tf_frame, tf).sort_index()
    base = pd.DataFrame(index=pd.DatetimeIndex(base_index, name="time")).sort_index()
    merged = pd.merge_asof(
        base,
        closed,
        left_index=True,
        right_index=True,
        direction="backward",
    )
    return merged


def build_tf_frames(
    m1: pd.DataFrame,
    end: pd.Timestamp | None = None,
) -> dict[str, pd.DataFrame]:
    """Resample an M1 frame to all configured timeframes.

    ``end`` (exclusive) trims the M1 feed *before* resampling so that no bar can
    straddle a train/val/test boundary — e.g. passing ``TRAIN_END_DATE``
    guarantees no higher-TF bar mixes pre- and post-2022 M1 ticks.
    """
    if end is not None:
        m1 = m1.loc[m1.index < end]
    return {tf: resample_ohlc(m1, tf) for tf in config.TIMEFRAMES}
