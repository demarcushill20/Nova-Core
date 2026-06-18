#!/usr/bin/env python3
"""Phase F — strict-OOS evaluation of the DreamerV3 gold bot (task 1053).

Loads a frozen checkpoint + train-only scaler, then runs the greedy policy over
validation (2022-2023) and test (2024) — recurrence carried forward bar-by-bar
(no reset), position applied t+1, cost charged on every change.

The headline question is NOT "did it make money" — long-only gold over a bull
window will. It is: **does the learned policy beat buy-and-hold gold and an
always-long bot, net of cost, out-of-sample?** Both benchmarks are reported
alongside the policy. Test window is 2024 ONLY (2025 absent from the feed).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from novatrade.research.dreamer import config, data, features
from novatrade.research.dreamer.agent import Dreamer

BARS_PER_YEAR = 288 * 252  # M5 bars in a trading year (approx, for annualising)


def equity_metrics(bar_returns: np.ndarray, positions: np.ndarray, name: str) -> dict:
    """Sharpe/Sortino/maxDD/CAGR/exposure from a per-bar net-return stream."""
    r = np.asarray(bar_returns, dtype="float64")
    eq = np.cumprod(1.0 + r)
    total = float(eq[-1] - 1.0)
    years = len(r) / BARS_PER_YEAR
    cagr = float(eq[-1] ** (1.0 / years) - 1.0) if years > 0 and eq[-1] > 0 else float("nan")
    mu, sd = r.mean(), r.std()
    downside = r[r < 0].std()
    sharpe = float(mu / sd * np.sqrt(BARS_PER_YEAR)) if sd > 0 else 0.0
    sortino = float(mu / downside * np.sqrt(BARS_PER_YEAR)) if downside > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min())
    pos = np.asarray(positions, dtype="float64")
    return {
        "name": name,
        "total_return": round(total, 4),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown": round(max_dd, 4),
        "exposure": round(float((pos != 0).mean()), 4),
        "n_bars": len(r),
        "position_changes": int(np.sum(np.abs(np.diff(pos, prepend=pos[0])) > 0)),
        "bar_win_rate": round(float((r > 0).mean()), 4),
    }


@torch.no_grad()
def run_policy(agent: Dreamer, feat: np.ndarray, fwd: np.ndarray, cost: float):
    """Greedy policy with recurrence carried forward; returns (net_r, positions)."""
    device = agent.device
    h, z = agent.world.initial(1, device)
    position = 0.0
    net_r, positions = [], []
    for t in range(len(feat) - 1):
        obs = np.empty(feat.shape[1] + 1, dtype=np.float32)
        obs[:-1] = feat[t]
        obs[-1] = position
        ot = torch.as_tensor(obs, device=device).view(1, -1)
        a_oh = torch.zeros(1, 2, device=device)
        a_oh[0, int(position)] = 1.0  # action that led to current position
        h, z, _, _ = agent.world.obs_step(h, z, a_oh, agent.world.encoder(ot))
        action = agent.act(h, z, greedy=True)
        new_pos = float(action)
        r = new_pos * float(fwd[t]) - cost * abs(new_pos - position)
        net_r.append(r)
        positions.append(new_pos)
        position = new_pos
    return np.array(net_r), np.array(positions)


def slice_period(matrix, lo, hi):
    return matrix.loc[(matrix.index >= lo) & (matrix.index < hi)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run dir with checkpoint.pt + scaler.json")
    ap.add_argument("--m1", default=str(data.DEFAULT_M1_PATH))
    args = ap.parse_args()
    run = Path(args.run)
    cfg = config.DEFAULT

    ckpt = torch.load(run / "checkpoint.pt", map_location="cpu", weights_only=False)
    scaler = features.FeatureScaler.from_dict(json.loads((run / "scaler.json").read_text()))
    # NOTE (task-1056): 2-action long/flat foundation read; corrected spec is
    # buy/sell/hold (n_actions=3), rewired in task-1060.
    agent = Dreamer(obs_dim=ckpt["obs_dim"], n_actions=2, horizon=cfg.horizon, device="cpu")
    agent.load_state_dict(ckpt["model"])
    agent.eval()

    m1 = data.load_m1(args.m1)
    matrix = features.build_features(m1)
    m5_close = data.resample_ohlc(m1, config.BASE_TF)["close"]

    periods = {
        "validation_2022_2023": config.VALIDATION_RANGE,
        "test_2024": config.TEST_RANGE,
    }
    report: dict = {"cost_per_change": cfg.cost, "note": config.TEST_DATA_AVAILABLE_NOTE, "periods": {}}

    for label, (lo, hi) in periods.items():
        pm = slice_period(matrix, lo, hi)
        if len(pm) < 100:
            report["periods"][label] = {"skipped": "insufficient rows"}
            continue
        feat = scaler.transform(pm).to_numpy(dtype="float32")
        close = m5_close.reindex(pm.index).to_numpy(dtype="float64")
        fwd = np.zeros_like(close)
        fwd[:-1] = close[1:] / close[:-1] - 1.0
        fwd = fwd.astype("float32")

        net_r, pos = run_policy(agent, feat, fwd, cfg.cost)
        # Benchmarks on the same bars.
        bh_r = fwd[:-1]  # buy-and-hold (always long, no churn cost beyond entry)
        always_pos = np.ones_like(pos)
        report["periods"][label] = {
            "policy": equity_metrics(net_r, pos, "dreamer_policy"),
            "buy_and_hold": equity_metrics(bh_r, always_pos, "buy_and_hold"),
        }
        p = report["periods"][label]["policy"]
        b = report["periods"][label]["buy_and_hold"]
        print(f"\n=== {label} ===")
        print(
            f"  policy : ret {p['total_return']:+.2%}  Sharpe {p['sharpe']:+.2f}  "
            f"maxDD {p['max_drawdown']:.2%}  exposure {p['exposure']:.0%}  "
            f"changes {p['position_changes']}"
        )
        print(f"  B&H    : ret {b['total_return']:+.2%}  Sharpe {b['sharpe']:+.2f}  maxDD {b['max_drawdown']:.2%}")
        verdict = "BEATS" if p["sharpe"] > b["sharpe"] else "LOSES TO"
        print(f"  -> policy {verdict} buy-and-hold on Sharpe")

    out = run / "eval_report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n[done] wrote {out}")


if __name__ == "__main__":
    main()
