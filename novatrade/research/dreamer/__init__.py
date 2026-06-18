"""DreamerV3 gold-bot research package (task 1053).

Model-based RL trading agent for XAUUSD M5. This package contains the
**leakage-gated foundation** (Phases A-C of the analyze-step plan):

    data.py     - M1 -> {M5,M15,H1,H4,D1,W1} resample + no-lookahead alignment
    features.py - 152-feature multi-timeframe matrix + train-only scaler
    env.py      - long/flat trading environment (cost=1e-4, t+1 fill)
    execution.py- H1-decision / M1-execution engine: ATR stop, RR take-profit,
                  flip + manual exits (task-1057)
    config.py   - single source of truth for all hyperparameters / split dates

The DreamerV3 world model (RSSM), actor-critic, imagination rollout, the
1M-step training loop, and the strict-OOS evaluation (Phases D-F) are
deliberately *not* in this commit: they are GPU-hours of compute and must
not run until these leakage tests pass review. See
``OUTPUT/1053_world_model_learning_analysis_20260618T180423.md``.
"""

from . import config

__all__ = ["config", "data", "env", "execution", "features"]
