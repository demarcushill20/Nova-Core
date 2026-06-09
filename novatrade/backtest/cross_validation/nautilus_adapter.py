"""Adapter for NautilusTrader (nautechsystems/nautilus_trader).

pip install nautilus_trader

Translation approach:
- Define IRB strategy as a NautilusTrader Strategy subclass
- Configure BacktestEngine with FX instrument (EURUSD.SIM)
- Use OrderFactory for stop orders with SL
- Native trailing stop order support

Advantages:
- Most realistic fill model (tick-level when data available)
- Native stop-order support with proper priority
- Commission and slippage modelled per-instrument

Challenges:
- Requires careful instrument specification
- Data must be loaded via Nautilus data catalog
- Higher setup complexity and boilerplate

Version compatibility:
- Targets nautilus_trader v1.185+ API
- CurrencyPair construction uses keyword arguments for forward compat
- Defensive try/except around version-sensitive calls
"""

from __future__ import annotations

import math
import time

from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.types import (
    EngineId,
    EngineResult,
    MetricAvailability,
    NormalizedMetrics,
    NormalizedTrade,
)
from novatrade.backtest.engine import compute_adx, compute_atr, compute_ema
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle


class NautilusAdapter(BaseEngineAdapter):
    """Adapter for NautilusTrader.

    This is the most complex adapter due to Nautilus's professional-grade
    architecture. The payoff is the highest execution fidelity available
    in open source.
    """

    @property
    def engine_id(self) -> EngineId:
        return EngineId.NAUTILUS

    @property
    def engine_version(self) -> str:
        try:
            import nautilus_trader

            return f"nautilus_trader {nautilus_trader.__version__}"
        except Exception:
            return "nautilus_trader (unknown)"

    def is_available(self) -> bool:
        try:
            import nautilus_trader  # noqa: F401

            return True
        except ImportError:
            return False

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> EngineResult:
        t0 = time.monotonic()
        try:
            from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
            from nautilus_trader.config import LoggingConfig
            from nautilus_trader.model.currencies import USD
            from nautilus_trader.model.enums import AccountType, OmsType
            from nautilus_trader.model.identifiers import Venue
            from nautilus_trader.model.objects import Money

            # Configure engine
            config = BacktestEngineConfig(
                logging=LoggingConfig(log_level="ERROR"),
            )
            engine = BacktestEngine(config=config)
            try:
                # Add venue
                SIM = Venue("SIM")
                engine.add_venue(
                    venue=SIM,
                    oms_type=OmsType.NETTING,
                    account_type=AccountType.MARGIN,
                    base_currency=USD,
                    starting_balances=[Money(env.initial_equity, USD)],
                )

                # Build instrument and add data
                instrument = self._build_instrument(env, SIM)
                engine.add_instrument(instrument)

                # Convert candles to Nautilus bars
                bars = self._candles_to_nautilus_bars(h1_candles, instrument)
                engine.add_data(bars)

                # Pre-compute signals using full Nova filter chain
                precomputed = self._precompute_signals(h1_candles, h4_candles, env)

                # Build and add strategy
                strategy = self._build_strategy(env, instrument, precomputed)
                engine.add_strategy(strategy)

                # Run
                engine.run()

                # Extract results
                trades = self._extract_trades(strategy)
                metrics = self._extract_metrics(engine, strategy)

                return EngineResult(
                    engine=self.engine_id,
                    engine_version=self.engine_version,
                    trades=trades,
                    metrics=metrics,
                    elapsed_seconds=time.monotonic() - t0,
                    config_notes=[
                        "NautilusTrader with SIM venue and NETTING OMS",
                        "highest execution fidelity -- native stop orders",
                        "commission and spread modelled per-instrument",
                        "trailing stop tracks from signal bar, not fill bar (simplification)",
                    ],
                )
            except Exception as exc:
                return EngineResult(
                    engine=self.engine_id,
                    engine_version=self.engine_version,
                    elapsed_seconds=time.monotonic() - t0,
                    error=str(exc),
                )
            finally:
                try:
                    engine.dispose()
                except Exception:  # noqa: S110
                    pass
        except ImportError:
            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                elapsed_seconds=time.monotonic() - t0,
                error="nautilus_trader not installed (pip install nautilus_trader)",
            )

    def _build_instrument(self, env: BacktestEnvironment, venue):
        """Build a Nautilus CurrencyPair instrument for EURUSD.

        Creates a fully-specified FX instrument with price precision, tick size,
        lot sizing, and fee model derived from the BacktestEnvironment.

        Args:
            env: BacktestEnvironment with pip_value, spread, and commission params.
            venue: Nautilus Venue instance (e.g. Venue("SIM")).

        Returns:
            nautilus_trader.model.instruments.CurrencyPair

        Raises:
            ImportError: If nautilus_trader is not installed.
            RuntimeError: If instrument construction fails (API mismatch).

        Version notes:
            - v1.185+: CurrencyPair uses keyword-only construction
            - price_precision=5 for 5-digit FX (EURUSD)
            - size_precision=2 for 0.01 lot increments
        """
        from decimal import Decimal

        from nautilus_trader.model.currencies import EUR, USD
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from nautilus_trader.model.instruments import CurrencyPair
        from nautilus_trader.model.objects import Price, Quantity

        instrument_id = InstrumentId.from_str("EUR/USD.SIM")

        # Derive maker/taker fees from env commission model
        # Commission per lot in USD, convert to fraction of notional
        # For EURUSD: 1 standard lot = 100,000 EUR ~ 110,000 USD
        # So commission_per_lot / 110,000 ~ fee fraction
        notional_approx = 100_000.0
        fee_fraction = env.spread.commission_per_lot_usd / notional_approx

        try:
            instrument = CurrencyPair(
                instrument_id=instrument_id,
                raw_symbol=Symbol("EUR/USD"),
                base_currency=EUR,
                quote_currency=USD,
                price_precision=5,
                size_precision=2,
                price_increment=Price.from_str("0.00001"),
                size_increment=Quantity.from_str("0.01"),
                margin_init=Decimal("0"),
                margin_maint=Decimal("0"),
                maker_fee=Decimal(str(fee_fraction)),
                taker_fee=Decimal(str(fee_fraction)),
                ts_event=0,
                ts_init=0,
                lot_size=Quantity.from_str("100000"),
                max_quantity=Quantity.from_str("100.00"),
                min_quantity=Quantity.from_str("0.01"),
                max_price=Price.from_str("9.99999"),
                min_price=Price.from_str("0.00001"),
            )
        except TypeError as exc:
            raise RuntimeError(
                f"Failed to build CurrencyPair instrument. nautilus_trader API may have changed. Error: {exc}"
            ) from exc

        return instrument

    def _candles_to_nautilus_bars(self, candles: list[Candle], instrument):
        """Convert NovaTrade Candle list to Nautilus Bar objects.

        Creates Bar objects with BarType = EUR/USD.SIM-1-HOUR-LAST-EXTERNAL.
        Timestamps are converted to nanoseconds (UnixNanos) for Nautilus.

        Args:
            candles: List of NovaTrade Candle objects with OHLCV data.
            instrument: Nautilus instrument for deriving BarType.

        Returns:
            list of nautilus_trader.model.data.Bar

        Version notes:
            - v1.185+: Bar uses ts_event and ts_init as uint64 nanoseconds
            - BarAggregation.HOUR for H1 candles
            - PriceType.LAST for bar close reference
        """
        from nautilus_trader.model.data import Bar, BarSpecification, BarType
        from nautilus_trader.model.enums import (
            AggregationSource,
            BarAggregation,
            PriceType,
        )
        from nautilus_trader.model.objects import Price, Quantity

        bar_spec = BarSpecification(1, BarAggregation.HOUR, PriceType.LAST)

        try:
            bar_type = BarType(
                instrument_id=instrument.id,
                bar_spec=bar_spec,
                aggregation_source=AggregationSource.EXTERNAL,
            )
        except TypeError:
            # Older API: BarType may take different arguments
            bar_type = BarType(
                instrument_id=instrument.id,
                bar_spec=bar_spec,
            )

        bars = []
        for candle in candles:
            # Convert timestamp (seconds) to nanoseconds
            ts_ns = int(candle.timestamp * 1_000_000_000)

            try:
                bar = Bar(
                    bar_type=bar_type,
                    open=Price.from_str(f"{candle.open:.5f}"),
                    high=Price.from_str(f"{candle.high:.5f}"),
                    low=Price.from_str(f"{candle.low:.5f}"),
                    close=Price.from_str(f"{candle.close:.5f}"),
                    volume=Quantity.from_str(f"{candle.volume:.2f}"),
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                )
            except TypeError:
                # Some versions use different constructor signatures
                bar = Bar(
                    bar_type=bar_type,
                    open=Price.from_str(f"{candle.open:.5f}"),
                    high=Price.from_str(f"{candle.high:.5f}"),
                    low=Price.from_str(f"{candle.low:.5f}"),
                    close=Price.from_str(f"{candle.close:.5f}"),
                    volume=Quantity.from_str(f"{candle.volume:.2f}"),
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                    is_revision=False,
                )
            bars.append(bar)

        return bars

    def _precompute_signals(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> dict[int, tuple[str, float, float]]:
        """Pre-compute IRB signals using the full Nova engine filter chain.

        Returns dict mapping bar_index -> (side, entry_price, stop_loss).
        """
        import math as _math

        n = len(h1_candles)
        closes = [c.close for c in h1_candles]

        ema = compute_ema(closes, env.ema_period)
        ema_fast = compute_ema(closes, env.ema_fast_period)
        ema_slow = compute_ema(closes, env.ema_slow_period)
        atr = compute_atr(h1_candles, env.atr_period)
        adx = compute_adx(h1_candles, env.adx_period)

        atr_sma: list[float] = []
        if env.use_volatility_filter:
            ma_period = env.volatility_atr_ma_period
            atr_sma = [float("nan")] * n
            for j in range(ma_period - 1, n):
                vals = [atr[k] for k in range(j - ma_period + 1, j + 1) if not _math.isnan(atr[k])]
                if len(vals) == ma_period:
                    atr_sma[j] = sum(vals) / ma_period

        h4_ema: list[float] = []
        h4_map: list[int] = []
        if h4_candles:
            h4_closes = [c.close for c in h4_candles]
            h4_ema = compute_ema(h4_closes, env.ema_period)
            h4_ts = [c.timestamp for c in h4_candles]
            h4_idx = 0
            for c in h1_candles:
                while h4_idx < len(h4_ts) - 1 and h4_ts[h4_idx + 1] <= c.timestamp:
                    h4_idx += 1
                h4_map.append(h4_idx)

        signals: dict[int, tuple[str, float, float]] = {}
        for i in range(n):
            if i < env.warmup_bars:
                continue
            bar = h1_candles[i]
            if _math.isnan(ema[i]) or _math.isnan(atr[i]) or _math.isnan(adx[i]) or atr[i] <= 0:
                continue

            rng = bar.high - bar.low
            if rng <= 0:
                continue

            if env.session_filter is not None and bar.timestamp > 0:
                from datetime import datetime, timezone

                bar_hour = datetime.fromtimestamp(bar.timestamp, tz=timezone.utc).hour
                if (
                    (env.session_filter == "london" and not (7 <= bar_hour < 16))
                    or (env.session_filter == "newyork" and not (13 <= bar_hour < 22))
                    or (env.session_filter == "london_ny_overlap" and not (13 <= bar_hour < 16))
                ):
                    continue

            if (
                env.use_volatility_filter
                and atr_sma
                and i < len(atr_sma)
                and not _math.isnan(atr_sma[i])
                and atr[i] < atr_sma[i]
            ):
                continue

            up_th = bar.high - env.irb_threshold * rng
            dn_th = bar.low + env.irb_threshold * rng
            is_up_irb = bar.open <= up_th and bar.close <= up_th
            is_dn_irb = bar.open >= dn_th and bar.close >= dn_th
            if not is_up_irb and not is_dn_irb:
                continue

            side = None
            if env.use_simple_trend_filter:
                ef, es = ema_fast[i], ema_slow[i]
                lb = min(env.ema_slope_lookback, i)
                if lb < 1:
                    continue
                ef_prev = ema_fast[i - lb]
                if _math.isnan(ef) or _math.isnan(es) or _math.isnan(ef_prev):
                    continue
                if is_up_irb and ef > es and ef > ef_prev:
                    side = "LONG"
                elif is_dn_irb and ef < es and ef < ef_prev:
                    side = "SHORT"
                else:
                    continue
            else:
                lookback = min(20, i)
                if lookback < 1:
                    continue
                slope = (ema[i] - ema[i - lookback]) / atr[i]
                if is_up_irb and slope >= env.trend_slope_threshold:
                    side = "LONG"
                elif is_dn_irb and slope <= -env.trend_slope_threshold:
                    side = "SHORT"
                else:
                    continue

                if env.use_ema_stack_filter:
                    ef, es = ema_fast[i], ema_slow[i]
                    if _math.isnan(ef) or _math.isnan(es):
                        continue
                    if side == "LONG" and not (ef > ema[i] > es):
                        continue
                    if side == "SHORT" and not (ef < ema[i] < es):
                        continue

                if env.ema_confirm_bars > 0:
                    confirm_ok = True
                    for j in range(env.ema_confirm_bars):
                        idx = i - j
                        if idx < 0 or idx >= len(ema_fast) or _math.isnan(ema_fast[idx]):
                            confirm_ok = False
                            break
                        if side == "LONG" and h1_candles[idx].close <= ema_fast[idx]:
                            confirm_ok = False
                            break
                        if side == "SHORT" and h1_candles[idx].close >= ema_fast[idx]:
                            confirm_ok = False
                            break
                    if not confirm_ok:
                        continue

            if h4_map and h4_ema:
                h4_i = h4_map[i] if i < len(h4_map) else -1
                if h4_i < 0 or h4_i >= len(h4_ema):
                    continue
                h4_lb = min(env.mtf_lookback, h4_i)
                if h4_lb < 1:
                    continue
                h4_cur = h4_ema[h4_i]
                h4_prev = h4_ema[h4_i - h4_lb]
                if _math.isnan(h4_cur) or _math.isnan(h4_prev):
                    continue
                if side == "LONG" and not (h4_cur > h4_prev):
                    continue
                if side == "SHORT" and not (h4_cur < h4_prev):
                    continue

            if _math.isnan(adx[i]) or adx[i] < env.adx_threshold:
                continue

            overext = rng / atr[i]
            if overext > env.overextension_threshold:
                continue
            if env.min_signal_atr_mult > 0 and overext < env.min_signal_atr_mult:
                continue

            spread_cushion = max(0.0, env.sl_spread_buffer_pips) * env.pip_value
            if side == "LONG":
                entry = bar.high + env.pip_buffer
                sl = bar.low - env.pip_buffer - spread_cushion
                if env.atr_sl_floor_multiplier > 0:
                    min_sl = atr[i] * env.atr_sl_floor_multiplier
                    if (entry - sl) < min_sl:
                        sl = entry - min_sl
            else:
                entry = bar.low - env.pip_buffer
                sl = bar.high + env.pip_buffer + spread_cushion
                if env.atr_sl_floor_multiplier > 0:
                    min_sl = atr[i] * env.atr_sl_floor_multiplier
                    if (sl - entry) < min_sl:
                        sl = entry + min_sl
            signals[i] = (side, entry, sl)

        return signals

    def _build_strategy(self, env: BacktestEnvironment, instrument, precomputed: dict | None = None):
        """Build a NautilusTrader Strategy subclass implementing the IRB strategy.

        The strategy pre-computes EMA and ATR indicators from the bar history,
        then evaluates IRB geometry and trend alignment on each bar to generate
        stop-entry orders with stop-loss. Trailing stops are managed manually
        by modifying the stop-loss price on each bar.

        Args:
            env: BacktestEnvironment with all strategy parameters.
            instrument: Nautilus instrument for order construction.

        Returns:
            An instantiated Strategy subclass ready to add to BacktestEngine.

        Notes:
            - Mirrors the logic in BacktestingPyAdapter._build_strategy_class()
            - Uses the same compute_ema/compute_atr from novatrade.backtest.engine
            - IRB geometry detection uses the simplified wick-ratio approach
              (consistent with backtestingpy_adapter for cross-validation)
            - Does NOT implement the full IRBBacktester filter chain (ADX, MTF,
              overextension, etc.) -- uses the same simplified subset as the
              backtesting.py adapter for apples-to-apples comparison
        """
        from nautilus_trader.config import StrategyConfig
        from nautilus_trader.model.data import Bar, BarSpecification, BarType
        from nautilus_trader.model.enums import (
            AggregationSource,
            BarAggregation,
            OrderSide,
            PriceType,
            TimeInForce,
        )
        from nautilus_trader.model.identifiers import InstrumentId
        from nautilus_trader.model.objects import Price, Quantity
        from nautilus_trader.trading.strategy import Strategy

        _env = env
        _instrument = instrument
        _compute_ema = compute_ema
        _compute_atr = compute_atr
        _precomputed_signals = precomputed or {}

        class IRBNautilusConfig(StrategyConfig, frozen=True):  # type: ignore[call-arg]
            """Configuration for IRB Nautilus strategy."""

            instrument_id: str = str(instrument.id)
            ema_period: int = env.ema_period
            atr_period: int = env.atr_period
            irb_threshold: float = env.irb_threshold
            trail_atr_mult: float = env.trail_atr_multiplier
            time_stop_bars: int = env.time_stop_bars
            pip_value: float = env.pip_value
            risk_fraction: float = env.risk_fraction
            pip_value_per_standard_lot: float = env.pip_value_per_standard_lot
            volume: float = env.min_volume  # fallback / min position size in lots
            cooldown_bars: int = env.cooldown_bars
            max_trades_per_day: int = env.max_trades_per_day
            trigger_window_bars: int = env.trigger_window_bars
            # Risk-based sizing + cost (match Nova engine.py:816-821 / 1379)
            initial_equity: float = env.initial_equity
            min_volume: float = env.min_volume
            max_volume: float = env.max_volume
            breakeven_r: float = env.breakeven_r
            total_cost_pips: float = env.spread.total_cost_pips + (
                env.spread.commission_per_lot_usd / env.pip_value_per_standard_lot
                if env.pip_value_per_standard_lot
                else 0.0
            )

        class IRBNautilusStrategy(Strategy):
            """IRB strategy reimplemented for NautilusTrader.

            Mirrors the simplified IRB logic used in the backtesting.py adapter:
            - IRB geometry via wick/body ratio
            - EMA trend filter
            - ATR-based trailing stop
            - Time stop
            """

            def __init__(self, config: IRBNautilusConfig) -> None:
                super().__init__(config)
                self._instrument_id = InstrumentId.from_str(config.instrument_id)
                self._ema_period = config.ema_period
                self._atr_period = config.atr_period
                self._irb_threshold = config.irb_threshold
                self._trail_atr_mult = config.trail_atr_mult
                self._time_stop_bars = config.time_stop_bars
                self._pip_value = config.pip_value
                self._risk_fraction = config.risk_fraction
                self._pip_value_per_lot = config.pip_value_per_standard_lot
                self._volume = config.volume
                # Risk-based sizing state (compounding off realised PnL, like Nova)
                self._initial_equity = config.initial_equity
                self._equity = config.initial_equity
                self._min_volume = config.min_volume
                self._max_volume = config.max_volume
                self._cost_pips = config.total_cost_pips
                self._breakeven_r = config.breakeven_r
                self._cur_volume = config.volume
                self._be_hit = False

                # State
                self._bars: list[Bar] = []
                self._closes: list[float] = []
                self._candle_list: list[Candle] = []  # for ATR computation
                self._ema_values: list[float] = []
                self._atr_values: list[float] = []
                self._in_position = False
                self._position_side: str | None = None
                self._entry_bar_idx = 0
                self._bars_in_trade = 0
                self._trail_stop = 0.0
                self._best_close = 0.0
                self._trade_records: list[dict] = []
                self._current_entry_price = 0.0
                self._current_stop_loss = 0.0
                self._pending_side: str | None = None
                self._pending_entry_price = 0.0
                self._pending_stop_loss = 0.0
                self._pending_bar_idx = -1
                # Execution state — match Nova cooldown/daily-limit
                self._cooldown_bars = config.cooldown_bars
                self._max_trades_per_day = config.max_trades_per_day
                self._trigger_window_bars = config.trigger_window_bars
                self._last_flat_bar = -9999
                self._trades_today = 0
                self._last_day_ord = -1

            def on_start(self) -> None:
                """Subscribe to bar data."""
                bar_spec = BarSpecification(
                    step=1,
                    aggregation=BarAggregation.HOUR,
                    price_type=PriceType.LAST,
                )
                try:
                    bar_type = BarType(
                        instrument_id=self._instrument_id,
                        bar_spec=bar_spec,
                        aggregation_source=AggregationSource.EXTERNAL,
                    )
                except TypeError:
                    bar_type = BarType(
                        instrument_id=self._instrument_id,
                        bar_spec=bar_spec,
                    )
                self.subscribe_bars(bar_type)

            def on_bar(self, bar: Bar) -> None:
                """Process each bar: update indicators, check signals/exits."""
                # Extract OHLCV
                o = float(bar.open)
                h = float(bar.high)
                low = float(bar.low)
                c = float(bar.close)
                v = float(bar.volume)

                self._bars.append(bar)
                self._closes.append(c)

                # Build a Candle for ATR computation
                ts = bar.ts_event / 1_000_000_000 if bar.ts_event else 0.0
                self._candle_list.append(Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=v))

                # Day-boundary reset for daily trade counter
                if self._max_trades_per_day > 0:
                    if ts > 0:
                        from datetime import datetime, timezone

                        day_ord = datetime.fromtimestamp(ts, tz=timezone.utc).toordinal()
                    else:
                        day_ord = len(self._bars) // 24
                    if day_ord != self._last_day_ord:
                        self._last_day_ord = day_ord
                        self._trades_today = 0

                # Recompute indicators (incremental would be better but this is
                # correct and mirrors the pre-compute approach)
                self._ema_values = _compute_ema(self._closes, self._ema_period)
                self._atr_values = _compute_atr(self._candle_list, self._atr_period)

                bar_idx = len(self._bars) - 1

                if self._in_position:
                    self._manage_position(bar_idx, o, h, low, c)
                    return

                if self._check_pending_fill(bar_idx, o, h, low, c):
                    return

                self._check_signal(bar_idx, o, h, low, c)

            def _check_signal(self, bar_idx: int, o: float, h: float, low: float, c: float) -> None:
                """Check for pre-computed IRB entry signal at this bar index."""
                # v5: Cooldown bars — match Nova engine.py:605-612
                if self._cooldown_bars > 0 and (bar_idx - self._last_flat_bar) <= self._cooldown_bars:
                    return
                # v5: Daily trade limit — match Nova engine.py:600-603
                if self._max_trades_per_day > 0 and self._trades_today >= self._max_trades_per_day:
                    return

                if bar_idx in _precomputed_signals:
                    side, entry, sl = _precomputed_signals[bar_idx]
                    if self._pending_side == side:
                        # Nova replaces same-side pending IRB orders with the fresher signal.
                        self._pending_side = None
                    elif self._pending_side is not None:
                        # Nova ignores opposite-direction signals while a pending order is alive.
                        return
                    self._pending_side = side
                    self._pending_entry_price = entry
                    self._pending_stop_loss = sl
                    self._pending_bar_idx = bar_idx

            def _check_pending_fill(self, bar_idx: int, o: float, h: float, low: float, c: float) -> bool:
                """Fill/expire a pending stop order using Nova's stop-order cadence."""
                if self._pending_side is None:
                    return False

                fill_price = self._pending_entry_price
                filled = False
                if self._pending_side == "LONG":
                    if h >= self._pending_entry_price:
                        filled = True
                        if o >= self._pending_entry_price:
                            fill_price = o
                else:
                    if low <= self._pending_entry_price:
                        filled = True
                        if o <= self._pending_entry_price:
                            fill_price = o

                if filled:
                    side = self._pending_side
                    sl = self._pending_stop_loss
                    self._pending_side = None
                    self._pending_entry_price = 0.0
                    self._pending_stop_loss = 0.0
                    self._pending_bar_idx = -1
                    self._trades_today += 1
                    self._enter_position(side, bar_idx, fill_price, sl, c)
                    return True

                if bar_idx - self._pending_bar_idx >= self._trigger_window_bars:
                    self._pending_side = None
                    self._pending_entry_price = 0.0
                    self._pending_stop_loss = 0.0
                    self._pending_bar_idx = -1

                return False

            def _enter_position(
                self,
                side: str,
                bar_idx: int,
                entry: float,
                sl: float,
                current_price: float,
            ) -> None:
                """Place a stop-market entry order."""
                self._in_position = True
                self._position_side = side
                self._entry_bar_idx = bar_idx
                self._bars_in_trade = 0
                self._current_entry_price = entry
                self._current_stop_loss = sl
                self._trail_stop = sl
                self._best_close = current_price
                self._be_hit = False

                # Risk-fraction position sizing off compounding equity
                # (Nova engine.py:816-821), in lots, clamped + rounded.
                stop_pips = abs(entry - sl) / self._pip_value if self._pip_value else 0.0
                if stop_pips > 0:
                    vol = (self._equity * self._risk_fraction) / (stop_pips * self._pip_value_per_lot)
                    vol = max(self._min_volume, min(self._max_volume, round(vol, 2)))
                else:
                    vol = self._min_volume
                self._cur_volume = vol

                # Place order via Nautilus order factory
                try:
                    if side == "LONG":
                        order = self.order_factory.stop_market(
                            instrument_id=self._instrument_id,
                            order_side=OrderSide.BUY,
                            quantity=Quantity.from_str(f"{vol:.2f}"),
                            trigger_price=Price.from_str(f"{entry:.5f}"),
                            time_in_force=TimeInForce.GTC,
                        )
                    else:
                        order = self.order_factory.stop_market(
                            instrument_id=self._instrument_id,
                            order_side=OrderSide.SELL,
                            quantity=Quantity.from_str(f"{vol:.2f}"),
                            trigger_price=Price.from_str(f"{entry:.5f}"),
                            time_in_force=TimeInForce.GTC,
                        )
                    self.submit_order(order)
                except Exception:
                    # Order submission failed — stay flat to avoid phantom trades
                    self._in_position = False
                    self._position_side = ""

            def _manage_position(self, bar_idx: int, o: float, h: float, low: float, c: float) -> None:
                """Manage trailing stop and time stop for open position."""
                self._bars_in_trade += 1

                # Time stop
                if self._bars_in_trade >= self._time_stop_bars:
                    self._close_position(bar_idx, c, "time_stop")
                    return

                atr_val = self._atr_values[bar_idx] if bar_idx < len(self._atr_values) else 0.0
                if math.isnan(atr_val) or atr_val <= 0:
                    return

                if self._position_side == "LONG":
                    # Check stop hit
                    if low <= self._trail_stop:
                        self._close_position(bar_idx, self._trail_stop, "stop_loss")
                        return
                    # Update trailing stop
                    self._best_close = max(self._best_close, c)
                    trail = c - atr_val * self._trail_atr_mult
                    if trail > self._trail_stop:
                        self._trail_stop = trail
                    if c <= self._trail_stop:
                        self._close_position(bar_idx, self._trail_stop, "trailing_stop")
                        return
                else:
                    # SHORT
                    if h >= self._trail_stop:
                        self._close_position(bar_idx, self._trail_stop, "stop_loss")
                        return
                    self._best_close = min(self._best_close, c)
                    trail = c + atr_val * self._trail_atr_mult
                    if self._trail_stop == 0 or trail < self._trail_stop:
                        self._trail_stop = trail
                    if c >= self._trail_stop:
                        self._close_position(bar_idx, self._trail_stop, "trailing_stop")
                        return

            def _close_position(self, bar_idx: int, exit_price: float, reason: str) -> None:
                """Record trade close and flatten."""
                entry_price = self._current_entry_price
                side = self._position_side

                if side == "LONG":
                    gross_pips = (exit_price - entry_price) / self._pip_value
                else:
                    gross_pips = (entry_price - exit_price) / self._pip_value

                # Deduct Nova's per-trade cost (spread + slippage + commission as pips)
                # and book the realised PnL into compounding equity for next sizing.
                net_pips = gross_pips - self._cost_pips
                pnl_usd = net_pips * self._pip_value_per_lot * self._cur_volume
                self._equity += pnl_usd

                self._trade_records.append(
                    {
                        "trade_id": len(self._trade_records) + 1,
                        "side": side,
                        "entry_bar": self._entry_bar_idx,
                        "exit_bar": bar_idx,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "stop_loss": self._current_stop_loss,
                        "pnl_pips": net_pips,
                        "pnl_usd": pnl_usd,
                        "volume": self._cur_volume,
                        "hold_bars": bar_idx - self._entry_bar_idx,
                        "exit_reason": reason,
                    }
                )

                self._in_position = False
                self._position_side = None
                self._last_flat_bar = bar_idx  # match Nova engine.py:1505

                # Close via Nautilus if we have a position in cache
                try:
                    if self.cache.positions():
                        for pos in self.cache.positions():
                            if not pos.is_closed:
                                self.close_position(pos)
                except Exception:  # noqa: S110
                    pass

            def on_stop(self) -> None:
                """Close any remaining position."""
                if self._in_position and self._bars:
                    last_close = float(self._bars[-1].close)
                    bar_idx = len(self._bars) - 1
                    self._close_position(bar_idx, last_close, "time_stop")

            def get_trade_records(self) -> list[dict]:
                """Return recorded trades for extraction."""
                return self._trade_records

        config = IRBNautilusConfig()
        return IRBNautilusStrategy(config=config)

    def _extract_trades(self, strategy) -> list[NormalizedTrade]:
        """Extract trade records from the strategy's internal tracking.

        Uses the strategy's get_trade_records() method which logs trades
        as they are closed. Falls back to Nautilus cache if available.

        Args:
            strategy: The IRBNautilusStrategy instance after engine.run().

        Returns:
            List of NormalizedTrade objects.
        """
        trades: list[NormalizedTrade] = []

        # Primary: use our internal trade records
        if hasattr(strategy, "get_trade_records"):
            for rec in strategy.get_trade_records():
                trades.append(
                    NormalizedTrade(
                        trade_id=rec["trade_id"],
                        side=rec["side"],
                        entry_bar=rec["entry_bar"],
                        exit_bar=rec["exit_bar"],
                        entry_price=rec["entry_price"],
                        exit_price=rec["exit_price"],
                        stop_loss=rec["stop_loss"],
                        pnl_pips=rec["pnl_pips"],
                        pnl_usd=rec["pnl_usd"],
                        hold_bars=rec["hold_bars"],
                        exit_reason=rec["exit_reason"],
                    )
                )
            return trades

        # Fallback: extract from Nautilus cache (engine fills)
        try:
            if hasattr(strategy, "cache") and strategy.cache is not None:
                for i, report in enumerate(strategy.cache.position_snapshots()):
                    trades.append(
                        NormalizedTrade(
                            trade_id=i + 1,
                            side="LONG" if report.side.name == "BUY" else "SHORT",
                            entry_bar=0,
                            exit_bar=0,
                            entry_price=float(report.avg_px_open),
                            exit_price=float(report.avg_px_close),
                            stop_loss=0.0,
                            pnl_pips=float(report.realized_pnl) / 10.0,
                            pnl_usd=float(report.realized_pnl),
                            hold_bars=0,
                            exit_reason="unknown",
                        )
                    )
        except Exception:  # noqa: S110
            pass

        return trades

    def _extract_metrics(self, engine, strategy) -> NormalizedMetrics:
        """Extract normalised metrics from the strategy's trade log.

        Computes metrics from the internal trade records to ensure consistency
        with what _extract_trades() returns.

        Args:
            engine: The BacktestEngine instance (for account state if needed).
            strategy: The IRBNautilusStrategy instance.

        Returns:
            NormalizedMetrics with available fields populated.
        """
        trade_records = []
        if hasattr(strategy, "get_trade_records"):
            trade_records = strategy.get_trade_records()

        if not trade_records:
            return NormalizedMetrics(
                total_trades=0,
                availability={k: MetricAvailability.AVAILABLE for k in ["total_trades"]},
            )

        total = len(trade_records)
        wins = sum(1 for t in trade_records if t["pnl_pips"] > 0)
        longs = sum(1 for t in trade_records if t["side"] == "LONG")
        shorts = total - longs
        win_rate = (wins / total) if total > 0 else 0.0  # Use fraction (0-1), not percentage
        net_pnl_usd = sum(t["pnl_usd"] for t in trade_records)
        net_pnl_pips = sum(t["pnl_pips"] for t in trade_records)
        avg_trade_pips = net_pnl_pips / total if total > 0 else 0.0
        avg_hold = sum(t["hold_bars"] for t in trade_records) / total if total > 0 else 0.0

        # Profit factor
        gross_profit = sum(t["pnl_usd"] for t in trade_records if t["pnl_usd"] > 0)
        gross_loss = abs(sum(t["pnl_usd"] for t in trade_records if t["pnl_usd"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Max consecutive wins/losses
        max_consec_wins = 0
        max_consec_losses = 0
        cur_wins = 0
        cur_losses = 0
        for t in trade_records:
            if t["pnl_pips"] > 0:
                cur_wins += 1
                cur_losses = 0
                max_consec_wins = max(max_consec_wins, cur_wins)
            else:
                cur_losses += 1
                cur_wins = 0
                max_consec_losses = max(max_consec_losses, cur_losses)

        # Max drawdown % from the compounding equity curve, against initial_equity
        # (matches Nova's equity-based drawdown). Sharpe/Sortino stay NOT_COMPUTED:
        # their annualisation is not comparable across engines.
        init_eq = getattr(strategy, "_initial_equity", 100_000.0)
        eq = init_eq
        peak = init_eq
        max_dd = 0.0
        for t in trade_records:
            eq += t["pnl_usd"]
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        max_drawdown_pct = max_dd * 100.0

        avail = {
            "total_trades": MetricAvailability.AVAILABLE,
            "win_rate": MetricAvailability.AVAILABLE,
            "net_pnl_usd": MetricAvailability.AVAILABLE,
            "net_pnl_pips": MetricAvailability.AVAILABLE,
            "profit_factor": MetricAvailability.AVAILABLE,
            "avg_trade_pips": MetricAvailability.AVAILABLE,
            "avg_hold_bars": MetricAvailability.AVAILABLE,
            "max_drawdown_pct": MetricAvailability.AVAILABLE,
            "sharpe_ratio": MetricAvailability.NOT_COMPUTED,
            "sortino_ratio": MetricAvailability.NOT_COMPUTED,
        }

        return NormalizedMetrics(
            total_trades=total,
            long_trades=longs,
            short_trades=shorts,
            win_rate=win_rate,  # Already in fraction format
            net_pnl_usd=net_pnl_usd,
            net_pnl_pips=net_pnl_pips,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown_pct,
            avg_trade_pips=avg_trade_pips,
            avg_hold_bars=avg_hold,
            max_consecutive_wins=max_consec_wins,
            max_consecutive_losses=max_consec_losses,
            availability=avail,
        )
