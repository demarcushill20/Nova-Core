"""Carry-basket strategy — pure dollar-neutral HML carry + de-risk-only vol scaling.

Mirrors novatrade.strategies.intraday_drift: NO network/file I/O, fully unit-testable.
Given monthly spot (USD per 1 unit foreign; USD column = 1.0) and lagged 3M annualized
rate panels, produces dollar-neutral long-high-yield / short-low-yield target weights and
a DE-RISK-ONLY volatility scalar (never levers above lev_cap=1.0x). carry_backtest uses
rank_weights as the SINGLE basket constructor (k = round(n_available*k_frac)) so the live
shadow path and the backtest produce identical target weights and the long/short legs can
never overlap. Coherent full-history numbers: Sharpe ~0.54, maxDD ~-31% (de-risk-only).
Data fetching lives in the caller, never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD"]
EM = ["MXN", "ZAR"]


def _default_cost() -> dict[str, float]:
    cost = {c: 2.0 for c in G10}
    cost.update({"MXN": 8.0, "ZAR": 10.0})
    return cost


@dataclass
class CarryConfig:
    currencies: tuple[str, ...] = tuple(G10 + EM)
    k_frac: float = 0.34
    vol_target: float = 0.08
    vol_window: int = 6
    lev_cap: float = 1.0
    cost_bps: dict = field(default_factory=_default_cost)


def rank_weights(lagged_rates: pd.Series, k_frac: float = 0.34) -> pd.Series:
    """Dollar-neutral HML weights from lagged short rates: long top-k highest-rate,
    short bottom-k lowest-rate, equal-weight, sum == 0. Returns all-zero if < 4 names."""
    r = lagged_rates.dropna()
    n = len(r)
    w = pd.Series(0.0, index=lagged_rates.index)
    if n < 4:
        return w
    k = max(1, round(n * k_frac))
    order = r.sort_values()
    w[order.index[-k:]] = 1.0 / k
    w[order.index[:k]] = -1.0 / k
    return w


def derisk_scalar(recent_returns: pd.Series, cfg: CarryConfig | None = None) -> float:
    """De-risk-ONLY vol scalar = min(lev_cap, vol_target / trailing realized vol).
    Returns lev_cap when history is insufficient or vol is zero (never scales above cap)."""
    cfg = cfg or CarryConfig()
    r = recent_returns.dropna()
    if len(r) < cfg.vol_window:
        return cfg.lev_cap
    rv = r.iloc[-cfg.vol_window :].std() * np.sqrt(12)
    if rv <= 0:
        return cfg.lev_cap
    return float(min(cfg.lev_cap, cfg.vol_target / rv))


def carry_returns_raw(spot: pd.DataFrame, rates: pd.DataFrame, cfg: CarryConfig | None = None) -> pd.Series:
    """Monthly HML carry returns BEFORE de-risk scaling, net of cost. The single source
    of the carry strategy's return stream — both carry_backtest and the shadow job
    de-risk off THIS series."""
    cfg = cfg or CarryConfig()
    ccys = [c for c in cfg.currencies if c in spot.columns and c in rates.columns]
    S, R = spot[ccys], rates[ccys]
    spot_ret = S.pct_change()
    rdiff = R.sub(R["USD"], axis=0) / 1200.0
    xs = spot_ret + rdiff.shift(1)
    rrank = R.shift(1)
    rows: dict = {}
    prev_long: set[str] = set()
    prev_short: set[str] = set()
    for dt in xs.index[2:]:
        rk = rrank.loc[dt].dropna()
        x = xs.loc[dt].dropna()
        avail = [c for c in rk.index if c in x.index]
        if len(avail) < 4:
            continue
        w = rank_weights(rk[avail], cfg.k_frac)
        if (w == 0.0).all():
            continue
        longs = set(w[w > 0].index)
        shorts = set(w[w < 0].index)
        ret = float((w * x.reindex(w.index).fillna(0.0)).sum())
        k = max(1, len(longs))
        turn = len(longs ^ prev_long) + len(shorts ^ prev_short)
        c = float(np.mean([cfg.cost_bps.get(name, 5.0) for name in (longs | shorts)])) * 1e-4
        ret -= turn / (2 * k) * c
        rows[dt] = ret
        prev_long, prev_short = longs, shorts
    return pd.Series(rows).sort_index().rename("carry_raw")


def carry_backtest(spot: pd.DataFrame, rates: pd.DataFrame, cfg: CarryConfig | None = None) -> pd.Series:
    """Monthly net carry returns with de-risk-only vol scaling (de-risks off the HML
    series from carry_returns_raw). spot = USD per 1 unit foreign (USD column = 1.0);
    rates = 3M annualized %. Reproduces the de-risk-only carry of
    scripts/probe_carry_portfolio.py."""
    cfg = cfg or CarryConfig()
    raw = carry_returns_raw(spot, rates, cfg)
    rv = raw.rolling(cfg.vol_window).std() * np.sqrt(12)
    lev = (cfg.vol_target / rv.shift(1)).clip(upper=cfg.lev_cap).fillna(cfg.lev_cap)
    return (lev * raw).rename("carry")
