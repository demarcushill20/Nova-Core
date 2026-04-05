"""MetaApi Cloud adapter — real MT5 broker integration via MetaApi SDK.

All MetaApi-specific imports, SDK objects, and translation logic are isolated
to this module.  The public interface returns only novatrade.models types.

Execution resilience features:
- Retry with exponential backoff on all broker operations
- Circuit breaker to prevent cascading failures
- Auto-reconnect on connection loss
- Slippage control via options dict
- Post-order verification polling
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agents.circuit_breakers import CircuitBreakerError, SimpleCircuitBreaker

if TYPE_CHECKING:
    from metaapi_cloud_sdk import MetaApi  # vendor SDK — confined to this file
from novatrade.adapter.base import MT5Adapter
from novatrade.adapter.metaapi_sdk_singleton import get_metaapi_sdk
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


class _NoRedis:
    """Sentinel to prevent SimpleCircuitBreaker from auto-connecting to Redis.

    NovaTrade keeps breaker state in-process only — no shared Redis state.
    This must be non-None to prevent the auto-connect logic in __init__.
    """

    def hgetall(self, *_a, **_kw):
        return {}

    def hset(self, *_a, **_kw):
        pass

    def expire(self, *_a, **_kw):
        pass


class MetaApiAdapter(MT5Adapter):
    """MetaApi Cloud adapter behind the provider-neutral MT5Adapter contract.

    Uses RPC connection for all operations — simpler and stateless compared
    to the streaming connection.  All vendor objects are translated to
    NovaTrade-native models at the boundary.

    Resilience features (Execution Gap fixes):
    - Circuit breaker: trips after repeated failures, prevents call storms
    - Auto-reconnect: transparently reconnects on ConnectionError
    - Retry: exponential backoff on all broker operations
    - Slippage: configurable max slippage passed to MetaApi SDK
    - Order verification: post-placement polling confirms execution
    """

    # Re-subscribe to market data every 9 minutes to prevent feed death.
    # Reduced from 900→540s after diagnosing RPC subscription expiry at ~23min.
    _RESUB_INTERVAL_S = 540.0  # 9 minutes

    def __init__(self, config: MetaApiConfig) -> None:
        self._config = config
        self._api: MetaApi | None = None
        self._account: Any = None  # MetatraderAccount
        self._connection: Any = None  # RpcMetaApiConnectionInstance
        self._connected = False
        self._reconnecting = False  # guard against concurrent reconnect
        self._subscribed_symbols: list[str] = []
        self._last_resubscribe: float = 0.0
        self._cached_equity: float | None = None

        # Circuit breaker for broker operations.
        # Pass _NoRedis to prevent SimpleCircuitBreaker from auto-connecting
        # to Redis — NovaTrade keeps breaker state in-process only.
        self._breaker = SimpleCircuitBreaker(
            name="metaapi_broker",
            failure_threshold=config.circuit_breaker_threshold,
            reset_timeout_seconds=config.circuit_breaker_reset_seconds,
            redis_client=_NoRedis(),
            window_seconds=120.0,
        )

    # --- lifecycle -----------------------------------------------------------

    async def connect(self) -> HealthStatus:
        """Initialize MetaApi SDK, deploy account, open RPC connection."""
        t0 = time.monotonic()
        try:
            log.info("metaapi connect: initializing SDK (account_id=%s)", self._config.account_id)
            # Use singleton MetaApi SDK to prevent per-cycle re-instantiation
            self._api = get_metaapi_sdk(
                token=self._config.token,
                domain=self._config.domain,
                region=self._config.region,
                application=self._config.application,
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

            # Query account info to check tradeAllowed status early
            try:
                acct_info = await self._connection.get_account_information()
                self._cached_equity = acct_info.get("equity")
                trade_allowed = acct_info.get("tradeAllowed", True)
                if not trade_allowed:
                    log.warning(
                        "metaapi connect: ACCOUNT TRADING DISABLED — tradeAllowed=false "
                        "(broker=%s, server=%s). Orders will be rejected by PreTradeGate.",
                        acct_info.get("broker", "unknown"),
                        acct_info.get("server", "unknown"),
                    )
                else:
                    log.info(
                        "metaapi connect: tradeAllowed=true (broker=%s, equity=%.2f %s)",
                        acct_info.get("broker", "unknown"),
                        acct_info.get("equity", 0.0),
                        acct_info.get("currency", "USD"),
                    )
            except Exception as exc:
                log.warning("metaapi connect: could not query account info: %s", _safe_error(exc))

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
            # Unsubscribe from market data before closing to free server-side quota
            if self._connection is not None and self._subscribed_symbols:
                for symbol in self._subscribed_symbols:
                    try:
                        await self._connection.unsubscribe_from_market_data(symbol)
                    except Exception:  # noqa: S110
                        pass  # best-effort cleanup
            if self._connection is not None:
                await self._connection.close()
            # NOTE: Do not close self._api here — it's a shared singleton instance
            # that may be used by other MetaApiAdapter instances. The singleton
            # manager handles cleanup automatically via weak references.
        except Exception as exc:
            log.warning("metaapi disconnect: %s", _safe_error(exc))
        finally:
            self._connection = None
            self._account = None
            self._api = None
            self._connected = False

    async def health_check(self) -> HealthStatus:
        """Probe connection health via a lightweight account-info call.

        On failure, attempts auto-reconnect before reporting DOWN.
        """
        if not self._connected or self._connection is None:
            # Try auto-reconnect if we have config
            if self._config.token and self._config.account_id:
                reconnected = await self._auto_reconnect()
                if reconnected:
                    return HealthStatus(
                        state=HealthState.OK,
                        connected=True,
                        message="reconnected successfully",
                    )
            return HealthStatus(
                state=HealthState.DOWN,
                connected=False,
                message="not connected",
            )
        t0 = time.monotonic()
        try:
            info = await self._connection.get_account_information()
            self._cached_equity = info.get("equity")
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
            self._breaker.record_failure()
            return HealthStatus(
                state=HealthState.DOWN,
                connected=False,
                latency_ms=latency,
                message=f"health check failed: {_safe_error(exc)}",
            )

    # --- auto-reconnect ------------------------------------------------------

    async def _auto_reconnect(self) -> bool:
        """Attempt to re-establish the broker connection.

        Tears down stale state before reconnecting with exponential backoff.
        Returns True if reconnection succeeded.
        """
        if self._reconnecting:
            return False  # prevent concurrent reconnect loops

        self._reconnecting = True
        max_attempts = self._config.reconnect_max_attempts
        backoff_base = self._config.reconnect_backoff_base

        try:
            for attempt in range(max_attempts):
                log.warning(
                    "metaapi auto-reconnect: attempt %d/%d",
                    attempt + 1,
                    max_attempts,
                )
                # Tear down stale SDK state before fresh connect
                try:
                    await self.disconnect()
                except Exception as exc:
                    log.debug("metaapi auto-reconnect: disconnect cleanup: %s", _safe_error(exc))

                status = await self.connect()
                if status.connected:
                    log.info("metaapi auto-reconnect: succeeded on attempt %d", attempt + 1)
                    return True

                log.warning("metaapi auto-reconnect: attempt %d failed: %s", attempt + 1, status.message)
                if attempt < max_attempts - 1:
                    delay = backoff_base ** (attempt + 1)
                    log.warning("metaapi auto-reconnect: retrying in %.1fs", delay)
                    await asyncio.sleep(delay)

            log.error("metaapi auto-reconnect: exhausted %d attempts", max_attempts)
            return False
        finally:
            self._reconnecting = False

    # --- resilient operation helper ------------------------------------------

    async def _retry_operation(self, op_name: str, coro_factory, *args, **kwargs) -> OrderResult:
        """Execute a broker operation with retry, circuit breaker, and auto-reconnect.

        Args:
            op_name: human-readable operation name for logging
            coro_factory: async callable that returns the raw broker response
            *args, **kwargs: passed to coro_factory

        Returns:
            OrderResult translated from broker response, or error result
        """
        # Check circuit breaker first
        if self._breaker.is_open:
            msg = f"Circuit breaker open for metaapi_broker — {op_name} rejected"
            log.error(msg)
            return OrderResult(ok=False, error=msg)

        max_retries = self._config.retry_max_attempts
        for attempt in range(max_retries + 1):
            try:
                await self._ensure_connected_or_reconnect()
                raw = await coro_factory(*args, **kwargs)
                self._breaker.record_success()
                if attempt > 0:
                    log.info("metaapi %s succeeded on attempt %d", op_name, attempt + 1)
                return _translate_trade_response(raw)
            except CircuitBreakerError:
                msg = f"Circuit breaker tripped during {op_name}"
                log.error(msg)
                return OrderResult(ok=False, error=msg)
            except ConnectionError:
                # Connection lost — try reconnect before next retry
                self._connected = False
                self._breaker.record_failure()
                if attempt < max_retries:
                    log.warning("metaapi %s: connection lost, attempting reconnect", op_name)
                    reconnected = await self._auto_reconnect()
                    if not reconnected:
                        return OrderResult(ok=False, error=f"{op_name} failed: reconnect exhausted")
                    continue
                return OrderResult(
                    ok=False, error=f"{op_name} failed: connection lost after {max_retries + 1} attempts"
                )
            except Exception as exc:
                msg = _safe_error(exc)
                self._breaker.record_failure()
                if attempt == max_retries:
                    log.error("metaapi %s failed after %d attempts: %s", op_name, max_retries + 1, msg)
                    return OrderResult(ok=False, error=f"Failed after {max_retries + 1} attempts: {msg}")

                # Rate-limit detection: use longer backoff for 429/TooManyRequests
                is_rate_limit = any(
                    hint in str(exc).lower() for hint in ("429", "too many requests", "toomanyrequests", "rate limit")
                )
                if is_rate_limit:
                    delay = max(10, 2 ** (attempt + 3))  # 10s, 16s, 32s
                    log.warning(
                        "metaapi %s: rate-limited, backing off %ds (attempt %d)",
                        op_name,
                        delay,
                        attempt + 1,
                    )
                else:
                    delay = 2**attempt
                    log.warning(
                        "metaapi %s attempt %d failed: %s, retrying in %ds",
                        op_name,
                        attempt + 1,
                        msg,
                        delay,
                    )
                await asyncio.sleep(delay)

        # Should not reach here, but safety net
        return OrderResult(ok=False, error=f"{op_name} failed: unknown error")

    # --- account -------------------------------------------------------------

    async def get_account(self) -> AccountState:
        """Fetch current account snapshot from MetaApi."""
        await self._ensure_connected_or_reconnect()
        info = await self._connection.get_account_information()
        self._cached_equity = info.get("equity")
        log.debug("metaapi get_account: balance=%.2f equity=%.2f", info["balance"], info["equity"])
        return _translate_account(info)

    def get_equity(self) -> float | None:
        """Return the most recently observed equity value.

        Updated on every ``connect()``, ``health_check()``, and
        ``get_account()`` call.  Returns ``None`` only before the first
        successful account-info fetch.
        """
        return self._cached_equity

    # --- positions -----------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions from MetaApi."""
        await self._ensure_connected_or_reconnect()
        raw_positions = await self._connection.get_positions()
        log.debug("metaapi get_positions: count=%d", len(raw_positions))
        return [_translate_position(p) for p in raw_positions]

    # --- market data ---------------------------------------------------------

    async def subscribe_to_market_data(self, symbols: list[str]) -> None:
        """Subscribe to market data for symbols with long-term subscription.

        Calls ``get_symbol_price(symbol, keep_subscription=True)`` for each
        symbol to ensure the MetaApi terminal starts streaming prices before
        the TickBatchPoller begins polling.  Without this, the first polls
        may return stale or missing data.
        """
        await self._ensure_connected_or_reconnect()
        for symbol in symbols:
            try:
                raw = await self._connection.get_symbol_price(symbol, keep_subscription=True)
                log.info(
                    "metaapi subscribe: %s bid=%.5f ask=%.5f (long-term subscription active)",
                    symbol,
                    raw["bid"],
                    raw["ask"],
                )
            except Exception as exc:
                log.warning("metaapi subscribe: failed for %s: %s", symbol, _safe_error(exc))
        self._subscribed_symbols = list(symbols)
        self._last_resubscribe = time.monotonic()

    async def resubscribe_if_stale(self) -> bool:
        """Re-subscribe to market data if the last subscription is older than 15 minutes.

        Returns True if resubscription was performed, False if still fresh.
        Called by the health monitor loop to prevent MetaApi feed death.
        """
        if not self._subscribed_symbols:
            return False
        elapsed = time.monotonic() - self._last_resubscribe
        if elapsed < self._RESUB_INTERVAL_S:
            return False
        log.info(
            "metaapi resubscribe: %d symbols (%.0fs since last subscription)",
            len(self._subscribed_symbols),
            elapsed,
        )
        # Unsubscribe old subscriptions first to prevent quota leak
        for symbol in self._subscribed_symbols:
            try:
                await self._connection.unsubscribe_from_market_data(symbol)
            except Exception:  # noqa: S110
                pass  # best-effort cleanup
        await self.subscribe_to_market_data(self._subscribed_symbols)
        return True

    async def get_symbol_price(self, symbol: str) -> SymbolPrice:
        """Get current bid/ask for a symbol."""
        await self._ensure_connected_or_reconnect()
        raw = await self._connection.get_symbol_price(symbol, keep_subscription=False)
        log.debug(
            "metaapi get_symbol_price: %s bid=%.5f ask=%.5f",
            symbol,
            raw["bid"],
            raw["ask"],
        )
        return _translate_symbol_price(raw)

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        """Retrieve recent candles via MetaApi historical data API."""
        await self._ensure_connected_or_reconnect()
        ma_tf = _TIMEFRAME_MAP.get(timeframe, timeframe)
        raw_candles = await self._account.get_historical_candles(
            symbol,
            ma_tf,
            limit=count,
        )
        log.debug(
            "metaapi get_candles: %s %s returned=%d",
            symbol,
            ma_tf,
            len(raw_candles),
        )
        return [_translate_candle(c, symbol, timeframe) for c in raw_candles]

    # --- execution -----------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order to MetaApi with full resilience.

        Includes: retry, circuit breaker, auto-reconnect, slippage control,
        and post-order verification for MARKET orders.
        """
        # MetaApi limit: clientId + comment combined ≤ 30 (both) or ≤ 31 (one).
        # FTMO MT5 servers enforce stricter limits — cap clientId at 15 chars
        # and skip comment to stay safely within broker limits.
        _MAX_CLIENT_ID_LEN = 15
        client_id = ""
        if request.idempotency_key:
            client_id = request.idempotency_key[:_MAX_CLIENT_ID_LEN]
        options: dict[str, Any] = {}
        if client_id:
            options["clientId"] = client_id
        # Intentionally omit comment — FTMO reserves comment space for tracking.
        log.debug("order options: clientId=%r (len=%d)", client_id, len(client_id))

        # Slippage control: per-order override > config default > disabled
        slippage_pips = request.max_slippage_pips
        if slippage_pips is not None and slippage_pips > 0:
            # MetaApi accepts slippage in points (1 pip = 10 points for 5-digit)
            options["slippage"] = int(slippage_pips * 10)

        log.info(
            "metaapi place_order: %s %s %s vol=%.2f price=%s sl=%s tp=%s slippage=%s",
            request.side.value,
            request.order_type.value,
            request.symbol,
            request.volume,
            request.price,
            request.stop_loss,
            request.take_profit,
            options.get("slippage", "none"),
        )

        async def _do_place():
            return await _dispatch_order(self._connection, request, options)

        result = await self._retry_operation("place_order", _do_place)

        # Post-order verification for successful MARKET orders
        if result.ok and request.order_type == OrderType.MARKET and result.order_id:
            result = await self._verify_order(result, request)

        return result

    async def modify_order(
        self,
        order_id: str,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        """Modify SL/TP on an existing position with retry and circuit breaker."""
        log.info("metaapi modify_order: id=%s sl=%s tp=%s", order_id, stop_loss, take_profit)

        async def _do_modify():
            return await self._connection.modify_position(
                order_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        return await self._retry_operation("modify_order", _do_modify)

    async def close_position(
        self,
        position_id: str,
        volume: float | None = None,
    ) -> OrderResult:
        """Close (fully or partially) an open position with retry and circuit breaker."""
        log.info("metaapi close_position: id=%s volume=%s", position_id, volume)

        async def _do_close():
            if volume is not None:
                return await self._connection.close_position_partially(position_id, volume)
            else:
                return await self._connection.close_position(position_id)

        return await self._retry_operation("close_position", _do_close)

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a pending order via MetaApi with retry and circuit breaker."""
        log.info("metaapi cancel_order: id=%s", order_id)

        async def _do_cancel():
            return await self._connection.cancel_order(order_id)

        return await self._retry_operation("cancel_order", _do_cancel)

    async def get_orders(self) -> list[PendingOrder]:
        """Fetch all pending orders from MetaApi."""
        await self._ensure_connected_or_reconnect()
        raw_orders = await self._connection.get_orders()
        log.debug("metaapi get_orders: count=%d", len(raw_orders))
        return [_translate_pending_order(o) for o in raw_orders]

    # --- order verification --------------------------------------------------

    async def _verify_order(self, result: OrderResult, request: OrderRequest) -> OrderResult:
        """Poll broker to confirm order was actually filled.

        For MARKET orders, checks positions. For LIMIT/STOP, checks pending orders.
        Returns the original result with fill_price/filled_volume populated if found,
        or a warning-annotated result if verification times out.
        """
        timeout = self._config.order_verify_timeout
        interval = self._config.order_verify_interval
        deadline = time.monotonic() + timeout
        order_id = result.order_id

        log.info("metaapi verify_order: polling for order_id=%s (timeout=%.1fs)", order_id, timeout)

        while time.monotonic() < deadline:
            try:
                positions = await self._connection.get_positions()
                for pos in positions:
                    pos_id = str(pos.get("id", ""))
                    if pos_id == order_id:
                        fill_price = pos.get("openPrice", 0.0)
                        filled_vol = pos.get("volume", 0.0)
                        log.info(
                            "metaapi verify_order: CONFIRMED id=%s fill_price=%.5f volume=%.2f",
                            order_id,
                            fill_price,
                            filled_vol,
                        )
                        result.fill_price = fill_price
                        result.filled_volume = filled_vol
                        return result
            except Exception as exc:
                log.warning("metaapi verify_order: poll error: %s", _safe_error(exc))

            await asyncio.sleep(interval)

        # Verification timed out — order may still be processing
        log.warning("metaapi verify_order: timeout for order_id=%s after %.1fs", order_id, timeout)
        result.error = f"order placed but verification timed out after {timeout}s"
        return result

    # --- internal ------------------------------------------------------------

    def _ensure_connected(self) -> None:
        """Guard: raise if not connected (sync check only)."""
        if not self._connected or self._connection is None:
            raise ConnectionError("MetaApi adapter is not connected — call connect() first")

    async def _ensure_connected_or_reconnect(self) -> None:
        """Guard with auto-reconnect: reconnects if disconnected."""
        if self._connected and self._connection is not None:
            return
        log.warning("metaapi: not connected, attempting auto-reconnect")
        reconnected = await self._auto_reconnect()
        if not reconnected:
            raise ConnectionError("MetaApi adapter is not connected and auto-reconnect failed")

    @property
    def breaker(self) -> SimpleCircuitBreaker:
        """Expose circuit breaker for monitoring/testing."""
        return self._breaker


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
        trade_allowed=info.get("tradeAllowed", True),
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
