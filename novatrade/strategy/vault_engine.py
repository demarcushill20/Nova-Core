"""VaultLiveEngine — live wrapper around vault Python's strategy core.

Drop-in replacement for `LiveStrategyEngine` that drives
`novatrade.strategies.vault_irb_state.IRBStrategyState` (the bar-by-bar
refactor of vault Python's validated +$45,310 batch loop). Same public API
as `LiveStrategyEngine` so the runner can swap engines via flag.

Architecture:
  on_bar(bar, M5) → append to rolling buffer → re-prepare DataFrame
                  → IRBStrategyState.process_bar(latest)
                  → translate TradeEvents into LiveSignals

Trade-offs:
  - SLOW: re-prepares DataFrame each bar (recomputes EMAs/ATR/HTF resample).
    Acceptable for live (one bar every 5 minutes); for backtest replay we
    burn ~3-4× the time of vault Python's batch run.
  - CORRECT: vault's exact loop drives every decision. No integration drift.
  - SIMPLE: no streaming-indicator optimization. Refactor later if needed.

Ledger / cost model:
  Vault Python ledger is zero-cost (no spread/slippage/commission). Live
  brokers add real costs. The engine emits raw fill prices; cost accounting
  happens at the broker bridge / supervisor layer in production.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.vault_irb_state import (
    IRBConfig,
    IRBStrategyState,
    TradeEvent,
    prepare_dataframe,
)
from novatrade.strategy.live_engine import (
    LiveConfig,
    LiveSignal,
    LiveState,
    OpenPosition,
    PendingOrder,
    SignalType,
)

log = logging.getLogger("novatrade.strategy.vault_engine")


def env_to_irb_config(env: BacktestEnvironment) -> IRBConfig:
    """Map nova `BacktestEnvironment` → vault `IRBConfig`.

    Used at engine construction to translate live-runtime parameters
    (set by the live champion YAML + runner overrides) into the IRBConfig
    that vault's strategy core expects.
    """
    higher_tf = (env.higher_timeframe or "H1").upper()
    htf_map = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "60min", "H4": "240min"}

    return IRBConfig(
        initial_capital=env.initial_equity,
        tick_size=0.00001,  # EUR/USD 0.1 pip
        enable_longs=True,
        enable_shorts=True,
        risk_pct=env.risk_fraction * 100.0,
        qty_step=1000.0,  # 0.01 lot = 1,000 units
        min_qty=max(1000.0, env.min_volume * 100_000.0),
        max_qty=env.max_volume * 100_000.0,
        ema20_len=env.ema_period,
        ema50_len=env.ema_slow_period if env.ema_slow_period > 0 else 50,
        ema20_slope_lookback=max(1, env.ema_slope_lookback),
        use_htf_filter=True,
        htf_timeframe=htf_map.get(higher_tf, "60min"),
        htf_ema_len=env.ema_period,
        require_htf_price_side=False,
        irb_percent=env.irb_threshold * 100.0,
        entry_buffer_ticks=1,
        stop_buffer_ticks=1,
        signal_expiry_bars=env.trigger_window_bars,
        replace_pending_signal=True,
        use_body_filter=False,
        max_body_percent=45.0,
        use_signal_atr_range_filter=True,
        signal_atr_len=env.atr_period,
        min_signal_atr_mult=env.min_signal_atr_mult,
        max_signal_atr_mult=env.overextension_threshold,
        use_pullback_filter=False,
        use_session_filter=(env.session_filter is not None),
        trade_session="0700-1600",  # London-NY default; not used unless session filter is on
        max_trades_per_day=env.max_trades_per_day if env.max_trades_per_day > 0 else 999,
        cooldown_bars=env.cooldown_bars,
        take_partial=env.partial_exit_enabled,
        partial_exit_pct=env.partial_exit_pct,
        partial_rr=env.partial_r_target,
        move_to_be_at_rr=env.breakeven_r,
        runner_atr_len=env.atr_period,
        runner_atr_mult=env.trail_atr_multiplier,
        use_time_stop=True,
        max_bars_in_trade=env.time_stop_bars,
    )


class VaultLiveEngine:
    """Live wrapper around vault Python's strategy core.

    Public API mirrors `novatrade.strategy.live_engine.LiveStrategyEngine`:
      - `seed_history(primary, higher)` — preload candles for indicator warmup
      - `on_bar(bar, timeframe)` — process one bar, return list of LiveSignals
      - `notify_fill(price, vol)` / `notify_close(pnl)` — for external (broker) updates
      - `cancel_pending()` — clear the pending order

    Internally drives `IRBStrategyState`. Each on_bar call rebuilds the
    indicator DataFrame and processes the latest row.
    """

    def __init__(
        self,
        env: BacktestEnvironment,
        config: LiveConfig | None = None,
    ) -> None:
        self._env = env
        self._config = config or LiveConfig(
            symbol=env.symbol_display,
            primary_timeframe=env.primary_timeframe or "M5",
            higher_timeframe=env.higher_timeframe or "H1",
            trigger_window_bars=env.trigger_window_bars,
        )
        self._irb_cfg = env_to_irb_config(env)
        self._state = IRBStrategyState(self._irb_cfg)

        # Rolling buffers (raw candles, not pre-computed)
        # NOTE: vault's IRBStrategyState tracks state via absolute bar indices
        # (`pending["signal_bar"]`, `position["entry_bar"]`, `last_flat_bar`).
        # Trimming the buffer would invalidate those indices because `i` is
        # derived from `len(df) - 1` and would no longer be monotonic. Until
        # we refactor to absolute counters, the buffer GROWS UNBOUNDED.
        # In production this is fine: O(N) prepare_dataframe per bar = a few
        # ms even at year-long buffers, and 1 bar / 5 min in live. For
        # backtest replay it makes the run O(N^2) but tractable.
        self._primary_candles: list[Candle] = []
        self._higher_candles: list[Candle] = []
        self._max_buffer = 1_000_000  # effectively no trim (~10y of M5)

        # Track which trade-events we've already turned into LiveSignals
        self._emitted_count = 0

        # SL propagation: track runner stop to emit MODIFY_SL when it tightens
        self._current_stop: float | None = None

        self._live_state = LiveState.WARMING_UP

    # ------------------------------------------------------------------
    # Public API (mirrors LiveStrategyEngine)
    # ------------------------------------------------------------------

    def seed_history(
        self,
        primary_candles: list[Candle],
        higher_candles: list[Candle] | None = None,
    ) -> None:
        self._primary_candles = list(primary_candles[-self._max_buffer :])
        self._higher_candles = list((higher_candles or [])[-self._max_buffer :])
        if len(self._primary_candles) >= max(self._env.warmup_bars, 60):
            self._live_state = LiveState.FLAT
            log.info(
                "VaultLiveEngine seeded %d primary + %d higher candles — READY",
                len(self._primary_candles),
                len(self._higher_candles),
            )
        else:
            log.info(
                "VaultLiveEngine seeded %d primary candles — need %d for warmup",
                len(self._primary_candles),
                self._env.warmup_bars,
            )

    def on_bar(self, bar: Candle, timeframe: str) -> list[LiveSignal]:
        if timeframe == self._config.higher_timeframe:
            self._higher_candles.append(bar)
            if len(self._higher_candles) > self._max_buffer:
                self._higher_candles = self._higher_candles[-self._max_buffer :]
            return []

        if timeframe != self._config.primary_timeframe:
            return []

        self._primary_candles.append(bar)
        if len(self._primary_candles) > self._max_buffer:
            self._primary_candles = self._primary_candles[-self._max_buffer :]

        if len(self._primary_candles) < max(self._env.warmup_bars, 60):
            return []

        if self._live_state == LiveState.WARMING_UP:
            self._live_state = LiveState.FLAT

        # Re-prepare DataFrame on the rolling buffer.
        df = self._build_df()
        i = len(df) - 1
        if i < 0:
            return []

        # Snapshot pending state BEFORE process_bar so we can detect new/replaced
        # pending orders and synthesize a SignalType.ENTRY for the agent. Without
        # this, the wrapper only emits PENDING_FILL when vault's internal path
        # simulation fills, but the agent has no record of the pending order so
        # it rejects the subsequent EXIT/PARTIAL_EXIT signals.
        prev_pending = dict(self._state.pending) if self._state.pending is not None else None

        row = df.iloc[i]
        events = self._state.process_bar(i, row, df)

        # Detect pending state transitions:
        #   prev_pending is None,    cur_pending is set     → NEW signal → emit ENTRY
        #   prev_pending differs,    cur_pending is set     → REPLACEMENT → emit ENTRY
        #   prev_pending is set,     cur_pending is None,
        #       AND no "entry" TradeEvent emitted this bar  → EXPIRY → emit CANCEL_PENDING
        #   prev_pending is set,     cur_pending is None,
        #       AND an "entry" TradeEvent was emitted       → FILL → already handled by translate
        cur_pending = self._state.pending
        signals = self._translate_events(events, bar)
        had_entry_event = any(ev.type == "entry" for ev in events)

        if cur_pending is not None:
            new_or_replaced = (
                prev_pending is None
                or prev_pending.get("signal_bar") != cur_pending.get("signal_bar")
                or prev_pending.get("side") != cur_pending.get("side")
            )
            if new_or_replaced:
                signals.insert(
                    0,
                    LiveSignal(
                        signal_type=SignalType.ENTRY,
                        side=cur_pending["side"].upper(),
                        symbol=self._config.symbol,
                        entry_price=cur_pending["entry_price"],
                        stop_loss=cur_pending["stop_price"],
                        # CRITICAL: vault qty is in raw units (e.g., 100k for 1 lot).
                        # Agent + supervisor expect LOTS. Convert.
                        volume=self._units_to_lots(cur_pending["qty"]),
                        metadata={
                            "bar_close_time": bar.timestamp,
                            "engine": "vault",
                            "signal_bar": cur_pending["signal_bar"],
                            "raw_units": cur_pending["qty"],
                        },
                    ),
                )
        elif prev_pending is not None and not had_entry_event:
            # Pending was cleared without a fill — vault's signal_expiry_bars
            # gate fired, OR re-validation cancelled it. Emit CANCEL_PENDING so
            # the agent cancels the corresponding broker order. Without this,
            # broker orders sit orphaned past vault's expiry window.
            signals.append(
                LiveSignal(
                    signal_type=SignalType.CANCEL_PENDING,
                    side=prev_pending["side"].upper(),
                    symbol=self._config.symbol,
                    metadata={
                        "bar_close_time": bar.timestamp,
                        "engine": "vault",
                        "reason": "expiry_or_revalidate",
                        "signal_bar": prev_pending.get("signal_bar"),
                    },
                )
            )

        # SL propagation: emit MODIFY_SL when vault's runner_stop tightens.
        # Vault computes runner_stop each bar (breakeven + ATR trail) and stores
        # it on position["runner_stop"]. We track the last-emitted stop and
        # signal the agent when it ratchets favourably.
        pos = self._state.position
        if pos is not None and "runner_stop" in pos:
            new_stop = pos["runner_stop"]
            if had_entry_event:
                self._current_stop = pos["initial_stop"]
            if self._current_stop is None:
                self._current_stop = pos["initial_stop"]
            side = pos["side"]
            tightened = (side == "long" and new_stop > self._current_stop) or (
                side == "short" and new_stop < self._current_stop
            )
            if tightened:
                old_stop = self._current_stop
                self._current_stop = new_stop
                signals.append(
                    LiveSignal(
                        signal_type=SignalType.MODIFY_SL,
                        side=side.upper(),
                        symbol=self._config.symbol,
                        new_stop=new_stop,
                        metadata={
                            "bar_close_time": bar.timestamp,
                            "engine": "vault",
                            "old_stop": old_stop,
                        },
                    )
                )
        elif pos is None:
            self._current_stop = None

        return signals

    @staticmethod
    def _units_to_lots(qty_units: float) -> float:
        """Vault's qty is in raw FX units (100,000 = 1 lot for EUR/USD).
        Agent + HardRiskSupervisor expect lots. Round to 0.01 lot precision."""
        return round(qty_units / 100_000.0, 2)

    def notify_fill(self, fill_price: float, volume: float) -> None:
        # External broker fill notification. The state already manages fills
        # internally via process_bar's path-based detection; this is a stub
        # for live-broker reconciliation if/when needed.
        log.info("VaultLiveEngine.notify_fill: price=%.5f vol=%.2f (state-managed internally)", fill_price, volume)

    def notify_close(self, pnl: float) -> None:
        """Called by the agent when a broker position is closed (either by
        agent action or out-of-band broker close like SL hit). MUST clear
        vault's internal position so vault's per-bar exit logic doesn't keep
        emitting EXIT signals for a position the broker has already closed.

        Without this, vault and agent state drift: agent FLAT, vault LONG;
        every subsequent vault EXIT signal gets rejected as 'invalid from FLAT'.
        """
        had_position = self._state.position is not None
        self._state.position = None
        self._current_stop = None
        # Track the bar where we went flat so cooldown logic still functions.
        if self._primary_candles:
            self._state.last_flat_bar = len(self._primary_candles) - 1
        log.info(
            "VaultLiveEngine.notify_close: pnl=%.2f had_position=%s (vault state cleared)",
            pnl,
            had_position,
        )

    def cancel_pending(self) -> None:
        if self._state.pending is not None:
            self._state.pending = None

    def recover_position_state(
        self,
        side: str,
        entry_price: float,
        stop_loss: float,
        volume: float,
    ) -> None:
        """Adopt a broker position discovered on startup (e.g. after restart).

        Conservative recovery (mirrors `LiveStrategyEngine.recover_position_state`):
        broker has ONE combined position; we don't know if partial was already
        taken. Treating the recovered volume as the RUNNER (with tp_qty_open=0)
        prevents the wrapper from trying to fire a second partial OR from
        leaving a phantom tp-leg open after the runner exits. Partial state is
        reconciled separately by the post-trade verifier if needed.
        """
        side_lower = side.lower()
        latest = self._primary_candles[-1] if self._primary_candles else None
        self._state.position = {
            "side": side_lower,
            "entry_time": latest.timestamp if latest else None,
            "entry_bar": len(self._primary_candles) - 1,
            "entry_price": entry_price,
            "qty_total": volume,
            # CRITICAL: treat as runner-only. Setting tp_qty_open=0 avoids the
            # post-runner-close phantom-leg bug where vault keeps emitting EXIT
            # signals for a tp-leg that the broker doesn't actually have.
            "tp_qty_open": 0.0,
            "runner_qty_open": volume,
            "initial_stop": stop_loss,
            "highest_since_entry": (latest.high if latest else entry_price) if side_lower == "long" else float("nan"),
            "lowest_since_entry": (latest.low if latest else entry_price) if side_lower == "short" else float("nan"),
        }
        self._state.pending = None
        self._current_stop = stop_loss
        log.info(
            "VaultLiveEngine.recover_position_state: %s entry=%.5f sl=%.5f vol=%.2f (treated as runner-only)",
            side_lower,
            entry_price,
            stop_loss,
            volume,
        )

    # ------------------------------------------------------------------
    # Legacy private-buffer accessors (for code that pokes at engine internals)
    # ------------------------------------------------------------------

    @property
    def _h1_candles(self) -> list[Candle]:
        """Legacy alias for primary-timeframe buffer (mirrors LiveStrategyEngine)."""
        return self._primary_candles

    @property
    def _h4_candles(self) -> list[Candle]:
        """Legacy alias for higher-timeframe buffer (mirrors LiveStrategyEngine)."""
        return self._higher_candles

    # ------------------------------------------------------------------
    # Properties (mirror LiveStrategyEngine)
    # ------------------------------------------------------------------

    @property
    def state(self) -> LiveState:
        if self._live_state == LiveState.WARMING_UP:
            return LiveState.WARMING_UP
        pos = self._state.position
        pen = self._state.pending
        if pos is not None:
            return LiveState.LONG if pos["side"] == "long" else LiveState.SHORT
        if pen is not None:
            return LiveState.PENDING_LONG if pen["side"] == "long" else LiveState.PENDING_SHORT
        return LiveState.FLAT

    @property
    def is_warmed_up(self) -> bool:
        return self._live_state != LiveState.WARMING_UP

    @property
    def position(self) -> OpenPosition | None:
        pos = self._state.position
        if pos is None:
            return None
        side_upper = pos["side"].upper()
        return OpenPosition(
            side=side_upper,
            entry_price=pos["entry_price"],
            stop_loss=self._current_stop or pos["initial_stop"],
            volume=pos["tp_qty_open"] + pos["runner_qty_open"],
            entry_bar=pos["entry_bar"],
            initial_stop=pos["initial_stop"],
            initial_volume=pos["qty_total"],
            tp_qty_open=pos["tp_qty_open"],
            runner_qty_open=pos["runner_qty_open"],
            highest_since_entry=pos.get("highest_since_entry") or pos["entry_price"],
            lowest_since_entry=pos.get("lowest_since_entry") or pos["entry_price"],
        )

    @property
    def pending(self) -> PendingOrder | None:
        pen = self._state.pending
        if pen is None:
            return None
        return PendingOrder(
            side=pen["side"].upper(),
            entry_price=pen["entry_price"],
            stop_loss=pen["stop_price"],
            volume=pen["qty"],
            bar_index=pen["signal_bar"],
        )

    @property
    def total_bars(self) -> int:
        return len(self._primary_candles)

    @property
    def equity(self) -> float:
        return self._state.equity

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "symbol": self._config.symbol,
            "total_bars": self.total_bars,
            "primary_buffered": len(self._primary_candles),
            "higher_buffered": len(self._higher_candles),
            "equity": self.equity,
            "has_pending": self._state.pending is not None,
            "has_position": self._state.position is not None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_df(self) -> pd.DataFrame:
        """Convert rolling candle buffer to a vault-shaped DataFrame."""
        rows = [
            {
                "time": pd.Timestamp(c.timestamp, unit="s", tz="UTC").tz_localize(None),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
            }
            for c in self._primary_candles
        ]
        df = pd.DataFrame(rows)
        return prepare_dataframe(df, self._irb_cfg)

    def _translate_events(self, events: list[TradeEvent], bar: Candle) -> list[LiveSignal]:
        """Translate vault TradeEvents → LiveSignals (matches existing engine).

        IMPORTANT: vault TradeEvent.qty is in raw FX units. Convert to lots
        before emitting (agent + supervisor expect lots).
        """
        signals: list[LiveSignal] = []
        for ev in events:
            qty_lots = self._units_to_lots(ev.qty)
            if ev.type == "entry":
                # vault emits an entry event when the pending order fills.
                signals.append(
                    LiveSignal(
                        signal_type=SignalType.PENDING_FILL,
                        side=ev.side.upper(),
                        symbol=self._config.symbol,
                        entry_price=ev.price,
                        volume=qty_lots,
                        metadata={"bar_close_time": bar.timestamp, "engine": "vault", "raw_units": ev.qty},
                    )
                )
            elif ev.type == "exit":
                if ev.reason == "partial_tp":
                    signals.append(
                        LiveSignal(
                            signal_type=SignalType.PARTIAL_EXIT,
                            side=ev.side.upper(),
                            symbol=self._config.symbol,
                            exit_price=ev.price,
                            volume=qty_lots,
                            exit_reason="partial_tp",
                            metadata={
                                "bar_close_time": bar.timestamp,
                                "engine": "vault",
                                "pnl": ev.pnl,
                                "raw_units": ev.qty,
                                # Size the live partial off the REAL (auto-sized)
                                # broker position, not the 1-lot-capped vault
                                # state. qty_lots above reflects the capped state
                                # and would close a trivial slice.
                                "partial_fraction": self._irb_cfg.partial_exit_pct / 100.0,
                            },
                        )
                    )
                else:
                    signals.append(
                        LiveSignal(
                            signal_type=SignalType.EXIT,
                            side=ev.side.upper(),
                            symbol=self._config.symbol,
                            exit_price=ev.price,
                            volume=qty_lots,
                            exit_reason=ev.reason or "",
                            metadata={
                                "bar_close_time": bar.timestamp,
                                "engine": "vault",
                                "pnl": ev.pnl,
                                "raw_units": ev.qty,
                            },
                        )
                    )
        return signals
