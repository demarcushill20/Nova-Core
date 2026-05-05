"""Diagnostic event emitter for Pine ↔ Nova parity diff.

When `BacktestEnvironment.diagnostic_logging` is enabled, the engine streams
JSONL events at every decision point matching the schema emitted by
`configs/pinescript/irb_v5_stag.pine` (DIAG_LOG toggle, section 17b). The
two streams can then be aligned bar-by-bar to localise the diffuse leak
identified in `docs/parity/exit-timing-audit.md`.

Schema parity contract (verified by `tests/test_diagnostic_emitter.py`):

  - `event` ∈ {"SIGNAL_PLACE", "SIGNAL_REPLACE", "FILL", "EXIT",
                "CANCEL", "TRAIL_UPDATE", "PER_BAR"}
  - `bar_close_time` is epoch milliseconds (matches Pine `time_close`)
  - `side` ∈ {"BUY", "SELL"}
  - Per-event fields mirror the Pine emission field-for-field
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DiagnosticEmitter:
    """Streams parity-audit events to a JSONL file.

    Cheap to construct, cheap to call when disabled (the engine should still
    short-circuit via `env.diagnostic_logging` before invoking emit_*). When
    enabled, every emit_* writes one JSON object per line and flushes
    immediately so a crashed run still produces partial diagnostics.
    """

    def __init__(self, output_path: str | Path) -> None:
        self.path = Path(output_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", buffering=1)
        self._count = 0

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> DiagnosticEmitter:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def event_count(self) -> int:
        return self._count

    def _write(self, payload: dict[str, Any]) -> None:
        self._fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._count += 1

    @staticmethod
    def _bar_close_ms(bar_timestamp_seconds: float) -> int:
        """Convert Candle.timestamp (seconds) → epoch ms (Pine `time_close`)."""
        return int(bar_timestamp_seconds * 1000)

    @staticmethod
    def _round5(x: float) -> float:
        """Round to 5 decimals — matches Pine `str.tostring(x, "#.#####")`."""
        return round(x, 5) if x == x else x  # NaN guard

    @staticmethod
    def _round2(x: float) -> float:
        return round(x, 2) if x == x else x

    @staticmethod
    def _round3(x: float) -> float:
        return round(x, 3) if x == x else x

    # ------------------------------------------------------------------
    # Event emitters — Pine schema parity
    # ------------------------------------------------------------------

    def emit_signal(
        self,
        *,
        is_replacement: bool,
        bar_close_time_seconds: float,
        bar_index: int,
        side: str,
        entry_price: float,
        stop_loss: float,
        volume: float,
        risk_dollars: float,
        bar_o: float,
        bar_h: float,
        bar_l: float,
        bar_c: float,
        irb_range: float,
        ema_10: float,
        ema_20: float,
        ema_50: float,
        ema_h4: float,
        trail_ema: float,
        ema_slope: float,
        adx: float,
        atr: float,
        oe_ratio: float,
        is_up_irb: bool,
        is_dn_irb: bool,
        trend_up: bool,
        trend_dn: bool,
        h4_up: bool,
        h4_dn: bool,
        sw_pass: bool,
        oe_pass: bool,
        ema_stack_bull: bool,
        ema_stack_bear: bool,
        ema10_confirm_long: bool,
        ema10_confirm_short: bool,
        strategy_state: str,
    ) -> None:
        pip_size = 0.0001  # EURUSD; Pine pip_size = mintick * 10
        stop_distance_pips = abs(entry_price - stop_loss) / pip_size
        self._write(
            {
                "event": "SIGNAL_REPLACE" if is_replacement else "SIGNAL_PLACE",
                "bar_close_time": self._bar_close_ms(bar_close_time_seconds),
                "bar_index": bar_index,
                "side": side,
                "entry_price": self._round5(entry_price),
                "stop_loss": self._round5(stop_loss),
                "stop_distance_pips": self._round5(stop_distance_pips),
                "volume": self._round2(volume),
                "risk_dollars": self._round2(risk_dollars),
                "bar_o": self._round5(bar_o),
                "bar_h": self._round5(bar_h),
                "bar_l": self._round5(bar_l),
                "bar_c": self._round5(bar_c),
                "irb_range": self._round5(irb_range),
                "ema_10": self._round5(ema_10),
                "ema_20": self._round5(ema_20),
                "ema_50": self._round5(ema_50),
                "ema_h4": self._round5(ema_h4),
                "trail_ema": self._round5(trail_ema),
                "ema_slope": self._round3(ema_slope),
                "adx": self._round2(adx),
                "atr": self._round5(atr),
                "oe_ratio": self._round3(oe_ratio),
                "is_up_irb": is_up_irb,
                "is_dn_irb": is_dn_irb,
                "trend_up": trend_up,
                "trend_dn": trend_dn,
                "h4_up": h4_up,
                "h4_dn": h4_dn,
                "sw_pass": sw_pass,
                "oe_pass": oe_pass,
                "ema_stack_bull": ema_stack_bull,
                "ema_stack_bear": ema_stack_bear,
                "ema10_confirm_long": ema10_confirm_long,
                "ema10_confirm_short": ema10_confirm_short,
                "strategy_state": strategy_state,
            }
        )

    def emit_fill(
        self,
        *,
        bar_close_time_seconds: float,
        bar_index: int,
        side: str,
        fill_price: float,
        stop_loss: float,
        qty: float,
        bars_to_fill: int,
        bar_o: float,
        bar_h: float,
        bar_l: float,
        bar_c: float,
    ) -> None:
        self._write(
            {
                "event": "FILL",
                "bar_close_time": self._bar_close_ms(bar_close_time_seconds),
                "bar_index": bar_index,
                "side": side,
                "fill_price": self._round5(fill_price),
                "stop_loss": self._round5(stop_loss),
                "qty": self._round2(qty),
                "bars_to_fill": bars_to_fill,
                "bar_o": self._round5(bar_o),
                "bar_h": self._round5(bar_h),
                "bar_l": self._round5(bar_l),
                "bar_c": self._round5(bar_c),
            }
        )

    def emit_exit(
        self,
        *,
        bar_close_time_seconds: float,
        bar_index: int,
        side: str,
        exit_price: float,
        entry_price: float,
        entry_bar_index: int,
        exit_bar_index: int,
        qty: float,
        exit_comment: str,
        pnl: float,
        bar_o: float,
        bar_h: float,
        bar_l: float,
        bar_c: float,
    ) -> None:
        self._write(
            {
                "event": "EXIT",
                "bar_close_time": self._bar_close_ms(bar_close_time_seconds),
                "bar_index": bar_index,
                "side": side,
                "exit_price": self._round5(exit_price),
                "entry_price": self._round5(entry_price),
                "entry_bar_index": entry_bar_index,
                "exit_bar_index": exit_bar_index,
                "qty": self._round2(qty),
                "exit_comment": exit_comment,
                "pnl": self._round2(pnl),
                "bar_o": self._round5(bar_o),
                "bar_h": self._round5(bar_h),
                "bar_l": self._round5(bar_l),
                "bar_c": self._round5(bar_c),
            }
        )

    def emit_cancel(
        self,
        *,
        bar_close_time_seconds: float,
        bar_index: int,
        side: str,
        reason: str,
        bars_pending: int,
    ) -> None:
        self._write(
            {
                "event": "CANCEL",
                "bar_close_time": self._bar_close_ms(bar_close_time_seconds),
                "bar_index": bar_index,
                "side": side,
                "reason": reason,
                "bars_pending": bars_pending,
            }
        )

    def emit_trail_update(
        self,
        *,
        bar_close_time_seconds: float,
        bar_index: int,
        side: str,
        old_stop: float,
        new_stop: float,
        trail_ema: float,
        bars_since_entry: int,
        best_close: float,
    ) -> None:
        self._write(
            {
                "event": "TRAIL_UPDATE",
                "bar_close_time": self._bar_close_ms(bar_close_time_seconds),
                "bar_index": bar_index,
                "side": side,
                "old_stop": self._round5(old_stop),
                "new_stop": self._round5(new_stop),
                "trail_ema": self._round5(trail_ema),
                "bars_since_entry": bars_since_entry,
                "best_close": self._round5(best_close),
            }
        )

    def emit_per_bar(
        self,
        *,
        bar_close_time_seconds: float,
        bar_index: int,
        state: str,
        pos_bars: int,
        pend_bars: int,
        cur_stop: float,
        best_cl: float,
        trail_ema: float,
        ema_10: float,
        ema_20: float,
        ema_50: float,
        ema_h4: float,
        ema_slope: float,
        atr: float,
        adx: float,
        oe_ratio: float,
        is_up_irb: bool,
        is_dn_irb: bool,
        trend_up: bool,
        trend_dn: bool,
        h4_up: bool,
        h4_dn: bool,
        sw_pass: bool,
        oe_pass: bool,
        ema_stack_bull: bool,
        ema_stack_bear: bool,
        ema10_confirm_long: bool,
        ema10_confirm_short: bool,
        sig_long: bool,
        sig_short: bool,
        bar_o: float,
        bar_h: float,
        bar_l: float,
        bar_c: float,
    ) -> None:
        self._write(
            {
                "event": "PER_BAR",
                "bar_close_time": self._bar_close_ms(bar_close_time_seconds),
                "bar_index": bar_index,
                "state": state,
                "pos_bars": pos_bars,
                "pend_bars": pend_bars,
                "cur_stop": self._round5(cur_stop),
                "best_cl": self._round5(best_cl),
                "trail_ema": self._round5(trail_ema),
                "ema_10": self._round5(ema_10),
                "ema_20": self._round5(ema_20),
                "ema_50": self._round5(ema_50),
                "ema_h4": self._round5(ema_h4),
                "ema_slope": self._round3(ema_slope),
                "atr": self._round5(atr),
                "adx": self._round2(adx),
                "oe_ratio": self._round3(oe_ratio),
                "is_up_irb": is_up_irb,
                "is_dn_irb": is_dn_irb,
                "trend_up": trend_up,
                "trend_dn": trend_dn,
                "h4_up": h4_up,
                "h4_dn": h4_dn,
                "sw_pass": sw_pass,
                "oe_pass": oe_pass,
                "ema_stack_bull": ema_stack_bull,
                "ema_stack_bear": ema_stack_bear,
                "ema10_confirm_long": ema10_confirm_long,
                "ema10_confirm_short": ema10_confirm_short,
                "sig_long": sig_long,
                "sig_short": sig_short,
                "bar_o": self._round5(bar_o),
                "bar_h": self._round5(bar_h),
                "bar_l": self._round5(bar_l),
                "bar_c": self._round5(bar_c),
            }
        )
