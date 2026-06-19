"""Task-1065 — the end-to-end orchestrator for the DreamerV3 gold bot.

Every other module in this package is one *layer* of the pipeline. Until now
there was no single object that ties them together; task-1063 confirmed "no
end-to-end CLI entrypoint exists yet". This module is that glue:

    DecisionSeries (the full H1 decision grid the walk-forward slides over)
        -> generate_folds            (walkforward.py: fixed-size sliding folds)
        -> per fold: fit a TRAIN-ONLY scaler, train + score checkpoints
        -> select_champion_checkpoint / consistency_gate   (selection.py)
        -> on gate-pass ONLY: run the champion ONCE on held-out test data
           via run_backtest          (execution.py: H1-decision / M1-execution)
        -> build_report              (reports.py: returns / drawdown / Sharpe ...)

Design — injected compute, pure orchestration (the house pattern)
-----------------------------------------------------------------
The GPU-heavy training loop (``agent.Dreamer`` + replay + 1-5M steps) is
deliberately *not* hard-wired here, exactly as ``walkforward`` injects its
``run_fold``/``run_test`` and ``execution`` injects its policy. The orchestrator
takes two callables:

    * ``train_fn(FoldData) -> TrainedFold``  — train on the fold's TRAIN window,
      score saved checkpoints on train-tail + validation, and report that
      fold's validation profit. This is the only place torch is needed; it is
      injected so the whole orchestration is importable and unit-testable with
      a stub on a CPU, ahead of the training loop landing.
    * ``evaluate_fn(model, DecisionSeries, FeatureScaler) -> list[Trade]`` — run
      a trained model over a window to produce closed trades. The default,
      :func:`actor_evaluator`, builds 64-bar observation windows, maps each
      through the actor to a {hold,buy,sell} action, and drives
      ``execution.run_backtest`` with the spec's ATR-stop / RR-target machinery.

Leakage discipline preserved end-to-end
----------------------------------------
* The scaler is fit on each fold's TRAIN window only (``features.FeatureScaler``)
  and applied frozen to validation/test — never re-fit downstream.
* Test data is sliced but only *touched* (run through ``evaluate_fn``) when the
  cross-fold consistency gate passes — the gate logic lives in
  ``run_walk_forward`` and is reused verbatim, so test discipline cannot drift.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import numpy as np

if TYPE_CHECKING:
    import pandas as pd

from .config import DEFAULT, DreamerConfig
from .execution import HOLD, Trade, run_backtest
from .features import FeatureScaler
from .policy import actions_to_policy
from .reports import PerformanceReport, build_report
from .selection import Checkpoint, flag_outside_good_region
from .walkforward import (
    Fold,
    FoldOutcome,
    WalkForwardResult,
    generate_folds,
    run_walk_forward,
)

# --------------------------------------------------------------------------- #
# The decision-bar series the walk-forward slides over
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DecisionSeries:
    """The full H1 decision grid, as parallel arrays the pipeline consumes.

    Every field is aligned position-for-position to one H1 decision bar:

    ``features``
        ``(T, n_features)`` raw (un-scaled) feature matrix, indexed by the bar's
        H1 close timestamp. Kept as a DataFrame so the train-only
        :class:`~novatrade.research.dreamer.features.FeatureScaler` can fit/apply
        by column name.
    ``decision_close``
        ``(T,)`` H1 close price of each decision bar (entries/flips fill here).
    ``atr``
        ``(T,)`` ATR aligned to ``decision_close`` (the stop-sizing ATR at that
        bar), as ``execution.run_backtest`` expects.
    ``m1_paths``
        ``m1_paths[i]`` is the ordered list of intrabar
        :class:`~novatrade.research.dreamer.execution.Bar` M1 bars spanning the
        window *after* H1 bar ``i`` closes and *before* bar ``i+1`` closes.

    Building this from a raw M1 feed (resample + no-lookahead alignment +
    M1-path bucketing) is the IO layer's job (``data``/``features``); the
    orchestrator consumes a prepared series so it stays pure and testable.
    """

    features: pd.DataFrame
    decision_close: np.ndarray
    atr: np.ndarray
    m1_paths: Sequence[Any]

    def __post_init__(self) -> None:
        n = len(self.features)
        if not (len(self.decision_close) == len(self.atr) == len(self.m1_paths) == n):
            raise ValueError(
                "DecisionSeries fields must share length T "
                f"(features={n}, decision_close={len(self.decision_close)}, "
                f"atr={len(self.atr)}, m1_paths={len(self.m1_paths)})"
            )

    def __len__(self) -> int:
        return len(self.features)

    @property
    def timestamps(self) -> list[pd.Timestamp]:
        """Per-bar H1 close timestamps (from the feature-matrix index)."""
        return list(self.features.index)

    def slice(self, start: int, end: int) -> DecisionSeries:
        """Half-open ``[start, end)`` positional slice across all aligned fields."""
        return DecisionSeries(
            features=self.features.iloc[start:end],
            decision_close=np.asarray(self.decision_close)[start:end],
            atr=np.asarray(self.atr)[start:end],
            m1_paths=list(self.m1_paths)[start:end],
        )


# --------------------------------------------------------------------------- #
# Per-fold data handed to the (injected) training step
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FoldData:
    """One fold's train/val/test windows plus the TRAIN-only fitted scaler.

    The scaler is fit on ``train.features`` and frozen; ``train_fn`` and the
    evaluator apply it unchanged to every window so the train distribution can
    never leak into validation/test standardisation.
    """

    fold: Fold
    train: DecisionSeries
    val: DecisionSeries
    test: DecisionSeries
    scaler: FeatureScaler


@dataclass(frozen=True)
class TrainedFold:
    """What an injected ``train_fn`` must return for one fold.

    ``model`` is the opaque trained object (only the last fold's is promoted).
    ``checkpoints`` are already scored on their train-tail + validation windows
    (see ``selection.window_score``). ``validation_profit`` is this fold's net
    validation result — only its *sign* matters to the consistency gate.
    """

    model: Any
    checkpoints: list[Checkpoint]
    validation_profit: float


def make_fold_data(series: DecisionSeries, fold: Fold) -> FoldData:
    """Slice ``series`` into a fold's three windows and fit a train-only scaler."""
    train = series.slice(*fold.train_range())
    val = series.slice(*fold.val_range())
    test = series.slice(*fold.test_range())
    scaler = FeatureScaler().fit(train.features)
    return FoldData(fold=fold, train=train, val=val, test=test, scaler=scaler)


# --------------------------------------------------------------------------- #
# Injection contracts
# --------------------------------------------------------------------------- #
TrainFn = Callable[[FoldData], TrainedFold]
"""Train + score-checkpoints for one fold. Wrap the GPU training loop here."""

EvaluateFn = Callable[[Any, DecisionSeries, FeatureScaler], list]
"""Run a trained model over a window and return its closed ``Trade`` list."""


@dataclass(frozen=True)
class _ChampionBundle:
    """Carried as ``FoldOutcome.model`` so ``run_test`` can re-evaluate the fold.

    Bundles the trained model with the fold's frozen scaler and prepared test
    window, so the gate-gated single test read has everything it needs without
    re-deriving the scaler (which would risk re-fitting on the wrong window).
    """

    model: Any
    scaler: FeatureScaler
    test: DecisionSeries


# --------------------------------------------------------------------------- #
# Default evaluator: actor -> actions -> execution backtest
# --------------------------------------------------------------------------- #


def actor_evaluator(
    actor_fn: Callable[[Any, np.ndarray], int],
    *,
    window: int = DEFAULT.window,
    sl_atr_mult: float = DEFAULT.atr_stop_mult,
    tp_rr: float = DEFAULT.take_profit_rr,
    max_holding_bars: int | None = None,
) -> EvaluateFn:
    """Build an :data:`EvaluateFn` that drives ``execution.run_backtest``.

    For each decision bar ``i`` with at least ``window`` bars of history, the
    scaled ``(window, n_features)`` observation is mapped through ``actor_fn``
    (which realises the trained model on one observation) to a 0/1/2 action;
    bars without enough history stay ``HOLD``. The action stream is wrapped via
    :func:`policy.actions_to_policy` and run through the H1-decision /
    M1-execution engine with the spec's ATR-stop / RR-target levels.
    """
    if window <= 0:
        raise ValueError(f"window must be > 0 (got {window})")

    def evaluate(model: Any, win: DecisionSeries, scaler: FeatureScaler) -> list[Trade]:
        scaled = scaler.transform(win.features).to_numpy().astype("float32")
        n = len(win)
        actions = [HOLD] * n
        for i in range(window - 1, n):
            obs = scaled[i - window + 1 : i + 1]
            a = int(actor_fn(model, obs))
            if a not in (0, 1, 2):
                raise ValueError(f"actor_fn returned invalid action {a!r}; expected 0/1/2")
            actions[i] = a
        policy = actions_to_policy(actions, sl_atr_mult=sl_atr_mult, tp_rr=tp_rr)
        return run_backtest(
            cast("Sequence[float]", win.decision_close),
            cast("Sequence[float]", win.atr),
            win.m1_paths,
            policy,
            max_holding_bars=max_holding_bars,
        )

    return evaluate


# --------------------------------------------------------------------------- #
# Orchestration result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OrchestrationResult:
    """Everything one end-to-end walk-forward run produced.

    ``test_trades`` / ``test_report`` are populated only when the consistency
    gate passed (otherwise the held-out test set was never touched and both are
    ``None`` — the honest "edge not proven, no test read" outcome).
    """

    walk_forward: WalkForwardResult
    test_trades: list[Trade] | None
    test_report: PerformanceReport | None
    champion_outside_good_region: bool | None

    @property
    def has_champion(self) -> bool:
        return self.walk_forward.has_champion

    @property
    def consistency(self):
        return self.walk_forward.consistency


def run_orchestration(
    series: DecisionSeries,
    *,
    train_fn: TrainFn,
    evaluate_fn: EvaluateFn,
    train_size: int,
    val_size: int,
    test_size: int,
    step: int | None = None,
    config: DreamerConfig = DEFAULT,
    rolling_window: int = 20,
) -> OrchestrationResult:
    """Drive the whole pipeline: folds -> train -> gate -> single test read -> report.

    ``train_fn`` trains and scores checkpoints per fold (it must NOT touch the
    fold's test window). ``evaluate_fn`` (e.g. :func:`actor_evaluator`) turns the
    promoted champion into closed trades on its held-out test window — invoked
    exactly once, and only if the cross-fold consistency gate passes. The fold
    geometry is the fixed-size sliding walk-forward from
    :func:`walkforward.generate_folds`.
    """
    folds = generate_folds(
        n_samples=len(series),
        train_size=train_size,
        val_size=val_size,
        test_size=test_size,
        step=step,
    )

    def run_fold(fold: Fold) -> FoldOutcome:
        fold_data = make_fold_data(series, fold)
        trained = train_fn(fold_data)
        bundle = _ChampionBundle(
            model=trained.model,
            scaler=fold_data.scaler,
            test=fold_data.test,
        )
        return FoldOutcome(
            fold=fold,
            checkpoints=trained.checkpoints,
            validation_profit=trained.validation_profit,
            model=bundle,
        )

    def run_test(fold: Fold, outcome: FoldOutcome) -> dict[str, Any]:
        bundle: _ChampionBundle = outcome.model
        trades = evaluate_fn(bundle.model, bundle.test, bundle.scaler)
        report = build_report(trades, bundle.test.timestamps, rolling_window=rolling_window)
        return {"trades": trades, "report": report}

    wf = run_walk_forward(folds, run_fold=run_fold, run_test=run_test, config=config)

    test_trades = wf.test_result["trades"] if wf.test_result is not None else None
    test_report = wf.test_result["report"] if wf.test_result is not None else None
    outside = flag_outside_good_region(wf.champion_checkpoint, config) if wf.champion_checkpoint is not None else None

    return OrchestrationResult(
        walk_forward=wf,
        test_trades=test_trades,
        test_report=test_report,
        champion_outside_good_region=outside,
    )
