from __future__ import annotations

import logging

from .config import Config
from .models import ParsedSignal
from .sizing import allocate_positions, get_symbol_spec
from .storage import Storage

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(self, *, storage: Storage, cfg: Config, balance: float = 10_000.0):
        self.storage = storage
        self.cfg = cfg
        self.balance = balance

    def _broker_symbol(self, signal_symbol: str) -> str:
        return self.cfg.symbol_map.get(signal_symbol, signal_symbol)

    async def execute(self, signal_id: int, signal: ParsedSignal) -> int:
        if signal.symbol is None or signal.direction is None or signal.sl is None:
            log.warning("Signal %s missing required fields; skipping", signal_id)
            return 0
        broker_symbol = self._broker_symbol(signal.symbol)
        try:
            spec = get_symbol_spec(broker_symbol)
        except KeyError:
            log.warning("No symbol spec for %s; skipping", broker_symbol)
            return 0
        # Stand-in entry: SL + 10% of SL→TP1 distance.
        # Phase 4 replaces this with a real MetaApi tick.
        if signal.direction.value == "BUY":
            entry = signal.sl + (signal.tps[0] - signal.sl) * 0.1
        else:
            entry = signal.sl - (signal.sl - signal.tps[0]) * 0.1
        plans = allocate_positions(
            balance=self.balance,
            account_risk_pct=self.cfg.risk.account_risk_pct,
            direction=signal.direction.value,
            entry=entry,
            sl=signal.sl,
            tps=signal.tps,
            symbol=spec,
        )
        if not plans:
            log.warning(
                "Signal %s allocated 0 positions (below min_lot or invalid SL)",
                signal_id,
            )
            return 0
        for p in plans:
            await self.storage.log_paper_fill(
                signal_id=signal_id,
                direction=p.direction,
                symbol=p.symbol,
                entry=p.entry,
                sl=p.sl,
                tp=p.tp,
                lot=p.lot,
            )
            log.info(
                "PAPER FILL sig=%s %s %s lot=%s entry=%s sl=%s tp=%s",
                signal_id,
                p.direction,
                p.symbol,
                p.lot,
                p.entry,
                p.sl,
                p.tp,
            )
        return len(plans)
