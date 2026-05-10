from __future__ import annotations

import asyncio
import logging
from typing import Any

from metaapi_cloud_sdk import MetaApi

from .config import Config
from .models import ParsedSignal
from .sizing import allocate_positions, get_symbol_spec
from .storage import Storage

log = logging.getLogger(__name__)


class MetaApiAdapter:
    def __init__(self, *, token: str, account_id: str, region: str = "new-york"):
        self._api = MetaApi(token)
        self._account_id = account_id
        self._region = region
        self._account: Any = None
        self._connection: Any = None

    async def connect(self) -> None:
        self._account = await self._api.metatrader_account_api.get_account(self._account_id)
        if self._account.state != "DEPLOYED":
            await self._account.deploy()
        await self._account.wait_connected()
        self._connection = self._account.get_streaming_connection()
        await self._connection.connect()
        await self._connection.wait_synchronized()
        log.info("MetaApi connected; state=%s", self._account.state)

    async def balance(self) -> float:
        info = self._connection.terminal_state.account_information
        if info is None:
            raise RuntimeError("account_information not available; connection not synchronized")
        return float(info["balance"])

    async def get_price(self, symbol: str) -> tuple[float, float]:
        terminal = self._connection.terminal_state
        price = terminal.price(symbol)
        if price is None:
            await self._connection.subscribe_to_market_data(symbol)
            await asyncio.sleep(2)
            price = terminal.price(symbol)
        if price is None:
            raise RuntimeError(f"No price for {symbol}")
        return float(price["bid"]), float(price["ask"])

    async def place_market(
        self,
        *,
        symbol: str,
        direction: str,
        lot: float,
        sl: float,
        tp: float,
        comment: str = "",
    ) -> str:
        method = (
            self._connection.create_market_buy_order
            if direction == "BUY"
            else self._connection.create_market_sell_order
        )
        result = await method(symbol, lot, sl, tp, {"comment": comment[:31]})
        return str(result.get("orderId") or result.get("positionId") or result)

    async def modify_position_sl(self, position_id: str, new_sl: float) -> None:
        await self._connection.modify_position(position_id, stop_loss=new_sl)

    async def close_position(self, position_id: str) -> None:
        await self._connection.close_position(position_id)

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()


class MetaApiExecutor:
    def __init__(self, *, adapter: MetaApiAdapter, storage: Storage, cfg: Config):
        self.adapter = adapter
        self.storage = storage
        self.cfg = cfg

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
        balance = await self.adapter.balance()
        bid, ask = await self.adapter.get_price(broker_symbol)
        entry = ask if signal.direction.value == "BUY" else bid
        plans = allocate_positions(
            balance=balance,
            account_risk_pct=self.cfg.risk.account_risk_pct,
            direction=signal.direction.value,
            entry=entry,
            sl=signal.sl,
            tps=signal.tps,
            symbol=spec,
        )
        if not plans:
            log.warning("Signal %s allocated 0 positions", signal_id)
            return 0
        for p in plans:
            try:
                order_id = await self.adapter.place_market(
                    symbol=p.symbol,
                    direction=p.direction,
                    lot=p.lot,
                    sl=p.sl,
                    tp=p.tp,
                    comment=f"sig{signal_id}",
                )
            except Exception:
                log.exception("place_market failed sig=%s tp=%s", signal_id, p.tp)
                continue
            await self.storage.log_metaapi_fill(
                signal_id=signal_id,
                broker_order_id=order_id,
                direction=p.direction,
                symbol=p.symbol,
                entry=entry,
                sl=p.sl,
                tp=p.tp,
                lot=p.lot,
            )
            log.info(
                "METAAPI FILL sig=%s order=%s %s %s lot=%s sl=%s tp=%s",
                signal_id,
                order_id,
                p.direction,
                p.symbol,
                p.lot,
                p.sl,
                p.tp,
            )
        return len(plans)
