"""Task-1057 — H1-decision / M1-execution engine for the DreamerV3 gold bot.

This is the deterministic execution layer the spec describes:

    At the close of each H1 candle:
        * build the feature vector,
        * give it to the trained RL agent,
        * the agent chooses {hold, buy, sell} together with an ATR stop-loss
          multiplier and a take-profit risk-multiple,
        * hold -> do nothing, buy -> open a long, sell -> open a short.

Stop loss (section 6) is ATR-based at entry:

    long  : stop = entry - sl_atr_mult * ATR
    short : stop = entry + sl_atr_mult * ATR

Take profit (section 7) is a multiple of the stop risk distance:

    risk        = |entry - stop|        ( = sl_atr_mult * ATR )
    long  target = entry + tp_rr * risk
    short target = entry - tp_rr * risk

Exit logic (section 8) — four exit types, resolved by walking M1 price through
each H1 decision bar in time order:

    * ``stop``   — price touches the stop level
    * ``target`` — price touches the take-profit level
    * ``flip``   — the model emits the opposite signal while a trade is open;
                   the open trade is closed at the H1 decision price, and a new
                   trade in the opposite direction may be opened
    * ``manual`` — a manual / timeout close (``max_holding_bars`` H1 bars
                   elapsed with neither stop nor target hit)

Decoupling note: the policy is injected as a callable returning a ``Decision``.
The DreamerV3 actor is still ``n_actions=2`` (long/flat) in the committed
foundation; the buy/sell/hold rewire lands in task-1060. Keeping the policy
behind a callback lets this engine be implemented and unit-tested now, against
the corrected three-action spec, without waiting on the model rewire.

Conservative same-M1-bar tie-break: if a single M1 bar's range straddles BOTH
the stop and the target, the **stop** is assumed to fill first (worst case for
the trade). This avoids optimistic fills that would inflate backtest results.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import config

# Action codes — identical to the env's {flat, long, short} mapping.
HOLD = 0  # do nothing / stay flat
BUY = 1  # open (or stay) long
SELL = 2  # open (or stay) short

# Position sides.
FLAT = 0
LONG = 1
SHORT = -1

_ACTION_SIDE = {HOLD: FLAT, BUY: LONG, SELL: SHORT}


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Decision:
    """One H1-close decision from the policy.

    ``sl_atr_mult`` / ``tp_rr`` are only meaningful for BUY / SELL; they are
    ignored for HOLD. Defaults follow the transcript's common behaviour
    (SL = 1.5 ATR, TP = 2x risk == 3 ATR, reward:risk = 2:1).
    """

    action: int
    sl_atr_mult: float = config.DEFAULT.atr_stop_mult
    tp_rr: float = config.DEFAULT.take_profit_rr

    def __post_init__(self) -> None:
        if self.action not in _ACTION_SIDE:
            raise ValueError(f"invalid action {self.action!r}; expected 0/1/2")
        if self.action != HOLD:
            if self.sl_atr_mult <= 0.0:
                raise ValueError("sl_atr_mult must be > 0 for an entry")
            if self.tp_rr <= 0.0:
                raise ValueError("tp_rr must be > 0 for an entry")

    @property
    def side(self) -> int:
        return _ACTION_SIDE[self.action]


@dataclass
class Trade:
    """An open or closed position with its protective levels."""

    side: int  # LONG (+1) or SHORT (-1)
    entry_price: float
    stop: float
    target: float
    entry_index: int  # H1 decision-bar index at which it opened
    # filled on close:
    exit_price: float | None = None
    exit_index: int | None = None
    exit_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_reason is None

    @property
    def risk(self) -> float:
        """Absolute per-unit stop distance (always positive)."""
        return abs(self.entry_price - self.stop)

    def pnl(self) -> float:
        """Signed price PnL per unit (entry->exit), 0 while still open."""
        if self.exit_price is None:
            return 0.0
        return self.side * (self.exit_price - self.entry_price)

    def r_multiple(self) -> float:
        """PnL expressed in units of initial risk (R)."""
        if self.exit_price is None or self.risk == 0.0:
            return 0.0
        return self.pnl() / self.risk


@dataclass(frozen=True)
class Bar:
    """A minimal OHLC bar for the M1 execution path."""

    high: float
    low: float
    close: float


# --------------------------------------------------------------------------- #
# Level construction (sections 6 & 7)
# --------------------------------------------------------------------------- #
def stop_loss(side: int, entry_price: float, atr: float, sl_atr_mult: float) -> float:
    """ATR-based stop. ``side`` is LONG (+1) or SHORT (-1)."""
    if atr <= 0.0:
        raise ValueError("ATR must be positive to size a stop")
    offset = sl_atr_mult * atr
    if side == LONG:
        return entry_price - offset
    if side == SHORT:
        return entry_price + offset
    raise ValueError(f"cannot size a stop for side {side!r}")


def take_profit(side: int, entry_price: float, stop: float, tp_rr: float) -> float:
    """Take-profit a multiple ``tp_rr`` of the stop-risk distance away."""
    risk = abs(entry_price - stop)
    if side == LONG:
        return entry_price + tp_rr * risk
    if side == SHORT:
        return entry_price - tp_rr * risk
    raise ValueError(f"cannot size a target for side {side!r}")


def open_trade(
    side: int,
    entry_price: float,
    atr: float,
    decision: Decision,
    entry_index: int,
) -> Trade:
    """Construct an open ``Trade`` with ATR stop + RR target from a decision."""
    stop = stop_loss(side, entry_price, atr, decision.sl_atr_mult)
    target = take_profit(side, entry_price, stop, decision.tp_rr)
    return Trade(
        side=side,
        entry_price=entry_price,
        stop=stop,
        target=target,
        entry_index=entry_index,
    )


# --------------------------------------------------------------------------- #
# Intrabar resolution (section 8: stop / target)
# --------------------------------------------------------------------------- #
def _stop_hit(trade: Trade, bar: Bar) -> bool:
    if trade.side == LONG:
        return bar.low <= trade.stop
    return bar.high >= trade.stop


def _target_hit(trade: Trade, bar: Bar) -> bool:
    if trade.side == LONG:
        return bar.high >= trade.target
    return bar.low <= trade.target


def resolve_path(trade: Trade, path: Sequence[Bar]) -> tuple[str, float] | None:
    """Walk the M1 ``path`` of one H1 bar; return ``(reason, price)`` or None.

    ``reason`` is ``"stop"`` or ``"target"``. Within a single M1 bar that
    straddles both levels, the stop is taken first (conservative). The first
    M1 bar to touch a level ends the walk.
    """
    for bar in path:
        stop = _stop_hit(trade, bar)
        target = _target_hit(trade, bar)
        if stop and target:
            return ("stop", trade.stop)  # worst-case tie-break
        if stop:
            return ("stop", trade.stop)
        if target:
            return ("target", trade.target)
    return None


def _close(trade: Trade, reason: str, price: float, index: int) -> None:
    trade.exit_reason = reason
    trade.exit_price = price
    trade.exit_index = index


# --------------------------------------------------------------------------- #
# Backtest driver
# --------------------------------------------------------------------------- #
PolicyFn = Callable[[int], Decision]
"""Maps an H1 decision-bar index to a ``Decision``. Wrap the RL agent here."""


def run_backtest(
    decision_close: Sequence[float],
    atr: Sequence[float],
    m1_paths: Sequence[Sequence[Bar]],
    policy: PolicyFn,
    *,
    max_holding_bars: int | None = None,
) -> list[Trade]:
    """Drive the H1-decision / M1-execution loop and return closed trades.

    Parameters
    ----------
    decision_close:
        Close price of each H1 decision bar; entries/flips fill here.
    atr:
        ATR aligned to ``decision_close`` (the ATR "at that moment").
    m1_paths:
        ``m1_paths[i]`` is the ordered list of M1 bars spanning the interval
        *after* H1 bar ``i`` closes and *before* bar ``i+1`` closes — the
        window over which an entry opened at bar ``i`` can hit its stop/target.
    policy:
        Callable returning a ``Decision`` for each decision-bar index.
    max_holding_bars:
        If set, an open trade still alive after this many H1 bars is closed
        ``"manual"`` (timeout) at the then-current H1 close.
    """
    n = len(decision_close)
    if not (len(atr) == len(m1_paths) == n):
        raise ValueError("decision_close, atr and m1_paths must share length")

    closed: list[Trade] = []
    trade: Trade | None = None

    for i in range(n):
        price = float(decision_close[i])
        decision = policy(i)

        # 1) Flip / manual-timeout management of any OPEN trade at this close.
        if trade is not None and trade.is_open:
            opposite = decision.side != FLAT and decision.side != trade.side
            timed_out = max_holding_bars is not None and (i - trade.entry_index) >= max_holding_bars
            if opposite:
                _close(trade, "flip", price, i)
                closed.append(trade)
                trade = None
            elif timed_out:
                _close(trade, "manual", price, i)
                closed.append(trade)
                trade = None
            # same-side signal or HOLD while open -> let the trade run.

        # 2) Open a new trade if flat and the model wants in.
        if trade is None and decision.side != FLAT:
            trade = open_trade(decision.side, price, float(atr[i]), decision, i)

        # 3) Walk this bar's M1 path to resolve stop / target.
        if trade is not None and trade.is_open:
            hit = resolve_path(trade, m1_paths[i])
            if hit is not None:
                reason, exit_price = hit
                _close(trade, reason, exit_price, i)
                closed.append(trade)
                trade = None

    # Force-close anything still open at the end of the series (timeout-style).
    if trade is not None and trade.is_open:
        _close(trade, "manual", float(decision_close[-1]), n - 1)
        closed.append(trade)

    return closed
