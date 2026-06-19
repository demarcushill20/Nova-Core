"""Task-1060 — policy adapter: trained actor -> execution-engine decisions.

The execution engine (``execution.py``) drives the H1-decision / M1-execution
loop behind a single injection point::

    PolicyFn = Callable[[int], Decision]

i.e. "given the index of an H1 decision bar, return the {hold, buy, sell}
``Decision`` (with its ATR stop multiple and take-profit risk-multiple)". The
engine docstring deferred the wiring of the buy/sell/hold actor into that
callback to *this* task. This module supplies that bridge plus the spec's
fixed-percent-of-equity position sizing.

Two deliberate design choices:

* **Model-agnostic bridge.** The training loop that connects the env's
  feature windows to the DreamerV3 world model (Phases D-F) is not yet
  committed, so the observation->latent->action contract is *not pinned*.
  Rather than guess it, the bridge consumes a *precomputed per-bar action
  sequence* (the 0/1/2 indices the actor emits) and turns it into a
  ``PolicyFn``. Whatever eventually produces those indices — a trained
  ``Dreamer.act`` rollout, a heuristic, or a test stub — plugs in unchanged.
  ``actor_actions`` is the thin helper for the common "map each observation
  through one ``actor_fn``" case.

* **Action convention matches the env.** ``0 = hold/flat``, ``1 = buy/long``,
  ``2 = sell/short`` — identical to ``env.TradingEnv`` and
  ``execution.{HOLD,BUY,SELL}`` — so an index never changes meaning as it
  crosses module boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from . import config
from .execution import Decision, PolicyFn


def position_size(
    equity: float,
    stop_distance: float,
    *,
    risk_pct: float = config.DEFAULT.risk_pct,
    value_per_point: float = 1.0,
) -> float:
    """Units to trade so the ATR stop costs exactly ``risk_pct`` of equity.

    Spec ("Position risk is based on a fixed percent of equity, such as 0.5%"):
    the cash at risk between entry and the stop is ``equity * risk_pct``; the
    per-unit risk is ``stop_distance * value_per_point``. Dividing gives the
    position size in units/contracts::

        units = (equity * risk_pct) / (stop_distance * value_per_point)

    ``stop_distance`` is the absolute entry->stop price gap (``= atr_stop_mult *
    ATR``); ``value_per_point`` converts one point of price into account
    currency per unit (1.0 for a 1:1 instrument).
    """
    if equity <= 0.0:
        raise ValueError(f"equity must be > 0 (got {equity})")
    if not (0.0 < risk_pct < 1.0):
        raise ValueError(f"risk_pct must be in (0, 1) (got {risk_pct})")
    if stop_distance <= 0.0:
        raise ValueError(f"stop_distance must be > 0 (got {stop_distance})")
    if value_per_point <= 0.0:
        raise ValueError(f"value_per_point must be > 0 (got {value_per_point})")
    return (equity * risk_pct) / (stop_distance * value_per_point)


def actions_to_policy(
    actions: Sequence[int],
    *,
    sl_atr_mult: float = config.DEFAULT.atr_stop_mult,
    tp_rr: float = config.DEFAULT.take_profit_rr,
    levels: Sequence[tuple[float, float]] | None = None,
) -> PolicyFn:
    """Wrap a per-H1-bar action sequence as an ``execution.PolicyFn``.

    ``actions[i]`` is the 0/1/2 decision the actor emits at H1 bar ``i``. The
    returned callable is exactly what ``execution.run_backtest`` expects.

    By default every entry uses the global ``sl_atr_mult`` / ``tp_rr`` from the
    config. If a future actor *also* emits per-bar stop/target multiples, pass
    ``levels`` as a parallel sequence of ``(sl_atr_mult, tp_rr)`` tuples and
    those override the defaults bar-by-bar (the values are ignored on HOLD).
    """
    actions = list(actions)
    if levels is not None and len(levels) != len(actions):
        raise ValueError("levels must be the same length as actions")

    def policy(i: int) -> Decision:
        if not 0 <= i < len(actions):
            raise IndexError(f"decision-bar index {i} out of range (n={len(actions)})")
        sl, tp = levels[i] if levels is not None else (sl_atr_mult, tp_rr)
        return Decision(action=int(actions[i]), sl_atr_mult=sl, tp_rr=tp)

    return policy


def actor_actions(
    actor_fn: Callable[[object], int],
    observations: Sequence[object],
) -> list[int]:
    """Map each H1 observation through ``actor_fn`` to its 0/1/2 action index.

    A convenience for the common case where the policy is "run one observation
    through the actor and read off its greedy action". ``actor_fn`` is whatever
    realises the trained model on a single observation; keeping it abstract
    avoids hard-coding the (not-yet-committed) env->Dreamer obs contract here.
    The result feeds straight into :func:`actions_to_policy`.
    """
    out: list[int] = []
    for obs in observations:
        a = int(actor_fn(obs))
        if a not in (0, 1, 2):
            raise ValueError(f"actor_fn returned invalid action {a!r}; expected 0/1/2")
        out.append(a)
    return out
