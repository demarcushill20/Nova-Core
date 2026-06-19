"""Torch-backed training/eval adapters that plug the DreamerV3 agent into the
walk-forward orchestrator (the injection points tasks 1057/1060/1065 left open).

The orchestrator (``orchestrator.run_orchestration``) needs two callables:

    * ``train_fn(FoldData) -> TrainedFold`` — train + score checkpoints on one
      fold's TRAIN window (never touching test).
    * ``evaluate_fn(model, DecisionSeries, FeatureScaler) -> list[Trade]`` — run
      the trained actor over a window through the H1/M1 execution engine.

This module supplies both, wrapping ``agent.Dreamer`` (the categorical RSSM +
actor-critic from task 1053). It is the ONLY place torch is imported, so the
rest of the package stays torch-free and unit-testable on a bare ``.venv``.

Observation contract (matches ``orchestrator.actor_evaluator``)
---------------------------------------------------------------
The evaluator hands the actor a ``(window, n_features)`` scaled feature matrix —
no explicit position channel. DreamerV3 is recurrent, so position/history is
carried by the RSSM's deterministic state (conditioned on the action it took
each step), not stapled onto the observation. ``obs_dim == n_features`` and
``n_actions == 3`` ({hold/flat, buy/long, sell/short}).

Leakage discipline: the scaler arrives already fit on TRAIN only (the
orchestrator's ``make_fold_data`` does this); we apply it frozen everywhere.
Checkpoints are scored on a train-tail + the validation window — the held-out
test window is never seen here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch

from .agent import Dreamer, ReplayBuffer
from .config import DEFAULT, DreamerConfig
from .env import TradingEnv, forward_returns
from .orchestrator import DecisionSeries, FoldData, TrainedFold, actor_evaluator
from .selection import Checkpoint, window_score


# --------------------------------------------------------------------------- #
# Actor bridge: a trained Dreamer + a (window, n_features) obs -> 0/1/2 action
# --------------------------------------------------------------------------- #
@torch.no_grad()
def greedy_actor_fn(model: Dreamer, obs: np.ndarray) -> int:
    """Run the RSSM over a (window, n_features) obs from a zero state -> action.

    Stateless per call (recomputes the recurrent state from the window's start),
    which is what the per-bar evaluator contract wants. Past actions inside the
    window are unknown to the evaluator, so they are fed as zeros — the world
    model still integrates the feature sequence to a final latent.
    """
    ot = torch.as_tensor(np.asarray(obs, dtype="float32"), device=model.device).unsqueeze(0)
    a_seq = torch.zeros(1, ot.shape[1], model.n_actions, device=model.device)
    roll = model.world.observe(ot, a_seq)
    return model.act(roll["h"][:, -1], roll["z"][:, -1], greedy=True)


def _onehot(action: int, n: int) -> np.ndarray:
    v = np.zeros(n, dtype="float32")
    v[action] = 1.0
    return v


# --------------------------------------------------------------------------- #
# Window scoring (drawdown-penalized, via the execution engine)
# --------------------------------------------------------------------------- #
def _score_window(model: Dreamer, win: DecisionSeries, scaler, evaluate_fn, weight: float) -> float:
    """Cumulative-R minus drawdown penalty for a model over one window.

    Runs the actor through the H1/M1 execution engine to get closed trades,
    builds an R-multiple equity curve, and returns ``selection.window_score``.
    """
    if len(win) <= DEFAULT.window:
        return 0.0
    trades = evaluate_fn(model, win, scaler)
    if not trades:
        return 0.0
    r = np.array([t.r_multiple() for t in trades], dtype="float64")
    equity = np.cumsum(r)
    peak = np.maximum.accumulate(equity)
    max_dd = float(np.max(peak - equity)) if len(equity) else 0.0
    return window_score(float(equity[-1]), max_dd, weight)


def _cumulative_r(model: Dreamer, win: DecisionSeries, scaler, evaluate_fn) -> float:
    """Net R over a window (sign feeds the cross-fold consistency gate)."""
    if len(win) <= DEFAULT.window:
        return 0.0
    trades = evaluate_fn(model, win, scaler)
    return float(sum(t.r_multiple() for t in trades))


# --------------------------------------------------------------------------- #
# train_fn factory
# --------------------------------------------------------------------------- #
@dataclass
class TrainFnConfig:
    steps_per_fold: int = 3000
    seq_len: int = 32
    n_checkpoints: int = 3
    train_tail_bars: int = 2000  # how many train-window bars form the "train tail"
    device: str = "cpu"
    seed: int = 0


def make_train_fn(cfg: DreamerConfig = DEFAULT, tcfg: TrainFnConfig | None = None, log=print):
    """Build a torch ``train_fn`` for the orchestrator.

    For each fold it: collects+trains a Dreamer on the TRAIN window (persistent
    recurrence during collection, t+1 reward via ``env.TradingEnv``), snapshots
    ``n_checkpoints`` weight states, scores each on the train-tail + validation
    windows through the execution engine, and promotes the best worst-case
    checkpoint as the fold's model. ``validation_profit`` is that champion's net
    validation R.
    """
    if tcfg is None:
        tcfg = TrainFnConfig()
    evaluate_fn = actor_evaluator(greedy_actor_fn, window=cfg.window)

    def train_fn(fold_data: FoldData) -> TrainedFold:
        torch.manual_seed(tcfg.seed)
        np.random.seed(tcfg.seed)

        feats = fold_data.scaler.transform(fold_data.train.features).to_numpy(dtype="float32")
        fwd = forward_returns(np.asarray(fold_data.train.decision_close, dtype="float64")).astype("float32")
        n = len(feats)
        obs_dim = feats.shape[1]

        env = TradingEnv(
            feats,
            fwd,
            window=cfg.window,
            cost=cfg.cost,
            allow_short=True,
            holding_penalty=cfg.holding_penalty,
        )
        agent = Dreamer(obs_dim=obs_dim, n_actions=3, horizon=cfg.horizon, device=tcfg.device)
        buf = ReplayBuffer(capacity=min(n, 100_000), obs_dim=obs_dim, n_actions=3)

        prefill = min(cfg.prefill_steps, tcfg.steps_per_fold // 4)
        ckpt_every = max(1, tcfg.steps_per_fold // tcfg.n_checkpoints)
        snapshots: list[tuple[int, dict]] = []

        env.reset()
        h, z = agent.world.initial(1, agent.device)
        prev_a = torch.zeros(1, 3, device=agent.device)
        for step in range(tcfg.steps_per_fold):
            t = env._t
            obs_vec = feats[t]
            with torch.no_grad():
                ot = torch.as_tensor(obs_vec, device=agent.device).view(1, -1)
                h, z, _, _ = agent.world.obs_step(h, z, prev_a, agent.world.encoder(ot))
                action = np.random.randint(0, 3) if step < prefill else agent.act(h, z, greedy=(step % 5 != 0))
            _, reward, done, _ = env.step(action)
            buf.add(obs_vec, _onehot(action, 3), reward, 0.0 if done else 1.0)
            prev_a = torch.as_tensor(_onehot(action, 3), device=agent.device).view(1, 3)

            if done:
                env.reset()
                h, z = agent.world.initial(1, agent.device)
                prev_a = torch.zeros(1, 3, device=agent.device)

            if step >= prefill and buf.size > tcfg.seq_len + 1 and step % cfg.train_every == 0:
                agent.train_step(buf.sample(cfg.batch_size, tcfg.seq_len, agent.device))

            if (step + 1) % ckpt_every == 0 or step == tcfg.steps_per_fold - 1:
                snapshots.append((step + 1, {k: v.detach().clone() for k, v in agent.state_dict().items()}))

        # Score every snapshot on train-tail + validation; promote best worst-case.
        tail = fold_data.train.slice(max(0, n - tcfg.train_tail_bars), n)
        val = fold_data.val
        checkpoints: list[Checkpoint] = []
        scored: list[tuple[Checkpoint, dict, float]] = []
        for stepno, state in snapshots:
            agent.load_state_dict(state)
            agent.eval()
            tail_s = _score_window(agent, tail, fold_data.scaler, evaluate_fn, cfg.drawdown_penalty_weight)
            val_s = _score_window(agent, val, fold_data.scaler, evaluate_fn, cfg.drawdown_penalty_weight)
            val_r = _cumulative_r(agent, val, fold_data.scaler, evaluate_fn)
            ck = Checkpoint(step=stepno, train_tail_score=tail_s, validation_score=val_s)
            checkpoints.append(ck)
            scored.append((ck, state, val_r))
            log(f"    ckpt@{stepno}: tail_score={tail_s:+.3f} val_score={val_s:+.3f} val_R={val_r:+.3f}")

        # best worst-case (min of the two window scores); tie -> earlier step.
        best = max(scored, key=lambda s: (s[0].checkpoint_score, -s[0].step))
        agent.load_state_dict(best[1])
        agent.eval()
        return TrainedFold(model=agent, checkpoints=checkpoints, validation_profit=best[2])

    return train_fn, evaluate_fn
