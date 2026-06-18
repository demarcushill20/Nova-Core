"""Single source of truth for the DreamerV3 gold-bot (task 1053).

Every hyperparameter named in the task spec (sections 11/12) lives here so the
data / feature / env / (future) training code never hard-codes a value twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# --------------------------------------------------------------------------- #
# Timeframes
# --------------------------------------------------------------------------- #
# M5 is the *base* / decision timeframe. The other five are higher-timeframe
# context features. Order is fixed and load-bearing: it defines column order in
# the 152-feature matrix.
BASE_TF = "M5"
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
# Train / validation / test split  (task section 12 — STRICT OOS)
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
    allow_short: bool = False  # long-only: actions are {flat, long}

    # --- agent ---
    horizon: int = 15  # imagination rollout length (~75 min on M5)

    # --- training loop ---
    training_steps: int = 1_000_000
    prefill_steps: int = 5_000  # random steps to seed the replay buffer
    batch_size: int = 16
    train_every: int = 4
    save_every: int = 10_000

    # --- bookkeeping ---
    timeframes: list[str] = field(default_factory=lambda: list(TIMEFRAMES))
    n_features: int = N_FEATURES

    def __post_init__(self) -> None:  # cheap invariants
        assert self.window > 0
        assert 0.0 <= self.cost < 0.01
        assert self.horizon > 0
        assert self.n_features == N_FEATURES


DEFAULT = DreamerConfig()
