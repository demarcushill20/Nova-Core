"""Multi-sleeve paper book — pure sleeve/blend logic (no I/O, no broker).

The validated 4-sleeve book from the FX alpha loop (2026-07-12, commit 2197f90):
  eur_short  0.58   11:00->12:00 UTC EURUSD short (measured from real hour prices)
  carry      0.22   crash-gated G10 HML carry, monthly book marked daily
  gold_tsmom 0.12   12m+3m sign TSMOM, vol-target 10%, cap 2x
  btc_gated  0.09   same TSMOM spec on BTC, scaled by the monthly vol-regime gate

Backtest reference: Sharpe +1.88 full / +1.96 sealed 27mo (121mo window);
paper deployment at LEVERAGE = 6x -> expected ~22% CAGR, daily maxDD ~ -13-15%.

Everything here is deterministic and unit-tested; the service layer only feeds
price frames in and writes ledgers out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import pandas as pd

# frozen from WORK/multisleeve_final_candidate.json (inverse-vol on pre-2024 data)
BLEND_WEIGHTS: dict[str, float] = {
    "eur_short": 0.5758,
    "carry": 0.2214,
    "gold_tsmom": 0.1151,
    "btc_gated": 0.0877,
}
LEVERAGE = 6.0

PIP = 1e-4
G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"]


@dataclass
class TsmomConfig:
    lookback_long: int = 252
    lookback_short: int = 63
    vol_window: int = 63
    vol_target: float = 0.10
    pos_cap: float = 2.0
    cost_bp: float = 2.0


def tsmom_positions(close: pd.Series, cfg: TsmomConfig) -> pd.Series:
    """Daily target position (signed leverage on the asset) — lag-safe at use time
    (caller applies pos.shift(1) to next-day returns)."""
    ret = close.pct_change(fill_method=None)
    vol = ret.rolling(cfg.vol_window, min_periods=40).std() * np.sqrt(252)
    sig = 0.5 * np.sign(close.pct_change(cfg.lookback_long, fill_method=None)) + 0.5 * np.sign(
        close.pct_change(cfg.lookback_short, fill_method=None)
    )
    return (sig * (cfg.vol_target / vol)).clip(-cfg.pos_cap, cfg.pos_cap)


def tsmom_daily_returns(close: pd.Series, cfg: TsmomConfig) -> pd.Series:
    """Net daily sleeve returns from a daily close series."""
    ret = close.pct_change(fill_method=None)
    pos = tsmom_positions(close, cfg)
    return (pos.shift(1) * ret - pos.diff().abs() * cfg.cost_bp * 1e-4).dropna()


def eur_short_daily_return(bid_1100: float, ask_1100: float, bid_1200: float, ask_1200: float) -> float:
    """One day of the 11:00->12:00 short: sell 11:00 bid, buy back 12:00 ask.
    Return on unit notional (spread fully paid)."""
    mid = (bid_1100 + ask_1100) / 2.0
    return (bid_1100 - ask_1200) / mid


def vol_regime_gate(daily_spots: pd.DataFrame, min_history: int = 36) -> pd.Series:
    """Monthly exposure gate in [0,1]: expanding percentile of cross-sectional mean
    63d vol; 1.0 below the 70th pctile, linear to 0.0 at the 95th. Lagged one month.
    Mirrors scripts/probe_fx_multifactor_v2.carry_gate (validated component)."""
    dret = daily_spots.pct_change(fill_method=None)
    vol = dret.rolling(63, min_periods=40).std() * np.sqrt(252)
    g = vol.mean(axis=1).resample("ME").last()
    pct = g.expanding(min_periods=min_history).apply(
        lambda a: float((a[:-1] <= a[-1]).mean()) if len(a) > 1 else 0.5, raw=True
    )
    return (((0.95 - pct) / 0.25).clip(0.0, 1.0)).shift(1)


@dataclass
class CarryBook:
    """A monthly HML carry book: per-currency signed weights vs USD (sum long = +1,
    sum short = -1 before gate scaling), formed at month-end for the next month."""

    weights: dict[str, float] = field(default_factory=dict)
    gate: float = 1.0

    def scaled(self) -> dict[str, float]:
        return {c: w * self.gate for c, w in self.weights.items()}


def form_carry_book(rates: pd.Series, vols: pd.Series, gate: float, k: int = 3) -> CarryBook:
    """rates: last known 3M rate per G10 ccy (incl USD). vols: 63d ann vol per ccy.
    Long top-k by rate, short bottom-k, inverse-vol weights within each leg."""
    r = rates.dropna().drop(labels=["USD"], errors="ignore")
    if len(r) < 2 * k:
        return CarryBook({}, gate)
    order = r.sort_values()
    shorts, longs = list(order.index[:k]), list(order.index[-k:])
    w: dict[str, float] = {}
    for side, ccys in ((1.0, longs), (-1.0, shorts)):
        iv = {c: 1.0 / float(vols.get(c, 0.10) or 0.10) for c in ccys}
        tot = sum(iv.values())
        for c, v in iv.items():
            w[c] = side * v / tot
    return CarryBook(w, gate)


def carry_daily_return(book: CarryBook, spot_ret: pd.Series, rate_diff_annual: pd.Series) -> float:
    """Mark the book for one day: spot return + daily rate accrual per leg."""
    total = 0.0
    for c, w in book.scaled().items():
        sr = float(spot_ret.get(c, 0.0) or 0.0)
        accr = float(rate_diff_annual.get(c, 0.0) or 0.0) / 100.0 / 252.0
        total += w * (sr + accr)
    return total


def blend_daily_return(sleeve_returns: dict[str, float], leverage: float = LEVERAGE) -> float:
    """Weighted, levered daily book return. Missing sleeves count as 0 (flat)."""
    base = sum(BLEND_WEIGHTS[s] * sleeve_returns.get(s, 0.0) for s in BLEND_WEIGHTS)
    return base * leverage


def ledger_stats(daily: pd.Series) -> dict[str, float]:
    """Running stats for the paper report."""
    d = daily.dropna()
    if len(d) < 5:
        return {"n": float(len(d))}
    ann_ret = d.mean() * 252
    ann_vol = d.std() * np.sqrt(252)
    eq = (1 + d).cumprod()
    dd = float(((eq - eq.cummax()) / eq.cummax()).min())
    return {
        "n": float(len(d)),
        "cum_ret": float(eq.iloc[-1] - 1),
        "ann_ret": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(ann_ret / ann_vol) if ann_vol else float("nan"),
        "max_dd": dd,
        "worst_day": float(d.min()),
    }
