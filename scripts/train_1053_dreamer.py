#!/usr/bin/env python3
"""Phase E — train the DreamerV3 gold bot on XAUUSD M5 (task 1053).

Strict-OOS contract: the world model + actor-critic see ONLY data strictly
before ``TRAIN_END_DATE`` (2022-01-01). The feature scaler is fit on this same
train slice and frozen (saved alongside the checkpoint) for the evaluator.

DreamerV3 uses recurrence for temporal memory, so each environment observation
is the per-bar 152-feature vector PLUS the agent's current position (=153 dims);
the 64-bar "window" of the spec is realised as the replay sequence length, not a
flattened input. Reward = position * fwd_return - cost * |Δposition|, applied on
the next bar (t+1 fill), matching env.py.

Usage (reduced smoke):
    python3 scripts/train_1053_dreamer.py --steps 2000 --seq-len 32 --out runs/dreamer_smoke
Full faithful run (CPU = multi-day; prefer background):
    python3 scripts/train_1053_dreamer.py --steps 1000000 --out runs/dreamer_full
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch

from novatrade.research.dreamer import config, data, features
from novatrade.research.dreamer.agent import Dreamer, ReplayBuffer


def build_train_arrays(m1_path: str):
    """Return (feat_train_z, fwd_returns_train, scaler) for rows < TRAIN_END_DATE."""
    m1 = data.load_m1(m1_path)
    matrix = features.build_features(m1)  # causal; no lookahead
    train_mask = matrix.index < config.TRAIN_END_DATE
    train_matrix = matrix.loc[train_mask]

    scaler = features.FeatureScaler().fit(train_matrix)
    feat_z = scaler.transform(train_matrix).to_numpy(dtype="float32")

    # M5 close aligned to the feature index -> forward returns.
    m5 = data.resample_ohlc(m1, config.BASE_TF)
    close = m5["close"].reindex(train_matrix.index).to_numpy(dtype="float64")
    fwd = np.zeros_like(close)
    fwd[:-1] = close[1:] / close[:-1] - 1.0
    return feat_z, fwd.astype("float32"), scaler, train_matrix.index


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=str(data.DEFAULT_M1_PATH))
    ap.add_argument("--steps", type=int, default=2000, help="env steps to collect/train over")
    ap.add_argument("--prefill", type=int, default=None, help="random steps (default cfg)")
    ap.add_argument("--seq-len", type=int, default=32, help="replay sequence length")
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--train-every", type=int, default=None)
    ap.add_argument("--out", default="runs/dreamer_smoke")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    cfg = config.DEFAULT
    prefill = args.prefill if args.prefill is not None else min(cfg.prefill_steps, args.steps // 4)
    batch = args.batch or cfg.batch_size
    train_every = args.train_every or cfg.train_every
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[load] building train features from {args.m1} ...", flush=True)
    feat, fwd, scaler, idx = build_train_arrays(args.m1)
    n = len(feat)
    obs_dim = feat.shape[1] + 1  # + current position
    print(f"[load] train rows={n} ({idx[0]} -> {idx[-1]}), obs_dim={obs_dim}", flush=True)

    agent = Dreamer(obs_dim=obs_dim, n_actions=2, horizon=cfg.horizon, device="cpu")
    buf = ReplayBuffer(capacity=min(n, 200_000), obs_dim=obs_dim, n_actions=2)

    def make_obs(t: int, position: float) -> np.ndarray:
        o = np.empty(obs_dim, dtype=np.float32)
        o[:-1] = feat[t]
        o[-1] = position
        return o

    # Online collect + train loop over the historical sequence.
    #
    # Recurrence is *persistent* across collection: the acting policy carries the
    # same (h, z) the training rollout will reconstruct, advancing one obs_step
    # per bar. (An earlier version reset the RSSM every step and re-ran observe()
    # from a zero state — that gave the policy only a 1-bar context, defeating the
    # recurrent world model, and was the dominant collection cost.)
    position = 0.0
    t = 0
    losses = []
    h, z = agent.world.initial(1, agent.device)
    prev_a = torch.zeros(1, 2, device=agent.device)  # action that set current position
    t0 = time.time()
    for step in range(args.steps):
        obs = make_obs(t, position)
        with torch.no_grad():
            ot = torch.as_tensor(obs, device=agent.device).view(1, -1)
            h, z, _, _ = agent.world.obs_step(h, z, prev_a, agent.world.encoder(ot))
            if step < prefill:
                action = np.random.randint(0, 2)
            else:
                # epsilon-greedy: explore 1-in-5 to keep the buffer from collapsing.
                action = agent.act(h, z, greedy=(step % 5 != 0))
        new_pos = float(action)  # 0 flat, 1 long
        reward = new_pos * float(fwd[t]) - cfg.cost * abs(new_pos - position)
        a_oh = np.zeros(2, dtype=np.float32)
        a_oh[action] = 1.0
        buf.add(obs, a_oh, reward, 1.0)

        prev_a = torch.as_tensor(a_oh, device=agent.device).view(1, 2)
        position = new_pos
        t += 1
        if t >= n - 1:  # wrap historical sequence; reset recurrence at the seam
            t = 0
            h, z = agent.world.initial(1, agent.device)
            prev_a = torch.zeros(1, 2, device=agent.device)
            position = 0.0

        if step >= prefill and buf.size > args.seq_len + 1 and step % train_every == 0:
            res = agent.train_step(buf.sample(batch, args.seq_len, agent.device))
            losses.append((step, res.world, res.actor, res.critic, res.recon, res.reward, res.kl))
            if len(losses) % 50 == 0:
                rate = step / (time.time() - t0 + 1e-9)
                print(
                    f"[{step}] world={res.world:.3f} actor={res.actor:.3f} "
                    f"critic={res.critic:.3f} recon={res.recon:.3f} kl={res.kl:.3f} "
                    f"({rate:.1f} steps/s)",
                    flush=True,
                )

    ckpt = out / "checkpoint.pt"
    torch.save({"model": agent.state_dict(), "obs_dim": obs_dim}, ckpt)
    (out / "scaler.json").write_text(json.dumps(scaler.to_dict()))
    (out / "losses.json").write_text(json.dumps(losses))
    (out / "meta.json").write_text(
        json.dumps(
            {
                "steps": args.steps,
                "prefill": prefill,
                "seq_len": args.seq_len,
                "batch": batch,
                "train_every": train_every,
                "n_train_rows": int(n),
                "train_start": str(idx[0]),
                "train_end": str(idx[-1]),
                "wall_seconds": round(time.time() - t0, 1),
            },
            indent=2,
        )
    )
    print(f"[done] saved {ckpt} ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
