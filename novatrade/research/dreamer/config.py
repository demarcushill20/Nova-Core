"""Single source of truth for the DreamerV3 gold-bot (task 1053).

Every hyperparameter named in the task spec (sections 11/12) lives here so the
data / feature / env / (future) training code never hard-codes a value twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# --------------------------------------------------------------------------- #
# Timeframes (CORRECTED per task-1056 transcript review)
# --------------------------------------------------------------------------- #
# The video's actual system makes trading DECISIONS on H1 candles and resolves
# stop-loss / take-profit order sequencing on M1 candles. Raw data is M1; it is
# resampled up to H1 for the decision grid.
#
#   * DECISION_TF  — the cadence the agent acts on (one decision per H1 close).
#   * EXECUTION_TF — the resolution used to walk price through SL/TP/flip inside
#                    each H1 bar (backtest execution engine, task 1057).
#
# MIGRATION NOTE: the Phase A–F foundation (9069e9d / 548fa7a) was built on an
# M5 base grid. ``BASE_TF`` therefore still reads "M5" so the green leakage /
# feature-shape tests stay valid while the H1-decision / M1-execution rewire
# lands in the follow-up step (task-1057, "At the close of each H1 candle").
# Downstream code must read DECISION_TF / EXECUTION_TF, not assume M5.
DECISION_TF = "H1"  # corrected: agent decides once per closed H1 candle
EXECUTION_TF = "M1"  # corrected: SL/TP/flip resolved on M1 sequencing
BASE_TF = "M5"  # legacy base grid (foundation only; superseded by DECISION_TF)
TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1", "W1"]

# pandas resample rule per timeframe.
RESAMPLE_RULE = {
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1D",
    "W1": "1W",
}

# Bar duration -> used to compute each bar's *close* time for no-lookahead
# alignment. A higher-TF bar is only knowable once it has closed, so we shift
# its timestamp forward by one full bar width before merging onto the M5 grid.
TF_DURATION = {
    "M5": pd.Timedelta(minutes=5),
    "M15": pd.Timedelta(minutes=15),
    "H1": pd.Timedelta(hours=1),
    "H4": pd.Timedelta(hours=4),
    "D1": pd.Timedelta(days=1),
    "W1": pd.Timedelta(days=7),
}

# --------------------------------------------------------------------------- #
# Feature matrix shape (hard-asserted in features.py)
# --------------------------------------------------------------------------- #
PER_TF_FEATURES = 24  # indicators computed on each timeframe's own OHLC
N_GLOBAL_FEATURES = 8  # time-of-day / session features on the M5 grid
N_FEATURES = PER_TF_FEATURES * len(TIMEFRAMES) + N_GLOBAL_FEATURES  # = 152

# --------------------------------------------------------------------------- #
# Data span (CORRECTED per task-1056 transcript review)
# --------------------------------------------------------------------------- #
# The transcript trains/tests on ~20 years of XAUUSD, going back to ~2003. The
# on-disk HistData feed is shorter (ends 2024-12-31); the desired span is
# recorded here so the data-loader can flag when the feed underfills it.
DATA_START_DATE_DESIRED = pd.Timestamp("2003-01-01")  # transcript: ~20yr of gold

# --------------------------------------------------------------------------- #
# Validation method (CORRECTED per task-1056 transcript review)
# --------------------------------------------------------------------------- #
# The video does NOT use one simple train/test split. It uses a fixed-size
# sliding WALK-FORWARD (multiple folds), and selects the champion checkpoint by
# its WORST-CASE train/validation score, then applies a consistency gate across
# folds. The single split below is retained as the foundation's strict-OOS read;
# the walk-forward + champion-selection machinery lands in tasks 1059 / 1061.
WALK_FORWARD = True  # corrected: sliding walk-forward, not one split
CHAMPION_BY_WORST_CASE_FOLD = True  # corrected: pick best worst-case fold score

# --------------------------------------------------------------------------- #
# Train / validation / test split  (foundation strict-OOS read)
# --------------------------------------------------------------------------- #
# Spec asks for test = 2024-2025, but the HistData XAUUSD feed ends 2024-12-31,
# so the usable test window is calendar-year 2024 only. Any results banner MUST
# state this; claiming "2024-2025 OOS" with this feed would be false.
TRAIN_END_DATE = pd.Timestamp("2022-01-01")  # train: strictly before this
VALIDATION_RANGE = (pd.Timestamp("2022-01-01"), pd.Timestamp("2024-01-01"))
TEST_RANGE = (pd.Timestamp("2024-01-01"), pd.Timestamp("2025-01-01"))
FEED_END_DATE = pd.Timestamp("2024-12-31")  # last bar available on disk
TEST_DATA_AVAILABLE_NOTE = "Test window = 2024 only; 2025 gold data is not in the HistData feed."


# --------------------------------------------------------------------------- #
# Training hyperparameters (task section 11)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DreamerConfig:
    # --- data / env ---
    window: int = 64  # bars of feature history per observation
    cost: float = 0.0001  # per-unit transaction cost on position change
    # --- reward shaping: Term 3, holding penalty (task-1058) ---------------- #
    # A tiny negative constant subtracted from the reward on every H1 bar a
    # position stays open. Purpose: discourage the agent from sitting in a
    # trade forever; reward trades that resolve quickly.
    #
    #     holding_penalty_applied = holding_penalty if position != 0 else 0.0
    #     reward = realized + unrealized - holding_penalty_applied   (per spec)
    #
    # AMBIGUITY (task-1058): the transcript is unclear on the exact decimal —
    # it reads as "-0.00002 or -0.0002" and names ``environment.py`` lines
    # 297-300 as the source of truth. That canonical file is NOT in this tree
    # (our foundation env is ``env.py``), so the value cannot be lifted
    # verbatim. We take the SMALLER reading, 2e-5, because the env's reward is
    # in raw fwd-return units where a typical gold H1 bar moves ~1e-3: at 2e-5
    # the penalty is ~1-4% of a typical bar (a genuine "tiny" nudge), whereas
    # 2e-4 would be 10-40% of a bar and would dominate rather than nudge.
    # Tunable; set to 0.0 to disable the term entirely.
    holding_penalty: float = 2e-5
    # CORRECTED (task-1056): the agent is NOT long-only. It can buy, sell, or
    # hold, so shorts are enabled and the action space is {hold, buy, sell}
    # (env: {flat=0, long=1, short=2}).
    allow_short: bool = True  # buy/sell/hold: actions are {flat, long, short}

    # --- exits (CORRECTED task-1056) ---
    # Trades exit on an ATR-based stop loss, a risk-multiple take profit, a flip
    # close, or a manual/time-based close. The execution engine (task-1057)
    # walks M1 price through these levels inside each H1 decision bar.
    atr_period: int = 14  # ATR lookback for stop distance
    atr_stop_mult: float = 1.5  # stop = entry -/+ atr_stop_mult * ATR
    take_profit_rr: float = 2.0  # take profit at take_profit_rr * risk

    # --- position sizing (task-1060, spec "Position risk") ------------------- #
    # Each entry risks a FIXED percent of current equity between entry and the
    # ATR stop. Units = (equity * risk_pct) / (stop_distance * value_per_point).
    # The transcript uses ~0.5%; kept small so a string of stops can't ruin the
    # account. See ``policy.position_size``.
    risk_pct: float = 0.005  # fraction of equity risked per trade (0.5%)

    # --- agent ---
    horizon: int = 15  # imagination rollout length (H1 decision grid)

    # --- training loop ---
    training_steps: int = 1_000_000
    prefill_steps: int = 5_000  # random steps to seed the replay buffer
    batch_size: int = 16
    train_every: int = 4
    save_every: int = 10_000

    # --- overfitting / training horizon (task-1059, spec section 14) --------- #
    # The transcript observed training reward rising while validation reward
    # fell after a point (classic overfit). The useful learning region was
    # roughly before ~2M steps; a final overfit model was trained to ~5M and
    # looked great on train but weaker on validation/test. We therefore train UP
    # TO max_training_steps but let the checkpoint selector — NOT a hard-coded
    # "2M is best" — pick the champion using validation. ``expected_good_region``
    # is a documented prior for sanity-checking the pick, never a constraint.
    max_training_steps: int = 5_000_000
    checkpoint_interval: int = 10_000  # steps between saved checkpoints
    early_selection_uses_validation: bool = True
    expected_good_region: tuple[int, int] = (1_000_000, 2_000_000)

    # --- drawdown-penalized scoring (task-1059, spec section 13) ------------- #
    # score = cumulative_reward - drawdown_penalty, where
    #   drawdown_penalty = drawdown_penalty_weight * max_drawdown.
    # This punishes a model that makes money with dangerous drawdown.
    drawdown_penalty_weight: float = 1.0

    # --- cross-fold consistency gate (task-1059, spec section 15) ------------ #
    # After all walk-forward folds finish: at least this fraction of folds must
    # be independently profitable, else reject the edge as luck. 0.60 = lenient,
    # 0.70 = stricter; configurable per the spec.
    min_profitable_fold_ratio: float = 0.60

    # --- bookkeeping ---
    timeframes: list[str] = field(default_factory=lambda: list(TIMEFRAMES))
    n_features: int = N_FEATURES

    def __post_init__(self) -> None:  # cheap invariants
        assert self.window > 0
        assert 0.0 <= self.cost < 0.01
        # Holding penalty must be a small, non-negative "tiny nudge" (the env
        # subtracts it). Cap at the cost scale so it can never dominate reward.
        assert 0.0 <= self.holding_penalty <= 0.01
        assert self.horizon > 0
        assert self.n_features == N_FEATURES
        assert self.atr_period > 0
        assert self.atr_stop_mult > 0.0
        assert self.take_profit_rr > 0.0
        assert self.max_training_steps >= self.training_steps
        assert self.checkpoint_interval > 0
        assert self.drawdown_penalty_weight >= 0.0
        assert 0.0 < self.min_profitable_fold_ratio <= 1.0
        lo, hi = self.expected_good_region
        assert 0 < lo <= hi <= self.max_training_steps


DEFAULT = DreamerConfig()
