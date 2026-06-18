"""Phase B — the 152-feature multi-timeframe matrix + train-only scaler.

Layout (load-bearing, asserted at runtime):

    24 features x 6 timeframes [M5,M15,H1,H4,D1,W1]  = 144
     8 global time/session features (on the M5 grid)  =   8
                                                      -----
                                              total   = 152

Base-TF (M5) features are used at the close of their own bar, so they need no
shift. The five higher TFs are aligned with ``data.align_backward`` (close-time
keyed, backward merge) so nothing from an unclosed bar ever reaches the model.

The scaler (mean/std per column) is fit on the **train slice only**, frozen,
and then applied unchanged to validation/test. Fitting on the full series would
leak the future distribution — the unit tests guard exactly this.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, data

_EPS = 1e-12


# --------------------------------------------------------------------------- #
# Indicator primitives (vectorised, causal — no use of future bars)
# --------------------------------------------------------------------------- #
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / (dn + _EPS)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(h: pd.Series, lo: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    pc = c.shift()
    tr = pd.concat([(h - lo), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def compute_tf_features(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Compute the fixed set of ``PER_TF_FEATURES`` (24) on one TF's OHLC.

    All features are scale-relative (returns, ratios, distances normalised by
    price) so the same indicator code is meaningful across every timeframe.
    """
    h, lo, c = ohlc["high"], ohlc["low"], ohlc["close"]
    rng = h - lo
    ret = np.log(c / c.shift())

    ema8, ema21, ema50 = _ema(c, 8), _ema(c, 21), _ema(c, 50)
    ema12, ema26 = _ema(c, 12), _ema(c, 26)
    macd = ema12 - ema26
    macd_sig = _ema(macd, 9)

    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    low14 = lo.rolling(14).min()
    high14 = h.rolling(14).max()
    stoch_k = (c - low14) / ((high14 - low14) + _EPS)
    max20 = c.rolling(20).max()
    min20 = c.rolling(20).min()

    f = pd.DataFrame(index=ohlc.index)
    f["ret"] = ret
    f["range_rel"] = rng / c
    f["close_pos"] = (c - lo) / (rng + _EPS)
    f["rsi14"] = _rsi(c, 14) / 100.0
    f["atr_rel"] = _atr(h, lo, c, 14) / c
    f["ema8_dist"] = (c - ema8) / c
    f["ema21_dist"] = (c - ema21) / c
    f["ema50_dist"] = (c - ema50) / c
    f["ema8_21"] = (ema8 - ema21) / c
    f["ema21_50"] = (ema21 - ema50) / c
    f["macd"] = macd / c
    f["macd_sig"] = macd_sig / c
    f["macd_hist"] = (macd - macd_sig) / c
    f["bb_pctb"] = (c - (mid - 2 * sd)) / ((4 * sd) + _EPS)
    f["bb_bw"] = (4 * sd) / (mid + _EPS)
    f["roc5"] = c / c.shift(5) - 1.0
    f["roc10"] = c / c.shift(10) - 1.0
    f["ret_sign"] = np.sign(ret)
    f["stoch_k"] = stoch_k
    f["stoch_d"] = stoch_k.rolling(3).mean()
    f["rvol14"] = ret.rolling(14).std()
    f["atr_ratio"] = _atr(h, lo, c, 7) / (_atr(h, lo, c, 28) + _EPS)
    f["dist_hi20"] = (c - max20) / c
    f["dist_lo20"] = (c - min20) / c

    assert f.shape[1] == config.PER_TF_FEATURES, f.shape
    return f


def _global_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Time-of-day / day-of-week / session features on the M5 grid (8 cols)."""
    hour = index.hour + index.minute / 60.0
    dow = index.dayofweek.to_numpy().astype("float64")
    g = pd.DataFrame(index=index)
    g["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    g["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    g["dow_sin"] = np.sin(2 * np.pi * dow / 7.0)
    g["dow_cos"] = np.cos(2 * np.pi * dow / 7.0)
    g["sess_asia"] = ((hour >= 0) & (hour < 8)).astype("float64")
    g["sess_london"] = ((hour >= 8) & (hour < 16)).astype("float64")
    g["sess_ny"] = ((hour >= 13) & (hour < 21)).astype("float64")
    g["week_pos"] = (dow * 24.0 + hour) / (7.0 * 24.0)
    assert g.shape[1] == config.N_GLOBAL_FEATURES, g.shape
    return g


def build_features(
    m1: pd.DataFrame,
    end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build the full 152-column feature matrix on the M5 grid.

    Higher-TF columns are aligned with no lookahead. Leading rows with NaN
    (indicator warmup / not-yet-closed higher-TF bars) are dropped so the
    returned frame is complete; downstream code can rely on no NaNs.
    """
    tf_frames = data.build_tf_frames(m1, end=end)
    base_index = tf_frames[config.BASE_TF].index

    cols: list[pd.DataFrame] = []
    for tf in config.TIMEFRAMES:
        feats = compute_tf_features(tf_frames[tf])
        if tf == config.BASE_TF:
            aligned = feats.reindex(base_index)
        else:
            aligned = data.align_backward(base_index, feats, tf)
        aligned = aligned.add_prefix(f"{tf}_")
        cols.append(aligned)

    cols.append(_global_features(base_index))
    matrix = pd.concat(cols, axis=1)
    matrix = matrix.dropna(how="any")

    assert matrix.shape[1] == config.N_FEATURES, f"expected {config.N_FEATURES} features, got {matrix.shape[1]}"
    return matrix


# --------------------------------------------------------------------------- #
# Train-only scaler (mean/std standardisation, frozen after fit)
# --------------------------------------------------------------------------- #
class FeatureScaler:
    """Per-column standardiser. Fit on train only, frozen, applied everywhere.

    ``transform`` clips to +/- ``clip`` standard deviations so a regime shift in
    val/test cannot hand the network a 50-sigma input.
    """

    def __init__(self, clip: float = 10.0) -> None:
        self.clip = clip
        self.columns: list[str] | None = None
        self.mean_: pd.Series | None = None
        self.std_: pd.Series | None = None

    def fit(self, train_slice: pd.DataFrame) -> FeatureScaler:
        self.columns = list(train_slice.columns)
        self.mean_ = train_slice.mean()
        self.std_ = train_slice.std().replace(0.0, 1.0)
        return self

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        if self.mean_ is None or self.std_ is None:
            raise RuntimeError("FeatureScaler.transform called before fit")
        z = (frame[self.columns] - self.mean_) / self.std_
        return z.clip(lower=-self.clip, upper=self.clip)

    def to_dict(self) -> dict:
        return {
            "clip": self.clip,
            "columns": self.columns,
            "mean": None if self.mean_ is None else self.mean_.to_dict(),
            "std": None if self.std_ is None else self.std_.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> FeatureScaler:
        s = cls(clip=d["clip"])
        s.columns = d["columns"]
        s.mean_ = None if d["mean"] is None else pd.Series(d["mean"])
        s.std_ = None if d["std"] is None else pd.Series(d["std"])
        return s
