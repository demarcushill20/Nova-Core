"""Pine v5 exit-timing parity audit harness.

Runs the Python IRBBacktester against the Pine-aligned config with one or more
``parity_audit_toggles`` applied, joins the result against the Pine baseline
trade log, and reports impact metrics (trades, PF, coverage, deltas vs the
no-toggle baseline). Outputs a timestamped JSON to ``data/parity_audit/``.

Usage examples:

    # Sanity check: run with no toggles (must match live champion: ~1066 trades, PF ~0.888).
    python scripts/parity_audit.py --baseline

    # Run a single toggle in isolation.
    python scripts/parity_audit.py --toggle d3_zero_cooldown

    # Run baseline + every known toggle individually (full audit sweep).
    python scripts/parity_audit.py --all-individual
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.commands.data import aggregate_h1_to_h4
from novatrade.cli.config_schema import StrategyConfig
from scripts.parity_match import H1_CSV, PINE_CSV, load_candles, load_pine

REPO = Path("/home/nova/nova-core")
CFG_PATH = REPO / "configs/strategies/irb_v5_m5_pine_aligned.yaml"
RESULTS_DIR = REPO / "data/parity_audit"

KNOWN_TOGGLES: tuple[str, ...] = (
    "d1_post_ratchet_stop_fill",
    "d2_strategy_close_next_open",
    "d3_zero_cooldown",
    "d12_gap_fill_at_open",
)


def run_backtest_with_toggles(
    h1: list,
    h4: list,
    cfg: StrategyConfig,
    toggles: frozenset[str],
) -> list:
    """Run the Python backtester with a specific toggle set.

    Mirrors ``scripts.parity_match.run_python`` but overrides the toggle set so
    the harness can sweep configurations without mutating the YAML on disk.
    """
    cfg_with_toggles = cfg.model_copy(update={"parity_audit_toggles": toggles})
    env_kwargs = cfg_with_toggles.to_environment_kwargs()
    env_kwargs["use_ema_stack_filter"] = True
    base = BacktestEnvironment.for_v5_m5()
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    env = replace(
        base,
        primary_timeframe="H1",
        higher_timeframe="H4",
        h1_to_h4_ratio=4,
        h1_bars_per_day=24,
        h4_bars_per_day=6,
        min_volume=1.0,
        max_volume=1.0,  # fixed-1-lot to match Pine sizing
        **valid,
    )
    bt = IRBBacktester(env=env)
    return bt.run(h1, h4).trades


def partition_trades(
    pine: list,
    py_trades: list,
    h1: list,
) -> tuple[set, set, set, dict, dict]:
    """Partition Pine vs Python trades by (entry_ts, side) key.

    Returns (matched, pine_only, python_only, py_by_key, pine_by_key).
    """
    py_by_key: dict[tuple[int, str], Any] = {}
    for t in py_trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        py_by_key[(ts, side)] = t

    pine_by_key = {(t.entry_ts, t.side): t for t in pine}
    pine_keys = set(pine_by_key.keys())
    py_keys = set(py_by_key.keys())

    matched = pine_keys & py_keys
    pine_only = pine_keys - py_keys
    python_only = py_keys - pine_keys
    return matched, pine_only, python_only, py_by_key, pine_by_key


def compute_metrics(
    py_trades: list,
    pine: list,
    matched: set,
    pine_only: set,
    python_only: set,
) -> dict[str, float]:
    """Compute headline metrics for one harness run."""
    gross_win = sum(t.pnl_usd for t in py_trades if t.pnl_usd > 0)
    gross_loss = sum(-t.pnl_usd for t in py_trades if t.pnl_usd < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    net_pnl = sum(t.pnl_usd for t in py_trades)
    coverage_pct = (len(matched) / len(pine) * 100.0) if pine else 0.0
    return {
        "trades": len(py_trades),
        "pf": pf,
        "net_pnl": net_pnl,
        "matched": len(matched),
        "pine_only": len(pine_only),
        "python_only": len(python_only),
        "coverage_pct": coverage_pct,
    }


def compute_delta(metrics: dict[str, float], baseline: dict[str, float]) -> dict[str, float]:
    """Compute delta vs the baseline run."""
    return {
        "delta_trades": metrics["trades"] - baseline["trades"],
        "delta_pf": metrics["pf"] - baseline["pf"],
        "delta_net_pnl": metrics["net_pnl"] - baseline["net_pnl"],
        "pine_only_resolved": baseline["pine_only"] - metrics["pine_only"],
        "python_only_resolved": baseline["python_only"] - metrics["python_only"],
        "coverage_pct_delta": metrics["coverage_pct"] - baseline["coverage_pct"],
    }


def format_run_label(toggles: frozenset[str]) -> str:
    if not toggles:
        return "baseline"
    return "+".join(sorted(toggles))


def print_run_summary(label: str, metrics: dict, delta: dict | None) -> None:
    print(f"\n--- run: {label} ---")
    print(
        f"  trades={metrics['trades']:4d}  pf={metrics['pf']:.3f}  "
        f"net_pnl={metrics['net_pnl']:+,.0f}  coverage={metrics['coverage_pct']:.1f}%"
    )
    print(
        f"  matched={metrics['matched']:4d}  pine_only={metrics['pine_only']:4d}  "
        f"python_only={metrics['python_only']:4d}"
    )
    if delta is not None:
        print(
            f"  Δtrades={delta['delta_trades']:+d}  Δpf={delta['delta_pf']:+.3f}  "
            f"Δnet_pnl={delta['delta_net_pnl']:+,.0f}  Δcoverage={delta['coverage_pct_delta']:+.2f}pp"
        )
        print(
            f"  pine_only_resolved={delta['pine_only_resolved']:+d}  "
            f"python_only_resolved={delta['python_only_resolved']:+d}"
        )


def execute_run(
    h1: list,
    h4: list,
    cfg: StrategyConfig,
    pine: list,
    toggles: frozenset[str],
) -> dict[str, Any]:
    """Execute one harness run and return the structured result dict."""
    label = format_run_label(toggles)
    print(f"[run] {label} ...", flush=True)
    t0 = time.time()
    py_trades = run_backtest_with_toggles(h1, h4, cfg, toggles)
    elapsed = time.time() - t0

    matched, pine_only, python_only, _, _ = partition_trades(pine, py_trades, h1)
    metrics = compute_metrics(py_trades, pine, matched, pine_only, python_only)

    return {
        "label": label,
        "toggles": sorted(toggles),
        "elapsed_s": round(elapsed, 2),
        "metrics": metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--toggle",
        nargs="+",
        metavar="NAME",
        help=f"Apply a set of toggles. Known: {', '.join(KNOWN_TOGGLES)}",
    )
    group.add_argument(
        "--baseline",
        action="store_true",
        help="Run with empty toggle set (live-equivalent sanity check).",
    )
    group.add_argument(
        "--all-individual",
        action="store_true",
        help="Run baseline plus each known toggle in isolation.",
    )
    args = parser.parse_args()

    # Validate toggle names if given.
    if args.toggle:
        unknown = [t for t in args.toggle if t not in KNOWN_TOGGLES]
        if unknown:
            parser.error(f"Unknown toggle(s): {unknown}. Known: {list(KNOWN_TOGGLES)}")

    # Build the run plan.
    runs: list[frozenset[str]] = []
    if args.baseline:
        runs.append(frozenset())
    elif args.toggle:
        runs.append(frozenset(args.toggle))
    elif args.all_individual:
        runs.append(frozenset())
        for tog in KNOWN_TOGGLES:
            runs.append(frozenset({tog}))

    # Load shared inputs once.
    print(f"[load] Pine baseline: {PINE_CSV.name}")
    pine = load_pine(PINE_CSV)
    print(f"        {len(pine):,} trades")

    print(f"[load] H1 candles: {H1_CSV.name}")
    h1 = load_candles(H1_CSV, "EURUSD", "H1")
    print(f"        {len(h1):,} bars")
    h4 = aggregate_h1_to_h4(h1)
    print(f"[derive] H4: {len(h4):,} bars")

    print(f"[load] strategy config: {CFG_PATH.name}")
    cfg = StrategyConfig.from_yaml(CFG_PATH)

    # Execute runs sequentially.
    results: list[dict[str, Any]] = []
    baseline_metrics: dict[str, float] | None = None
    for toggles in runs:
        run_result = execute_run(h1, h4, cfg, pine, toggles)
        if not toggles:
            baseline_metrics = run_result["metrics"]
            run_result["delta"] = None
        else:
            run_result["delta"] = (
                compute_delta(run_result["metrics"], baseline_metrics) if baseline_metrics is not None else None
            )
        results.append(run_result)
        print_run_summary(run_result["label"], run_result["metrics"], run_result["delta"])

    # Persist.
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"results_{ts}.json"
    payload = {
        "schema_version": 1,
        "generated_at_utc": ts,
        "config_path": str(CFG_PATH),
        "pine_baseline_path": str(PINE_CSV),
        "h1_candles_path": str(H1_CSV),
        "known_toggles": list(KNOWN_TOGGLES),
        "runs": results,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n[write] {out_path}")


if __name__ == "__main__":
    main()
