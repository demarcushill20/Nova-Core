"""Month-End Rebalancing-Flow strategy — pure logic (no I/O, fully testable).

Validated edge (2026-06-11, 2004-2024, 84 quarters): currency-hedged international
equity investors rebalance FX hedges into the 16:00 London (WM/R) fix on the last
business day of each quarter. When equities rose over the quarter they must SELL
USD into the fix -> non-USD currencies drift UP pre-fix. Signal = sign of the
quarter-to-date S&P 500 return (computed with NO look-ahead). Enter the basket a
few hours before the fix, exit AT the fix. Quarter-ends only (largest flows);
non-JPY USD legs (JPY transmits the flow poorly).

Backtest: quarterly Sharpe +0.67 (t=3.07, DSR 0.984), 18/21 positive years,
holdout (2017-2024) stronger than train. See OUTPUT/ensemble_gauntlet2.py and the
project_strategy_search memory.

This module is import-clean (no network/secrets) so the service and tests share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

QUARTER_END_MONTHS = frozenset({3, 6, 9, 12})


@dataclass(frozen=True)
class Leg:
    symbol: str
    usd_is_base: bool  # True for USDxxx (selling USD == SELL the pair)


# The validated non-JPY USD-leg basket. Each leg expresses "USD sold into the fix".
DEFAULT_BASKET: tuple[Leg, ...] = (
    Leg("EURUSD", usd_is_base=False),
    Leg("GBPUSD", usd_is_base=False),
    Leg("AUDUSD", usd_is_base=False),
    Leg("NZDUSD", usd_is_base=False),
    Leg("USDCAD", usd_is_base=True),
)


@dataclass
class MonthEndConfig:
    basket: tuple[Leg, ...] = DEFAULT_BASKET
    hours_before_fix: int = 4  # enter 4h before the 16:00 London fix
    fix_hour_london: int = 16  # WM/R 16:00 London
    leverage: float = 5.0  # total basket notional = leverage * equity
    contract_size: float = 100_000.0
    min_lot: float = 0.01
    max_lot: float = 50.0
    lot_step: float = 0.01
    quarter_ends_only: bool = True


def _round_step(x: float, step: float) -> float:
    return round(round(x / step) * step, 8)


def next_business_day(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:  # Sat/Sun
        n += timedelta(days=1)
    return n


def is_trade_day(d: date, cfg: MonthEndConfig | None = None) -> bool:
    """True if `d` is the last business day of a quarter-end month (Mon-Fri, and
    the next business day falls in a different month). Holiday edge cases (rare at
    quarter-end) are not modelled; the broker simply rejects on a closed market."""
    cfg = cfg or MonthEndConfig()
    if d.weekday() >= 5:
        return False
    months = QUARTER_END_MONTHS if cfg.quarter_ends_only else frozenset(range(1, 13))
    if d.month not in months:
        return False
    return next_business_day(d).month != d.month


def compute_signal(sp_closes, asof: date) -> int | None:
    """Sign (+1 sell-USD / -1 buy-USD) of the quarter-to-date S&P return, using
    only data strictly BEFORE `asof` (the fix precedes the US equity close, so the
    last usable close is the prior trading day's).

    sp_closes: mapping/Series of date -> close (any prior-sorted iterable of
    (date, close)). Returns None if insufficient history.
    """
    import pandas as pd

    s = pd.Series(dict(sp_closes)) if not isinstance(sp_closes, pd.Series) else sp_closes
    s = s.sort_index()
    s.index = pd.to_datetime(s.index)
    asof_ts = pd.Timestamp(asof)
    prior = s.index[s.index < asof_ts.normalize()]
    if len(prior) < 2:
        return None
    sig_date = prior[-1]
    month_start = asof_ts.replace(day=1).normalize()
    pm_candidates = s.index[s.index < month_start]
    if len(pm_candidates) == 0:
        return None
    pm_end = pm_candidates[-1]
    ret = float(s.loc[sig_date] / s.loc[pm_end] - 1.0)
    return 1 if ret > 0 else -1


@dataclass
class BasketOrder:
    symbol: str
    side: str  # "BUY" | "SELL"
    lot: float


def basket_orders(signal_sign: int, equity: float, cfg: MonthEndConfig | None = None) -> list[BasketOrder]:
    """Translate the signal into per-leg market orders. signal_sign>0 means SELL
    USD (long EUR/GBP/AUD/NZD, short USDCAD). Equal notional per leg."""
    cfg = cfg or MonthEndConfig()
    n = len(cfg.basket)
    per_leg_notional = (cfg.leverage * equity) / n
    lot = _round_step(per_leg_notional / cfg.contract_size, cfg.lot_step)
    lot = max(cfg.min_lot, min(cfg.max_lot, lot))
    orders = []
    for leg in cfg.basket:
        direction = signal_sign * (-1 if leg.usd_is_base else 1)  # sell USD
        orders.append(BasketOrder(leg.symbol, "BUY" if direction > 0 else "SELL", lot))
    return orders
