"""MetaApi Cloud adapter — real MT5 broker integration via MetaApi SDK.

All MetaApi-specific imports, SDK objects, and translation logic are isolated
to this module.  The public interface returns only novatrade.models types.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from metaapi_cloud_sdk import MetaApi  # vendor SDK — confined to this file

from novatrade.adapter.base import MT5Adapter
from novatrade.config import MetaApiConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    Candle,
    HealthState,
    HealthStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PendingOrder,
    Position,
    SymbolPrice,
)

log = logging.getLogger("novatrade.adapter.metaapi")

# MetaApi trade-response numeric codes that indicate success.
_SUCCESS_CODES = {0, 10008, 10009, 10010, 10025}

# Map NovaTrade timeframe strings to MetaApi format.
_TIMEFRAME_MAP = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
    "W1": "1w",
    "MN1": "1mn",
    # pass-through if already in MetaApi format
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
    "1mn": "1mn",
}


class MetaApiAdapter(MT5Adapter):
    """MetaApi Cloud adapter behind the provider-neutral MT5Adapter contract.

    Uses RPC connection for all operations — simpler and stateless compared
    to the streaming connection.  All vendor objects are translated to
    NovaTrade-native models at the boundary.
    """

    def __init__(self, config: MetaApiConfig) -> None:
        self._config = config
        self._api: MetaApi | None = None
        self._account: Any = None  # MetatraderAccount
        self._connection: Any = None  # RpcMetaApiConnectionInstance
        self._connected = False

    # --- lifecycle -----------------------------------------------------------

    async def connect(self) -> HealthStatus:
        """Initialize MetaApi SDK, deploy account, open RPC connection."""
        t0 = time.monotonic()
        try:
            log.info("metaapi connect: initializing SDK (account_id=%s)", self._config.account_id)
            self._api = MetaApi(
                self._config.token,
                {
                    "domain": self._config.domain,
                    "region": self._config.region,
                    "application": self._config.application,
                },
            )

            self._account = await self._api.metatrader_account_api.get_account(
                self._config.account_id,
            )

            # Deploy if not already deployed.
            if self._account.state != "DEPLOYED":
                log.info("metaapi connect: deploying account (state=%s)", self._account.state)
                await self._account.deploy()
                await self._account.wait_deployed()

            # Wait for terminal + broker connection.
            log.info("metaapi connect: waiting for broker connection")
            await self._account.wait_connected()

            # Open RPC connection.
            self._connection = self._account.get_rpc_connection()
            await self._connection.connect()
            await self._connection.wait_synchronized()

            latency = (time.monotonic() - t0) * 1000
            self._connected = True
            log.info("metaapi connect: ready (%.0f ms)", latency)
            return HealthStatus(
                state=HealthState.OK,
                connected=True,
                latency_ms=latency,
                message="connected and synchronized",
            )
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            self._connected = False
            msg = f"connection failed: {_safe_error(exc)}"
            log.error("metaapi connect: %s", msg)
            return HealthStatus(
                state=HealthState.DOWN,
                connected=False,
                latency_ms=latency,
                message=msg,
            )

    async def disconnect(self) -> None:
        """Close connection and SDK resources."""
        log.info("metaapi disconnect: closing")
        try:
            if self._connection is not None:
                await self._connection.close()
            if self._api is not None:
                self._api.close()
        except Exception as exc:
            log.warning("metaapi disconnect: %s", _safe_error(exc))
        finally:
            self._connection = None
            self._account = None
            self._api = None
            self._connected = False

    async def health_check(self) -> HealthStatus:
        """Probe connection health via a lightweight account-info call."""
        if not self._connected or self._connection is None:
            return HealthStatus(
                state=HealthState.DOWN,
                connected=False,
                message="not connected",
            )
        t0 = time.monotonic()
        try:
            await self._connection.get_account_information()
            latency = (time.monotonic() - t0) * 1000
            return HealthStatus(
                state=HealthState.OK,
                connected=True,
                latency_ms=latency,
                message="ok",
            )
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            self._connected = False
            return HealthStatus(
                state=HealthState.DOWN,
                connected=False,
                latency_ms=latency,
                message=f"health check failed: {_safe_error(exc)}",
            )

    # --- account -------------------------------------------------------------

    async def get_account(self) -> AccountState:
        """Fetch current account snapshot from MetaApi."""
        self._ensure_connected()
        info = await self._connection.get_account_information()
        log.debug("metaapi get_account: balance=%.2f equity=%.2f", info["balance"], info["equity"])
        return _translate_account(info)

    # --- positions -----------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions from MetaApi."""
        self._ensure_connected()
        raw_positions = await self._connection.get_positions()
        log.debug("metaapi get_positions: count=%d", len(raw_positions))
        return [_translate_position(p) for p in raw_positions]

    # --- market data ---------------------------------------------------------

    async def get_symbol_price(self, symbol: str) -> SymbolPrice:
        """Get current bid/ask for a symbol."""
        self._ensure_connected()
        raw = await self._connection.get_symbol_price(symbol)
        log.debug("metaapi get_symbol_price: %s bid=%.5f ask=%.5f", symbol, raw["bid"], raw["ask"])
        return _translate_symbol_price(raw)

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        """Retrieve recent candles via MetaApi historical data API."""
        self._ensure_connected()
        ma_tf = _TIMEFRAME_MAP.get(timeframe, timeframe)
        raw_candles = await self._account.get_historical_candles(
            symbol,
            ma_tf,
            limit=count,
        )
        log.debug("metaapi get_candles: %s %s returned=%d", symbol, ma_tf, len(raw_candles))
        return [_translate_candle(c, symbol, timeframe) for c in raw_candles]

    # --- execution -----------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order to MetaApi.  Respects idempotency_key via comment."""
        self._ensure_connected()
        comment = request.comment or ""
        if request.idempotency_key:
            comment = f"[idem:{request.idempotency_key}] {comment}".strip()

        options = {}
        if comment:
            options["comment"] = comment

        log.info(
            "metaapi place_order: %s %s %s vol=%.2f price=%s sl=%s tp=%s",
            request.side.value,
            request.order_type.value,
            request.symbol,
            request.volume,
            request.price,
            request.stop_loss,
            request.take_profit,
        )

        try:
            raw = await _dispatch_order(self._connection, request, options)
            return _translate_trade_response(raw)
        except Exception as exc:
            msg = _safe_error(exc)
            log.error("metaapi place_order failed: %s", msg)
            return OrderResult(ok=False, error=msg)

    async def modify_order(
        self,
        order_id: str,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        """Modify SL/TP on an existing position."""
        self._ensure_connected()
        log.info("metaapi modify_order: id=%s sl=%s tp=%s", order_id, stop_loss, take_profit)
        try:
            raw = await self._connection.modify_position(
                order_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            return _translate_trade_response(raw)
        except Exception as exc:
            msg = _safe_error(exc)
            log.error("metaapi modify_order failed: %s", msg)
            return OrderResult(ok=False, error=msg)

    async def close_position(
        self,
        position_id: str,
        volume: float | None = None,
    ) -> OrderResult:
        """Close (fully or partially) an open position."""
        self._ensure_connected()
        log.info("metaapi close_position: id=%s volume=%s", position_id, volume)
        try:
            if volume is not None:
                raw = await self._connection.close_position_partially(position_id, volume)
            else:
                raw = await self._connection.close_position(position_id)
            return _translate_trade_response(raw)
        except Exception as exc:
            msg = _safe_error(exc)
            log.error("metaapi close_position failed: %s", msg)
            return OrderResult(ok=False, error=msg)

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a pending order via MetaApi."""
        self._ensure_connected()
        log.info("metaapi cancel_order: id=%s", order_id)
        try:
            raw = await self._connection.cancel_order(order_id)
            return _translate_trade_response(raw)
        except Exception as exc:
            msg = _safe_error(exc)
            log.error("metaapi cancel_order failed: %s", msg)
            return OrderResult(ok=False, error=msg)

    async def get_orders(self) -> list[PendingOrder]:
        """Fetch all pending orders from MetaApi."""
        self._ensure_connected()
        raw_orders = await self._connection.get_orders()
        log.debug("metaapi get_orders: count=%d", len(raw_orders))
        return [_translate_pending_order(o) for o in raw_orders]

    # --- internal ------------------------------------------------------------

    def _ensure_connected(self) -> None:
        """Guard: raise if not connected."""
        if not self._connected or self._connection is None:
            raise ConnectionError("MetaApi adapter is not connected — call connect() first")


# ---------------------------------------------------------------------------
# Translation helpers — vendor dicts → NovaTrade models
# ---------------------------------------------------------------------------


def _translate_account(info: dict) -> AccountState:
    """Convert MetaApi account-information dict to AccountState."""
    acct_type = info.get("type", "")
    if "DEMO" in acct_type.upper():
        mode = AccountMode.DEMO
    elif "CONTEST" in acct_type.upper():
        mode = AccountMode.CHALLENGE
    else:
        mode = AccountMode.DEMO  # default safe

    return AccountState(
        balance=info.get("balance", 0.0),
        equity=info.get("equity", 0.0),
        margin=info.get("margin", 0.0),
        free_margin=info.get("freeMargin", 0.0),
        currency=info.get("currency", "USD"),
        leverage=int(info.get("leverage", 100)),
        mode=mode,
        server=info.get("server", ""),
        broker=info.get("broker", ""),
    )


def _translate_position(p: dict) -> Position:
    """Convert MetaApi position dict to Position."""
    ptype = p.get("type", "")
    side = OrderSide.BUY if "BUY" in ptype.upper() else OrderSide.SELL

    open_time = 0.0
    if "time" in p and isinstance(p["time"], datetime):
        open_time = p["time"].timestamp()

    return Position(
        position_id=str(p.get("id", "")),
        symbol=p.get("symbol", ""),
        side=side,
        volume=p.get("volume", 0.0),
        open_price=p.get("openPrice", 0.0),
        current_price=p.get("currentPrice", 0.0),
        unrealized_pnl=p.get("unrealizedProfit", p.get("profit", 0.0)),
        stop_loss=p.get("stopLoss"),
        take_profit=p.get("takeProfit"),
        open_time=open_time,
        comment=p.get("comment", ""),
    )


def _translate_symbol_price(raw: dict) -> SymbolPrice:
    """Convert MetaApi symbol-price dict to SymbolPrice."""
    ts = 0.0
    if "time" in raw and isinstance(raw["time"], datetime):
        ts = raw["time"].timestamp()
    return SymbolPrice(
        symbol=raw.get("symbol", ""),
        bid=raw.get("bid", 0.0),
        ask=raw.get("ask", 0.0),
        timestamp=ts or time.time(),
    )


def _translate_candle(c: dict, symbol: str, timeframe: str) -> Candle:
    """Convert MetaApi candle dict to Candle."""
    ts = 0.0
    if "time" in c and isinstance(c["time"], datetime):
        ts = c["time"].timestamp()
    return Candle(
        timestamp=ts,
        open=c.get("open", 0.0),
        high=c.get("high", 0.0),
        low=c.get("low", 0.0),
        close=c.get("close", 0.0),
        volume=c.get("tickVolume", c.get("volume", 0.0)),
        symbol=symbol,
        timeframe=timeframe,
    )


def _translate_pending_order(o: dict) -> PendingOrder:
    """Convert MetaApi order dict to PendingOrder."""
    otype_raw = o.get("type", "")
    if "BUY" in otype_raw.upper():
        side = OrderSide.BUY
    else:
        side = OrderSide.SELL

    if "STOP_LIMIT" in otype_raw.upper():
        otype = OrderType.STOP_LIMIT
    elif "STOP" in otype_raw.upper():
        otype = OrderType.STOP
    elif "LIMIT" in otype_raw.upper():
        otype = OrderType.LIMIT
    else:
        otype = OrderType.MARKET

    created = 0.0
    if "time" in o and isinstance(o["time"], datetime):
        created = o["time"].timestamp()

    return PendingOrder(
        order_id=str(o.get("id", "")),
        symbol=o.get("symbol", ""),
        side=side,
        order_type=otype,
        volume=o.get("volume", 0.0),
        open_price=o.get("openPrice", 0.0),
        stop_loss=o.get("stopLoss"),
        take_profit=o.get("takeProfit"),
        comment=o.get("comment", ""),
        created_time=created,
    )


def _translate_trade_response(raw: dict) -> OrderResult:
    """Convert MetaApi trade-response dict to OrderResult."""
    code = raw.get("numericCode", -1)
    ok = code in _SUCCESS_CODES
    string_code = raw.get("stringCode", "")

    if "DONE_PARTIAL" in string_code:
        status = OrderStatus.PARTIALLY_FILLED
    elif "DONE" in string_code or "PLACED" in string_code or code == 0:
        status = OrderStatus.FILLED
    elif "REJECT" in string_code:
        status = OrderStatus.REJECTED
    elif "CANCEL" in string_code:
        status = OrderStatus.CANCELLED
    else:
        status = OrderStatus.PENDING

    return OrderResult(
        ok=ok,
        order_id=str(raw.get("orderId", raw.get("positionId", ""))),
        status=status,
        error="" if ok else raw.get("message", f"code={code} {string_code}"),
        broker_raw=raw,
    )


# ---------------------------------------------------------------------------
# Order dispatch — maps NovaTrade OrderType×Side to MetaApi SDK methods
# ---------------------------------------------------------------------------


async def _dispatch_order(conn, request: OrderRequest, options: dict) -> dict:
    """Route an OrderRequest to the correct MetaApi create_* method."""
    side = request.side
    otype = request.order_type
    sl = request.stop_loss
    tp = request.take_profit

    if otype == OrderType.MARKET:
        if side == OrderSide.BUY:
            return await conn.create_market_buy_order(
                request.symbol,
                request.volume,
                sl,
                tp,
                options or None,
            )
        else:
            return await conn.create_market_sell_order(
                request.symbol,
                request.volume,
                sl,
                tp,
                options or None,
            )

    elif otype == OrderType.LIMIT:
        if side == OrderSide.BUY:
            return await conn.create_limit_buy_order(
                request.symbol,
                request.volume,
                request.price,
                sl,
                tp,
                options or None,
            )
        else:
            return await conn.create_limit_sell_order(
                request.symbol,
                request.volume,
                request.price,
                sl,
                tp,
                options or None,
            )

    elif otype == OrderType.STOP:
        if side == OrderSide.BUY:
            return await conn.create_stop_buy_order(
                request.symbol,
                request.volume,
                request.price,
                sl,
                tp,
                options or None,
            )
        else:
            return await conn.create_stop_sell_order(
                request.symbol,
                request.volume,
                request.price,
                sl,
                tp,
                options or None,
            )

    elif otype == OrderType.STOP_LIMIT:
        if side == OrderSide.BUY:
            return await conn.create_stop_limit_buy_order(
                request.symbol,
                request.volume,
                request.price,
                request.price,
                sl,
                tp,
                options or None,
            )
        else:
            return await conn.create_stop_limit_sell_order(
                request.symbol,
                request.volume,
                request.price,
                request.price,
                sl,
                tp,
                options or None,
            )

    raise ValueError(f"unsupported order type: {otype}")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _safe_error(exc: Exception) -> str:
    """Format exception without leaking secrets (e.g. tokens in URLs)."""
    msg = str(exc)
    # Strip anything that looks like a bearer token or API key
    for prefix in ("token=", "Token ", "Bearer ", "apiKey="):
        if prefix.lower() in msg.lower():
            idx = msg.lower().index(prefix.lower())
            msg = msg[:idx] + prefix + "***REDACTED***"
            break
    return msg
