"""The search driver — generate -> score -> refine-around-survivors -> confirm.

Round 1 lays down a broad seed grid. Each later round REFINES around the current
grounded train-leaders (neighbour hours / stops) — the self-directed part of the
loop. Every candidate scored increments the trial count, so the Deflated Sharpe
bar the survivors must clear gets STRICTER as the loop searches harder. Only
after selection is frozen do survivors touch the sealed hold-out, once.

This programmatic generator is the v1 proposer. An LLM proposer (the full
"Karpathy" flavour) plugs in at `propose_refinements` — it can read the
leaderboard and emit new Candidates — without changing the scoring contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from novatrade.research.autoresearch.data import Split, make_dataset, sealed_split
from novatrade.research.autoresearch.families import Candidate
from novatrade.research.autoresearch.gauntlet import (
    Thresholds,
    Verdict,
    holdout_sharpe,
    score_candidate,
    train_metrics,
)

STOPS = (15.0, 20.0, 30.0)  # FX pips
STOPS_BPS = (150.0, 250.0, 400.0)  # crypto: bps of price (wider, vol-appropriate)


# ----------------------------- generation -----------------------------
def seed_grid(stops: tuple[float, ...] = STOPS, session_stops: tuple[float, ...] = (30.0, 50.0)) -> list[Candidate]:
    cands: list[Candidate] = []
    # hour_drift: every UTC hour, both directions, a few stops (grounded: flow seasonality)
    for hour in range(24):
        for direction in (-1, 1):
            for stop in stops:
                cands.append(
                    Candidate.make(
                        "hour_drift",
                        {"hour": hour, "direction": direction, "hold": 1, "stop_pips": stop},
                        mechanism=f"own-hours flow drift @ {hour:02d}:00 UTC",
                        grounded=True,
                    )
                )
    # session_drift: canonical session holds (grounded)
    for h0, h1, d in ((6, 13, -1), (13, 20, 1), (7, 12, -1), (0, 7, 1), (8, 16, -1)):
        for stop in session_stops:
            cands.append(
                Candidate.make(
                    "session_drift",
                    {"start_hour": h0, "end_hour": h1, "direction": d, "stop_pips": stop},
                    mechanism=f"session drift {h0:02d}-{h1:02d} UTC",
                    grounded=True,
                )
            )
    # mr_fade: Bollinger fade (UNGROUNDED control — expected to fail)
    for k in (2.0, 2.5):
        for atr_stop in (1.0, 1.5):
            cands.append(
                Candidate.make(
                    "mr_fade",
                    {"bb_n": 20, "bb_k": k, "atr_stop": atr_stop, "max_hold": 12},
                    mechanism="indicator mean-reversion (no structural basis)",
                    grounded=False,
                )
            )
    # breakout: opening-range (UNGROUNDED control — expected to fail)
    for so in (7, 13):
        for rr in (1.5, 2.0):
            cands.append(
                Candidate.make(
                    "breakout",
                    {"session_open": so, "or_bars": 2, "rr": rr, "min_w": 8, "max_w": 40, "sess_hours": 10},
                    mechanism="opening-range breakout (no structural basis)",
                    grounded=False,
                )
            )
    return cands


def propose_refinements(leaders: list[Candidate], stops: tuple[float, ...] = STOPS) -> list[Candidate]:
    """Neighbours of the current grounded leaders: ±1 hour and the other stops.
    (This is the hook an LLM proposer would replace/augment.)"""
    out: list[Candidate] = []
    for c in leaders:
        p = c.pdict()
        if c.family == "hour_drift":
            for dh in (-1, 1):
                nh = (int(p["hour"]) + dh) % 24
                for stop in stops:
                    out.append(
                        Candidate.make(
                            "hour_drift",
                            {"hour": nh, "direction": int(p["direction"]), "hold": 1, "stop_pips": stop},
                            mechanism=f"refine near {nh:02d}:00 UTC",
                            grounded=True,
                        )
                    )
            # try a 2-hour hold around the leader
            out.append(
                Candidate.make(
                    "hour_drift",
                    {
                        "hour": int(p["hour"]),
                        "direction": int(p["direction"]),
                        "hold": 2,
                        "stop_pips": float(p["stop_pips"]),
                    },
                    mechanism=f"refine 2h hold @ {int(p['hour']):02d}:00 UTC",
                    grounded=True,
                )
            )
    return out


# ----------------------------- the loop -----------------------------
# An LLM proposer: given the interim leaderboard + already-tried keys, returns a
# batch of new candidates (a proposer.ProposalResult, or a bare list[Candidate]).
# Kept as a plain callable so the loop never imports a transport.
Proposer = Callable[[list[Verdict], set[str]], object]


@dataclass
class SearchResult:
    verdicts: list[Verdict]
    n_trials: int
    split: Split
    leaderboard: list[Verdict] = field(default_factory=list)
    new_family_requests: list[str] = field(default_factory=list)


def _interim_verdicts(cands, metrics, n_trials, th) -> list[Verdict]:
    """Train-only leaderboard (hold-out untouched) to hand the LLM proposer."""
    vs = [score_candidate(cands[k], metrics[k], n_trials, th) for k in metrics]
    return sorted(vs, key=lambda v: -v.sharpe)


def run_search(
    rounds: int = 2,
    refine_top: int = 5,
    holdout_frac: float = 0.30,
    th: Thresholds | None = None,
    proposer: Proposer | None = None,
    instrument: str = "EURUSD",
) -> SearchResult:
    th = th or Thresholds()
    ds = make_dataset(instrument)
    split = sealed_split(ds, holdout_frac)
    # crypto stops are in bps of price and need a vol-appropriate (wider) scale
    stops = STOPS_BPS if ds.relative_pip else STOPS
    session_stops = (200.0, 400.0) if ds.relative_pip else (30.0, 50.0)

    cands: dict[str, Candidate] = {}
    metrics: dict[str, dict] = {}

    def add_and_score(batch: list[Candidate]) -> None:
        for c in batch:
            if c.key in metrics:
                continue
            cands[c.key] = c
            metrics[c.key] = train_metrics(c, split.train, th.cost_pips)

    add_and_score(seed_grid(stops, session_stops))
    for _ in range(max(rounds - 1, 0)):
        leaders = sorted(
            (cands[k] for k in metrics if cands[k].grounded),
            key=lambda c: metrics[c.key]["sharpe"],
            reverse=True,
        )[:refine_top]
        add_and_score(propose_refinements(leaders, stops))

    # LLM-proposer round: it sees the interim (train-only) leaderboard and emits
    # new candidates, scored through the same gauntlet. The hold-out stays sealed.
    new_family_requests: list[str] = []
    if proposer is not None:
        interim = _interim_verdicts(cands, metrics, len(metrics), th)
        result: Any = proposer(interim, set(metrics.keys()))
        new_family_requests = list(getattr(result, "new_family_requests", []))
        add_and_score(list(getattr(result, "candidates", result)))

    n_trials = len(metrics)

    # train-only verdicts (holdout untouched)
    verdicts = {k: score_candidate(cands[k], metrics[k], n_trials, th) for k in metrics}
    # survivors of the train gate get the one-shot sealed hold-out confirm
    for k, v in list(verdicts.items()):
        if v.tier != "reject":
            hs = holdout_sharpe(cands[k], split.holdout, th.cost_pips)
            verdicts[k] = score_candidate(cands[k], metrics[k], n_trials, th, holdout_sr=hs)

    ordered = sorted(
        verdicts.values(),
        key=lambda v: ({"deploy": 0, "watch": 1, "reject": 2}[v.tier], -(v.holdout_sharpe or -9), -v.sharpe),
    )
    return SearchResult(
        verdicts=list(verdicts.values()),
        n_trials=n_trials,
        split=split,
        leaderboard=ordered,
        new_family_requests=new_family_requests,
    )
