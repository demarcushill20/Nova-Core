"""Phase C — minimal long/flat trading environment (no gym dependency).

Deliberately tiny: a ``reset()/step()`` protocol over pre-built arrays, so the
DreamerV3 replay loop (Phase E) can drive it without pulling in gymnasium.

Fill / reward timing (matches the analyze-step leakage notes):

    * The agent observes the 64-bar feature window ending at bar ``t``.
    * Its action sets the position held over the *next* bar (no same-bar fill).
    * Reward at step ``t`` = ``position_t * fwd_ret[t] - cost * |position_t -
      position_{t-1}|`` where ``fwd_ret[t]`` is the close[t]->close[t+1] return.

Actions: ``0 = flat``, ``1 = long``. Long-only (``allow_short=False``); the
short branch is wired but disabled to match the spec.
"""

from __future__ import annotations

import numpy as np

from . import config


class TradingEnv:
    """A single-asset long/flat environment over a fixed feature matrix.

    Parameters
    ----------
    features:
        ``(T, n_features)`` standardised feature matrix (scaler already applied).
    fwd_returns:
        ``(T,)`` close-to-next-close simple returns, aligned to ``features``;
        ``fwd_returns[t]`` is earned by a position held *into* bar ``t+1``.
    """

    def __init__(
        self,
        features: np.ndarray,
        fwd_returns: np.ndarray,
        window: int = config.DEFAULT.window,
        cost: float = config.DEFAULT.cost,
        allow_short: bool = config.DEFAULT.allow_short,
    ) -> None:
        features = np.asarray(features, dtype="float32")
        fwd_returns = np.asarray(fwd_returns, dtype="float32")
        if features.shape[0] != fwd_returns.shape[0]:
            raise ValueError("features and fwd_returns must share length T")
        if features.shape[0] <= window:
            raise ValueError("need more rows than the window length")

        self.features = features
        self.fwd_returns = fwd_returns
        self.window = window
        self.cost = float(cost)
        self.allow_short = bool(allow_short)
        self.n_features = features.shape[1]
        self.n_actions = 3 if allow_short else 2  # {flat,long[,short]}

        # Last index that still has a realised forward return.
        self._last_t = features.shape[0] - 2
        self._t = window - 1
        self._position = 0.0

    # ------------------------------------------------------------------ #
    def _obs(self) -> np.ndarray:
        return self.features[self._t - self.window + 1 : self._t + 1]

    def reset(self) -> np.ndarray:
        self._t = self.window - 1
        self._position = 0.0
        return self._obs()

    def _action_to_position(self, action: int) -> float:
        if action == 0:
            return 0.0
        if action == 1:
            return 1.0
        if action == 2 and self.allow_short:
            return -1.0
        raise ValueError(f"invalid action {action} (allow_short={self.allow_short})")

    def step(self, action: int):
        """Advance one bar. Returns ``(obs, reward, done, info)``."""
        prev_position = self._position
        position = self._action_to_position(action)

        gross = position * float(self.fwd_returns[self._t])
        turnover_cost = self.cost * abs(position - prev_position)
        reward = gross - turnover_cost

        self._position = position
        self._t += 1
        done = self._t >= self._last_t

        info = {
            "position": position,
            "gross": gross,
            "cost": turnover_cost,
            "t": self._t,
        }
        return self._obs(), reward, done, info


def forward_returns(close: np.ndarray) -> np.ndarray:
    """Close-to-next-close simple returns; last element is 0 (no future bar)."""
    close = np.asarray(close, dtype="float64")
    fwd = np.zeros_like(close)
    fwd[:-1] = close[1:] / close[:-1] - 1.0
    return fwd
