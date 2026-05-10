from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    contract_size: float
    tick_size: float
    tick_value: float
    min_lot: float = 0.01
    lot_step: float = 0.01


@dataclass(frozen=True)
class PositionPlan:
    direction: str
    symbol: str
    entry: float
    sl: float
    tp: float
    lot: float


def _round_lot(lot: float, step: float, min_lot: float) -> float:
    if lot < min_lot:
        return 0.0
    return floor(lot / step) * step


def allocate_positions(
    *,
    balance: float,
    account_risk_pct: float,
    direction: str,
    entry: float,
    sl: float,
    tps: list[float],
    symbol: SymbolSpec,
) -> list[PositionPlan]:
    if not tps:
        return []
    risk_usd = balance * account_risk_pct
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return []
    ticks = sl_distance / symbol.tick_size
    risk_per_lot = ticks * symbol.tick_value
    if risk_per_lot <= 0:
        return []
    total_lot = risk_usd / risk_per_lot
    n = len(tps)
    per_lot = total_lot / n
    rounded = _round_lot(per_lot, symbol.lot_step, symbol.min_lot)
    if rounded <= 0:
        return []
    return [
        PositionPlan(
            direction=direction,
            symbol=symbol.symbol,
            entry=entry,
            sl=sl,
            tp=tp,
            lot=rounded,
        )
        for tp in tps
    ]


_REGISTRY: dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec("XAUUSD", contract_size=100.0, tick_size=0.01, tick_value=1.0),
    "XAGUSD": SymbolSpec("XAGUSD", contract_size=5000.0, tick_size=0.001, tick_value=5.0),
    "EURUSD": SymbolSpec("EURUSD", contract_size=100_000.0, tick_size=0.0001, tick_value=10.0),
    "GBPUSD": SymbolSpec("GBPUSD", contract_size=100_000.0, tick_size=0.0001, tick_value=10.0),
    "US30": SymbolSpec("US30", contract_size=1.0, tick_size=1.0, tick_value=1.0),
    "NAS100": SymbolSpec("NAS100", contract_size=1.0, tick_size=0.1, tick_value=0.1),
}


def get_symbol_spec(symbol: str) -> SymbolSpec:
    return _REGISTRY[symbol]
