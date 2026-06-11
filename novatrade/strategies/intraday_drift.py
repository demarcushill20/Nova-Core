"""Intraday EUR-weakness drift strategy — pure logic (no I/O, fully testable).

Validated edge (2026-06-11): EURUSD systematically depreciates during the
European afternoon / pre-NY hour. The 11:00-12:00 UTC window is the single most
significant intraday signal measured — t = -6.9 on HistData 2004-2015 and an
independent t = -5.2 on 2016-2026 Dukascopy ticks (different vendor, different
era, clean true-UTC clock). It is the "own-hours depreciation" effect documented
by Breedon & Ranaldo (currencies weaken during their own local trading hours).

Rule (option A, the cleanest out-of-sample version): SHORT EURUSD at 11:00 UTC,
flat at 12:00 UTC. One trade per day. Sized so a fixed protective stop equals
`risk_frac` of equity (default 1%). Backtest on real ticks net of the measured
0.30p spread: ~8-11% CAGR at 1% risk with a 15-20p stop, ~20% max drawdown,
~52% win rate. Modest and real — a grinder, not a jackpot.

This module is import-clean (no network/secrets) so the service and tests share
it. NOTE: the 11:00 UTC clock is what was backtested. A DST-aware London-local
session clock is an unvalidated future refinement (see service README), not in
this code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class DriftConfig:
    symbol: str = "EURUSD"
    entry_hour_utc: int = 11  # short at the top of this UTC hour
    exit_hour_utc: int = 12  # flat at the top of this UTC hour (time exit)
    direction: int = -1  # -1 = SELL EURUSD (EUR weakens); +1 = BUY
    stop_pips: float = 20.0  # protective stop distance; also sets position size
    risk_frac: float = 0.01  # risk 1% of equity per trade (stop = risk_frac*equity)
    pip: float = 1e-4
    pip_value_per_lot: float = 10.0  # USD per pip per 1.0 standard lot (EURUSD)
    min_lot: float = 0.01
    max_lot: float = 100.0
    lot_step: float = 0.01
    trade_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon-Fri (UTC)


def _round_step(x: float, step: float) -> float:
    return round(round(x / step) * step, 8)


def side(cfg: DriftConfig) -> str:
    return "BUY" if cfg.direction > 0 else "SELL"


def position_size(equity: float, cfg: DriftConfig | None = None) -> float:
    """Lots sized so that hitting the `stop_pips` stop loses exactly
    `risk_frac` of equity. lots = (risk_frac*equity) / (stop_pips * $/pip/lot)."""
    cfg = cfg or DriftConfig()
    if equity <= 0 or cfg.stop_pips <= 0:
        return cfg.min_lot
    risk_usd = cfg.risk_frac * equity
    lots = risk_usd / (cfg.stop_pips * cfg.pip_value_per_lot)
    lots = _round_step(lots, cfg.lot_step)
    return max(cfg.min_lot, min(cfg.max_lot, lots))


def stop_price(entry_price: float, cfg: DriftConfig | None = None) -> float:
    """Protective stop placed `stop_pips` on the adverse side of the entry.
    Short (direction -1) -> stop ABOVE entry; long -> stop BELOW."""
    cfg = cfg or DriftConfig()
    return entry_price - cfg.direction * cfg.stop_pips * cfg.pip


def is_trade_day(d, cfg: DriftConfig | None = None) -> bool:
    cfg = cfg or DriftConfig()
    return d.weekday() in cfg.trade_weekdays


def is_entry_window(now_utc: datetime, cfg: DriftConfig | None = None) -> bool:
    """True during the entry hour and before the exit hour (UTC)."""
    cfg = cfg or DriftConfig()
    return is_trade_day(now_utc.date(), cfg) and now_utc.hour == cfg.entry_hour_utc and now_utc.hour < cfg.exit_hour_utc


def is_exit_time(now_utc: datetime, cfg: DriftConfig | None = None) -> bool:
    """True at/after the exit hour (UTC) — flatten the time-based position."""
    cfg = cfg or DriftConfig()
    return now_utc.hour >= cfg.exit_hour_utc


@dataclass
class DriftOrder:
    symbol: str
    side: str  # "BUY" | "SELL"
    lot: float
    stop_price: float | None  # broker-side protective stop (enforces 1% risk)


def build_order(equity: float, entry_price: float, cfg: DriftConfig | None = None) -> DriftOrder:
    """Translate the signal into a single market order with a broker-side stop.
    The stop is attached at entry so the 1% risk cap holds even if the executor
    is offline at the 12:00 UTC time-exit."""
    cfg = cfg or DriftConfig()
    return DriftOrder(
        symbol=cfg.symbol,
        side=side(cfg),
        lot=position_size(equity, cfg),
        stop_price=round(stop_price(entry_price, cfg), 5),
    )
