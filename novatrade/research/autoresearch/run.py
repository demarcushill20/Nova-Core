"""CLI entry point for the autoresearch loop.

    python -m novatrade.research.autoresearch.run [--rounds N] [--holdout 0.3]

Prints the tiered leaderboard (deploy / watch / reject) and writes the full
result to OUTPUT/autoresearch/leaderboard.json. "deploy" = passed train Sharpe +
positive-year consistency + trial-count-deflated Sharpe + sealed hold-out
confirm + mechanism gate. "watch" = strong but ungrounded (no structural reason)
or not yet hold-out-confirmed. "reject" = failed a gate (most candidates).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novatrade.research.autoresearch.gauntlet import Thresholds
from novatrade.research.autoresearch.loop import run_search

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "OUTPUT" / "autoresearch"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--instrument", default="EURUSD", help="EURUSD, GBPUSD, USDJPY, ... or BTCUSD")
    ap.add_argument("--holdout", type=float, default=0.30)
    ap.add_argument("--cost", type=float, default=0.30, help="round-turn cost in pips")
    ap.add_argument("--top", type=int, default=25, help="leaderboard rows to print")
    ap.add_argument("--llm", action="store_true", help="add an LLM-proposer round (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--llm-model", default="claude-opus-4-8")
    args = ap.parse_args()

    proposer = None
    if args.llm:
        from novatrade.research.autoresearch.proposer import LLMProposer, anthropic_complete_fn

        try:
            proposer = LLMProposer(anthropic_complete_fn(model=args.llm_model))
            print("LLM-proposer round enabled (Anthropic).")
        except RuntimeError as e:
            print(f"LLM-proposer unavailable ({e}); running programmatic search only.")

    th = Thresholds(cost_pips=args.cost)
    res = run_search(
        rounds=args.rounds, holdout_frac=args.holdout, th=th, proposer=proposer, instrument=args.instrument
    )
    if res.new_family_requests:
        print("\nLLM new-family requests (for a human/coding-agent to implement):")
        for r in res.new_family_requests:
            print(f"  - {r}")

    counts = {"deploy": 0, "watch": 0, "reject": 0}
    for v in res.verdicts:
        counts[v.tier] += 1
    print(
        f"\nAUTORESEARCH — {res.n_trials} candidates scored | hold-out cut {res.split.cut.date()} "
        f"({len(res.split.train.bars15):,} train / {len(res.split.holdout.bars15):,} holdout 15m bars)"
    )
    print(
        f"cost={args.cost}p | DSR penalises all {res.n_trials} trials | "
        f"deploy={counts['deploy']} watch={counts['watch']} reject={counts['reject']}\n"
    )
    hdr = f"{'tier':<8}{'family':<15}{'sharpe':>7}{'posYr':>7}{'DSR':>7}{'OOS':>7}  key / reason"
    print(hdr)
    print("-" * len(hdr))
    for v in res.leaderboard[: args.top]:
        oos = "-" if v.holdout_sharpe is None else f"{v.holdout_sharpe:.2f}"
        detail = v.key if v.tier != "reject" else v.reason
        print(f"{v.tier:<8}{v.family:<15}{v.sharpe:>7.2f}{v.frac_pos_years:>7.2f}{v.dsr:>7.2f}{oos:>7}  {detail[:48]}")

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "n_trials": res.n_trials,
        "holdout_cut": str(res.split.cut),
        "cost_pips": args.cost,
        "counts": counts,
        "leaderboard": [v.to_dict() for v in res.leaderboard],
    }
    (OUT / "leaderboard.json").write_text(json.dumps(payload, indent=2))
    print(f"\n-> wrote {OUT / 'leaderboard.json'}")


if __name__ == "__main__":
    main()
